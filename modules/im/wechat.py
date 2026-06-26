"""WeChat personal messaging adapter via iLink bot protocol.

Implements BaseIMClient for WeChat using HTTP long-poll for inbound messages
and HTTP POST for outbound messages, with CDN upload/download for media.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp
import base64

from .base import (
    BaseIMClient,
    BaseIMConfig,
    FileAttachment,
    FileDownloadResult,
    InlineKeyboard,
    MessageContext,
)
from config.paths import get_state_dir
from vibe.i18n import t as i18n_t
from vibe.proxy import resolve_proxy
from modules.im import wechat_api as _wechat_api_mod
from modules.im import wechat_cdn as _wechat_cdn_mod
from modules.im.formatters.wechat_formatter import WeChatFormatter

logger = logging.getLogger(__name__)


def _raise_if_wechat_api_error(resp: Optional[Dict[str, Any]], action: str) -> None:
    """Treat missing `ret` as success; raise only on explicit API errors."""

    if resp is None:
        raise RuntimeError(f"WeChat {action} failed: empty response")

    ret = resp.get("ret")
    errcode = resp.get("errcode")
    if ret in (None, 0) and errcode in (None, 0):
        return

    code = errcode if errcode not in (None, 0) else ret
    errmsg = resp.get("errmsg") or resp.get("msg", "")
    raise RuntimeError(f"WeChat {action} failed: code={code} errmsg={errmsg}")


def _get_wechat_error_code(resp: Optional[Dict[str, Any]]) -> Any:
    if not resp:
        return None
    errcode = resp.get("errcode")
    if errcode not in (None, 0):
        return errcode
    return resp.get("ret")


def _is_session_expired_code(code: Any) -> bool:
    return str(code) == str(_SESSION_EXPIRED_ERRCODE)


def _get_updates_error_code(resp: Optional[Dict[str, Any]]) -> Any:
    code = _get_wechat_error_code(resp)
    return code if code not in (None, 0) else None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_MS = 35000
_MIN_POLL_TIMEOUT_MS = 5000
_MAX_POLL_TIMEOUT_MS = 60000
_SHORT_RETRY_SECONDS = 2
_LONG_RETRY_SECONDS = 30
_MAX_CONSECUTIVE_FAILURES = 3
_DEDUP_SET_MAX = 1000
_DEDUP_CLEAN_INTERVAL_SECONDS = 300
_SESSION_EXPIRED_ERRCODE = -14
_CONTEXT_TOKEN_CACHE_VERSION = 1

# Regex patterns for stripping markdown
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_STAR = re.compile(r"\*(.+?)\*")
_MD_ITALIC_UNDER = re.compile(r"_(.+?)_")
_MD_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_CODE_FENCE = re.compile(r"```[\w]*\n?(.*?)```", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MD_HR = re.compile(r"^---+$", re.MULTILINE)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class WeChatConfig(BaseIMConfig):
    """Configuration for the WeChat iLink bot adapter."""

    bot_token: str = ""
    base_url: str = "https://api.ilink.bot"
    allowed_users: List[str] = field(default_factory=list)
    proxy_url: Optional[str] = None

    def validate(self) -> None:
        self.validate_required_string(self.bot_token, "wechat.bot_token")


# ---------------------------------------------------------------------------
# Low-level iLink API helpers
# ---------------------------------------------------------------------------


class _WeChatAPI:
    """Thin async wrapper delegating to the ``wechat_api`` module.

    The ``wechat_api`` module contains the correctly ported iLink HTTP API
    calls (proper URLs, headers, request bodies).  This class keeps the
    same interface that the rest of ``WeChatBot`` expects.
    """

    def __init__(self, timeout_total: int = 60):
        self._timeout_total = timeout_total

    async def get_updates(
        self,
        base_url: str,
        token: str,
        sync_buf: str,
        timeout_ms: int = _POLL_TIMEOUT_MS,
        proxy: Optional[str] = None,
    ) -> dict:
        """Long-poll for new messages."""
        return await _wechat_api_mod.get_updates(
            base_url,
            token,
            sync_buf,
            timeout_ms=timeout_ms,
        )

    async def send_message(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        context_token: str,
        item_list: List[Dict[str, Any]],
        proxy: Optional[str] = None,
    ) -> dict:
        """Send a message (one or more items) to a user."""
        return await _wechat_api_mod.send_message(
            base_url,
            token,
            to_user_id,
            context_token,
            item_list,
        )

    async def send_typing(
        self,
        base_url: str,
        token: str,
        to_user_id: str,
        typing_ticket: str,
        status: int = _wechat_api_mod.TYPING_START,
        proxy: Optional[str] = None,
    ) -> bool:
        """Send a typing indicator (best-effort)."""
        try:
            resp = await _wechat_api_mod.send_typing(
                base_url,
                token,
                to_user_id,
                typing_ticket,
                status=status,
            )
            _raise_if_wechat_api_error(resp, "send_typing")
            return True
        except Exception:
            return False

    async def get_config(
        self,
        base_url: str,
        token: str,
        ilink_user_id: str,
        context_token: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> dict:
        """Fetch per-user WeChat bot config."""

        return await _wechat_api_mod.get_config(
            base_url,
            token,
            ilink_user_id,
            context_token=context_token,
        )


wechat_api = _WeChatAPI()


# ---------------------------------------------------------------------------
# CDN helpers
# ---------------------------------------------------------------------------


class _WeChatCDN:
    """Thin async wrapper around iLink CDN upload/download."""

    def __init__(self, timeout_total: int = 120):
        self._timeout = aiohttp.ClientTimeout(total=timeout_total)

    async def upload_file_to_cdn(
        self,
        base_url: str,
        token: str,
        cdn_base_url: str,
        to_user_id: str,
        file_path: str,
        media_type: int = _wechat_api_mod.UPLOAD_MEDIA_FILE,
        proxy: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await _wechat_cdn_mod.upload_file_to_cdn(
                base_url=base_url,
                token=token,
                cdn_base_url=cdn_base_url,
                to_user_id=to_user_id,
                file_path=file_path,
                media_type=media_type,
            )
        except Exception as exc:
            logger.error("CDN upload error: %s", exc)
            return None

    async def upload_image_to_cdn(
        self,
        base_url: str,
        token: str,
        cdn_base_url: str,
        to_user_id: str,
        file_path: str,
        proxy: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            return await _wechat_cdn_mod.upload_image_to_cdn(
                base_url=base_url,
                token=token,
                cdn_base_url=cdn_base_url,
                to_user_id=to_user_id,
                file_path=file_path,
            )
        except Exception as exc:
            logger.error("CDN image upload error: %s", exc)
            return None

    async def download_and_decrypt(
        self,
        base_url: str,
        token: str,
        cdn_base_url: str,
        file_info: Dict[str, Any],
        target_path: str,
        proxy: Optional[str] = None,
    ) -> bool:
        """Download and decrypt a CDN file to a local path."""
        cdn_info = file_info.get("cdn_info") or {}
        wechat_item = file_info.get("wechat_item") or {}
        encrypted_query_param = cdn_info.get("encrypt_query_param") or file_info.get("url", "")
        aes_key_b64 = cdn_info.get("aes_key", "")

        if not aes_key_b64:
            item_aes_hex = wechat_item.get("aeskey")
            if item_aes_hex:
                try:
                    aes_key_b64 = base64.b64encode(bytes.fromhex(item_aes_hex)).decode("ascii")
                except ValueError:
                    logger.error("Invalid WeChat image aeskey hex for file %s", file_info.get("name", ""))
                    return False

        if not encrypted_query_param:
            logger.error("Missing encrypt_query_param for WeChat attachment: %s", file_info)
            return False

        dest = Path(target_path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            if aes_key_b64:
                content = await _wechat_cdn_mod.download_and_decrypt(
                    cdn_base_url,
                    encrypted_query_param,
                    aes_key_b64,
                )
            else:
                content = await _wechat_cdn_mod.download_plain(
                    cdn_base_url,
                    encrypted_query_param,
                )

            dest.write_bytes(content)
            return True
        except Exception as exc:
            logger.error("CDN download error: %s", exc)
            return False


wechat_cdn = _WeChatCDN()


# ---------------------------------------------------------------------------
# Auth manager (QR code login lifecycle)
# ---------------------------------------------------------------------------


class WeChatAuthManager:
    """Manages QR code login flow and token refresh for iLink bots."""

    def __init__(self) -> None:
        self.login_url: Optional[str] = None
        self.is_logged_in: bool = False

    async def check_login_status(self, base_url: str, token: str) -> bool:
        """Verify the bot token is valid and the session is active."""
        try:
            url = f"{base_url}/getLoginStatus"
            payload = {"token": token}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    self.is_logged_in = data.get("ret", -1) == 0
                    return self.is_logged_in
        except Exception as exc:
            logger.warning("Login status check failed: %s", exc)
            return False

    async def request_qr_login(self, base_url: str, token: str) -> Optional[str]:
        """Request a new QR code login URL."""
        try:
            url = f"{base_url}/getQRCode"
            payload = {"token": token}
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            ) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    self.login_url = data.get("qr_url")
                    return self.login_url
        except Exception as exc:
            logger.warning("QR login request failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class WeChatBot(BaseIMClient):
    """WeChat personal messaging adapter via iLink bot protocol."""

    _MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS = 100

    def __init__(self, config: WeChatConfig):
        super().__init__(config)
        self.config: WeChatConfig = config
        self.formatter = WeChatFormatter()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._message_callback_tasks: Set[asyncio.Task[Any]] = set()

        # Context tokens per user (needed for replies)
        self._context_tokens: Dict[str, str] = {}
        self._context_token_observed_at: Dict[str, float] = {}
        self._typing_tickets: Dict[tuple[str, str], str] = {}

        # getUpdates cursor
        self._sync_buf: str = ""
        self._poll_timeout_ms: int = _POLL_TIMEOUT_MS
        self._session_expired_logged: bool = False
        self._connection_notified: bool = False

        # Auth manager
        self._auth_manager = WeChatAuthManager()

        # Injected collaborators (set by controller)
        self.settings_manager: Any = None
        self.sessions: Any = None
        self._controller: Any = None

        # Extra callbacks captured via register_callbacks
        self._on_ready: Optional[Callable] = None
        self._on_settings_update: Optional[Callable] = None
        self._on_change_cwd: Optional[Callable] = None
        self._on_routing_update: Optional[Callable] = None
        self._on_resume_session: Optional[Callable] = None

        # Event deduplication
        self._seen_message_ids: Set[str] = set()
        self._last_dedup_clean: float = time.monotonic()

    # ------------------------------------------------------------------
    # Lifecycle / injection
    # ------------------------------------------------------------------

    def set_settings_manager(self, settings_manager: Any) -> None:
        """Set the settings manager for user/channel tracking."""
        self.settings_manager = settings_manager
        self.sessions = getattr(settings_manager, "sessions", None)

    def set_controller(self, controller: Any) -> None:
        """Set the controller reference."""
        self._controller = controller

    # ------------------------------------------------------------------
    # i18n helpers
    # ------------------------------------------------------------------

    def _get_lang(self, channel_id: Optional[str] = None) -> str:
        if self._controller and hasattr(self._controller, "config"):
            if hasattr(self._controller, "_get_lang"):
                return self._controller._get_lang()
            return getattr(self._controller.config, "language", "en")
        return "en"

    def _t(self, key: str, channel_id: Optional[str] = None, **kwargs: Any) -> str:
        lang = self._get_lang(channel_id)
        return i18n_t(key, lang, **kwargs)

    # ------------------------------------------------------------------
    # Platform metadata
    # ------------------------------------------------------------------

    @property
    def _proxy_url(self) -> Optional[str]:
        return resolve_proxy(self.config.proxy_url)

    def get_default_parse_mode(self) -> str:
        """WeChat only supports plain text."""
        return "plain"

    def should_use_thread_for_reply(self) -> bool:
        """WeChat DMs have no thread concept."""
        return False

    def should_use_thread_for_dm_session(self) -> bool:
        """WeChat DMs have no thread concept."""
        return False

    def format_markdown(self, text: str) -> str:
        """Strip markdown formatting for WeChat plain text rendering."""
        if not text:
            return text
        # Order matters: code fences before inline code, bold before italic
        result = _MD_CODE_FENCE.sub(r"\1", text)
        result = _MD_IMAGE.sub(r"\1", result)
        result = _MD_LINK.sub(r"\1", result)
        result = _MD_BOLD.sub(r"\1", result)
        result = _MD_STRIKETHROUGH.sub(r"\1", result)
        result = _MD_ITALIC_STAR.sub(r"\1", result)
        result = _MD_ITALIC_UNDER.sub(r"\1", result)
        result = _MD_INLINE_CODE.sub(r"\1", result)
        result = _MD_HEADING.sub("", result)
        result = _MD_HR.sub("---", result)
        return result.strip()

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def register_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_command: Optional[Dict[str, Callable]] = None,
        on_callback_query: Optional[Callable] = None,
        **kwargs: Any,
    ) -> None:
        """Register callbacks, capturing WeChat-specific extras."""
        super().register_callbacks(on_message, on_command, on_callback_query, **kwargs)
        if "on_settings_update" in kwargs:
            self._on_settings_update = kwargs["on_settings_update"]
        if "on_change_cwd" in kwargs:
            self._on_change_cwd = kwargs["on_change_cwd"]
        if "on_routing_update" in kwargs:
            self._on_routing_update = kwargs["on_routing_update"]
        if "on_resume_session" in kwargs:
            self._on_resume_session = kwargs["on_resume_session"]
        if "on_ready" in kwargs:
            self._on_ready = kwargs["on_ready"]

    def register_handlers(self) -> None:
        """No-op: handlers are wired via register_callbacks."""
        pass

    # ------------------------------------------------------------------
    # Sending messages
    # ------------------------------------------------------------------

    def supports_message_editing(self, context: Optional[MessageContext] = None) -> bool:
        return False

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> str:
        """Send a plain text message to a WeChat user.

        ``subtext`` (concise status-bubble footer) is part of the BaseIMClient
        contract; WeChat has no native footer styling, so it is accepted and
        ignored (no behavior change)."""
        if not text:
            raise ValueError("WeChat send_message requires non-empty text")

        user_id = context.user_id
        context_token = self._get_context_token(context)

        # Build TEXT item
        item_list = [{"type": 1, "text_item": {"text": self.format_markdown(text)}}]
        resp: Optional[Dict[str, Any]] = None

        try:
            resp = await wechat_api.send_message(
                self.config.base_url,
                self.config.bot_token,
                user_id,
                context_token,
                item_list,
                proxy=self._proxy_url,
            )
            if _is_session_expired_code(_get_wechat_error_code(resp)):
                self._mark_session_expired(_get_wechat_error_code(resp))
            _raise_if_wechat_api_error(resp, "send_message")
        except Exception as exc:
            logger.error("WeChat send_message error: %s", exc)
            raise

        # Generate a synthetic message ID (iLink may not return one)
        message_id = resp.get("message_id", "") if resp else ""
        if not message_id:
            message_id = f"wc-{uuid.uuid4().hex[:12]}"
        return message_id

    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
    ) -> str:
        """Send a message with button labels appended as text hints.

        WeChat personal messaging does not support inline buttons, so we
        render button labels as a text footer.
        """
        # Build button hint footer
        button_labels: List[str] = []
        for row in keyboard.buttons:
            for btn in row:
                button_labels.append(f"[{btn.text}]")

        footer = ""
        if button_labels:
            footer = f"\n\n---\nOptions: {' '.join(button_labels)}"

        return await self.send_message(context, text + footer, parse_mode=parse_mode)

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> bool:
        """WeChat does not support editing sent messages.

        ``subtext`` is accepted for the BaseIMClient contract and ignored."""
        return False

    async def answer_callback(
        self,
        callback_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        """WeChat does not support callback queries."""
        return False

    # ------------------------------------------------------------------
    # User / channel info
    # ------------------------------------------------------------------

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Return basic user info. WeChat doesn't expose rich user profiles."""
        return {
            "id": user_id,
            "name": "WeChat User",
            "display_name": "WeChat User",
            "real_name": "WeChat User",
            "platform": "wechat",
        }

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        """Return basic channel info (always DM for personal WeChat)."""
        return {
            "id": channel_id,
            "name": "WeChat DM",
            "type": "dm",
        }

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _get_typing_ticket(self, user_id: str, context_token: str) -> str:
        cache_key = (user_id, context_token)
        cached = self._typing_tickets.get(cache_key) or self._typing_tickets.get((user_id, ""))
        if cached:
            return cached

        resp = await wechat_api.get_config(
            self.config.base_url,
            self.config.bot_token,
            user_id,
            context_token=context_token or None,
            proxy=self._proxy_url,
        )
        _raise_if_wechat_api_error(resp, "get_config")

        typing_ticket = str(resp.get("typing_ticket") or "").strip()
        if not typing_ticket:
            raise RuntimeError(f"WeChat get_config failed: missing typing_ticket for {user_id}")

        self._typing_tickets[cache_key] = typing_ticket
        self._typing_tickets[(user_id, "")] = typing_ticket
        return typing_ticket

    async def send_typing_indicator(
        self,
        context: MessageContext,
    ) -> bool:
        """Send a typing indicator (best-effort, don't block on errors)."""
        user_id = context.user_id
        context_token = self._get_context_token(context)
        if not context_token:
            return False
        try:
            typing_ticket = await self._get_typing_ticket(user_id, context_token)
        except Exception as exc:
            logger.debug("Failed to fetch WeChat typing ticket for %s: %s", user_id, exc)
            return False

        ok = await wechat_api.send_typing(
            self.config.base_url,
            self.config.bot_token,
            user_id,
            typing_ticket,
            status=_wechat_api_mod.TYPING_START,
            proxy=self._proxy_url,
        )
        if not ok:
            self._typing_tickets.pop((user_id, context_token), None)
            self._typing_tickets.pop((user_id, ""), None)
        return ok

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        """Cancel the active typing indicator for the current WeChat DM."""

        user_id = context.user_id
        context_token = self._get_context_token(context)
        if not context_token:
            return False

        try:
            typing_ticket = await self._get_typing_ticket(user_id, context_token)
        except Exception as exc:
            logger.debug("Failed to fetch WeChat typing ticket for cancel %s: %s", user_id, exc)
            return False

        ok = await wechat_api.send_typing(
            self.config.base_url,
            self.config.bot_token,
            user_id,
            typing_ticket,
            status=_wechat_api_mod.TYPING_CANCEL,
            proxy=self._proxy_url,
        )
        if not ok:
            self._typing_tickets.pop((user_id, context_token), None)
            self._typing_tickets.pop((user_id, ""), None)
        return ok

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    @staticmethod
    def _is_video_file(file_path: str, title: Optional[str] = None) -> bool:
        suffix = Path(title or file_path).suffix.lower()
        return suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

    async def upload_file_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a file via CDN and send it as a file message."""
        if self._is_video_file(file_path, title):
            return await self.upload_video_from_path(context, file_path, title=title)

        cdn_meta = await wechat_cdn.upload_file_to_cdn(
            self.config.base_url,
            self.config.bot_token,
            getattr(self.config, "cdn_base_url", self.config.base_url),
            context.user_id,
            file_path,
            proxy=self._proxy_url,
        )
        if cdn_meta is None:
            logger.error("Failed to upload file to CDN: %s", file_path)
            return ""

        user_id = context.user_id
        context_token = self._get_context_token(context)
        display_name = title or Path(file_path).name

        item_list = [
            {
                "type": 4,  # FILE
                "file_item": {
                    "media": {
                        "encrypt_query_param": cdn_meta.get("encrypt_query_param", ""),
                        "aes_key": cdn_meta.get("aes_key", ""),
                        "encrypt_type": 1,
                    },
                    "file_name": display_name,
                    "len": str(cdn_meta.get("file_size", 0)),
                },
            }
        ]

        try:
            resp = await wechat_api.send_message(
                self.config.base_url,
                self.config.bot_token,
                user_id,
                context_token,
                item_list,
                proxy=self._proxy_url,
            )
            if _is_session_expired_code(_get_wechat_error_code(resp)):
                self._mark_session_expired(_get_wechat_error_code(resp))
            _raise_if_wechat_api_error(resp, "send file message")
        except Exception as exc:
            logger.error("WeChat send file message error: %s", exc)
            return ""

        return cdn_meta.get("file_id", f"wc-file-{uuid.uuid4().hex[:8]}")

    async def upload_image_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload an image via CDN and send it as an image message."""
        if self._is_video_file(file_path, title):
            return await self.upload_video_from_path(context, file_path, title=title)

        cdn_meta = await wechat_cdn.upload_image_to_cdn(
            self.config.base_url,
            self.config.bot_token,
            getattr(self.config, "cdn_base_url", self.config.base_url),
            context.user_id,
            file_path,
            proxy=self._proxy_url,
        )
        if cdn_meta is None:
            logger.error("Failed to upload image to CDN: %s", file_path)
            return ""

        user_id = context.user_id
        context_token = self._get_context_token(context)

        item_list = [
            {
                "type": 2,  # IMAGE
                "image_item": {
                    "media": {
                        "encrypt_query_param": cdn_meta.get("encrypt_query_param", ""),
                        "aes_key": cdn_meta.get("aes_key", ""),
                        "encrypt_type": 1,
                    },
                    "mid_size": cdn_meta.get("file_size_ciphertext", 0),
                },
            }
        ]

        try:
            resp = await wechat_api.send_message(
                self.config.base_url,
                self.config.bot_token,
                user_id,
                context_token,
                item_list,
                proxy=self._proxy_url,
            )
            if _is_session_expired_code(_get_wechat_error_code(resp)):
                self._mark_session_expired(_get_wechat_error_code(resp))
            _raise_if_wechat_api_error(resp, "send image message")
        except Exception as exc:
            logger.error("WeChat send image message error: %s", exc)
            return ""

        return cdn_meta.get("file_id", f"wc-img-{uuid.uuid4().hex[:8]}")

    async def upload_video_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a video via CDN and send it as a native video message."""

        cdn_meta = await wechat_cdn.upload_file_to_cdn(
            self.config.base_url,
            self.config.bot_token,
            getattr(self.config, "cdn_base_url", self.config.base_url),
            context.user_id,
            file_path,
            media_type=_wechat_api_mod.UPLOAD_MEDIA_VIDEO,
        )
        if cdn_meta is None:
            logger.error("Failed to upload video to CDN: %s", file_path)
            return ""

        user_id = context.user_id
        context_token = self._get_context_token(context)
        display_name = title or Path(file_path).name

        item_list = [
            {
                "type": 5,  # VIDEO
                "video_item": {
                    "media": {
                        "encrypt_query_param": cdn_meta.get("encrypt_query_param", ""),
                        "aes_key": cdn_meta.get("aes_key", ""),
                        "encrypt_type": 1,
                    },
                    "video_size": cdn_meta.get("file_size_ciphertext", 0),
                },
                "file_name": display_name,
            }
        ]

        try:
            resp = await wechat_api.send_message(
                self.config.base_url,
                self.config.bot_token,
                user_id,
                context_token,
                item_list,
                proxy=self._proxy_url,
            )
            if _is_session_expired_code(_get_wechat_error_code(resp)):
                self._mark_session_expired(_get_wechat_error_code(resp))
            _raise_if_wechat_api_error(resp, "send video message")
        except Exception as exc:
            logger.error("WeChat send video message error: %s", exc)
            return ""

        return cdn_meta.get("file_id", f"wc-video-{uuid.uuid4().hex[:8]}")

    async def download_file_to_path(
        self,
        file_info: Dict[str, Any],
        target_path: str,
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> FileDownloadResult:
        """Download a CDN file to a local path."""
        success = await wechat_cdn.download_and_decrypt(
            self.config.base_url,
            self.config.bot_token,
            getattr(self.config, "cdn_base_url", self.config.base_url),
            file_info,
            target_path,
            proxy=self._proxy_url,
        )
        if not success:
            return FileDownloadResult(False, "CDN download/decrypt failed")

        # Enforce max_bytes after download if specified
        if max_bytes is not None:
            dest = Path(target_path)
            if dest.exists() and dest.stat().st_size > max_bytes:
                dest.unlink(missing_ok=True)
                return FileDownloadResult(
                    False,
                    f"File exceeds max_bytes ({max_bytes})",
                )

        return FileDownloadResult(True)

    async def download_file(
        self,
        file_info: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> Optional[bytes]:
        """Download a CDN file into memory."""
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        try:
            result = await self.download_file_to_path(
                file_info,
                tmp_path,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
            if not result.success:
                return None
            return Path(tmp_path).read_bytes()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # DM convenience
    # ------------------------------------------------------------------

    async def send_dm(self, user_id: str, text: str, **kwargs: Any) -> Optional[str]:
        """Send a direct message by user ID.

        For WeChat, every conversation is already a DM, so we just need a
        cached context_token for the user.
        """
        context_token = self._get_context_token_for_user(user_id)
        if not context_token:
            logger.warning(
                "No context_token cached for user %s; cannot send DM",
                user_id,
            )
            return None

        context = MessageContext(
            user_id=user_id,
            channel_id=user_id,
            thread_id=None,
            message_id=None,
            platform="wechat",
            platform_specific={"platform": "wechat", "is_dm": True, "context_token": context_token},
        )
        try:
            return await self.send_message(context, text)
        except Exception as exc:
            logger.error("send_dm failed for user %s: %s", user_id, exc)
            return None

    # ------------------------------------------------------------------
    # Run / shutdown
    # ------------------------------------------------------------------

    async def _notify_connection_state(self, action: str) -> None:
        """Best-effort online-state notification for the iLink gateway."""
        if not self.config.bot_token:
            return
        try:
            if action == "start":
                if self._connection_notified:
                    return
                resp = await _wechat_api_mod.notify_start(self.config.base_url, self.config.bot_token)
            elif action == "stop":
                if not self._connection_notified:
                    return
                resp = await _wechat_api_mod.notify_stop(self.config.base_url, self.config.bot_token)
            else:
                raise ValueError(f"unknown WeChat notify action: {action}")
            _raise_if_wechat_api_error(resp, f"notify_{action}")
            self._connection_notified = action == "start"
        except Exception as exc:
            logger.warning("WeChat notify_%s failed: %s", action, exc)

    def run(self) -> None:
        """Start the WeChat bot: validate config, run the async poll loop."""
        if not self.config.bot_token:
            logger.warning(
                "WeChat bot_token is not configured. "
                "The bot will idle until a token is set via the Web UI QR login. "
                "Access the setup wizard to complete WeChat configuration."
            )

        logger.info("Starting WeChat bot via iLink protocol...")

        async def _start() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop_event = asyncio.Event()

            # Notify on_ready callback (starts UI server even without token)
            if self._on_ready:
                try:
                    await self._on_ready()
                except Exception as exc:
                    logger.error("on_ready callback failed: %s", exc, exc_info=True)

            if not self.config.bot_token:
                logger.info("WeChat bot idling (no bot_token). Complete QR login via the Web UI to activate.")
                await self._stop_event.wait()
                return

            # Load persisted sync buffer
            self._load_sync_buf()
            self._load_context_tokens()

            # Check login status
            logged_in = await self._auth_manager.check_login_status(
                self.config.base_url,
                self.config.bot_token,
            )
            if logged_in:
                logger.info("WeChat bot session is active")
            else:
                logger.warning(
                    "WeChat bot session is not active; messages may fail until the session is re-authenticated",
                )

            await self._notify_connection_state("start")

            # Start poll loop as a background task
            self._poll_task = asyncio.create_task(self._poll_loop())

            logger.info("WeChat bot started, entering poll loop")

            # Block until stop is signalled
            await self._stop_event.wait()

            # Cancel poll task on shutdown
            if self._poll_task and not self._poll_task.done():
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass

            await self._notify_connection_state("stop")

            logger.info("WeChat bot stopped")

        try:
            asyncio.run(_start())
        except KeyboardInterrupt:
            logger.info("WeChat bot shutting down (keyboard interrupt)...")

    def stop(self) -> None:
        """Signal the bot to stop."""
        if self._stop_event is None:
            return
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()

    async def shutdown(self) -> None:
        """Best-effort async shutdown for platform resources."""
        if self._stop_event is not None:
            self._stop_event.set()
        await self._notify_connection_state("stop")
        # Persist sync buffer so we don't re-process old messages on restart
        self._save_sync_buf()

    @staticmethod
    def _normalize_poll_timeout(timeout_ms: Any) -> int:
        try:
            value = int(timeout_ms)
        except (TypeError, ValueError):
            return _POLL_TIMEOUT_MS
        return max(_MIN_POLL_TIMEOUT_MS, min(_MAX_POLL_TIMEOUT_MS, value))

    def _mark_session_expired(self, errcode: Any) -> None:
        self._auth_manager.is_logged_in = False
        self._typing_tickets.clear()
        self._clear_context_tokens()
        if not self._session_expired_logged:
            logger.error(
                "WeChat session expired (errcode %s); re-authentication required via the Web UI QR login",
                errcode,
            )
            self._session_expired_logged = True

    def _mark_session_active(self) -> None:
        self._auth_manager.is_logged_in = True
        self._session_expired_logged = False

    # ------------------------------------------------------------------
    # Long-poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main message receiving loop via HTTP long-poll."""
        if self._stop_event is None:
            return

        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                resp = await wechat_api.get_updates(
                    self.config.base_url,
                    self.config.bot_token,
                    self._sync_buf,
                    timeout_ms=self._poll_timeout_ms,
                    proxy=self._proxy_url,
                )

                errcode = _get_updates_error_code(resp)
                if errcode is not None:
                    ret = resp.get("ret", 0)
                    logger.warning(
                        "getUpdates error: ret=%s errcode=%s msg=%s",
                        ret,
                        errcode,
                        resp.get("errmsg") or resp.get("msg", ""),
                    )

                    # Handle session expired
                    if _is_session_expired_code(errcode):
                        self._mark_session_expired(errcode)

                    consecutive_failures += 1
                    if _is_session_expired_code(errcode):
                        await asyncio.sleep(_LONG_RETRY_SECONDS)
                        consecutive_failures = 0
                    elif consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                        logger.warning(
                            "Multiple consecutive poll failures (%d); backing off to %ds",
                            consecutive_failures,
                            _LONG_RETRY_SECONDS,
                        )
                        await asyncio.sleep(_LONG_RETRY_SECONDS)
                        consecutive_failures = 0
                    else:
                        await asyncio.sleep(_SHORT_RETRY_SECONDS)
                    continue

                # Success path
                consecutive_failures = 0
                self._mark_session_active()

                suggested_timeout_ms = resp.get("longpolling_timeout_ms")
                if suggested_timeout_ms is not None:
                    next_timeout_ms = self._normalize_poll_timeout(suggested_timeout_ms)
                    if next_timeout_ms != self._poll_timeout_ms:
                        logger.info(
                            "WeChat long-poll timeout adjusted from %sms to %sms",
                            self._poll_timeout_ms,
                            next_timeout_ms,
                        )
                        self._poll_timeout_ms = next_timeout_ms

                # Update sync cursor
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    self._sync_buf = new_buf
                    self._save_sync_buf()

                # Process messages
                msgs = resp.get("msgs", [])
                if msgs:
                    logger.info("Received %d message(s)", len(msgs))
                for msg in msgs:
                    try:
                        await self._process_inbound_message(msg)
                    except Exception as msg_exc:
                        logger.error(
                            "Failed to process message %s: %s",
                            msg.get("message_id", "?"),
                            msg_exc,
                            exc_info=True,
                        )

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Poll loop error: %s", exc, exc_info=True)
                consecutive_failures += 1
                delay = (
                    _LONG_RETRY_SECONDS if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES else _SHORT_RETRY_SECONDS
                )
                await asyncio.sleep(delay)
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    # ------------------------------------------------------------------
    # Inbound message processing
    # ------------------------------------------------------------------

    async def _wait_for_message_callback_capacity(self) -> None:
        while len(self._message_callback_tasks) >= self._MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS:
            pending = tuple(self._message_callback_tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _spawn_message_callback_task(self, context: MessageContext, text: str) -> None:
        if not self.on_message_callback:
            return
        await self._wait_for_message_callback_capacity()
        task = asyncio.create_task(self.on_message_callback(context, text))
        self._message_callback_tasks.add(task)
        task.add_done_callback(self._handle_message_callback_task_done)

    def _handle_message_callback_task_done(self, task: asyncio.Task[Any]) -> None:
        self._message_callback_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("WeChat message callback task failed")

    def _get_pending_bind_menu_hint_user(self, user_id: str) -> Any:
        manager = self.settings_manager
        if manager is None:
            return None
        store_getter = getattr(manager, "get_store", None)
        if not callable(store_getter):
            return None
        try:
            store = store_getter()
            user = store.get_user(user_id, platform="wechat")
            if user is None or not user.pending_bind_menu_hint:
                return None
            return user
        except Exception as exc:
            logger.warning("Failed to read WeChat bind menu hint for %s: %s", user_id, exc)
            return None

    def _clear_pending_bind_menu_hint(self, user_id: str, user: Any) -> None:
        manager = self.settings_manager
        if manager is None:
            return
        store_getter = getattr(manager, "get_store", None)
        if not callable(store_getter):
            return
        try:
            store = store_getter()
            user.pending_bind_menu_hint = False
            store.update_user(user_id, user, platform="wechat")
            manager_reload = getattr(manager, "_reload_if_changed", None)
            if callable(manager_reload):
                manager_reload()
        except Exception as exc:
            logger.warning("Failed to clear WeChat bind menu hint for %s: %s", user_id, exc)

    async def _send_bind_menu_hint_if_pending(self, context: MessageContext) -> None:
        user = self._get_pending_bind_menu_hint_user(context.user_id)
        if user is None:
            return
        try:
            await self.send_message(context, self._t("bind.menuHintStart", context.channel_id))
        except Exception as exc:
            logger.warning("Failed to send WeChat bind menu hint to %s: %s", context.user_id, exc)
            return
        self._clear_pending_bind_menu_hint(context.user_id, user)

    async def _process_inbound_message(self, msg: dict) -> None:
        """Convert an iLink message to MessageContext and dispatch."""
        message_id = str(msg.get("message_id", ""))

        # Dedup
        if message_id and not self._mark_message_seen(message_id):
            return

        from_user = msg.get("from_user_id", "")
        if not from_user:
            logger.debug("Skipping message with empty from_user_id")
            return

        context_token = msg.get("context_token", "")
        logger.info(
            "Processing inbound message: id=%s from=%s context_token=%s items=%d",
            message_id,
            from_user,
            context_token[:16] + "..." if len(context_token) > 16 else context_token,
            len(msg.get("item_list", [])),
        )

        # Cache context_token for replies
        if context_token:
            self._remember_context_token(from_user, context_token)

        # Extract text from item_list
        text = self._extract_text(msg)

        # Build MessageContext
        context = MessageContext(
            user_id=from_user,
            channel_id=from_user,  # WeChat DM: channel == user
            thread_id=None,  # No threads in WeChat
            message_id=message_id,
            platform="wechat",
            platform_specific={
                "platform": "wechat",
                "message": msg,
                "is_dm": True,  # Always DM for personal messaging
                "context_token": context_token,
            },
            files=[],
        )

        # Handle media attachments
        await self._process_media_items(msg, context)

        # Authorization check
        auth_result = self.check_authorization(
            user_id=from_user,
            channel_id=from_user,
            is_dm=True,
            text=text,
            settings_manager=self.settings_manager,
        )
        if not auth_result.allowed:
            logger.info(
                "Dropping unauthorized WeChat message from=%s denial=%s",
                from_user,
                auth_result.denial,
            )
            denial_text = self.build_auth_denial_text(
                auth_result.denial,
                channel_id=from_user,
            )
            if denial_text:
                try:
                    await self.send_message(context, denial_text)
                except Exception as exc:
                    logger.error("Failed to send auth denial: %s", exc)
            return

        await self._send_bind_menu_hint_if_pending(context)

        # Try slash command dispatch first
        allow_plain_bind = self.should_allow_plain_bind(
            user_id=from_user,
            is_dm=True,
            settings_manager=self.settings_manager,
        )
        if await self.dispatch_text_command(
            context,
            text,
            allow_plain_bind=allow_plain_bind,
        ):
            return

        # Dispatch to message handler
        if self.on_message_callback:
            try:
                await self._spawn_message_callback_task(context, text)
            except Exception as exc:
                logger.error(
                    "Message callback error for user %s: %s",
                    from_user,
                    exc,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Text / media extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(msg: dict) -> str:
        """Extract text content from iLink message item_list.

        The ``type`` field is an integer: 1=TEXT, 2=IMAGE, 3=VOICE, 4=FILE, 5=VIDEO.
        """
        text_parts: List[str] = []
        quoted_title = ""
        for item in msg.get("item_list", []):
            item_type = item.get("type", 0)
            if item_type == 1 or item_type in ("TEXT", "text"):
                text_item = item.get("text_item") or {}
                content = text_item.get("text") or item.get("content", "")
                if content:
                    text_parts.append(str(content))
                ref_msg = item.get("ref_msg") or {}
                quoted_title = quoted_title or str(ref_msg.get("title") or "").strip()
                continue
            if item_type == 3 or item_type in ("VOICE", "voice"):
                voice_item = item.get("voice_item") or {}
                content = voice_item.get("text", "")
                if content:
                    playtime_ms = voice_item.get("playtime")
                    seconds = 0.0
                    try:
                        seconds = max(0.0, float(str(playtime_ms)) / 1000.0)
                    except (TypeError, ValueError):
                        seconds = 0.0
                    prefix = f"[Voice {seconds:.1f}s] " if seconds > 0 else "[Voice] "
                    text_parts.append(prefix + str(content))

        text = " ".join(text_parts).strip()
        if quoted_title:
            if text:
                return f"{text}\n[Quoted message: {quoted_title}]"
            return f"[Quoted message: {quoted_title}]"
        return text

    @staticmethod
    def _extract_reference_media_item(msg: dict) -> Optional[dict]:
        for item in msg.get("item_list", []):
            if item.get("type", 0) not in (1, "TEXT", "text"):
                continue
            ref_msg = item.get("ref_msg") or {}
            ref_item = ref_msg.get("message_item") or {}
            ref_type = ref_item.get("type", 0)
            if ref_type == 2 and (ref_item.get("image_item") or {}).get("media", {}).get("encrypt_query_param"):
                return ref_item
            if ref_type == 5 and (ref_item.get("video_item") or {}).get("media", {}).get("encrypt_query_param"):
                return ref_item
            if ref_type == 4 and (ref_item.get("file_item") or {}).get("media", {}).get("encrypt_query_param"):
                return ref_item
            if ref_type == 3:
                voice_item = ref_item.get("voice_item") or {}
                if (voice_item.get("media") or {}).get("encrypt_query_param") and not voice_item.get("text"):
                    return ref_item
        return None

    async def _process_media_items(
        self,
        msg: dict,
        context: MessageContext,
    ) -> None:
        """Populate context.files from media items in the message."""
        if context.files is None:
            context.files = []

        processed_any = False

        for item in msg.get("item_list", []):
            item_type = item.get("type", 0)
            item_data: Dict[str, Any] = {}
            media: Dict[str, Any] = {}
            file_name = ""
            size: Optional[int] = None
            mimetype = "application/octet-stream"

            if item_type == 2:
                item_data = item.get("image_item") or {}
                media = item_data.get("media") or {}
                file_name = item.get("file_name", "") or "wechat_image.jpg"
                mimetype = "image/jpeg"
            elif item_type == 3:
                item_data = item.get("voice_item") or {}
                media = item_data.get("media") or {}
                file_name = item.get("file_name", "") or "wechat_voice.silk"
                mimetype = "audio/silk"
            elif item_type == 4:
                item_data = item.get("file_item") or {}
                media = item_data.get("media") or {}
                file_name = item_data.get("file_name", "") or item.get("file_name", "")
                try:
                    size = int(item_data.get("len", 0)) or None
                except (TypeError, ValueError):
                    size = None
                mimetype = "application/octet-stream"
            elif item_type == 5:
                item_data = item.get("video_item") or {}
                media = item_data.get("media") or {}
                file_name = item.get("file_name", "") or "wechat_video.mp4"
                mimetype = "video/mp4"
            else:
                continue

            attachment = FileAttachment(
                name=file_name or f"wechat_{item_type}_attachment",
                mimetype=mimetype,
                url=media.get("encrypt_query_param", ""),
                size=size,
            )
            # Store CDN info for later download
            attachment.__dict__["cdn_info"] = media
            attachment.__dict__["wechat_item"] = item_data
            context.files.append(attachment)
            processed_any = True

        if not processed_any:
            ref_item = self._extract_reference_media_item(msg)
            if ref_item:
                await self._process_media_items({"item_list": [ref_item]}, context)

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _mark_message_seen(self, message_id: str) -> bool:
        """Return True if this is a new message; False if already seen."""
        self._maybe_clean_dedup_set()

        if message_id in self._seen_message_ids:
            logger.debug("Duplicate message ignored: %s", message_id)
            return False

        self._seen_message_ids.add(message_id)
        return True

    def _maybe_clean_dedup_set(self) -> None:
        """Periodically trim the dedup set to avoid unbounded growth."""
        now = time.monotonic()
        if now - self._last_dedup_clean < _DEDUP_CLEAN_INTERVAL_SECONDS:
            return

        self._last_dedup_clean = now
        if len(self._seen_message_ids) > _DEDUP_SET_MAX:
            # Keep the most recent half by clearing the whole set.
            # This is a simple strategy; messages arriving during the clean
            # window are unlikely to be duplicates.
            excess = len(self._seen_message_ids) - _DEDUP_SET_MAX // 2
            # Sets don't have ordering, so just discard arbitrary elements
            to_remove = list(self._seen_message_ids)[:excess]
            for mid in to_remove:
                self._seen_message_ids.discard(mid)
            logger.debug(
                "Dedup set trimmed: removed %d, remaining %d",
                len(to_remove),
                len(self._seen_message_ids),
            )

    # ------------------------------------------------------------------
    # Sync buffer persistence
    # ------------------------------------------------------------------

    def _get_sync_buf_path(self) -> Path:
        """Return the path for persisting the getUpdates cursor."""
        state_dir = get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "wechat_sync_buf.json"

    def _load_sync_buf(self) -> None:
        """Load the persisted sync buffer from disk."""
        path = self._get_sync_buf_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._sync_buf = data.get("sync_buf", "")
            if self._sync_buf:
                logger.info("Loaded persisted sync buffer (%d chars)", len(self._sync_buf))
        except Exception as exc:
            logger.warning("Failed to load sync buffer: %s", exc)

    def _save_sync_buf(self) -> None:
        """Persist the current sync buffer to disk."""
        if not self._sync_buf:
            return
        try:
            path = self._get_sync_buf_path()
            path.write_text(
                json.dumps({"sync_buf": self._sync_buf}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save sync buffer: %s", exc)

    # ------------------------------------------------------------------
    # Context token helpers
    # ------------------------------------------------------------------

    def _get_context_token_cache_path(self) -> Path:
        state_dir = get_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "wechat_context_tokens.json"

    def _load_context_tokens(self) -> None:
        path = self._get_context_token_cache_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_tokens = data.get("tokens") if isinstance(data, dict) else None
            if not isinstance(raw_tokens, dict):
                return

            loaded_tokens: Dict[str, str] = {}
            loaded_observed_at: Dict[str, float] = {}
            for user_id, record in raw_tokens.items():
                token = ""
                observed_at = 0.0
                if isinstance(record, dict):
                    token = str(record.get("context_token") or "")
                    try:
                        observed_at = float(record.get("observed_at") or 0)
                    except (TypeError, ValueError):
                        observed_at = 0.0
                elif isinstance(record, str):
                    token = record
                if not user_id or not token:
                    continue
                loaded_tokens[str(user_id)] = token
                loaded_observed_at[str(user_id)] = observed_at

            self._context_tokens.update(loaded_tokens)
            self._context_token_observed_at.update(loaded_observed_at)
            if loaded_tokens:
                logger.info("Loaded persisted WeChat context tokens for %d user(s)", len(loaded_tokens))
        except Exception as exc:
            logger.warning("Failed to load WeChat context tokens: %s", exc)

    def _save_context_tokens(self) -> None:
        try:
            path = self._get_context_token_cache_path()
            payload = {
                "version": _CONTEXT_TOKEN_CACHE_VERSION,
                "tokens": {
                    user_id: {
                        "context_token": token,
                        "observed_at": self._context_token_observed_at.get(user_id, 0),
                    }
                    for user_id, token in self._context_tokens.items()
                    if token
                },
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=path.parent,
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False)
                temp_path = Path(handle.name)
            temp_path.replace(path)
        except Exception as exc:
            logger.warning("Failed to save WeChat context tokens: %s", exc)

    def _remember_context_token(self, user_id: str, context_token: str) -> None:
        if not user_id or not context_token:
            return
        self._context_tokens[user_id] = context_token
        self._context_token_observed_at[user_id] = time.time()
        self._save_context_tokens()

    def _clear_context_tokens(self) -> None:
        if not self._context_tokens and not self._context_token_observed_at:
            return
        self._context_tokens.clear()
        self._context_token_observed_at.clear()
        self._save_context_tokens()

    def _get_context_token_for_user(self, user_id: str) -> str:
        token = self._context_tokens.get(user_id, "")
        if token:
            return token
        self._load_context_tokens()
        return self._context_tokens.get(user_id, "")

    def _get_context_token(self, context: MessageContext) -> str:
        """Resolve the context_token for a given message context.

        Tries platform_specific first, then falls back to the cached map.
        """
        ps = context.platform_specific or {}
        token = ps.get("context_token", "")
        if token:
            return token
        return self._get_context_token_for_user(context.user_id)

    # ------------------------------------------------------------------
    # Reactions (unsupported)
    # ------------------------------------------------------------------

    async def add_reaction(
        self,
        context: MessageContext,
        message_id: str,
        emoji: str,
    ) -> bool:
        """WeChat personal messaging does not support reactions."""
        return False

    async def remove_reaction(
        self,
        context: MessageContext,
        message_id: str,
        emoji: str,
    ) -> bool:
        """WeChat personal messaging does not support reactions."""
        return False
