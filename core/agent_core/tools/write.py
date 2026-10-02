"""The ``write`` tool (C-7 section 3).

Ported from Pi ``packages/coding-agent/src/core/tools/write.ts`` (MIT, Copyright
(c) 2025 Mario Zechner); the description and result text are Pi's.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Mapping

from core.agent_core.tools.args import ToolInputError, error_result, os_error_text, str_arg, text_result
from core.agent_core.tools.base import ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.paths import file_mutation_lock, resolve_to_cwd

WRITE_DESCRIPTION = (
    "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates "
    "parent directories."
)

WRITE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
        "content": {"type": "string", "description": "Content to write to the file"},
    },
}


class WriteTool:
    def __init__(self) -> None:
        self._spec = ToolSpec(name="write", description=WRITE_DESCRIPTION, input_schema=WRITE_SCHEMA)

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            path = str_arg(arguments, "path")
            content = str_arg(arguments, "content")
            absolute = resolve_to_cwd(path, ctx.cwd)
        except ToolInputError as exc:
            return error_result(str(exc))
        async with file_mutation_lock(absolute):
            # Checked before each step, never in the middle of one, so the lock is held until
            # the filesystem operation in progress has finished.
            if ctx.cancel.cancelled:
                return error_result("Operation aborted")
            try:
                await asyncio.to_thread(os.makedirs, os.path.dirname(absolute), exist_ok=True)
                if ctx.cancel.cancelled:
                    return error_result("Operation aborted")
                await asyncio.to_thread(write_text, absolute, content)
            except OSError as exc:
                return error_result(os_error_text(exc))
        return text_result(f"Successfully wrote to {path}")


def write_text(path: str, content: str) -> None:
    """UTF-8 without newline translation; a lone surrogate (possible from JSON) is replaced."""
    with open(path, "w", encoding="utf-8", errors="replace", newline="") as handle:
        handle.write(content)
