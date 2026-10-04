"""The ``edit`` tool (C-7 section 4).

Ported from Pi ``packages/coding-agent/src/core/tools/edit.ts`` (MIT, Copyright
(c) 2025 Mario Zechner); the description and model-facing strings are Pi's.
``replaceAll`` is an Avibe addition, so its schema text and the "unless
replaceAll is true" clause are Avibe's.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping, Optional

from core.agent_core.tools.args import ToolInputError, error_result, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.edit_diff import Edit, EditError, ResultTooLarge, apply_edits, display_diff
from core.agent_core.tools.paths import (
    NotRegularFile,
    errno_name,
    file_mutation_lock,
    open_regular,
    read_at_most,
    resolve_to_cwd,
    target_kind,
    to_thread_joined,
)
from core.agent_core.tools.text import decode_file, encode_file, model_text
from core.agent_core.tools.truncate import format_size
from core.agent_core.tools.write import FileChanged, FileIdentity, NotPinned, NotReplaceable, write_bytes

#: Avibe: larger files are refused rather than loaded whole; the display diff stops at a smaller size.
MAX_EDIT_BYTES = 10 * 1024 * 1024
MAX_DIFF_BYTES = 1024 * 1024
#: Avibe: planning keeps a few hundred bytes of Python objects per line, so lines have a budget too.
MAX_EDIT_LINES = 200_000

EDIT_DESCRIPTION = (
    "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping "
    "region of the original file. If two changes affect the same block or nearby lines, merge them into one edit "
    "instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes."
)

EDIT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["path", "edits"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
        "edits": {
            "type": "array",
            "minItems": 1,
            "description": (
                "One or more targeted replacements. Each edit is matched against the original file, not "
                "incrementally. Do not include overlapping or nested edits. If two changes touch the same block or "
                "nearby lines, merge them into one edit instead."
            ),
            "items": {
                "type": "object",
                "required": ["oldText", "newText"],
                "properties": {
                    "oldText": {
                        "type": "string",
                        "description": (
                            "Exact text for one targeted replacement. It must be unique in the original file unless "
                            "replaceAll is true, and must not overlap with any other edits[].oldText in the same call."
                        ),
                    },
                    "newText": {"type": "string", "description": "Replacement text for this targeted edit."},
                    "replaceAll": {
                        "type": "boolean",
                        "description": (
                            "Replace every occurrence of oldText instead of requiring exactly one. Defaults to false."
                        ),
                    },
                },
            },
        },
    },
}


def _is_single_edit(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("oldText"), str) and isinstance(value.get("newText"), str)


def prepare_edit_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Pi's repairs: ``edits`` sent as a JSON string or a single object, and the legacy top-level pair."""
    args = dict(arguments)
    edits = args.get("edits")
    if isinstance(edits, str):
        try:
            parsed = json.loads(edits)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            args["edits"] = parsed
        elif _is_single_edit(parsed):
            args["edits"] = [parsed]
    elif _is_single_edit(edits):
        args["edits"] = [edits]
    if isinstance(args.get("oldText"), str) and isinstance(args.get("newText"), str):
        listed = list(args["edits"]) if isinstance(args.get("edits"), list) else []
        listed.append({"oldText": args.pop("oldText"), "newText": args.pop("newText")})
        args["edits"] = listed
    return args


def _edits_arg(arguments: Mapping[str, Any]) -> list[Edit]:
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ToolInputError("Edit tool input is invalid. edits must contain at least one replacement.")
    parsed: list[Edit] = []
    for index, item in enumerate(edits):
        if not isinstance(item, dict):
            raise ToolInputError(f"edits[{index}] must be an object")
        old_text, new_text = item.get("oldText"), item.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolInputError(f"edits[{index}] needs string oldText and newText")
        replace_all = item.get("replaceAll", False)
        if replace_all is None:
            replace_all = False
        if not isinstance(replace_all, bool):
            raise ToolInputError(f"edits[{index}].replaceAll must be a boolean")
        # A lone surrogate (possible from JSON) cannot be written; it becomes U+FFFD.
        parsed.append(Edit(model_text(old_text), model_text(new_text), replace_all))
    return parsed


