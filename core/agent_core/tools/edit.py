"""The ``edit`` tool (C-7 section 4).

Ported from Pi ``packages/coding-agent/src/core/tools/edit.ts`` (MIT, Copyright
(c) 2025 Mario Zechner); the description and model-facing strings are Pi's.
``replaceAll`` is an Avibe addition, so its schema text and the "unless
replaceAll is true" clause are Avibe's.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from typing import Any, Mapping

from core.agent_core.tools.args import ToolInputError, error_result, os_error_text, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.edit_diff import (
    Edit,
    EditError,
    apply_edits_to_normalized_content,
    detect_line_ending,
    generate_diff_string,
    generate_unified_patch,
    normalize_to_lf,
    restore_line_endings,
    split_bom,
)
from core.agent_core.tools.paths import file_mutation_lock, resolve_to_cwd
from core.agent_core.tools.write import write_text

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
        parsed.append(Edit(old_text, new_text, replace_all))
    return parsed


def _plan_edits(absolute: str, edits: list[Edit], path: str) -> tuple[str, str, str, str]:
    """Read the file and compute the edited content: ``(bom, line_ending, before, after)``, LF-normalized."""
    with open(absolute, "rb") as handle:
        raw = handle.read().decode("utf-8", "replace")
    # The model never includes an invisible BOM in oldText.
    bom, content = split_bom(raw)
    ending = detect_line_ending(content)
    base = normalize_to_lf(content)
    return bom, ending, base, apply_edits_to_normalized_content(base, edits, path)


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
        except ToolInputError as exc:
            return error_result(str(exc))
        absolute = resolve_to_cwd(path, ctx.cwd)

        async with file_mutation_lock(absolute):
            if ctx.cancel.cancelled:
                return error_result("Operation aborted")
            if not os.access(absolute, os.R_OK | os.W_OK):
                code = errno.errorcode.get(errno.ENOENT if not os.path.exists(absolute) else errno.EACCES, "EACCES")
                return error_result(f"Could not edit file: {path}. Error code: {code}.")
            try:
                bom, ending, base, new_content = await asyncio.to_thread(_plan_edits, absolute, edits, path)
                if ctx.cancel.cancelled:
                    return error_result("Operation aborted")
                await asyncio.to_thread(write_text, absolute, bom + restore_line_endings(new_content, ending))
            except EditError as exc:
                return error_result(str(exc))
            except OSError as exc:
                return error_result(os_error_text(exc))

        diff, first_changed_line = generate_diff_string(base, new_content)
        return text_result(
            f"Successfully replaced {len(edits)} block(s) in {path}.",
            details={
                "diff": diff,
                "patch": generate_unified_patch(path, base, new_content),
                "first_changed_line": first_changed_line,
            },
        )
