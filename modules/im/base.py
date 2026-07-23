"""Base classes and data structures for IM platform abstraction"""

import logging
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any, List, Tuple, cast
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Data structures for platform-agnostic messaging
@dataclass
class FileAttachment:
    """Platform-agnostic file attachment"""

    name: str  # File name
    mimetype: str  # MIME type (e.g., "image/png", "application/pdf")
    url: Optional[str] = None  # URL to download the file (platform-specific)
    content: Optional[bytes] = None  # Downloaded file content
    local_path: Optional[str] = None  # Local path after download
    size: Optional[int] = None  # File size in bytes


@dataclass
class FileDownloadResult:
    """Result for downloading a remote attachment to a local path."""

    success: bool
    error: Optional[str] = None


@dataclass
class MessageContext:
    """Platform-agnostic message context"""

    user_id: str
    channel_id: str
    platform: Optional[str] = None
    thread_id: Optional[str] = None
    message_id: Optional[str] = None
    platform_specific: Optional[Dict[str, Any]] = None
    files: Optional[List[FileAttachment]] = None  # List of file attachments


@dataclass
class InlineButton:
    """Platform-agnostic inline button"""

    text: str
    callback_data: str


@dataclass
class InlineKeyboard:
    """Platform-agnostic inline keyboard"""

    buttons: list[list[InlineButton]]  # 2D array for row/column layout


# Configuration base class
@dataclass
class BaseIMConfig(ABC):
    """Abstract base class for IM platform configurations"""

    # Optional outbound proxy used by the platform's HTTP transport.
    # Schemes: socks5://, socks4://, http://, https://. When unset, the
    # adapter may fall back to a system SOCKS proxy via vibe.proxy.
    # Lark/Feishu currently has no proxy hook in lark-oapi and ignores
    # this field with a logged warning.
    proxy_url: Optional[str] = None

    @abstractmethod
    def validate(self) -> None:
        """Validate the configuration

        Raises:
            ValueError: If configuration is invalid
        """
        pass

    def validate_required_string(self, value: Optional[str], field_name: str) -> None:
        """Helper method to validate required string fields

        Args:
            value: The value to validate
            field_name: Name of the field for error messages

        Raises:
            ValueError: If value is None or empty
        """
        if not value or not value.strip():
            raise ValueError(f"{field_name} is required and cannot be empty")

    def validate_optional_int(self, value: Optional[str], field_name: str) -> Optional[int]:
        """Helper method to validate and convert optional integer fields

        Args:
            value: String value to convert
            field_name: Name of the field for error messages

        Returns:
            Converted integer or None

        Raises:
            ValueError: If value is not a valid integer
        """
        if value is None or value == "":
            return None

        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field_name} must be a valid integer, got: {value}")


