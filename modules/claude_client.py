import logging
import os
from typing import Optional, Callable
from modules.claude_sdk_compat import (
    CLAUDE_SDK_MAX_BUFFER_SIZE,
    ClaudeAgentOptions,
    SystemMessage,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
)
from config.v2_compat import ClaudeCompatConfig
from vibe.claude_config import CLAUDE_NATIVE_MCP_DISABLED
from modules.im.formatters import BaseMarkdownFormatter, SlackFormatter


logger = logging.getLogger(__name__)


class ClaudeClient:
    def __init__(self, config: ClaudeCompatConfig, formatter: Optional[BaseMarkdownFormatter] = None):
        self.config = config
        self.formatter = formatter or SlackFormatter()
        self.options = ClaudeAgentOptions(
            permission_mode=config.permission_mode,  # type: ignore[arg-type]
            cwd=config.cwd,
            system_prompt=config.system_prompt,
            max_buffer_size=CLAUDE_SDK_MAX_BUFFER_SIZE,
            **CLAUDE_NATIVE_MCP_DISABLED,
        )  # type: ignore[arg-type]

    def format_message(
        self,
        message,
        get_relative_path: Optional[Callable[[str], str]] = None,
        formatter: Optional[BaseMarkdownFormatter] = None,
    ) -> str:
        """Format different types of messages according to specified rules"""
        active_formatter = formatter or self.formatter
        try:
            if isinstance(message, SystemMessage):
                return self._format_system_message(message, active_formatter)
            elif isinstance(message, AssistantMessage):
                return self._format_assistant_message(message, active_formatter, get_relative_path)
            elif isinstance(message, UserMessage):
                return self._format_user_message(message, active_formatter, get_relative_path)
            elif isinstance(message, ResultMessage):
                return self._format_result_message(message, active_formatter)
            else:
                return active_formatter.format_warning(f"Unknown message type: {type(message)}")
        except Exception as e:
            logger.error(f"Error formatting message: {e}")
            return active_formatter.format_error(f"Error formatting message: {str(e)}")

    def _process_content_blocks(
        self,
        content_blocks,
        formatter: BaseMarkdownFormatter,
        get_relative_path: Optional[Callable[[str], str]] = None,
    ) -> list:
        """Process content blocks (TextBlock, ToolUseBlock) and return formatted parts"""
        formatted_parts = []

        for block in content_blocks:
            if isinstance(block, TextBlock):
                # Don't escape here - let the formatter handle it during final formatting
                # This avoids double escaping
                formatted_parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                tool_info = self._format_tool_use_block(block, formatter, get_relative_path)
                formatted_parts.append(tool_info)
            elif isinstance(block, ToolResultBlock):
                result_info = self._format_tool_result_block(block, formatter)
                formatted_parts.append(result_info)

        return formatted_parts

    def _get_relative_path(self, full_path: str) -> str:
        """Convert absolute path to relative path based on ClaudeCode cwd"""
        # Get ClaudeCode's current working directory
        cwd = self.options.cwd or os.getcwd()

        # Normalize paths for consistent comparison
        cwd = os.path.normpath(cwd)
        full_path = os.path.normpath(full_path)

        try:
            # If the path starts with cwd, make it relative
            if full_path.startswith(cwd + os.sep) or full_path == cwd:
                relative = os.path.relpath(full_path, cwd)
                # Use "./" prefix for current directory files
                if not relative.startswith(".") and relative != ".":
                    relative = "./" + relative
                return relative
            else:
                # If not under cwd, just return the path as is
                return full_path
        except Exception as e:
            # Fallback to original path if any error
            logger.debug("Failed to get relative path for %s: %s", full_path, e)
            return full_path

    def _format_tool_use_block(
        self,
        block: ToolUseBlock,
        formatter: BaseMarkdownFormatter,
        get_relative_path: Optional[Callable[[str], str]] = None,
    ) -> str:
        """Format ToolUseBlock using formatter"""
        # Prefer caller-provided get_relative_path (per-session cwd), fallback to self
        rel = get_relative_path if get_relative_path else self._get_relative_path
        return formatter.format_tool_use(block.name, block.input, get_relative_path=rel)

    def _format_tool_result_block(self, block: ToolResultBlock, formatter: BaseMarkdownFormatter) -> str:
        """Format ToolResultBlock using formatter"""
        is_error = bool(block.is_error) if block.is_error is not None else False
        content = block.content if isinstance(block.content, str) else None
        return formatter.format_tool_result(is_error, content)

    def _format_system_message(self, message: SystemMessage, formatter: BaseMarkdownFormatter) -> str:
        """Format SystemMessage using formatter"""
        cwd = message.data.get("cwd", "Unknown")
        session_id = message.data.get("session_id", None)
        return formatter.format_system_message(cwd, message.subtype, session_id)

    def _format_assistant_message(
        self,
        message: AssistantMessage,
        formatter: BaseMarkdownFormatter,
        get_relative_path: Optional[Callable[[str], str]] = None,
    ) -> str:
        """Format AssistantMessage using formatter"""
        content_parts = self._process_content_blocks(message.content, formatter, get_relative_path)
        return formatter.format_assistant_message(content_parts)

    def _format_user_message(
        self,
        message: UserMessage,
        formatter: BaseMarkdownFormatter,
        get_relative_path: Optional[Callable[[str], str]] = None,
    ) -> str:
        """Format UserMessage using formatter"""
        content_parts = self._process_content_blocks(message.content, formatter, get_relative_path)
        return formatter.format_user_message(content_parts)

    def _format_result_message(self, message: ResultMessage, formatter: BaseMarkdownFormatter) -> str:
        """Format ResultMessage using formatter"""
        return formatter.format_result_message(message.subtype, message.duration_ms, message.result)

    def _is_skip_message(self, message) -> bool:
        """Check if the message should be skipped"""
        if isinstance(message, AssistantMessage):
            if not message.content:
                return True
        elif isinstance(message, UserMessage):
            if not message.content:
                return True
        return False
