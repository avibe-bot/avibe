"""Compatibility helpers for environments without claude_agent_sdk installed."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CLAUDE_SDK_MAX_BUFFER_SIZE = 16 * 1024 * 1024
CLAUDE_SDK_BUFFER_ERROR_TEXT = "JSON message exceeded maximum buffer size"


def is_claude_sdk_buffer_error(error: object) -> bool:
    """Return True when Claude SDK failed on a single oversized JSON message."""
    return CLAUDE_SDK_BUFFER_ERROR_TEXT in str(error)


try:
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient as _ClaudeSDKClient,
        PermissionResultAllow,
        ResultMessage,
        SystemMessage,
        TaskNotificationMessage,
        TaskProgressMessage,
        TaskStartedMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
    from claude_agent_sdk._errors import CLIConnectionError, MessageParseError
    CLAUDE_SDK_AVAILABLE = True

    def _should_ignore_message_parse_error(data: object) -> bool:
        """Skip SDK event types that are safe to ignore in older client versions."""
        return isinstance(data, dict) and data.get("type") == "rate_limit_event"

    class ClaudeSDKClient(_ClaudeSDKClient):
        async def receive_messages(self):
            """Receive all messages from Claude, tolerating non-fatal SDK event additions."""
            if not self._query:
                raise CLIConnectionError("Not connected. Call connect() first.")

            from claude_agent_sdk._internal.message_parser import parse_message

            async for data in self._query.receive_messages():
                try:
                    message = parse_message(data)
                except MessageParseError:
                    if _should_ignore_message_parse_error(data):
                        logger.info(
                            "Ignoring unsupported Claude SDK message type from CLI: %s",
                            data.get("type"),
                        )
                        continue
                    raise
                if message is None:
                    logger.info(
                        "Ignoring unsupported Claude SDK message type from CLI: %s",
                        data.get("type") if isinstance(data, dict) else type(data),
                    )
                    continue
                yield message
except ModuleNotFoundError:  # pragma: no cover - exercised only in minimal test envs
    CLAUDE_SDK_AVAILABLE = False

    class _MissingClaudeSDK:
        def __init__(self, *args, **kwargs):
            raise ModuleNotFoundError("claude_agent_sdk is required for Claude backend functionality")

    class ClaudeAgentOptions(_MissingClaudeSDK):
        pass

    class ClaudeSDKClient(_MissingClaudeSDK):
        pass

    class PermissionResultAllow:
        def __init__(self, behavior="allow", updated_input=None, updated_permissions=None):
            self.behavior = behavior
            self.updated_input = updated_input
            self.updated_permissions = updated_permissions

    class SystemMessage:  # minimal placeholders for isinstance checks
        pass

    class AssistantMessage:
        pass

    class UserMessage:
        pass

    class ResultMessage:
        pass

    class TaskStartedMessage(SystemMessage):
        pass

    class TaskProgressMessage(SystemMessage):
        pass

    class TaskNotificationMessage(SystemMessage):
        pass

    class TextBlock:
        pass

    class ToolUseBlock:
        pass

    class ToolResultBlock:
        pass