# IM Client base class
class BaseIMClient(ABC):
    """Abstract base class for IM platform clients"""

    def __init__(self, config: BaseIMConfig):
        self.config = config
        # Initialize callback storage
        self.on_message_callback: Optional[Callable] = None
        self.on_command_callbacks: Dict[str, Callable] = {}
        self.on_callback_query_callback: Optional[Callable] = None
        # Platform-specific formatter will be set by subclasses
        self.formatter: Optional[Any] = None

    def get_default_parse_mode(self) -> Optional[str]:
        """Get the default parse mode for this platform

        Returns:
            Default parse mode string for the platform
        """
        # Default implementation - subclasses should override
        return None

    def should_use_thread_for_reply(self) -> bool:
        """Check if this platform uses threads for replies

        Returns:
            True if platform uses threads (like Slack), False otherwise
        """
        # Default implementation - subclasses should override
        return False

    def supports_message_editing(self, context: Optional[MessageContext] = None) -> bool:
        """Whether sent messages can be edited after delivery."""
        return True

    def verify_stopped(self) -> bool:
        """Return whether adapter-owned runtime resources have stopped.

        ``MultiIMClient.remove_client`` joins the adapter's top-level runtime
        thread. Adapters that start nested SDK/runtime threads should override
        this so hot-remove can fail instead of silently leaking callbacks.
        """
        return True

    def should_use_thread_for_dm_session(self) -> bool:
        """Check if DM conversations should use thread-based session IDs.

        Platforms differ here: some DMs support thread/topic replies, while
        others only have a flat DM timeline.
        """
        return False

    def should_use_message_id_for_channel_session(self, context: Optional[MessageContext] = None) -> bool:
        """Whether non-thread channel messages should default to per-message sessions."""
        return True

    def is_scheduled_thread_active(self, channel_id: Optional[str], thread_id: Optional[str]) -> bool:
        """Return whether a thread is active for the synthetic scheduled owner."""
        sessions = getattr(self, "sessions", None)
        if not sessions or not channel_id or not thread_id:
            return False
        exact_checker = getattr(sessions, "is_thread_active_for_user", None)
        if callable(exact_checker):
            return bool(exact_checker("scheduled", channel_id, thread_id))
        return False

    async def prepare_turn_context(self, context: MessageContext, source: str) -> MessageContext:
        """Allow IM adapters to adjust reply topology for a turn source.

        `source` is one of the higher-level inbound turn types such as
        ``human`` or ``scheduled``.
        """
        return context

    @staticmethod
    def extract_command_action(text: str, allow_plain_bind: bool = False) -> str:
        """Extract command action name from slash command text.

        Examples:
            "/settings" -> "settings"
            "/setcwd /tmp" -> "set_cwd"
            "bind abc123" -> "bind" (when ``allow_plain_bind`` is True)
            "hello" -> ""
        """
        parsed = BaseIMClient.parse_text_command(text, allow_plain_bind=allow_plain_bind)
        return parsed[0] if parsed else ""

    @staticmethod
    def parse_text_command(text: str, allow_plain_bind: bool = False) -> Optional[Tuple[str, str]]:
        """Parse slash-style command text.

        Returns:
            (command, args) when ``text`` starts with ``/`` and contains a
            non-empty command; otherwise ``None``.
        """
        if not text:
            return None
        stripped = text.strip()
        if not stripped:
            return None

        parts = stripped.split(maxsplit=1)
        head = parts[0]
        args = parts[1] if len(parts) > 1 else ""

        if head.startswith("/"):
            command = head[1:]
            if not command:
                return None
            if command == "setcwd":
                command = "set_cwd"
            return command, args

        if allow_plain_bind and head == "bind":
            return "bind", args

        return None

    def should_allow_plain_bind(self, *, user_id: str, is_dm: bool, settings_manager: Any = None) -> bool:
        """Allow bare ``bind <code>`` only for unbound DM users.

        This keeps normal conversations unchanged for already-bound users while
        working around platforms like Slack that reserve leading ``/`` in DMs.
        """
        if not is_dm:
            return False

        manager = settings_manager or getattr(self, "settings_manager", None)
        if manager is None:
            return True

        try:
            return not manager.is_bound_user(user_id)
        except Exception:
            logger.debug("Falling back to disabled plain bind alias", exc_info=True)
            return False

    async def dispatch_text_command(self, context: MessageContext, text: str, allow_plain_bind: bool = False) -> bool:
        """Dispatch slash-style text command if registered.

        Returns ``True`` if a matching command handler ran.
        """
        parsed = self.parse_text_command(text, allow_plain_bind=allow_plain_bind)
        if not parsed:
            return False
        command, args = parsed
        handler = self.on_command_callbacks.get(command)
        if not handler:
            return False
        await handler(context, args)
        return True

    def check_authorization(
        self,
        *,
        user_id: str,
        channel_id: str,
        thread_id: Optional[str] = None,
        is_dm: bool,
        text: str = "",
        action: str = "",
        settings_manager: Any = None,
    ):
        """Run centralized auth with shared action extraction logic."""
        from core.auth import check_auth

        allow_plain_bind = self.should_allow_plain_bind(
            user_id=user_id,
            is_dm=is_dm,
            settings_manager=settings_manager,
        )
        resolved_action = action or self.extract_command_action(text, allow_plain_bind=allow_plain_bind)
        return check_auth(
            user_id=user_id,
            channel_id=channel_id,
            thread_id=thread_id,
            is_dm=is_dm,
            platform=getattr(settings_manager, "platform", None),
            action=resolved_action,
            settings_manager=settings_manager,
        )

    def build_auth_denial_text(self, denial: str, channel_id: Optional[str] = None) -> Optional[str]:
        """Build a localized denial message from centralized auth result.

        Keeps denial copy in one place so IM implementations only handle
        delivery mechanics (how to send), not message wording/branching.
        """

        def _translate(key: str, **kwargs) -> str:
            translator = getattr(self, "_t", None)
            if callable(translator):
                typed_translator = cast(Callable[..., str], translator)
                try:
                    return typed_translator(key, channel_id, **kwargs)
                except TypeError:
                    return typed_translator(key, **kwargs)
            from vibe.i18n import t as i18n_t

            return i18n_t(key, "en", **kwargs)

        if denial == "unbound_dm":
            return _translate("bind.dmNotBound")
        if denial == "unauthorized_channel":
            return f"❌ {_translate('error.channelNotEnabled')}"
        if denial == "not_admin":
            return _translate("permission.adminOnly")
        return None

    @abstractmethod
    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> Optional[str]:
        """Send a text message

        ``subtext`` is an optional de-emphasized footer (concise status bubble):
        platforms that support it (Slack context block, Discord ``-#``) render it
        as small secondary text; others may ignore it. Only ever passed on the
        Slack/Discord concise status path.

        Args:
            context: Message context (channel, thread, etc)
            text: Message text
            parse_mode: Optional formatting mode (markdown, html, etc)
            reply_to: Optional message ID to reply to

        Returns:
            Message ID of sent message, or None when not delivered
        """
        pass

    @abstractmethod
    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> Optional[str]:
        """Send a message with inline buttons

        Args:
            context: Message context
            text: Message text
            keyboard: Inline keyboard configuration
            parse_mode: Optional formatting mode
            subtext: Optional de-emphasized footer (see send_message); only passed
                by status-bubble platforms, so non-bubble adapters need not accept it.

        Returns:
            Message ID of sent message, or None when not delivered
        """
        pass

    async def upload_markdown(
        self,
        context: MessageContext,
        title: str,
        content: str,
        filetype: str = "markdown",
    ) -> str:
        """Upload markdown content as a file (optional per platform)."""
        raise NotImplementedError

    async def upload_file_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a local file to the conversation (optional per platform).

        Args:
            context: Message context (channel, thread, etc)
            file_path: Absolute path to the local file
            title: Display title (defaults to the file basename)

        Returns:
            File ID or empty string on failure
        """
        raise NotImplementedError

    async def upload_image_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a local image to the conversation (optional per platform).

        Default behavior falls back to ``upload_file_from_path`` so platforms
        without native image upload support still deliver the attachment.
        """
        return await self.upload_file_from_path(context, file_path, title=title)

    async def upload_video_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a local video to the conversation.

        Default behavior falls back to ``upload_file_from_path`` so platforms
        without native video support still deliver the attachment.
        """

        return await self.upload_file_from_path(context, file_path, title=title)

    async def download_file(
        self,
        file_info: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> Optional[bytes]:
        """Download a remote file into memory (optional per platform)."""
        raise NotImplementedError

    async def download_file_to_path(
        self,
        file_info: Dict[str, Any],
        target_path: str,
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> FileDownloadResult:
        """Download a remote file directly to a local path.

        Platforms can override this to stream large files directly to disk.
        The default implementation falls back to ``download_file`` and writes
        the bytes afterward, which is less memory-efficient but preserves
        compatibility.
        """
        content = await self.download_file(file_info, max_bytes=max_bytes, timeout_seconds=timeout_seconds)
        if content is None:
            return FileDownloadResult(False, "Download returned no content")

        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(content)
        except Exception as err:
            return FileDownloadResult(False, f"Failed to write downloaded file: {err}")
        return FileDownloadResult(True)

    @abstractmethod
    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> bool:
        """Edit an existing message

        Args:
            context: Message context
            message_id: ID of message to edit
            text: New text (if provided)
            keyboard: New keyboard (if provided)
            parse_mode: Optional formatting mode (markdown, html, etc)
            subtext: Optional de-emphasized footer (see send_message); platforms
                that don't support it may ignore it.

        Returns:
            Success status
        """
        pass

    async def delete_message(self, context: MessageContext, message_id: str) -> bool:
        """Delete a previously-sent message.

        Default is a no-op returning ``False`` for platforms that cannot delete
        (callers must treat ``False`` as "not deleted" and fall back). Platforms
        that support deletion override this; ``PlatformCapabilities.
        supports_message_deletion`` advertises support without a probe call.
        """
        return False

    async def remove_inline_keyboard(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Remove inline keyboard / actions from a message."""
        if text is None:
            return await self.edit_message(context, message_id, keyboard=None)
        return await self.edit_message(
            context,
            message_id,
            text=text,
            keyboard=None,
            parse_mode=parse_mode,
        )

    async def dismiss_form_message(self, context: MessageContext) -> None:
        """Dismiss a form card message after successful submission.

        Platforms that render forms as persistent chat messages (Feishu,
        Discord) should override this to delete or replace the card so
        it doesn't remain interactive.  Platforms whose forms are
        ephemeral modals (Slack) can keep the default no-op.
        """
        pass

    @abstractmethod
    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        """Answer a callback query from inline button

        Args:
            callback_id: Callback query ID
            text: Optional notification text
            show_alert: Show as alert popup

        Returns:
            Success status
        """
        pass

    @abstractmethod
    def register_handlers(self):
        """Register platform-specific message and command handlers"""
        pass

    @abstractmethod
    def run(self):
        """Start the bot/client"""
        pass

    async def shutdown(self) -> None:
        """Best-effort async shutdown for platform resources."""
        return None

    @abstractmethod
    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Get information about a user

        Args:
            user_id: Platform-specific user ID

        Returns:
            User information dict
        """
        pass

    @abstractmethod
    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        """Get information about a channel/chat

        Args:
            channel_id: Platform-specific channel ID

        Returns:
            Channel information dict
        """
        pass

    def register_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_command: Optional[Dict[str, Callable]] = None,
        on_callback_query: Optional[Callable] = None,
        **kwargs,
    ):
        """Register callback functions for different events

        Args:
            on_message: Callback for text messages
            on_command: Dict of command callbacks
            on_callback_query: Callback for button clicks
            **kwargs: Additional platform-specific callbacks
        """
        self.on_message_callback = on_message
        self.on_command_callbacks = on_command or {}
        self.on_callback_query_callback = on_callback_query

        # Store any additional callbacks
        for key, value in kwargs.items():
            setattr(self, f"{key}_callback", value)

    def log_error(self, message: str, exception: Optional[Exception] = None):
        """Standardized error logging

        Args:
            message: Error message
            exception: Optional exception to log
        """
        if exception:
            logger.error(f"{message}: {exception}")
        else:
            logger.error(message)

    def log_info(self, message: str):
        """Standardized info logging

        Args:
            message: Info message
        """
        logger.info(message)

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        """Add a reaction to an existing message.

        Default implementation returns False (unsupported).
        """
        return False

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        """Remove a reaction from an existing message.

        Default implementation returns False (unsupported).
        """
        return False

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        """Start or refresh a typing indicator for the current conversation."""

        return False

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        """Clear a previously started typing indicator for the conversation."""

        return False

    async def run_on_client_loop(self, coro):
        """Run a coroutine on the platform client's preferred event loop."""

        return await coro

    async def send_dm(self, user_id: str, text: str, **kwargs) -> Optional[str]:
        """Send a direct message to a user by their platform user ID.

        This is a convenience method for sending DMs without requiring a
        pre-existing MessageContext.  Subclasses should override with
        platform-specific DM-open + send logic.

        Args:
            user_id: Platform-specific user ID
            text: Message text
            **kwargs: Additional platform-specific arguments (e.g. blocks for Slack)

        Returns:
            Message ID of the sent message, or None on failure
        """
        logger.warning("send_dm not implemented for this platform")
        return None

    @abstractmethod
    def format_markdown(self, text: str) -> str:
        """Format markdown text for the specific platform

        Args:
            text: Text with common markdown formatting

        Returns:
            Platform-specific formatted text
        """
        pass
