"""The Claude Agent SDK and Avibe's compatibility layer over it.

Imported only through ``modules.claude_sdk_compat`` on first use of an SDK name,
so a controller that never runs the Claude Code backend never loads the SDK
(about 63k tracked objects and 23 MiB with the MCP client it brings).
"""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from config.atomic_io import write_atomic

logger = logging.getLogger("modules.claude_sdk_compat")

try:
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient as _ClaudeSDKClient,
        PermissionResultAllow,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
    from claude_agent_sdk._errors import CLIConnectionError, MessageParseError

    try:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
        )
    except ImportError:
        # Task lifecycle frames were added after the core SDK types. Avibe
        # detects them by class name/subtype, so an older SDK can keep running.
        class TaskStartedMessage(SystemMessage):
            pass

        class TaskProgressMessage(SystemMessage):
            pass

        class TaskNotificationMessage(SystemMessage):
            pass

    try:
        from claude_agent_sdk import HookMatcher  # type: ignore[import-not-found]

        CLAUDE_SDK_HOOKS_AVAILABLE = True
    except ImportError:
        # In-process hooks postdate the core SDK types. Callers must check
        # CLAUDE_SDK_HOOKS_AVAILABLE before passing `hooks=` to
        # ClaudeAgentOptions, since it is a dataclass and rejects unknown kwargs.
        HookMatcher = None  # type: ignore[assignment]
        CLAUDE_SDK_HOOKS_AVAILABLE = False

    CLAUDE_SDK_AVAILABLE = True

    def _should_ignore_message_parse_error(data: object) -> bool:
        """Skip SDK event types that are safe to ignore in older client versions."""
        return isinstance(data, dict) and data.get("type") == "rate_limit_event"

    class ClaudeSDKClient(_ClaudeSDKClient):
        def _prepare_launch_settings(self) -> None:
            if getattr(self, "_launch_settings", None) is not None:
                return
            raw = self.options.settings
            if not raw or not raw.lstrip().startswith("{"):
                return
            settings = json.loads(raw)
            if not settings.get("env"):
                return
            # SDK sandbox merging otherwise expands even a settings file back
            # into argv. Merge here and pass only the private file's path.
            if self.options.sandbox is not None:
                settings["sandbox"] = self.options.sandbox
            resources = ExitStack()
            self._launch_settings = resources
            directory = resources.enter_context(tempfile.TemporaryDirectory(prefix="avibe-claude-settings-"))
            path = Path(directory) / "settings.json"
            write_atomic(path, json.dumps(settings))
            resources.callback(setattr, self, "options", self.options)
            self.options = replace(self.options, settings=str(path), sandbox=None)

        def _close_launch_settings(self) -> None:
            resources = getattr(self, "_launch_settings", None)
            if resources is not None:
                self._launch_settings = None
                resources.close()

        async def connect(self, prompt=None) -> None:
            try:
                self._prepare_launch_settings()
                await super().connect(prompt)
            except BaseException:
                self._close_launch_settings()
                raise

        async def disconnect(self) -> None:
            try:
                await super().disconnect()
            finally:
                self._close_launch_settings()

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
    CLAUDE_SDK_HOOKS_AVAILABLE = False
    HookMatcher = None  # type: ignore[assignment]

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


def _patch_sdk_buffer_limits() -> None:
    """Raise the SDK transport's buffer limit before any client is built (applied once, on load)."""
    from modules.claude_sdk_compat import CLAUDE_SDK_MAX_BUFFER_SIZE

    try:
        from claude_agent_sdk._internal.transport import subprocess_cli
    except Exception as exc:
        logger.warning("Claude SDK patch skipped: %s", exc)
        return
    patched = []
    for attr in ("_DEFAULT_MAX_BUFFER_SIZE", "_MAX_BUFFER_SIZE"):
        if not hasattr(subprocess_cli, attr):
            continue
        previous = getattr(subprocess_cli, attr)
        setattr(subprocess_cli, attr, CLAUDE_SDK_MAX_BUFFER_SIZE)
        if previous != CLAUDE_SDK_MAX_BUFFER_SIZE:
            patched.append(f"{attr} from {previous} to {CLAUDE_SDK_MAX_BUFFER_SIZE} bytes")
    if patched:
        logger.info("Patched claude_agent_sdk buffer limits: %s", ", ".join(patched))


if CLAUDE_SDK_AVAILABLE:
    _patch_sdk_buffer_limits()
