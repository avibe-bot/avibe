"""Information cocoon's after-only hooks for Vibey's local read and command tools.

The wrapper changes only model-visible content and error presentation. Execution,
job ownership, exit status and termination stay the tool's facts. Recovery never
runs a command or hook again: a response marked by this policy fails closed if its
result was not committed before interruption (see vibey-source-filter-hooks.md).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any, Mapping, Sequence

from core.agent_core.messages import UserMessage, message_from_dict, message_to_dict, text
from core.agent_core.tools.base import Tool, ToolContext, ToolResult, ToolSpec
from core.handlers.model_hub.source_filter import PROTOCOL_VERSION, REASON_MAX_CHARS, HookFailed, Say, SourceFilterHooks

logger = logging.getLogger(__name__)
LOCAL_TOOL_HOOKS = {"read": "after_read", "bash": "after_bash", "powershell": "after_bash"}
RECOVERY_MARKER = "source_filter_local"


def interrupted_local_result(result: ToolResult, say: Say) -> ToolResult:
    """No raw job output on recovery, and no rerun of a possibly effectful hook."""
    return replace(result, content=(text(say("modelHub.sourceFilter.interrupted")),), is_error=True)


def _output(result: ToolResult) -> dict[str, Any]:
    return {"content": message_to_dict(UserMessage(result.content))["content"], "is_error": result.is_error}


def _replacement(output: Mapping[str, Any], result: ToolResult) -> ToolResult:
    try:
        message = message_from_dict({"role": "user", "content": output["content"]})
    except (TypeError, ValueError, KeyError, RecursionError):
        raise HookFailed("output.content must contain valid text or image blocks") from None
    return replace(result, content=message.content, is_error=output["is_error"])


class _AfterLocalTool:
    def __init__(self, tool: Tool, hooks: SourceFilterHooks, say: Say) -> None:
        self._tool, self._hooks, self._say = tool, hooks, say

    @property
    def spec(self) -> ToolSpec:
        return self._tool.spec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tool, name)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        # Streaming tails are not final results and must not escape before the after hook.
        # Parameters, cwd, env, cancellation and the job identity are otherwise unchanged.
        try:
            result = await self._tool.execute(arguments, replace(ctx, on_progress=None))
        except Exception as error:
            logger.exception("Local tool execution failed: %s", self.spec.name)
            result = ToolResult((text((str(error) or f"{type(error).__name__}: tool execution failed.")[:500]),), is_error=True)
        hook = LOCAL_TOOL_HOOKS[self.spec.name]
        request = {
            "v": PROTOCOL_VERSION, "hook": hook, "tool": self.spec.name,
            "args": dict(arguments), "origin": "tool", "output": await asyncio.to_thread(_output, result),
        }
        try:
            answer = (await self._hooks.run(hook, request)).response
            if answer is None or answer["action"] == "pass":
                return result
            logger.info("Source filter %s: %s", hook, answer["action"])
            if answer["action"] == "block":
                reason = answer.get("reason", "").strip()[:REASON_MAX_CHARS] if answer.get("reason") else ""
                message = (self._say("modelHub.sourceFilter.blocked", reason=reason) if reason
                           else self._say("modelHub.sourceFilter.blockedNoReason"))
                return replace(result, content=(text(message),), is_error=True)
            return await asyncio.to_thread(_replacement, answer["output"], result)
        except HookFailed as error:
            self._hooks.note_failure(hook, error)
            return replace(result, content=(text(self._say(
                "modelHub.sourceFilter.failed", hook=f"{hook}.py", reason=str(error),
            )),), is_error=True)


def filter_local_tools(tools: Sequence[Tool], hooks: SourceFilterHooks | None, say: Say) -> tuple[Tool, ...]:
    """Frozen per Turn; lookup of each script still happens only at its after point."""
    return tuple(_AfterLocalTool(tool, hooks, say) if hooks is not None and tool.spec.name in LOCAL_TOOL_HOOKS else tool
                 for tool in tools)