class _TooLarge(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


class _TooManyLines(Exception):
    def __init__(self, lines: int) -> None:
        super().__init__(lines)
        self.lines = lines


def _line_count(text: str) -> int:
    """Lines as edit's view counts them: ``\\r\\n``, ``\\r`` and ``\\n`` each end one."""
    return text.count("\n") + text.count("\r") - text.count("\r\n") + 1


def _plan_edits(
    absolute: str, edits: list[Edit], path: str, pinned: Optional[str] = None
) -> tuple[int, bytes, str, str, FileIdentity]:
    """Read the file and compute its new bytes: ``(size, data, view_before, view_after, identity)``.

    One descriptor gives the size and the contents, read up to the limit, so a file that grew after an
    earlier check by path cannot get past it. ``surrogateescape`` carries bytes that are not UTF-8
    through unchanged; only replaced spans change. ``identity`` is the file that was read, so the
    result is published only over it.
    """
    real = os.path.realpath(absolute)
    if pinned is not None and real != pinned:
        raise NotPinned()
    fd = open_regular(real)
    try:
        st = os.fstat(fd)
        size = st.st_size
        if size > MAX_EDIT_BYTES:
            raise _TooLarge(size)
        raw = read_at_most(fd, MAX_EDIT_BYTES)
    finally:
        os.close(fd)
    if len(raw) > MAX_EDIT_BYTES:
        raise _TooLarge(max(size, len(raw)))
    text = decode_file(raw)
    lines = _line_count(text)
    if lines > MAX_EDIT_LINES:
        raise _TooManyLines(lines)
    new_text, before, after = apply_edits(text, edits, path, max_result_chars=MAX_EDIT_BYTES)
    return len(raw), encode_file(new_text), before, after, FileIdentity.of(real, st)


class EditTool:
    def __init__(self) -> None:
        self._spec = ToolSpec(name="edit", description=EDIT_DESCRIPTION, input_schema=EDIT_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            arguments = prepare_edit_arguments(arguments)
            path = str_arg(arguments, "path")
            edits = _edits_arg(arguments)
            absolute = resolve_to_cwd(path, ctx.cwd)
        except ToolInputError as exc:
            return error_result(str(exc))

        async with file_mutation_lock(absolute):
            if ctx.cancel.cancelled:
                return error_result("Operation aborted")
            # Pi's access check, extended to every kind of file and checked without opening it (a FIFO would block).
            try:
                kind = target_kind(absolute)
            except OSError as exc:
                return error_result(f"Could not edit file: {path}. Error code: {errno_name(exc)}.")
            if kind == "directory":
                return error_result(f"Could not edit file: {path}. Error code: EISDIR.")
            if kind == "other":
                return error_result(f"Could not edit file: {path}. It is not a regular file.")
            if not os.access(absolute, os.R_OK | os.W_OK):
                return error_result(f"Could not edit file: {path}. Error code: EACCES.")
            try:
                size, data, base, new_content, identity = await to_thread_joined(
                    _plan_edits, absolute, edits, path, ctx.pinned_target
                )
                if len(data) > MAX_EDIT_BYTES:
                    raise ResultTooLarge()
                if ctx.cancel.cancelled:
                    return error_result("Operation aborted")
                await to_thread_joined(write_bytes, absolute, data, identity, ctx.pinned_target)
            except _TooLarge as exc:
                # The whole file and several copies would sit in the process every Session shares.
                return error_result(
                    f"File {path} is {format_size(exc.size)}, over the {format_size(MAX_EDIT_BYTES)} edit limit. "
                    "Use bash (for example sed or a short script) to change files this large."
                )
            except _TooManyLines as exc:
                return error_result(
                    f"File {path} has {exc.lines} lines, over the {MAX_EDIT_LINES} line edit limit. "
                    "Use bash (for example sed or a short script) to change files this large."
                )
            except ResultTooLarge:
                return error_result(
                    f"File {path} would be over the {format_size(MAX_EDIT_BYTES)} edit limit after this edit. "
                    "Use bash (for example sed or a short script) to change files this large."
                )
            except NotReplaceable:
                return error_result(
                    f"Could not edit file: {path}. Its directory is not writable, so the file cannot be replaced safely."
                )
            except EditError as exc:
                return error_result(str(exc))
            except NotPinned:
                return error_result(f"Could not edit file: {path}. It no longer resolves to the authorized location.")
            except FileChanged:
                # Another writer (bash, an editor) changed it after it was read: the edit would overwrite that.
                return error_result(
                    f"Could not edit file: {path}. It changed while the edit was being applied; read it again."
                )
            except NotRegularFile as exc:
                detail = "Error code: EISDIR." if exc.kind == "directory" else "It is not a regular file."
                return error_result(f"Could not edit file: {path}. {detail}")
            except OSError as exc:
                return error_result(f"Could not edit file: {path}. Error code: {errno_name(exc)}.")

        result = f"Successfully replaced {len(edits)} block(s) in {path}."
        if max(size, len(data)) > MAX_DIFF_BYTES:
            # The diff is for display only, and difflib is superlinear on large inputs.
            return text_result(result, details={"diff_skipped": True})
        shown_diff = await asyncio.to_thread(display_diff, path, base, new_content)
        if shown_diff is None:
            return text_result(result, details={"diff_skipped": True})
        diff, first_changed_line, patch = shown_diff
        return text_result(result, details={"diff": diff, "patch": patch, "first_changed_line": first_changed_line})
