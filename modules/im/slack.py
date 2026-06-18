import asyncio
import hashlib
import json
import logging
import re
import time
import aiohttp
from typing import Dict, Any, Optional, Callable, List
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.errors import SlackApiError
from markdown_to_mrkdwn import SlackMarkdownConverter

from .base import (
    BaseIMClient,
    FileDownloadResult,
    MessageContext,
    InlineKeyboard,
    InlineButton,
    FileAttachment,
)
from config.v2_config import SlackConfig
from core.auth import AuthResult
from .formatters import SlackFormatter
from .slack_modal import parse_routing_modal_selection
from vibe.i18n import get_supported_languages, t as i18n_t
from vibe.proxy import resolve_proxy
from modules.agents.opencode.utils import (
    build_claude_reasoning_options,
    build_opencode_model_option_items,
    build_codex_reasoning_options,
    format_claude_model_label,
    build_reasoning_effort_options,
    resolve_opencode_allowed_providers,
    resolve_opencode_provider_preferences,
)
from modules.agents.native_sessions.display import format_display_summary, format_display_time
from modules.agents.native_sessions.types import NativeResumeSession

logger = logging.getLogger(__name__)

_UNSET = object()
_SLACK_SECTION_TEXT_LIMIT = 3000
_SLACK_MARKDOWN_TEXT_LIMIT = 12000
_BARE_HTTP_URL_RE = re.compile(r"https?://[^\s<>\|]+")
_TRAILING_URL_PUNCTUATION = ".,!?;:"
_EVENT_TASK_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 70.0


class SlackBot(BaseIMClient):
    """Slack implementation of the IM client"""

    def __init__(self, config: SlackConfig):
        super().__init__(config)
        self.config = config
        self.web_client: Optional[AsyncWebClient] = None
        self.socket_client: Optional[SocketModeClient] = None

        # Initialize Slack formatter
        self.formatter = SlackFormatter()

        # Initialize markdown to mrkdwn converter
        self.markdown_converter = SlackMarkdownConverter()

        # Note: Thread handling now uses user's message timestamp directly

        # Store callback handlers
        self.command_handlers: Dict[str, Callable] = {}
        self.slash_command_handlers: Dict[str, Callable] = {}

        # Store trigger IDs for modal interactions
        self.trigger_ids: Dict[str, str] = {}

        # Settings manager for thread tracking (will be injected later)
        self.settings_manager = None
        self.sessions = None
        # Controller reference for update button handling (will be injected later)
        self._controller = None
        self._recent_event_ids: Dict[str, float] = {}
        self._processed_mention_event_keys: Dict[str, float] = {}
        self._event_tasks: set[asyncio.Task] = set()
        self._event_task_shutdown_drain_timeout = _EVENT_TASK_SHUTDOWN_DRAIN_TIMEOUT_SECONDS
        self._user_info_cache: Dict[str, Dict[str, Any]] = {}
        self._channel_info_cache: Dict[str, Dict[str, Any]] = {}
        self._bot_user_id: Optional[str] = None
        self._bot_id: Optional[str] = None
        self._bot_identity_lookup_attempted = False
        self._stop_event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._on_ready: Optional[Callable] = None

        # RTM typing indicator (best-effort, may not work with modern Slack apps)
        self._rtm_ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._rtm_session: Optional[aiohttp.ClientSession] = None
        self._rtm_available: Optional[bool] = None  # None = not yet attempted
        self._rtm_msg_id: int = 0
        self._rtm_drain_task: Optional[asyncio.Task] = None

    def set_settings_manager(self, settings_manager):
        """Set the settings manager for thread tracking"""
        self.settings_manager = settings_manager
        self.sessions = getattr(settings_manager, "sessions", None)

    def set_controller(self, controller):
        """Set the controller reference for handling update button clicks"""
        self._controller = controller

    def _get_lang(self, channel_id: Optional[str] = None) -> str:
        """Get the global language setting from config."""
        # Read from global config via controller
        if self._controller and hasattr(self._controller, "config"):
            if hasattr(self._controller, "_get_lang"):
                return self._controller._get_lang()
            return getattr(self._controller.config, "language", "en")
        return "en"

    def _t(self, key: str, channel_id: Optional[str] = None, **kwargs) -> str:
        """Translate a key for the given channel's language."""
        lang = self._get_lang(channel_id)
        return i18n_t(key, lang, **kwargs)

    def _get_bound_user_record(self, user_id: Optional[str]) -> Optional[Any]:
        if not user_id or self.settings_manager is None:
            return None
        store_getter = getattr(self.settings_manager, "get_store", None)
        if not callable(store_getter):
            return None
        try:
            store = store_getter()
        except Exception:
            logger.debug("Failed to access settings store for Slack DM validation", exc_info=True)
            return None
        try:
            store.maybe_reload()
        except Exception:
            logger.debug("Failed to reload settings store before Slack DM validation", exc_info=True)
        try:
            record = store.get_user(user_id, platform="slack")
        except TypeError:
            record = store.get_user(user_id)
        except Exception:
            logger.debug("Failed to read bound Slack user record for %s", user_id, exc_info=True)
            return None
        if record is None:
            return None
        return record

    def _persist_bound_dm_channel(self, user_id: str, record: Any, dm_channel_id: str) -> None:
        normalized_channel_id = str(dm_channel_id or "").strip()
        if not normalized_channel_id:
            return
        existing_channel_id = str(getattr(record, "dm_chat_id", "") or "").strip()
        if existing_channel_id == normalized_channel_id:
            return
        setattr(record, "dm_chat_id", normalized_channel_id)
        if self.settings_manager is None:
            return
        store_getter = getattr(self.settings_manager, "get_store", None)
        if not callable(store_getter):
            return
        try:
            store = store_getter()
        except Exception:
            logger.debug("Failed to access settings store for Slack DM persistence", exc_info=True)
            return
        try:
            store.update_user(user_id, record, platform="slack")
        except TypeError:
            store.update_user(user_id, record)
        except Exception:
            logger.debug("Failed to persist Slack dm_chat_id for %s", user_id, exc_info=True)
            return
        logger.info("Updated recorded Slack dm_chat_id for user %s to %s", user_id, normalized_channel_id)

    async def _match_bound_dm_channel(self, user_id: Optional[str], channel_id: Optional[str]) -> Optional[bool]:
        if not user_id or not channel_id:
            return None
        record = self._get_bound_user_record(user_id)
        if record is None:
            return None
        dm_chat_id = str(getattr(record, "dm_chat_id", "") or "").strip()
        if dm_chat_id == channel_id:
            return True
        if dm_chat_id and dm_chat_id != channel_id:
            logger.warning(
                "Slack DM channel mismatch for user %s: recorded=%s incoming=%s",
                user_id,
                dm_chat_id,
                channel_id,
            )

        try:
            canonical_dm_channel = await self._open_dm_channel(user_id)
        except Exception as exc:
            logger.warning("Failed to resolve canonical Slack DM channel for user %s: %s", user_id, exc)
            return None
        canonical_dm_channel = str(canonical_dm_channel or "").strip()
        if not canonical_dm_channel:
            logger.warning("Slack returned no canonical DM channel for user %s during DM validation", user_id)
            return None
        self._persist_bound_dm_channel(user_id, record, canonical_dm_channel)
        return canonical_dm_channel == channel_id

    def _is_duplicate_event(self, event_id: Optional[str], team_id: Optional[str] = None) -> bool:
        """Deduplicate Slack events using a persistent short-lived event claim."""
        if not event_id:
            return False
        record_key = f"{team_id}:{event_id}" if team_id else event_id
        recorder = getattr(self.sessions, "try_record_runtime_event", None)
        if callable(recorder):
            try:
                if not recorder(
                    "slack_event",
                    record_key,
                    {"event_id": event_id, "team_id": team_id or ""},
                    ttl_seconds=300,
                ):
                    logger.debug("Ignoring duplicate Slack event_id %s", event_id)
                    return True
                return False
            except Exception:
                logger.debug("Failed to persist Slack event dedup claim for %s", event_id, exc_info=True)
        now = time.time()
        expiry = now - 30  # retain for 30s
        for key in list(self._recent_event_ids.keys()):
            if self._recent_event_ids[key] < expiry:
                del self._recent_event_ids[key]
        if record_key in self._recent_event_ids:
            logger.debug(f"Ignoring duplicate Slack event_id {event_id}")
            return True
        self._recent_event_ids[record_key] = now
        return False

    def _mark_mention_event_processed(self, channel_id: Optional[str], message_id: Optional[str]) -> bool:
        """Track mention handling across Slack message/app_mention event pairs."""
        if not channel_id or not message_id:
            return True
        now = time.time()
        expiry = now - 300
        for key in list(self._processed_mention_event_keys.keys()):
            if self._processed_mention_event_keys[key] < expiry:
                del self._processed_mention_event_keys[key]

        key = f"{channel_id}:{message_id}"
        if key in self._processed_mention_event_keys:
            logger.debug("Ignoring duplicate Slack mention event for %s", key)
            return False
        self._processed_mention_event_keys[key] = now
        return True

    def get_default_parse_mode(self) -> str:
        """Get the default parse mode for Slack"""
        return "markdown"

    def should_use_thread_for_reply(self) -> bool:
        """Slack uses threads for replies"""
        return True

    def should_use_thread_for_dm_session(self) -> bool:
        """Slack DMs also support thread-based replies."""
        return True

    async def prepare_resume_context(
        self,
        context: MessageContext,
        *,
        host_message_ts: Optional[str] = None,
        is_dm: bool = False,
    ) -> MessageContext:
        if context.thread_id or not host_message_ts:
            return context
        return MessageContext(
            user_id=context.user_id,
            channel_id=context.channel_id,
            platform=context.platform,
            thread_id=host_message_ts,
            message_id=context.message_id,
            platform_specific=context.platform_specific,
            files=context.files,
        )

    def _ensure_clients(self):
        """Ensure web and socket clients are initialized"""
        proxy = resolve_proxy(self.config.proxy_url)
        if self.web_client is None:
            self.web_client = AsyncWebClient(token=self.config.bot_token, proxy=proxy)

        if self.socket_client is None and self.config.app_token:
            self.socket_client = SocketModeClient(
                app_token=self.config.app_token,
                web_client=self.web_client,
                proxy=proxy,
            )

    def _extract_bot_user_id_from_payload(self, payload: Dict[str, Any]) -> Optional[str]:
        authorizations = payload.get("authorizations") or []
        for authorization in authorizations:
            if isinstance(authorization, dict):
                user_id = authorization.get("user_id")
                if isinstance(user_id, str) and user_id:
                    return user_id

        authed_users = payload.get("authed_users") or []
        for user_id in authed_users:
            if isinstance(user_id, str) and user_id:
                return user_id

        return None

    async def _get_bot_user_id(self, payload: Optional[Dict[str, Any]] = None) -> Optional[str]:
        payload_user_id = self._extract_bot_user_id_from_payload(payload or {})
        if payload_user_id:
            self._bot_user_id = payload_user_id
            if not self._bot_id:
                await self._hydrate_bot_identity()
            return payload_user_id

        if self._bot_user_id:
            if not self._bot_id:
                await self._hydrate_bot_identity()
            return self._bot_user_id

        await self._hydrate_bot_identity()
        return self._bot_user_id

    async def _hydrate_bot_identity(self) -> None:
        if (self._bot_user_id and self._bot_id) or self._bot_identity_lookup_attempted:
            return

        self._ensure_clients()
        auth_test = getattr(self.web_client, "auth_test", None)
        if not callable(auth_test):
            return

        try:
            response = await auth_test()
        except Exception as exc:
            logger.debug("Failed to resolve Slack bot identity: %s", exc)
            return

        self._bot_identity_lookup_attempted = True
        user_id = self._slack_response_get(response, "user_id")
        if isinstance(user_id, str) and user_id:
            self._bot_user_id = user_id
        bot_id = self._slack_response_get(response, "bot_id")
        if isinstance(bot_id, str) and bot_id:
            self._bot_id = bot_id

    @staticmethod
    def _has_specific_mention(text: str, user_id: Optional[str]) -> bool:
        if not user_id:
            return False
        return bool(re.search(rf"<@{re.escape(user_id)}>", text))

    @staticmethod
    def _strip_specific_mention(text: str, user_id: Optional[str], *, anywhere: bool = False) -> str:
        if not user_id:
            return text
        if anywhere:
            pattern = rf"<@{re.escape(user_id)}>\s*"
        else:
            pattern = rf"^\s*<@{re.escape(user_id)}>\s*"
        return re.sub(pattern, "", text, count=1).strip()

    def _convert_markdown_to_slack_mrkdwn(self, text: str) -> str:
        """Convert standard markdown to Slack mrkdwn format using third-party library

        Uses markdown-to-mrkdwn library for comprehensive conversion including:
        - Bold: ** to *
        - Italic: * to _
        - Strikethrough: ~~ to ~
        - Code blocks: ``` preserved
        - Inline code: ` preserved
        - Links: [text](url) to <url|text>
        - Headers, lists, quotes, and more
        """
        try:
            # Use the third-party converter for comprehensive markdown to mrkdwn conversion
            converted_text = self.markdown_converter.convert(text)
            return converted_text
        except Exception as e:
            logger.warning(f"Error converting markdown to mrkdwn: {e}, using original text")
            # Fallback to original text if conversion fails
            return text

    @staticmethod
    def _channel_looks_like_dm(channel_id: Optional[str]) -> bool:
        return isinstance(channel_id, str) and channel_id.startswith("D")

    def _is_dm_context(self, context: MessageContext) -> bool:
        if bool((context.platform_specific or {}).get("is_dm", False)):
            return True
        return self._channel_looks_like_dm(context.channel_id)

    def _is_own_bot_message(self, event: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
        bot_profile = event.get("bot_profile") if isinstance(event.get("bot_profile"), dict) else {}
        if bot_user_id and event.get("user") == bot_user_id:
            return True
        if bot_user_id and bot_profile.get("user_id") == bot_user_id:
            return True
        if self._bot_id and event.get("bot_id") == self._bot_id:
            return True
        config_app_id = getattr(self.config, "app_id", None)
        if config_app_id and event.get("app_id") == config_app_id:
            return True
        return bool(config_app_id and bot_profile.get("app_id") == config_app_id)

    @staticmethod
    def _slack_response_get(response: Any, key: str) -> Any:
        getter = getattr(response, "get", None)
        if callable(getter):
            return getter(key)
        try:
            return response[key]
        except Exception:
            return None

    async def _get_channel_info_cached(self, channel_id: Optional[str]) -> Dict[str, Any]:
        if not channel_id:
            return {}
        cached = self._channel_info_cache.get(channel_id)
        if cached is not None:
            return cached

        self._ensure_clients()
        conversations_info = getattr(self.web_client, "conversations_info", None)
        if not callable(conversations_info):
            return {}

        try:
            response = await conversations_info(channel=channel_id)
            channel = self._slack_response_get(response, "channel")
            if isinstance(channel, dict):
                self._channel_info_cache[channel_id] = channel
                return channel
        except SlackApiError as err:
            error_code = getattr(err, "response", {}).get("error") if getattr(err, "response", None) else None
            logger.debug("Failed to fetch Slack channel info for %s: %s", channel_id, error_code or err)
        except Exception as err:
            logger.debug("Failed to fetch Slack channel info for %s: %s", channel_id, err)
        return {}

    async def _is_slack_connect_channel(self, channel_id: Optional[str]) -> bool:
        channel = await self._get_channel_info_cached(channel_id)
        return bool(channel.get("is_ext_shared"))

    async def _open_dm_channel(self, user_id: str) -> Optional[str]:
        self._ensure_clients()
        resp = await self.web_client.conversations_open(users=[user_id])
        if not resp.get("ok"):
            logger.warning("Failed to open DM channel with user %s", user_id)
            return None
        return resp.get("channel", {}).get("id")

    async def _chat_post_message(self, **kwargs):
        if getattr(self.config, "disable_link_unfurl", False):
            kwargs["unfurl_links"] = False
            kwargs["unfurl_media"] = False
        return await self.web_client.chat_postMessage(**kwargs)

    async def _post_message_with_dm_recovery(
        self,
        context: MessageContext,
        kwargs: Dict[str, Any],
        *,
        log_label: str,
    ):
        try:
            return await self._chat_post_message(**kwargs)
        except SlackApiError as err:
            error_code = err.response.get("error") if getattr(err, "response", None) else None
            if error_code != "channel_not_found" or not self._is_dm_context(context) or not context.user_id:
                raise

            recovered_channel_id = await self._open_dm_channel(context.user_id)
            if not recovered_channel_id or recovered_channel_id == kwargs.get("channel"):
                raise

            logger.warning(
                "Retrying Slack %s in recovered DM channel %s for user %s",
                log_label,
                recovered_channel_id,
                context.user_id,
            )
            kwargs["channel"] = recovered_channel_id
            kwargs.pop("thread_ts", None)
            context.channel_id = recovered_channel_id
            context.thread_id = None
            return await self._chat_post_message(**kwargs)

    @staticmethod
    def _find_text_split_index(text: str, max_chars: int) -> int:
        minimum_boundary = max_chars // 2
        for separator in ("\n\n", "\n", " "):
            index = text.rfind(separator, 0, max_chars + 1)
            if index >= minimum_boundary:
                candidate = index + len(separator)
                return candidate if candidate <= max_chars else index
        return max_chars

    @classmethod
    def _split_text(cls, text: str, max_chars: int) -> List[str]:
        if len(text) <= max_chars:
            return [text]

        chunks: List[str] = []
        remaining = text
        while len(remaining) > max_chars:
            split_at = cls._find_text_split_index(remaining, max_chars)
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    @classmethod
    def _get_visible_text(cls, text: str, max_chars: int = _SLACK_SECTION_TEXT_LIMIT) -> str:
        return cls._split_text(text, max_chars)[-1]

    @staticmethod
    def _build_section_block(text: str, parse_mode: Optional[str] = None) -> Dict[str, Any]:
        return {
            "type": "section",
            "text": {
                "type": "mrkdwn" if parse_mode == "markdown" else "plain_text",
                "text": text,
            },
        }

    @staticmethod
    def _build_markdown_block(text: str) -> Dict[str, Any]:
        return {
            "type": "markdown",
            "text": text,
        }

    @staticmethod
    def _build_actions_blocks(keyboard: InlineKeyboard) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for row_idx, row in enumerate(keyboard.buttons):
            elements = []
            for button in row:
                elements.append(
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": button.text},
                        "action_id": button.callback_data,
                        "value": button.callback_data,
                    }
                )

            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"actions_{row_idx}",
                    "elements": elements,
                }
            )

        return blocks

    def _build_button_blocks(
        self,
        text: Optional[str],
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        if text:
            blocks.append(self._build_section_block(text, parse_mode=parse_mode))

        blocks.extend(self._build_actions_blocks(keyboard))
        return blocks

    @staticmethod
    def _is_markdown_block_rejection(error: SlackApiError) -> bool:
        response = getattr(error, "response", None)
        error_code = response.get("error") if hasattr(response, "get") else None
        return error_code in {
            "invalid_blocks",
            "unsupported_block_type",
            "invalid_arguments",
        }

    @staticmethod
    def _linkify_bare_urls_for_verbatim_mrkdwn(text: str) -> str:
        """Convert bare URLs to explicit Slack links when automatic parsing is off."""
        if not text or "http" not in text:
            return text

        def _linkify_segment(segment: str) -> str:
            token_ranges = [
                (match.start(), match.end())
                for match in re.finditer(r"<[^<>\s][^<>]*>", segment)
            ]

            def _replace(match: re.Match) -> str:
                url = match.group(0)
                start = match.start()
                end = match.end()
                if any(token_start < start < token_end for token_start, token_end in token_ranges):
                    return url

                trailing = ""
                while url and url[-1] in _TRAILING_URL_PUNCTUATION:
                    trailing = url[-1] + trailing
                    url = url[:-1]

                while url.endswith(")") and url.count("(") < url.count(")"):
                    trailing = ")" + trailing
                    url = url[:-1]

                if end < len(segment) and segment[end : end + 1] == ">":
                    return url + trailing
                if not url:
                    return match.group(0)
                if len(url) + 2 > _SLACK_SECTION_TEXT_LIMIT:
                    return url + trailing
                return f"<{url}>{trailing}"

            return _BARE_HTTP_URL_RE.sub(_replace, segment)

        result: List[str] = []
        in_fenced_code = False
        for line in text.splitlines(keepends=True):
            cursor = 0
            processed_parts: List[str] = []
            for part in re.split(r"(```|`)", line):
                if part == "```":
                    in_fenced_code = not in_fenced_code
                    processed_parts.append(part)
                elif part == "`":
                    if in_fenced_code:
                        processed_parts.append(part)
                    else:
                        cursor ^= 1
                        processed_parts.append(part)
                elif in_fenced_code or cursor:
                    processed_parts.append(part)
                else:
                    processed_parts.append(_linkify_segment(part))
            result.append("".join(processed_parts))
        return "".join(result)

    @classmethod
    def _split_text_for_verbatim_mrkdwn(cls, text: str, max_chars: int) -> List[str]:
        if len(cls._linkify_bare_urls_for_verbatim_mrkdwn(text)) <= max_chars:
            return [text]

        chunks: List[str] = []
        remaining = text
        while remaining and len(cls._linkify_bare_urls_for_verbatim_mrkdwn(remaining)) > max_chars:
            split_at = cls._find_text_split_index(remaining, min(len(remaining) - 1, max_chars // 2))
            if split_at <= 0:
                split_at = max(1, min(len(remaining) - 1, max_chars // 2))
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _send_prepared_text_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ):
        kwargs = {"channel": context.channel_id, "text": text}

        if context.thread_id:
            kwargs["thread_ts"] = context.thread_id
            if context.platform_specific and context.platform_specific.get("reply_broadcast"):
                kwargs["reply_broadcast"] = True
        elif reply_to:
            kwargs["thread_ts"] = reply_to

        if parse_mode == "markdown":
            kwargs["mrkdwn"] = True

        if "\n" in text and len(text) <= _SLACK_SECTION_TEXT_LIMIT:
            kwargs["blocks"] = [self._build_section_block(text, parse_mode=parse_mode)]

        return await self._post_message_with_dm_recovery(
            context,
            kwargs,
            log_label="message send",
        )

    async def send_dm(self, user_id: str, text: str, **kwargs) -> Optional[str]:
        """Send a direct message to a Slack user by opening a DM channel first."""
        self._ensure_clients()
        try:
            dm_channel = await self._open_dm_channel(user_id)
            if not dm_channel:
                return None
            msg_kwargs = {"channel": dm_channel, "text": text}
            msg_kwargs.update(kwargs)
            result = await self._chat_post_message(**msg_kwargs)
            return result.get("ts")
        except Exception as e:
            logger.error("Failed to send DM to Slack user %s: %s", user_id, e)
            return None

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message to Slack"""
        self._ensure_clients()
        try:
            if not text:
                raise ValueError("Slack send_message requires non-empty text")
            # Convert markdown to Slack mrkdwn if needed
            if parse_mode == "markdown":
                text = self._convert_markdown_to_slack_mrkdwn(text)

            response = await self._send_prepared_text_message(
                context,
                text,
                parse_mode=parse_mode,
                reply_to=reply_to,
            )

            # Mark thread as active if we sent a message to a thread
            if self.settings_manager and (context.thread_id or reply_to):
                thread_ts = context.thread_id or reply_to
                if self.sessions:
                    self.sessions.mark_thread_active(context.user_id, context.channel_id, thread_ts)
                logger.debug(f"Marked thread {thread_ts} as active after bot message")

            return response["ts"]

        except SlackApiError as e:
            logger.error(f"Error sending Slack message: {e}")
            raise

    async def send_markdown_message(
        self,
        context: MessageContext,
        text: str,
        keyboard: Optional[InlineKeyboard] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send standard Markdown using Slack's native markdown block.

        This is intended for LLM-authored final results. Control messages and
        editable status updates still use the legacy section/mrkdwn path.
        """
        self._ensure_clients()

        if not text:
            raise ValueError("Slack send_markdown_message requires non-empty text")

        if len(text) > _SLACK_MARKDOWN_TEXT_LIMIT:
            if keyboard:
                return await self.send_message_with_buttons(
                    context,
                    text,
                    keyboard,
                    parse_mode="markdown",
                    reply_to=reply_to,
                )
            return await self.send_message(context, text, parse_mode="markdown", reply_to=reply_to)

        blocks = [self._build_markdown_block(text)]
        if keyboard:
            blocks.extend(self._build_actions_blocks(keyboard))

        kwargs = {
            "channel": context.channel_id,
            "text": self._get_visible_text(text),
            "blocks": blocks,
        }

        if context.thread_id:
            kwargs["thread_ts"] = context.thread_id
            if context.platform_specific and context.platform_specific.get("reply_broadcast"):
                kwargs["reply_broadcast"] = True
        elif reply_to:
            kwargs["thread_ts"] = reply_to

        try:
            response = await self._post_message_with_dm_recovery(
                context,
                kwargs,
                log_label="native-markdown message send",
            )
        except SlackApiError as e:
            if not self._is_markdown_block_rejection(e):
                logger.error(f"Error sending Slack native markdown message: {e}")
                raise
            logger.warning("Slack rejected native markdown block; falling back to legacy mrkdwn rendering")
            if keyboard:
                return await self.send_message_with_buttons(
                    context,
                    text,
                    keyboard,
                    parse_mode="markdown",
                    reply_to=reply_to,
                )
            return await self.send_message(context, text, parse_mode="markdown", reply_to=reply_to)

        if self.settings_manager and (context.thread_id or reply_to):
            thread_ts = context.thread_id or reply_to
            if self.sessions:
                self.sessions.mark_thread_active(context.user_id, context.channel_id, thread_ts)
            logger.debug(f"Marked thread {thread_ts} as active after bot native markdown message")

        return response["ts"]

    async def upload_markdown(
        self,
        context: MessageContext,
        title: str,
        content: str,
        filetype: str = "markdown",
    ) -> str:
        self._ensure_clients()
        data = content or ""
        result = await self.web_client.files_upload_v2(
            channel=context.channel_id,
            thread_ts=context.thread_id,
            filename=title,
            title=title,
            content=data,
        )
        file_id = result.get("file", {}).get("id")
        if not file_id:
            file_id = result.get("files", [{}])[0].get("id")
        return file_id or ""

    async def upload_file_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a local file to the Slack conversation."""
        import os

        self._ensure_clients()
        filename = os.path.basename(file_path)
        display_title = title or filename

        result = await self.web_client.files_upload_v2(
            channel=context.channel_id,
            thread_ts=context.thread_id,
            file=file_path,
            filename=filename,
            title=display_title,
        )
        file_id = result.get("file", {}).get("id")
        if not file_id:
            file_id = result.get("files", [{}])[0].get("id")
        return file_id or ""

    async def download_file(
        self,
        file_info: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> Optional[bytes]:
        """Download a Slack file using the private URL.

        Args:
            file_info: Slack file object containing url_private_download and other metadata
            max_bytes: Maximum file size to download
            timeout_seconds: Request timeout in seconds (default 30s)

        Returns:
            File content as bytes, or None if download fails
        """
        file_info = await self._resolve_downloadable_file_info(file_info)
        url = file_info.get("url_private_download") or file_info.get("url_private") or file_info.get("url")
        if not url:
            logger.warning(f"No download URL for file: {file_info.get('name')}")
            return None

        # Check file size before download if available
        file_size = file_info.get("size")
        if max_bytes is not None and file_size and file_size > max_bytes:
            logger.warning(f"File too large ({file_size} bytes > {max_bytes}), skipping: {file_info.get('name')}")
            return None

        try:
            headers = {"Authorization": f"Bearer {self.config.bot_token}"}
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Failed to download file: HTTP {response.status}")
                        return None

                    # Check content-length header
                    content_length = response.headers.get("Content-Length")
                    if max_bytes is not None and content_length and int(content_length) > max_bytes:
                        logger.warning(f"File too large ({content_length} bytes), skipping: {file_info.get('name')}")
                        return None

                    # Stream download with size limit
                    chunks = []
                    total_size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total_size += len(chunk)
                        if max_bytes is not None and total_size > max_bytes:
                            logger.warning(f"File exceeds max size during download, aborting: {file_info.get('name')}")
                            return None
                        chunks.append(chunk)

                    return b"".join(chunks)

        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading file: {file_info.get('name')}")
            return None
        except Exception as e:
            logger.error(f"Error downloading Slack file: {e}")
            return None

    async def download_file_to_path(
        self,
        file_info: Dict[str, Any],
        target_path: str,
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> FileDownloadResult:
        file_info = await self._resolve_downloadable_file_info(file_info)
        url = file_info.get("url_private_download") or file_info.get("url_private") or file_info.get("url")
        if not url:
            logger.warning(f"No download URL for file: {file_info.get('name')}")
            return FileDownloadResult(False, "No download URL available")

        file_size = file_info.get("size")
        if max_bytes is not None and file_size and file_size > max_bytes:
            logger.warning(f"File too large ({file_size} bytes > {max_bytes}), skipping: {file_info.get('name')}")
            return FileDownloadResult(False, f"File exceeds the allowed size limit ({max_bytes} bytes)")

        try:
            headers = {"Authorization": f"Bearer {self.config.bot_token}"}
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        logger.error(f"Failed to download file: HTTP {response.status}")
                        return FileDownloadResult(False, f"Download failed with HTTP {response.status}")

                    content_length = response.headers.get("Content-Length")
                    if max_bytes is not None and content_length and int(content_length) > max_bytes:
                        logger.warning(f"File too large ({content_length} bytes), skipping: {file_info.get('name')}")
                        return FileDownloadResult(False, f"File exceeds the allowed size limit ({max_bytes} bytes)")

                    total_size = 0
                    with open(target_path, "wb") as file_obj:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total_size += len(chunk)
                            if max_bytes is not None and total_size > max_bytes:
                                logger.warning(
                                    f"File exceeds max size during download, aborting: {file_info.get('name')}"
                                )
                                return FileDownloadResult(
                                    False, f"File exceeds the allowed size limit ({max_bytes} bytes)"
                                )
                            file_obj.write(chunk)
                    return FileDownloadResult(True)

        except asyncio.TimeoutError:
            logger.error(f"Timeout downloading file: {file_info.get('name')}")
            return FileDownloadResult(False, f"Download timed out after {timeout_seconds} seconds")
        except Exception as e:
            logger.error(f"Error downloading Slack file: {e}")
            return FileDownloadResult(False, f"Download error: {e}")

    async def _resolve_downloadable_file_info(self, file_info: Dict[str, Any]) -> Dict[str, Any]:
        """Best-effort hydrate a thin Slack file event before download."""
        if file_info.get("url_private_download") or file_info.get("url_private") or file_info.get("url"):
            return file_info

        file_id = file_info.get("slack_file_id") or file_info.get("id") or file_info.get("file_id")
        if not file_id or not self.web_client:
            return file_info

        try:
            response = await self.web_client.files_info(file=file_id)
            slack_file = self._slack_response_get(response, "file")
            if not isinstance(slack_file, dict) or not slack_file:
                return file_info
            resolved = {**file_info, **slack_file}
            resolved.setdefault("slack_file_id", file_id)
            return resolved
        except SlackApiError as err:
            error_code = getattr(err, "response", {}).get("error") if getattr(err, "response", None) else None
            logger.warning("Failed to resolve Slack file info for %s: %s", file_id, error_code or err)
        except Exception as err:
            logger.warning("Failed to resolve Slack file info for %s: %s", file_id, err)
        return file_info

    def _extract_file_attachments(self, files: List[Dict[str, Any]]) -> List[FileAttachment]:
        """Convert Slack file objects to FileAttachment list.

        Args:
            files: List of Slack file objects from event

        Returns:
            List of FileAttachment objects
        """
        attachments = []
        for f in files:
            file_id = f.get("id")
            name = f.get("name") or f.get("title") or file_id or "slack-file"
            attachment = FileAttachment(
                name=name,
                mimetype=f.get("mimetype", "application/octet-stream"),
                url=f.get("url_private_download") or f.get("url_private"),
                size=f.get("size"),
            )
            if file_id:
                attachment.__dict__["slack_file_id"] = file_id
            attachments.append(attachment)
        return attachments

    async def _extract_shared_message_content(self, event: Dict[str, Any]) -> Optional[str]:
        """Extract content from shared/forwarded messages.

        Handles both:
        - Messages with subtype "message_share"
        - Messages with attachments containing "is_share: true" (no subtype)

        For explicit thread shares, attempts to fetch the full thread via
        conversations.replies. Falls back to the single shared message text
        if thread fetch fails or the share is not a thread.

        Returns:
            Formatted string with shared message content, or None if not a shared message.
        """
        attachments = event.get("attachments", [])
        if not attachments:
            return None

        # Find shared message attachments
        shared_attachments = [a for a in attachments if a.get("is_share") or a.get("is_msg_unfurl")]
        if not shared_attachments:
            # Also check if this is a message_share subtype (may not have is_share flag)
            if event.get("subtype") != "message_share":
                return None
            # For message_share subtype, treat all attachments as shared content
            shared_attachments = attachments

        if not shared_attachments:
            return None

        logger.info(
            f"Detected shared/forwarded message with {len(shared_attachments)} shared attachment(s), "
            f"subtype={event.get('subtype')}"
        )

        # Extract the single shared message text (best-effort fallback)
        shared_messages_text = []
        source_channel_id = None
        source_ts = None

        for att in shared_attachments:
            # Log the attachment structure for debugging (avoid logging full URLs)
            logger.debug(
                f"Shared attachment keys: {list(att.keys())}, channel_id={att.get('channel_id')}, ts={att.get('ts')}"
            )

            # Extract text with fallback chain for robustness
            att_text = (
                att.get("text")
                or att.get("fallback")
                or att.get("pretext")
                or (att.get("original_message") or {}).get("text")
                or (att.get("message") or {}).get("text")
                or ""
            )
            author_name = att.get("author_name") or att.get("author_subname", "")
            channel_name = att.get("channel_name", "")

            if att_text:
                header = "[Shared message"
                if author_name:
                    header += f" from {author_name}"
                if channel_name:
                    header += f" in #{channel_name}"
                header += "]"
                shared_messages_text.append(f"{header}\n{att_text}")

            # Only attempt thread fetch for explicit thread shares (not link unfurls)
            if not source_channel_id and self._is_explicit_thread_share(event, att):
                candidate_channel = att.get("channel_id")
                candidate_ts = att.get("thread_ts") or (att.get("original_message") or {}).get("thread_ts")
                if candidate_channel and candidate_ts:
                    source_channel_id = candidate_channel
                    source_ts = candidate_ts

        # Attempt to fetch the full thread only for explicit thread shares
        thread_content = None
        if source_channel_id and source_ts:
            thread_content = await self._try_fetch_shared_thread(source_channel_id, source_ts)

        if thread_content:
            return thread_content

        # Fallback: return the shared attachment text
        if shared_messages_text:
            return "\n\n".join(shared_messages_text)

        return None

    @staticmethod
    def _is_explicit_thread_share(event: Dict[str, Any], att: Dict[str, Any]) -> bool:
        """Check if the attachment represents an explicit thread share.

        Only returns True when the shared message is a thread root with replies,
        not a single message or a link unfurl. This prevents accidentally pulling
        entire threads when only one message was shared.
        """
        # Link unfurls (is_msg_unfurl) should never trigger thread fetch
        if att.get("is_msg_unfurl"):
            return False
        # Only message_share subtype or is_share flag indicates intentional share
        if event.get("subtype") != "message_share" and not att.get("is_share"):
            return False
        # Check if the original message is a thread root with replies
        msg = att.get("original_message") or att.get("message") or {}
        thread_ts = att.get("thread_ts") or msg.get("thread_ts")
        ts = att.get("ts") or msg.get("ts")
        reply_count = msg.get("reply_count")
        # Thread root: thread_ts == ts and has replies
        if thread_ts and ts and thread_ts == ts and reply_count:
            return True
        return False

    @staticmethod
    def _has_shared_attachments(event: Dict[str, Any]) -> bool:
        """Quick check whether the event contains shared/forwarded message attachments.

        This is a lightweight check (no API calls) used to decide whether to
        pass the event to _extract_shared_message_content later.
        """
        if event.get("subtype") == "message_share":
            return bool(event.get("attachments"))
        for att in event.get("attachments", []):
            if att.get("is_share") or att.get("is_msg_unfurl"):
                return True
        return False

    async def _try_fetch_shared_thread(self, channel_id: str, message_ts: str) -> Optional[str]:
        """Try to fetch the full thread from the source channel.

        Args:
            channel_id: The source channel ID where the original message lives
            message_ts: The timestamp of the shared message (preferably thread_ts)

        Returns:
            Formatted thread content string, or None if fetch fails
        """
        self._ensure_clients()

        try:
            result = await self.web_client.conversations_replies(
                channel=channel_id,
                ts=message_ts,
                limit=50,
            )

            messages = result.get("messages", [])
            if not messages:
                logger.debug(f"No messages found in thread {channel_id}/{message_ts}")
                return None

            # Check if this is actually a thread (more than one message)
            # If it's just a single message, no need for thread context
            if len(messages) <= 1:
                logger.debug(f"Shared message is not a thread (single message), will use attachment text instead")
                return None

            # Check if there are more messages beyond our limit
            has_more = result.get("has_more") or bool((result.get("response_metadata") or {}).get("next_cursor"))

            # Format the full thread
            logger.info(f"Successfully fetched {len(messages)} messages from shared thread {channel_id}/{message_ts}")

            header = f"[Shared thread with {len(messages)} messages"
            if has_more:
                header += ", showing first 50"
            header += "]"

            thread_parts = [header]
            for msg in messages:
                user = msg.get("user") or msg.get("username") or msg.get("bot_id") or "unknown"
                text = msg.get("text", "")
                if text:
                    thread_parts.append(f"<@{user}>: {text}")

            return "\n".join(thread_parts)

        except SlackApiError as e:
            error_code = e.response.get("error", "") if e.response else ""
            if error_code == "ratelimited":
                logger.warning(f"Rate limited fetching shared thread {channel_id}/{message_ts}")
            else:
                logger.info(
                    f"Cannot fetch shared thread from {channel_id}/{message_ts}: "
                    f"{error_code} - Bot may not be in the source channel"
                )
            return None
        except Exception as e:
            logger.warning(f"Unexpected error fetching shared thread from {channel_id}/{message_ts}: {e}")
            return None

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        """Add a reaction emoji to a Slack message."""
        self._ensure_clients()

        name = (emoji or "").strip()
        if name.startswith(":") and name.endswith(":") and len(name) > 2:
            name = name[1:-1]
        if name in ["👀", "eyes", "eye"]:
            name = "eyes"
        elif name in ["🤖", "robot_face", "robot"]:
            name = "robot_face"

        if not name:
            return False

        try:
            await self.web_client.reactions_add(
                channel=context.channel_id,
                timestamp=message_id,
                name=name,
            )
            return True
        except SlackApiError as err:
            try:
                if getattr(err, "response", None) and err.response.get("error") == "already_reacted":
                    return True
            except Exception:
                pass

            error_code = None
            needed = None
            try:
                if getattr(err, "response", None):
                    error_code = err.response.get("error")
                    needed = err.response.get("needed")
            except Exception:
                pass

            # NOTE: reaction failures were previously DEBUG-only; surface at INFO/WARN for operability.
            if error_code in ["missing_scope", "not_in_channel", "channel_not_found"]:
                logger.warning(f"Slack reaction add failed: error={error_code}, needed={needed}")
            else:
                logger.info(f"Slack reaction add failed: {err}")
            return False
        except Exception as err:
            logger.debug(f"Failed to add Slack reaction: {err}")
            return False

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        """Remove a reaction emoji from a Slack message."""
        self._ensure_clients()

        name = (emoji or "").strip()
        if name.startswith(":") and name.endswith(":") and len(name) > 2:
            name = name[1:-1]
        if name in ["👀", "eyes", "eye"]:
            name = "eyes"
        elif name in ["🤖", "robot_face", "robot"]:
            name = "robot_face"

        if not name:
            return False

        try:
            await self.web_client.reactions_remove(
                channel=context.channel_id,
                timestamp=message_id,
                name=name,
            )
            return True
        except SlackApiError as err:
            logger.debug(f"Failed to remove Slack reaction: {err}")
            return False
        except Exception as err:
            logger.debug(f"Failed to remove Slack reaction: {err}")
            return False

    # ------------------------------------------------------------------
    # RTM-based typing indicator (best-effort)
    # ------------------------------------------------------------------

    async def _ensure_rtm_connection(self) -> Optional[aiohttp.ClientWebSocketResponse]:
        """Lazily establish an RTM WebSocket solely for typing indicator events.

        Returns the WebSocket on success or ``None`` when RTM is unavailable
        (e.g. modern Slack apps that lack the ``rtm:stream`` scope).  A
        negative result is cached so subsequent calls return immediately.
        """
        if self._rtm_available is False:
            return None

        # Reuse an existing live connection
        if self._rtm_ws is not None and not self._rtm_ws.closed:
            return self._rtm_ws

        # Close stale resources before reconnecting
        await self._close_rtm()

        self._ensure_clients()
        try:
            resp = await self.web_client.rtm_connect()
            if not resp.get("ok"):
                err_msg = resp.get("error", "unknown")
                logger.info("Slack RTM unavailable (%s) — typing indicator disabled", err_msg)
                self._rtm_available = False
                return None

            wss_url = resp["url"]
            self._rtm_session = aiohttp.ClientSession()
            self._rtm_ws = await self._rtm_session.ws_connect(
                wss_url,
                heartbeat=30,
                autoclose=True,
            )
            self._rtm_available = True
            self._rtm_drain_task = asyncio.create_task(self._rtm_drain_loop())
            logger.info("Slack RTM WebSocket connected for typing indicator")
            return self._rtm_ws
        except Exception as exc:
            logger.info("Slack RTM connect failed: %s — typing indicator disabled", exc)
            self._rtm_available = False
            await self._close_rtm()
            return None

    async def _rtm_drain_loop(self) -> None:
        """Read and discard incoming RTM messages to prevent buffer buildup."""
        try:
            ws = self._rtm_ws
            if ws is None:
                return
            async for _ in ws:
                pass  # discard all incoming RTM events
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # connection lost — handled on next send attempt

    async def _close_rtm(self) -> None:
        """Tear down RTM WebSocket and its aiohttp session."""
        if self._rtm_drain_task is not None:
            self._rtm_drain_task.cancel()
            try:
                await self._rtm_drain_task
            except (asyncio.CancelledError, Exception):
                pass
            self._rtm_drain_task = None

        if self._rtm_ws is not None:
            try:
                await self._rtm_ws.close()
            except Exception:
                pass
            self._rtm_ws = None

        if self._rtm_session is not None:
            try:
                await self._rtm_session.close()
            except Exception:
                pass
            self._rtm_session = None

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        """Send a typing indicator via RTM WebSocket (best-effort)."""
        ws = await self._ensure_rtm_connection()
        if ws is None:
            return False
        try:
            self._rtm_msg_id += 1
            await ws.send_json(
                {
                    "id": self._rtm_msg_id,
                    "type": "typing",
                    "channel": context.channel_id,
                }
            )
            return True
        except Exception as exc:
            logger.debug("Slack RTM typing send failed: %s", exc)
            # Connection may be broken; reset so next call reconnects
            self._rtm_ws = None
            return False

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        """Slack typing indicators auto-expire (~3 s); no explicit clear needed."""
        return True

    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Send a message with interactive buttons"""
        self._ensure_clients()
        try:
            # Default to markdown for Slack if not specified
            if not parse_mode:
                parse_mode = "markdown"

            # Convert markdown to Slack mrkdwn if needed
            if parse_mode == "markdown":
                text = self._convert_markdown_to_slack_mrkdwn(text)

            linkify_verbatim_urls = parse_mode == "markdown" and getattr(self.config, "disable_link_unfurl", False)
            chunks = (
                self._split_text_for_verbatim_mrkdwn(text, _SLACK_SECTION_TEXT_LIMIT)
                if linkify_verbatim_urls
                else self._split_text(text, _SLACK_SECTION_TEXT_LIMIT)
            )
            for chunk in chunks[:-1]:
                await self._send_prepared_text_message(context, chunk, parse_mode=parse_mode, reply_to=reply_to)
            text = chunks[-1]
            visible_text = self._linkify_bare_urls_for_verbatim_mrkdwn(text) if linkify_verbatim_urls else text

            blocks = self._build_button_blocks(visible_text, keyboard, parse_mode=parse_mode)
            if blocks and blocks[0].get("type") == "section":
                blocks[0]["text"]["verbatim"] = True

            # Prepare message kwargs
            kwargs = {
                "channel": context.channel_id,
                "blocks": blocks,
                "text": visible_text,  # Fallback text
            }

            # Handle thread replies
            if context.thread_id:
                kwargs["thread_ts"] = context.thread_id
            elif reply_to:
                kwargs["thread_ts"] = reply_to

            response = await self._post_message_with_dm_recovery(
                context,
                kwargs,
                log_label="message-with-buttons send",
            )

            # Mark thread as active if we sent a message to a thread
            if self.settings_manager and (context.thread_id or reply_to):
                thread_ts = context.thread_id or reply_to
                if self.sessions:
                    self.sessions.mark_thread_active(context.user_id, context.channel_id, thread_ts)
                logger.debug(f"Marked thread {thread_ts} as active after bot message with buttons")

            return response["ts"]

        except SlackApiError as e:
            logger.error(f"Error sending Slack message with buttons: {e}")
            raise

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Edit an existing Slack message"""
        self._ensure_clients()
        try:
            if text and parse_mode == "markdown":
                text = self._convert_markdown_to_slack_mrkdwn(text)

            kwargs = {"channel": context.channel_id, "ts": message_id}

            if text is not None:
                kwargs["text"] = self._get_visible_text(text) if keyboard else text

            if keyboard:
                visible_text = kwargs.get("text") if isinstance(kwargs.get("text"), str) else None
                kwargs["blocks"] = self._build_button_blocks(visible_text, keyboard, parse_mode=parse_mode)

            await self.web_client.chat_update(**kwargs)
            return True

        except SlackApiError as e:
            logger.error(f"Error editing Slack message: {e}")
            return False

    async def remove_inline_keyboard(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Remove interactive buttons from a Slack message."""
        self._ensure_clients()
        try:
            if not message_id:
                return False

            payload = (context.platform_specific or {}).get("payload") if context.platform_specific else None
            payload_message = payload.get("message") if isinstance(payload, dict) else None

            blocks = []
            if isinstance(payload_message, dict):
                payload_blocks = payload_message.get("blocks")
                if isinstance(payload_blocks, list):
                    for block in payload_blocks:
                        if isinstance(block, dict) and block.get("type") != "actions":
                            blocks.append(block)

            fallback_text = text
            if fallback_text is not None and parse_mode == "markdown":
                fallback_text = self._convert_markdown_to_slack_mrkdwn(fallback_text)
            if fallback_text:
                fallback_text = self._get_visible_text(fallback_text)

            if fallback_text is None and isinstance(payload_message, dict):
                payload_text = payload_message.get("text")
                if isinstance(payload_text, str):
                    fallback_text = self._get_visible_text(payload_text)

            if not blocks and fallback_text:
                blocks = [self._build_section_block(fallback_text, parse_mode=parse_mode)]

            kwargs = {"channel": context.channel_id, "ts": message_id, "blocks": blocks}
            if fallback_text is not None:
                kwargs["text"] = fallback_text
            await self.web_client.chat_update(**kwargs)
            return True
        except SlackApiError as e:
            logger.error(f"Error removing Slack buttons: {e}")
            return False

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        """Answer a Slack interactive callback"""
        # Slack does not have a direct equivalent to answer_callback_query
        # Instead, we typically update the message or send an ephemeral message
        # This will be handled in the event processing
        return True

    def register_handlers(self):
        """Register Slack event handlers"""
        if not self.socket_client:
            logger.warning("Socket mode client not configured, skipping handler registration")
            return

        # Register socket mode request handler
        self.socket_client.socket_mode_request_listeners.append(self._handle_socket_mode_request)

    async def _handle_socket_mode_request(self, client: SocketModeClient, req: SocketModeRequest):
        """Handle incoming Socket Mode requests"""
        try:
            if req.type == "events_api":
                # Acknowledge Events API immediately; file downloads, ASR, and
                # agent turns can exceed Slack's Socket Mode ack deadline.
                response = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(response)
                self._create_event_task(req.payload)
            elif req.type == "slash_commands":
                # Handle slash commands
                await self._handle_slash_command(req.payload)
                # Acknowledge after handling slash commands
                response = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(response)
            elif req.type == "interactive":
                # For interactive components, acknowledge FIRST to avoid Slack timeout
                # This is important for long-running operations like updates
                response = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(response)
                # Then handle the interaction
                await self._handle_interactive(req.payload)
            else:
                # Unknown request type, still acknowledge
                response = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(response)

        except Exception as e:
            logger.error(f"Error handling socket mode request: {e}")
            # Still acknowledge even on error (if not already acknowledged)
            try:
                response = SocketModeResponse(envelope_id=req.envelope_id)
                await client.send_socket_mode_response(response)
            except Exception:
                pass  # Already acknowledged or connection issue

    def _create_event_task(self, payload: Dict[str, Any]) -> None:
        task = asyncio.create_task(self._handle_event(payload))
        self._event_tasks.add(task)
        task.add_done_callback(self._handle_event_task_done)

    def _handle_event_task_done(self, task: asyncio.Task) -> None:
        self._event_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Error handling Slack event asynchronously", exc_info=True)

    async def _drain_event_tasks(self) -> None:
        if not self._event_tasks:
            return
        tasks = list(self._event_tasks)
        timeout = max(0.0, float(self._event_task_shutdown_drain_timeout))
        logger.info("Waiting for %s in-flight Slack event task(s) before shutdown", len(tasks))
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.error("Error handling Slack event during shutdown drain", exc_info=True)
        if not pending:
            return
        logger.warning(
            "Canceling %s Slack event task(s) that did not finish within %.1fs shutdown drain",
            len(pending),
            timeout,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._event_tasks.difference_update(pending)

    async def _handle_event(self, payload: Dict[str, Any]):
        """Handle Events API events"""
        event = payload.get("event", {})
        event_type = event.get("type")
        event_id = payload.get("event_id")
        if self._is_duplicate_event(event_id, payload.get("team_id")):
            return

        if event_type == "message":
            # Hot-reload config BEFORE reading any config values (require_mention, etc.)
            if self._controller and hasattr(self._controller, "_refresh_config_from_disk"):
                self._controller._refresh_config_from_disk()

            # Check for file attachments first
            slack_files = event.get("files", [])
            has_files = bool(slack_files)

            # Ignore most message subtypes (edited, deleted, joins, etc.)
            # But allow file-related subtypes and shared/forwarded messages
            event_subtype = event.get("subtype")
            allowed_subtypes = {"file_share", "file_comment", "file_mention", "message_share"}
            if event_subtype:
                if not has_files and event_subtype not in allowed_subtypes:
                    logger.debug(f"Ignoring Slack message with subtype: {event_subtype}")
                    return

            channel_id = event.get("channel")
            bot_profile = event.get("bot_profile") if isinstance(event.get("bot_profile"), dict) else {}
            user_id = event.get("user") or bot_profile.get("user_id") or event.get("bot_id")
            is_dm = self._channel_looks_like_dm(channel_id)
            if is_dm:
                dm_match = await self._match_bound_dm_channel(user_id, channel_id)
                if dm_match is False:
                    logger.warning(
                        "Ignoring Slack DM-like message from mismatched channel %s for user %s",
                        channel_id,
                        user_id,
                    )
                    return

            # Check if this message contains a bot mention. Use a normalized
            # copy for routing/commands while preserving the raw Slack text for
            # the agent.
            raw_text = (event.get("text") or "").strip()
            route_text = raw_text
            agent_text = raw_text

            had_mention_only = False
            bot_user_id = await self._get_bot_user_id(payload)
            bot_mention = f"<@{bot_user_id}>" if bot_user_id else None
            has_bot_mention = self._has_specific_mention(raw_text, bot_user_id)
            cleaned_text = self._strip_specific_mention(raw_text, bot_user_id)
            handled_bot_mention_in_message_event = False
            if event.get("bot_id"):
                if self._is_own_bot_message(event, bot_user_id):
                    return
                if not has_bot_mention:
                    return
            if has_bot_mention:
                if is_dm:
                    route_text = cleaned_text
                    had_mention_only = not route_text
                    if had_mention_only:
                        agent_text = ""
                elif await self._is_slack_connect_channel(channel_id):
                    route_text = self._strip_specific_mention(raw_text, bot_user_id, anywhere=True)
                    had_mention_only = not route_text
                    if had_mention_only:
                        agent_text = ""
                    handled_bot_mention_in_message_event = True
                    logger.info("Processing Slack Connect message event with bot mention: '%s'", event.get("text"))
                else:
                    logger.info(f"Skipping message event with bot mention: '{raw_text}'")
                    return

            # Extract file attachments (slack_files already checked above)
            file_attachments = self._extract_file_attachments(slack_files) if slack_files else None

            # Detect if this is a shared/forwarded message (check attachments early)
            # We need to know this before the empty-text check, but defer the
            # potentially expensive thread fetch until after authorization
            has_shared_content = self._has_shared_attachments(event)

            # Ignore messages without user or without actual text/files/shared content
            if not user_id:
                logger.debug("Ignoring Slack message without user id")
                return
            if not route_text and not file_attachments and not has_shared_content and not had_mention_only:
                logger.debug("Ignoring Slack message with empty text and no files")
                return

            if handled_bot_mention_in_message_event and not self._mark_mention_event_processed(
                channel_id, event.get("ts")
            ):
                return

            # Check if we require mention in channels (not DMs).
            # In threads, human activity does not bypass the mention requirement;
            # scheduled follow-up threads are the only no-mention exception.
            is_thread_reply = event.get("thread_ts") is not None

            # Resolve effective require_mention: per-channel override or global default
            effective_require_mention = self.config.require_mention
            if self.settings_manager:
                effective_require_mention = self.settings_manager.get_require_mention(
                    channel_id, global_default=self.config.require_mention
                )

            if effective_require_mention and not is_dm:
                # In channel main thread: require mention (silently ignore)
                if handled_bot_mention_in_message_event:
                    logger.debug("Processing message event because Slack Connect bot mention was already detected")
                elif not is_thread_reply:
                    logger.debug(f"Ignoring non-mention message in channel: '{route_text}'")
                    return

                # In thread: require a fresh bot mention unless this is a scheduled follow-up thread.
                elif is_thread_reply:
                    thread_ts = event.get("thread_ts")
                    if self.settings_manager:
                        scheduled_thread_active = self.is_scheduled_thread_active(channel_id, thread_ts)
                        if not scheduled_thread_active:
                            logger.debug(f"Ignoring message in inactive thread {thread_ts}: '{route_text}'")
                            return
                    else:
                        # Without settings_manager, fall back to ignoring non-mention in threads
                        logger.debug(f"No settings_manager, ignoring thread message: '{route_text}'")
                        return

            auth = self.check_authorization(
                user_id=user_id,
                channel_id=channel_id,
                is_dm=is_dm,
                text=route_text,
                settings_manager=self.settings_manager,
            )
            if not auth.allowed:
                await self._send_auth_denial(channel_id, user_id, auth)
                return

            # Now extract shared/forwarded message content (after authorization)
            # This may make API calls to fetch thread context from source channel
            shared_text = None
            if has_shared_content:
                shared_text = await self._extract_shared_message_content(event)
                # If shared content extraction found nothing and we have no text/files,
                # there's nothing to process
                if not shared_text and not route_text and not file_attachments:
                    logger.debug("Ignoring shared message with no extractable content")
                    return

            # Extract context
            # For Slack: if no thread_ts, use the message's own ts as thread_id (start of thread)
            thread_id = event.get("thread_ts") or event.get("ts")

            context = MessageContext(
                user_id=user_id,
                channel_id=channel_id,
                thread_id=thread_id,  # Always have a thread_id
                message_id=event.get("ts"),
                platform_specific={
                    "team_id": payload.get("team_id"),
                    "event": event,
                    "is_dm": is_dm,
                    "bot_user_id": bot_user_id,
                    "bot_mention": bot_mention,
                    "control_text": route_text,
                },
                files=file_attachments,
            )

            if handled_bot_mention_in_message_event and self.settings_manager and thread_id:
                if self.sessions:
                    self.sessions.mark_thread_active(user_id, channel_id, thread_id)
                logger.info(f"Marked thread {thread_id} as active due to Slack Connect @mention")

            # Handle slash commands in regular messages (before appending shared content)
            # Use mention-normalized text for internal command detection;
            # keep agent_text as the original Slack message.
            if await self.dispatch_text_command(
                context,
                route_text,
                allow_plain_bind=self.should_allow_plain_bind(
                    user_id=user_id,
                    is_dm=is_dm,
                    settings_manager=self.settings_manager,
                ),
            ):
                return

            # Append shared content to user text (after command parsing)
            if shared_text:
                if agent_text:
                    agent_text = f"{agent_text}\n\n{shared_text}"
                else:
                    agent_text = shared_text

            # Handle as regular message
            if self.on_message_callback:
                await self.on_message_callback(context, agent_text)

        elif event_type == "app_mention":
            # Handle @mentions
            channel_id = event.get("channel")
            user_id_mention = event.get("user")
            if not self._mark_mention_event_processed(channel_id, event.get("ts")):
                return

            raw_text = (event.get("text") or "").strip()
            bot_user_id = await self._get_bot_user_id(payload)
            bot_mention = f"<@{bot_user_id}>" if bot_user_id else None
            # Remove the mention from the text first so we can parse commands for auth
            route_text = self._strip_specific_mention(
                raw_text,
                bot_user_id,
                anywhere=True,
            ).strip()

            # Parse command action for proper admin-protected auth check
            parsed_command = self.parse_text_command(route_text)
            auth_action = parsed_command[0] if parsed_command else ""

            # Centralized auth gate (app_mention is always in a channel, not DM)
            auth = self.check_authorization(
                user_id=user_id_mention or "",
                channel_id=channel_id,
                is_dm=False,
                action=auth_action,
                settings_manager=self.settings_manager,
            )
            if not auth.allowed:
                await self._send_auth_denial(channel_id, user_id_mention or "", auth)
                return

            # For Slack: if no thread_ts, use the message's own ts as thread_id (start of thread)
            thread_id = event.get("thread_ts") or event.get("ts")

            # Extract file attachments if present
            slack_files = event.get("files", [])
            file_attachments = self._extract_file_attachments(slack_files) if slack_files else None

            # Extract shared/forwarded message content (defer appending until after command check)
            shared_text = await self._extract_shared_message_content(event)

            had_mention_only = not route_text and not file_attachments and not shared_text

            context = MessageContext(
                user_id=event.get("user"),
                channel_id=channel_id,
                thread_id=thread_id,  # Always have a thread_id
                message_id=event.get("ts"),
                platform_specific={
                    "team_id": payload.get("team_id"),
                    "event": event,
                    "is_dm": channel_id.startswith("D"),
                    "bot_user_id": bot_user_id,
                    "bot_mention": bot_mention,
                    "control_text": route_text,
                },
                files=file_attachments,
            )

            # Mark thread as active only when the mention carries actionable content.
            if self.settings_manager and thread_id:
                if self.sessions:
                    self.sessions.mark_thread_active(event.get("user"), channel_id, thread_id)
                logger.info(f"Marked thread {thread_id} as active due to @mention")

            logger.info(f"App mention processed: original='{event.get('text')}', cleaned='{route_text}'")

            # Check if this is a command after mention (command already parsed above for auth)
            if parsed_command:
                command, args = parsed_command
                logger.info(f"Command detected: '{command}', available: {list(self.on_command_callbacks.keys())}")

                handler = self.on_command_callbacks.get(command)
                if handler:
                    logger.info(f"Executing command handler for: {command}")
                    await handler(context, args)
                    return
                logger.warning(f"Command '{command}' not found in callbacks")

            # Append shared content to user text (after command parsing)
            agent_text = "" if had_mention_only else raw_text
            if shared_text:
                if agent_text:
                    agent_text = f"{agent_text}\n\n{shared_text}"
                else:
                    agent_text = shared_text

            # Handle as regular message
            logger.info(f"Handling as regular message: '{agent_text}'")
            if self.on_message_callback:
                await self.on_message_callback(context, agent_text)

    async def _handle_slash_command(self, payload: Dict[str, Any]):
        """Handle native Slack slash commands"""
        command = payload.get("command", "").lstrip("/")
        channel_id = payload.get("channel_id")
        user_id = payload.get("user_id", "")
        response_url = payload.get("response_url")

        # Centralized auth gate
        is_dm = isinstance(channel_id, str) and channel_id.startswith("D")
        auth = self.check_authorization(
            user_id=user_id,
            channel_id=channel_id,
            is_dm=is_dm,
            action=command,
            settings_manager=self.settings_manager,
        )
        if not auth.allowed:
            await self._send_auth_denial(channel_id, user_id, auth, response_url=response_url)
            return

        # Map Slack slash commands to internal commands
        # Only /start and /stop commands are exposed to users
        command_mapping = {"start": "start", "stop": "stop"}

        # Get the actual command name
        actual_command = command_mapping.get(command, command)

        # Create context for slash command
        context = MessageContext(
            user_id=payload.get("user_id"),
            channel_id=payload.get("channel_id"),
            platform_specific={
                "trigger_id": payload.get("trigger_id"),
                "response_url": payload.get("response_url"),
                "command": command,
                "text": payload.get("text"),
                "payload": payload,
                "is_dm": is_dm,
            },
        )

        # Send immediate acknowledgment to Slack

        # Try to handle as registered command
        if actual_command in self.on_command_callbacks:
            handler = self.on_command_callbacks[actual_command]

            # Send immediate "processing" response for long-running commands
            if response_url and actual_command not in [
                "start",
                "status",
                "clear",
                "cwd",
                "queue",
            ]:
                await self.send_slash_response(
                    response_url, f"⏳ {self._t('common.processing', channel_id, command=command)}"
                )

            await handler(context, payload.get("text", ""))
        elif actual_command in self.slash_command_handlers:
            handler = self.slash_command_handlers[actual_command]
            await handler(context, payload.get("text", ""))
        else:
            # Send response back to Slack for unknown command
            if response_url:
                await self.send_slash_response(
                    response_url,
                    f"❌ {self._t('error.unknownCommand', channel_id, command=command)}",
                )

    async def _handle_interactive(self, payload: Dict[str, Any]):
        """Handle interactive components (buttons, modal submissions, etc.)"""
        if payload.get("type") == "block_actions":
            # Handle button clicks / select changes
            user = payload.get("user", {})
            actions = payload.get("actions", [])
            view = payload.get("view", {})

            # In Slack modals, `channel` is often missing. We store the originating
            # channel_id in `view.private_metadata` when opening the modal.
            channel_id = payload.get("channel", {}).get("id") or payload.get("container", {}).get("channel_id")

            # For modal actions, try to extract channel_id from private_metadata JSON
            if not channel_id and isinstance(view, dict):
                private_metadata = view.get("private_metadata")
                if private_metadata:
                    try:
                        import json

                        metadata = json.loads(private_metadata)
                        channel_id = metadata.get("channel_id") if isinstance(metadata, dict) else private_metadata
                    except (json.JSONDecodeError, TypeError):
                        # Fallback: treat private_metadata as channel_id directly (legacy behavior)
                        channel_id = private_metadata

            # Determine the primary action for auth check
            primary_action = actions[0].get("action_id", "") if actions else ""

            # Centralized auth gate
            is_dm = isinstance(channel_id, str) and channel_id.startswith("D")
            auth = self.check_authorization(
                user_id=user.get("id", ""),
                channel_id=channel_id or "",
                is_dm=is_dm,
                action=primary_action,
                settings_manager=self.settings_manager,
            )
            if not auth.allowed:
                try:
                    await self._send_auth_denial(channel_id or "", user.get("id", ""), auth)
                except Exception as e:
                    logger.debug("Failed to send auth denial to channel %s: %s", channel_id, e)
                return

            # Handle update button click
            for action in actions:
                if action.get("action_id") == "vibe_update_now":
                    from core.update_checker import handle_update_button_click

                    if hasattr(self, "_controller") and self._controller:
                        await handle_update_button_click(self._controller, payload)
                    return

            for action in actions:
                action_type = action.get("type")
                if action_type == "button":
                    callback_data = action.get("action_id")

                    if self.on_callback_query_callback:
                        thread_id = (
                            payload.get("container", {}).get("thread_ts")
                            or payload.get("message", {}).get("thread_ts")
                            or payload.get("message", {}).get("ts")
                        )
                        # Create a context for the callback
                        context = MessageContext(
                            user_id=user.get("id"),
                            channel_id=channel_id,
                            thread_id=thread_id,
                            message_id=payload.get("message", {}).get("ts"),
                            platform_specific={
                                "trigger_id": payload.get("trigger_id"),
                                "response_url": payload.get("response_url"),
                                "action": action,
                                "payload": payload,
                                "is_dm": is_dm,
                            },
                        )

                        await self.on_callback_query_callback(context, callback_data)
                elif action_type in {"static_select", "external_select"}:
                    action_id = action.get("action_id")
                    # Trigger modal update for backend selection and all backend-specific selectors
                    routing_modal_actions = {
                        "backend_select",
                        "opencode_agent_select",
                        "opencode_model_select",
                        "claude_agent_select",
                        "claude_model_select",
                        "codex_model_select",
                    }
                    # Also check for prefixed action IDs (reasoning selects have unique suffixes)
                    should_update = (
                        action_id in routing_modal_actions
                        or (action_id and action_id.startswith("opencode_reasoning_select"))
                        or (action_id and action_id.startswith("codex_reasoning_select"))
                    )
                    if should_update:
                        if hasattr(self, "_on_routing_modal_update"):
                            channel_from_view = view.get("private_metadata")
                            effective_channel = channel_from_view or channel_id
                            selection = parse_routing_modal_selection(
                                view=view,
                                action=action,
                                fallback_selected_backend="",
                            )
                            await self._on_routing_modal_update(
                                user.get("id"),
                                effective_channel,
                                view.get("id"),
                                view.get("hash"),
                                selection,
                                is_dm=isinstance(effective_channel, str) and effective_channel.startswith("D"),
                            )
                elif action_type == "plain_text_input":
                    action_id = action.get("action_id")
                    # Handle manual_input in resume_session_modal - show agent_block when user types
                    if action_id == "manual_input" and view.get("callback_id") == "resume_session_modal":
                        await self._handle_resume_modal_manual_input(view, action)

        elif payload.get("type") == "view_submission":
            # Handle modal submissions asynchronously to avoid Slack timeouts
            asyncio.create_task(self._handle_view_submission(payload))
            return

    async def _handle_view_submission(self, payload: Dict[str, Any]):
        """Handle modal dialog submissions"""
        view = payload.get("view", {})
        callback_id = view.get("callback_id")

        if callback_id == "settings_modal":
            # Handle settings modal submission
            user_id = payload.get("user", {}).get("id")
            values = view.get("state", {}).get("values", {})

            # Extract selected show message types
            show_types_data = values.get("show_message_types", {}).get("show_types_select", {})
            selected_options = show_types_data.get("selected_options", [])

            # Get the values from selected options
            show_types = [opt.get("value") for opt in selected_options]

            # Extract require_mention setting
            require_mention_data = values.get("require_mention_block", {}).get("require_mention_select", {})
            require_mention_value = require_mention_data.get("selected_option", {}).get("value")
            # Convert to Optional[bool]: "__default__" -> None, "true" -> True, "false" -> False
            if require_mention_value == "__default__":
                require_mention = None
            elif require_mention_value == "true":
                require_mention = True
            elif require_mention_value == "false":
                require_mention = False
            else:
                require_mention = None

            # Extract language setting
            language_data = values.get("language_block", {}).get("language_select", {})
            language_value = language_data.get("selected_option", {}).get("value")
            supported_languages = set(get_supported_languages())
            # Convert to Optional[str]: use explicit language if supported
            language = language_value if language_value in supported_languages else None

            # Get channel_id from the view's private_metadata if available
            channel_id = view.get("private_metadata")

            # Update settings - need access to settings manager
            if hasattr(self, "_on_settings_update"):
                await self._on_settings_update(
                    user_id,
                    show_types,
                    channel_id,
                    require_mention,
                    language,
                    is_dm=isinstance(channel_id, str) and channel_id.startswith("D"),
                )

        elif callback_id == "change_cwd_modal":
            # Handle change CWD modal submission
            user_id = payload.get("user", {}).get("id")
            values = view.get("state", {}).get("values", {})

            # Extract new CWD path
            new_cwd_data = values.get("new_cwd_block", {}).get("new_cwd_input", {})
            new_cwd = new_cwd_data.get("value", "")

            # Get channel_id from private_metadata
            channel_id = view.get("private_metadata")

            # Update CWD - need access to controller or settings manager
            if hasattr(self, "_on_change_cwd"):
                is_dm = isinstance(channel_id, str) and channel_id.startswith("D")
                await self._on_change_cwd(user_id, new_cwd, channel_id, is_dm)

            # Send success message to the user (via DM or channel)
            # We need to find the right channel to send the message
            # For now, we'll rely on the controller to handle this

        elif callback_id == "resume_session_modal":
            user_id = payload.get("user", {}).get("id")
            values = view.get("state", {}).get("values", {})

            agent_data = values.get("agent_block", {}).get("agent_select", {})
            agent = agent_data.get("selected_option", {}).get("value")

            manual_data = values.get("manual_block", {}).get("manual_input", {})
            manual_session = (manual_data.get("value") or "").strip()

            session_data = values.get("session_block", {}).get("session_select", {}) if values else {}
            selected_option = session_data.get("selected_option") or {}
            selected_value = selected_option.get("value") if isinstance(selected_option, dict) else None
            selected_session = None
            selected_agent = None
            if selected_value and "|" in selected_value:
                selected_agent, selected_session = selected_value.split("|", 1)

            metadata_raw = view.get("private_metadata")
            channel_id = None
            thread_id = None
            host_message_ts = None
            default_agent = None
            try:
                import json

                md = json.loads(metadata_raw) if metadata_raw else {}
                channel_id = md.get("channel_id")
                thread_id = md.get("thread_id")
                host_message_ts = md.get("host_message_ts")
                default_agent = md.get("default_agent")
            except Exception:
                pass

            # Manual input takes precedence and should respect the manual agent selector.
            if manual_session:
                chosen_session = manual_session
                # When manually entering session ID, use selected agent or default
                chosen_agent = agent or selected_agent or default_agent
            else:
                chosen_session = selected_session
                chosen_agent = selected_agent or agent

            if hasattr(self, "_on_resume_session"):
                is_dm = isinstance(channel_id, str) and channel_id.startswith("D")
                callback = self._on_resume_session
                try:
                    await callback(user_id, channel_id, thread_id, chosen_agent, chosen_session, host_message_ts, is_dm)
                except TypeError:
                    # Backward compatibility: older callback signature omitted is_dm.
                    await callback(user_id, channel_id, thread_id, chosen_agent, chosen_session, host_message_ts)

        elif callback_id == "claude_question_modal":
            # Generic question modal handling for Claude
            user_id = payload.get("user", {}).get("id")
            values = view.get("state", {}).get("values", {})
            metadata_raw = view.get("private_metadata")

            try:
                import json

                metadata = json.loads(metadata_raw) if metadata_raw else {}
            except Exception:
                metadata = {}

            channel_id = metadata.get("channel_id")
            thread_id = metadata.get("thread_id")
            # Get callback_prefix from metadata, fallback to deriving from callback_id
            callback_prefix = metadata.get("callback_prefix")
            if not callback_prefix:
                callback_prefix = callback_id.replace("_modal", "")

            answers = []
            q_count = int(metadata.get("question_count") or 1)
            for idx in range(q_count):
                block_id = f"q{idx}"
                action_id = "select"
                data = values.get(block_id, {}).get(action_id, {})
                selected_options = data.get("selected_options")
                if isinstance(selected_options, list):
                    answers.append([opt.get("value") for opt in selected_options if opt.get("value")])
                else:
                    selected = data.get("selected_option")
                    if selected and selected.get("value") is not None:
                        answers.append([str(selected.get("value"))])
                    else:
                        answers.append([])

            if self.on_callback_query_callback:
                context = MessageContext(
                    user_id=user_id,
                    channel_id=str(channel_id) if channel_id else "",
                    thread_id=str(thread_id) if thread_id else None,
                    platform_specific={
                        "payload": payload,
                        "is_dm": isinstance(channel_id, str) and channel_id.startswith("D"),
                    },
                )
                # Use callback_prefix to route to correct agent
                await self.on_callback_query_callback(
                    context,
                    f"{callback_prefix}:modal:" + json.dumps({"answers": answers}),
                )

        elif callback_id == "routing_modal":
            # Handle routing modal submission
            user_id = payload.get("user", {}).get("id")
            values = view.get("state", {}).get("values", {})
            channel_id = view.get("private_metadata")

            # Extract backend
            backend_data = values.get("backend_block", {}).get("backend_select", {})
            backend = backend_data.get("selected_option", {}).get("value")

            # Extract OpenCode agent (optional)
            oc_agent_data = values.get("opencode_agent_block", {}).get("opencode_agent_select", {})
            oc_agent = oc_agent_data.get("selected_option", {}).get("value")
            if oc_agent == "__default__":
                oc_agent = None

            # Extract OpenCode model (optional)
            oc_model_data = values.get("opencode_model_block", {}).get("opencode_model_select", {})
            oc_model = oc_model_data.get("selected_option", {}).get("value")
            if oc_model == "__default__":
                oc_model = None

            # Extract OpenCode reasoning effort (optional)
            oc_reasoning = None
            reasoning_block = values.get("opencode_reasoning_block", {})
            if isinstance(reasoning_block, dict):
                for action_id, action_data in reasoning_block.items():
                    if (
                        isinstance(action_id, str)
                        and action_id.startswith("opencode_reasoning_select")
                        and isinstance(action_data, dict)
                    ):
                        oc_reasoning = action_data.get("selected_option", {}).get("value")
                        break
            if oc_reasoning == "__default__":
                oc_reasoning = None

            # Extract require_mention (optional)
            require_mention_data = values.get("require_mention_block", {}).get("require_mention_select", {})
            require_mention_value = require_mention_data.get("selected_option", {}).get("value")
            # Convert to Optional[bool]: "__default__" -> None, "true" -> True, "false" -> False
            if require_mention_value == "__default__":
                require_mention = None
            elif require_mention_value == "true":
                require_mention = True
            elif require_mention_value == "false":
                require_mention = False
            else:
                require_mention = None

            # Extract Claude agent (optional)
            claude_agent_data = values.get("claude_agent_block", {}).get("claude_agent_select", {})
            claude_agent = claude_agent_data.get("selected_option", {}).get("value")
            if claude_agent == "__default__":
                claude_agent = None

            # Extract Claude model (optional)
            claude_model_data = values.get("claude_model_block", {}).get("claude_model_select", {})
            claude_model = claude_model_data.get("selected_option", {}).get("value")
            if claude_model == "__default__":
                claude_model = None

            # Extract Claude reasoning effort (optional)
            claude_reasoning_data = values.get("claude_reasoning_block", {}).get("claude_reasoning_select", {})
            claude_reasoning = claude_reasoning_data.get("selected_option", {}).get("value")
            if claude_reasoning == "__default__":
                claude_reasoning = None

            # Extract Codex model (optional)
            codex_agent_data = values.get("codex_agent_block", {}).get("codex_agent_select", {})
            codex_agent = codex_agent_data.get("selected_option", {}).get("value")
            if codex_agent == "__default__":
                codex_agent = None

            # Extract Codex model (optional)
            codex_model_data = values.get("codex_model_block", {}).get("codex_model_select", {})
            codex_model = codex_model_data.get("selected_option", {}).get("value")
            if codex_model == "__default__":
                codex_model = None

            # Extract Codex reasoning effort (optional)
            codex_reasoning = None
            codex_reasoning_block = values.get("codex_reasoning_block", {})
            if isinstance(codex_reasoning_block, dict):
                for action_id, action_data in codex_reasoning_block.items():
                    if (
                        isinstance(action_id, str)
                        and action_id.startswith("codex_reasoning_select")
                        and isinstance(action_data, dict)
                    ):
                        codex_reasoning = action_data.get("selected_option", {}).get("value")
                        break
            if codex_reasoning == "__default__":
                codex_reasoning = None

            # Update routing via callback
            if hasattr(self, "_on_routing_update"):
                await self._on_routing_update(
                    user_id,
                    channel_id,
                    backend,
                    oc_agent,
                    oc_model,
                    oc_reasoning,
                    claude_agent,
                    claude_model,
                    claude_reasoning,
                    codex_agent,
                    codex_model,
                    codex_reasoning,
                    is_dm=isinstance(channel_id, str) and channel_id.startswith("D"),
                )

    def run(self):
        """Run the Slack bot"""
        if self.config.app_token:
            # Socket Mode
            logger.info("Starting Slack bot in Socket Mode...")

            async def start():
                self._ensure_clients()
                self.register_handlers()
                self._loop = asyncio.get_running_loop()
                self._stop_event = asyncio.Event()
                await self.socket_client.connect()
                # Call on_ready callback after successful connection
                if self._on_ready:
                    try:
                        await self._on_ready()
                    except Exception as e:
                        logger.error(f"on_ready callback failed: {e}", exc_info=True)
                await self._stop_event.wait()
                await self._async_close()

            asyncio.run(start())
        else:
            # Web API only mode (for development/testing)
            logger.warning("No app token provided, running in Web API only mode")

            async def start():
                self._ensure_clients()
                self._loop = asyncio.get_running_loop()
                self._stop_event = asyncio.Event()
                # Call on_ready callback (even in Web API only mode)
                if self._on_ready:
                    try:
                        await self._on_ready()
                    except Exception as e:
                        logger.error(f"on_ready callback failed: {e}", exc_info=True)
                await self._stop_event.wait()
                await self._async_close()

            try:
                asyncio.run(start())
            except KeyboardInterrupt:
                logger.info("Shutting down...")

    def stop(self) -> None:
        if self._stop_event is None:
            return
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        else:
            self._stop_event.set()

    async def shutdown(self) -> None:
        """Best-effort async shutdown for Slack clients."""
        if self._stop_event is not None:
            self._stop_event.set()

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._loop and self._loop.is_running() and self._loop is not current_loop:
            try:
                future = asyncio.run_coroutine_threadsafe(self._async_close(), self._loop)
                future.result(timeout=5)
            except Exception as exc:
                logger.debug(f"Slack shutdown dispatch failed: {exc}")
            return

        await self._async_close()

    async def _async_close(self) -> None:
        if self.socket_client is not None:
            try:
                disconnect = getattr(self.socket_client, "disconnect", None)
                if callable(disconnect):
                    result = disconnect()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                logger.debug(f"Socket mode disconnect failed: {exc}")
            try:
                close = getattr(self.socket_client, "close", None)
                if callable(close):
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                logger.debug(f"Socket mode close failed: {exc}")

        await self._drain_event_tasks()
        await self._close_rtm()

        if self.web_client is not None:
            try:
                await self.web_client.close()
            except Exception as exc:
                logger.debug(f"Slack web client close failed: {exc}")

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Get information about a Slack user (cached permanently)"""
        cached = self._user_info_cache.get(user_id)
        if cached is not None:
            return cached
        self._ensure_clients()
        try:
            response = await self.web_client.users_info(user=user_id)
            user = response["user"]
            profile = user.get("profile", {})
            info = {
                "id": user["id"],
                "name": user.get("name"),
                "real_name": profile.get("real_name_normalized") or user.get("real_name"),
                "real_name_normalized": profile.get("real_name_normalized"),
                "display_name": profile.get("display_name_normalized") or profile.get("display_name"),
                "display_name_normalized": profile.get("display_name_normalized"),
                "email": profile.get("email"),
                "is_bot": user.get("is_bot", False),
            }
        except SlackApiError as e:
            logger.error(f"Error getting user info: {e}")
            info = {"id": user_id}
        self._user_info_cache[user_id] = info
        return info

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        """Get information about a Slack channel"""
        self._ensure_clients()
        try:
            response = await self.web_client.conversations_info(channel=channel_id)
            channel = response["channel"]
            return {
                "id": channel["id"],
                "name": channel.get("name"),
                "is_private": channel.get("is_private", False),
                "is_im": channel.get("is_im", False),
                "is_channel": channel.get("is_channel", False),
                "topic": channel.get("topic", {}).get("value"),
                "purpose": channel.get("purpose", {}).get("value"),
            }
        except SlackApiError as e:
            logger.error(f"Error getting channel info: {e}")
            raise

    def format_markdown(self, text: str) -> str:
        """Format markdown text for Slack mrkdwn format

        Slack uses single asterisks for bold and different formatting rules
        """
        # Convert double asterisks to single for bold
        formatted = text.replace("**", "*")

        # Convert inline code blocks (backticks work the same)
        # Lists work similarly
        # Links work similarly [text](url) -> <url|text>
        # But we'll keep simple for now - just handle bold

        return formatted

    async def open_settings_modal(
        self,
        trigger_id: str,
        user_settings: Any,
        message_types: list,
        display_names: dict,
        channel_id: str = None,
        current_require_mention: object = None,  # None=default, True, False
        global_require_mention: bool = False,
        current_language: str = None,  # Current language setting
    ):
        """Open a modal dialog for settings"""
        self._ensure_clients()

        # Get translations for the channel's language
        t = lambda key, **kwargs: self._t(key, channel_id, **kwargs)

        # Create options for the multi-select menu
        options = []
        selected_options = []

        for msg_type in message_types:
            display_name = display_names.get(msg_type, msg_type)
            option = {
                "text": {"type": "plain_text", "text": display_name, "emoji": True},
                "value": msg_type,
                "description": {
                    "type": "plain_text",
                    "text": self._get_message_type_description(msg_type, channel_id),
                    "emoji": True,
                },
            }
            options.append(option)

            # If this type is shown, add THE SAME option object to selected options
            if msg_type in user_settings.show_message_types:
                selected_options.append(option)  # Same object reference!

        logger.debug("Creating modal with %d options, %d selected", len(options), len(selected_options))
        logger.debug("Show types: %s", user_settings.show_message_types)

        if logger.isEnabledFor(logging.DEBUG):
            import json

            logger.debug("Options: %s", json.dumps(options, indent=2))
            logger.debug("Selected options: %s", json.dumps(selected_options, indent=2))

        # Create the multi-select element
        multi_select_element = {
            "type": "multi_static_select",
            "placeholder": {
                "type": "plain_text",
                "text": t("modal.settings.showMessageTypesPlaceholder"),
                "emoji": True,
            },
            "options": options,
            "action_id": "show_types_select",
        }

        # Only add initial_options if there are selected options
        if selected_options:
            multi_select_element["initial_options"] = selected_options

        # Build require_mention selector
        global_mention_label = (
            t("modal.settings.mentionStatusOn") if global_require_mention else t("modal.settings.mentionStatusOff")
        )
        require_mention_options = [
            {
                "text": {"type": "plain_text", "text": t("modal.settings.optionDefault", status=global_mention_label)},
                "value": "__default__",
            },
            {
                "text": {"type": "plain_text", "text": t("modal.settings.optionRequireMention")},
                "value": "true",
            },
            {
                "text": {"type": "plain_text", "text": t("modal.settings.optionDontRequireMention")},
                "value": "false",
            },
        ]

        # Determine initial option for require_mention
        initial_require_mention = require_mention_options[0]  # Default
        if current_require_mention is not None:
            target_value = "true" if current_require_mention else "false"
            for opt in require_mention_options:
                if opt["value"] == target_value:
                    initial_require_mention = opt
                    break

        require_mention_select = {
            "type": "static_select",
            "action_id": "require_mention_select",
            "placeholder": {"type": "plain_text", "text": t("modal.settings.selectMentionBehavior")},
            "options": require_mention_options,
            "initial_option": initial_require_mention,
        }

        # Build language selector
        language_options = []
        for code in get_supported_languages():
            label = t(f"language.{code}")
            if label == f"language.{code}":
                label = code
            language_options.append({"text": {"type": "plain_text", "text": label}, "value": code})

        # Determine initial option for language
        initial_language = language_options[0]
        if current_language:
            for opt in language_options:
                if opt["value"] == current_language:
                    initial_language = opt
                    break

        language_select = {
            "type": "static_select",
            "action_id": "language_select",
            "placeholder": {"type": "plain_text", "text": t("modal.settings.language")},
            "options": language_options,
            "initial_option": initial_language,
        }

        # Create the modal view
        view = {
            "type": "modal",
            "callback_id": "settings_modal",
            "private_metadata": channel_id or "",  # Store channel_id for later use
            "title": {"type": "plain_text", "text": t("modal.settings.title"), "emoji": True},
            "submit": {"type": "plain_text", "text": t("common.save"), "emoji": True},
            "close": {"type": "plain_text", "text": t("common.cancel"), "emoji": True},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "language_block",
                    "element": language_select,
                    "label": {
                        "type": "plain_text",
                        "text": t("modal.settings.language"),
                        "emoji": True,
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_{t('modal.settings.languageHint')}_",
                        }
                    ],
                },
                {"type": "divider"},
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": t("modal.settings.channelBehavior"),
                        "emoji": True,
                    },
                },
                {
                    "type": "input",
                    "block_id": "require_mention_block",
                    "element": require_mention_select,
                    "label": {
                        "type": "plain_text",
                        "text": t("modal.settings.requireMention"),
                        "emoji": True,
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_{t('modal.settings.requireMentionHint')}_",
                        }
                    ],
                },
                {"type": "divider"},
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": t("modal.settings.messageVisibility"),
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": t("modal.settings.messageVisibilityDesc"),
                    },
                },
                {
                    "type": "input",
                    "block_id": "show_message_types",
                    "element": multi_select_element,
                    "label": {
                        "type": "plain_text",
                        "text": t("modal.settings.showMessageTypes"),
                        "emoji": True,
                    },
                    "optional": True,
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_💡 {t('modal.settings.tip')}_",
                        }
                    ],
                },
            ],
        }

        try:
            await self.web_client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as e:
            logger.error(f"Error opening modal: {e}")
            raise

    def _get_message_type_description(self, msg_type: str, channel_id: str = None) -> str:
        """Get description for a message type"""
        key = f"messageType.{msg_type}Desc"
        return self._t(key, channel_id)

    async def open_change_cwd_modal(self, trigger_id: str, current_cwd: str, channel_id: str = None):
        """Open a modal dialog for changing working directory"""
        self._ensure_clients()

        # Get translations for the channel's language
        t = lambda key, **kwargs: self._t(key, channel_id, **kwargs)

        # Create the modal view
        view = {
            "type": "modal",
            "callback_id": "change_cwd_modal",
            "private_metadata": channel_id or "",  # Store channel_id for later use
            "title": {
                "type": "plain_text",
                "text": t("modal.cwd.title"),
                "emoji": True,
            },
            "submit": {"type": "plain_text", "text": t("common.change"), "emoji": True},
            "close": {"type": "plain_text", "text": t("common.cancel"), "emoji": True},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{t('modal.cwd.current')}\n`{current_cwd}`",
                    },
                },
                {"type": "divider"},
                {
                    "type": "input",
                    "block_id": "new_cwd_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "new_cwd_input",
                        "placeholder": {
                            "type": "plain_text",
                            "text": t("modal.cwd.placeholder"),
                            "emoji": True,
                        },
                        "initial_value": current_cwd,
                    },
                    "label": {
                        "type": "plain_text",
                        "text": t("modal.cwd.new"),
                        "emoji": True,
                    },
                    "hint": {
                        "type": "plain_text",
                        "text": t("modal.cwd.hint"),
                        "emoji": True,
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"💡 _{t('modal.cwd.tip')}_",
                        }
                    ],
                },
            ],
        }

        try:
            await self.web_client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as e:
            logger.error(f"Error opening change CWD modal: {e}")
            raise

    async def open_resume_session_modal(
        self,
        trigger_id: str,
        sessions: List[NativeResumeSession],
        channel_id: Optional[str],
        thread_id: Optional[str],
        host_message_ts: Optional[str],
    ):
        """Open a modal to let users select or input a session to resume."""
        self._ensure_clients()

        # Build agent options limited to enabled backends when available
        common_agents = ["claude", "codex", "opencode"]
        registered_backends = None
        if getattr(self, "_controller", None) and getattr(self._controller, "agent_service", None):
            registered_backends = list(self._controller.agent_service.agents.keys())
        allowed_agents = set(registered_backends) if registered_backends else set(common_agents)
        agent_keys = allowed_agents
        agent_options = []
        for agent in sorted(agent_keys):
            agent_options.append(
                {
                    "text": {"type": "plain_text", "text": agent.capitalize(), "emoji": True},
                    "value": agent,
                }
            )

        session_options = []
        total_session_options = 0
        max_session_options = 100
        for item in sessions:
            if total_session_options >= max_session_options:
                break
            if item.agent not in allowed_agents:
                continue
            label = format_display_summary(item)
            desc = format_display_time(item)
            session_options.append(
                {
                    "text": {"type": "plain_text", "text": label[:75], "emoji": True},
                    "value": f"{item.agent}|{item.native_session_id}",
                    "description": {"type": "plain_text", "text": desc[:75], "emoji": True},
                }
            )
            total_session_options += 1

        blocks: list = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": self._t("modal.resume.description"),
                },
            },
        ]

        if session_options:
            blocks.append(
                {
                    "type": "input",
                    "block_id": "session_block",
                    "optional": True,
                    "label": {"type": "plain_text", "text": self._t("modal.resume.pickExisting"), "emoji": True},
                    "element": {
                        "type": "static_select",
                        "action_id": "session_select",
                        "options": session_options,
                        "placeholder": {
                            "type": "plain_text",
                            "text": self._t("modal.resume.selectSession"),
                            "emoji": True,
                        },
                    },
                }
            )
            if total_session_options >= max_session_options:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"_{self._t('modal.resume.showingFirst100')}_",
                            }
                        ],
                    }
                )
        else:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"_{self._t('modal.resume.noSessionsFound')}_",
                        }
                    ],
                }
            )

        blocks.append(
            {
                "type": "input",
                "block_id": "manual_block",
                "optional": True,
                "label": {"type": "plain_text", "text": self._t("modal.resume.pasteId"), "emoji": True},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "manual_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": self._t("modal.resume.pasteIdPlaceholder"),
                        "emoji": True,
                    },
                    "dispatch_action_config": {
                        "trigger_actions_on": ["on_character_entered"],
                    },
                },
                "dispatch_action": True,
            }
        )

        # Agent backend selector is NOT included initially.
        # It will be dynamically added when user types in the manual session ID field.

        metadata = {
            "channel_id": channel_id,
            "thread_id": thread_id,
            "host_message_ts": host_message_ts,
            "default_agent": agent_options[0]["value"] if agent_options else "opencode",
            "agent_options": agent_options,  # Store for dynamic update
        }

        view = {
            "type": "modal",
            "callback_id": "resume_session_modal",
            "private_metadata": json.dumps(metadata),
            "title": {"type": "plain_text", "text": self._t("modal.resume.title"), "emoji": True},
            "submit": {"type": "plain_text", "text": self._t("common.resume"), "emoji": True},
            "close": {"type": "plain_text", "text": self._t("common.cancel"), "emoji": True},
            "blocks": blocks,
        }

        try:
            await self.web_client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as e:
            logger.error(f"Error opening resume modal: {e}")
            raise

    async def _handle_resume_modal_manual_input(self, view: Dict[str, Any], action: Dict[str, Any]):
        """Handle manual_input changes in resume_session_modal - dynamically show/hide agent_block."""
        self._ensure_clients()

        view_id = view.get("id")
        blocks = view.get("blocks", [])
        metadata_raw = view.get("private_metadata", "{}")

        try:
            metadata = json.loads(metadata_raw)
        except Exception:
            metadata = {}

        agent_options = metadata.get("agent_options", [])
        if not agent_options:
            return

        # Check if agent_block already exists
        has_agent_block = any(b.get("block_id") == "agent_block" for b in blocks)

        # Get current input value
        input_value = (action.get("value") or "").strip()

        # Determine if we need to update
        should_show_agent = bool(input_value)

        if should_show_agent and has_agent_block:
            # Already showing, no update needed
            return
        if not should_show_agent and not has_agent_block:
            # Already hidden, no update needed
            return

        # Build updated blocks
        new_blocks = [b for b in blocks if b.get("block_id") != "agent_block"]

        if should_show_agent:
            # Add agent_block at the end
            new_blocks.append(
                {
                    "type": "input",
                    "block_id": "agent_block",
                    "label": {"type": "plain_text", "text": self._t("modal.resume.agentBackend"), "emoji": True},
                    "element": {
                        "type": "static_select",
                        "action_id": "agent_select",
                        "options": agent_options,
                        "placeholder": {
                            "type": "plain_text",
                            "text": self._t("modal.resume.selectAgentBackend"),
                            "emoji": True,
                        },
                    },
                }
            )

        updated_view = {
            "type": "modal",
            "callback_id": "resume_session_modal",
            "private_metadata": metadata_raw,
            "title": {"type": "plain_text", "text": self._t("modal.resume.title"), "emoji": True},
            "submit": {"type": "plain_text", "text": self._t("common.resume"), "emoji": True},
            "close": {"type": "plain_text", "text": self._t("common.cancel"), "emoji": True},
            "blocks": new_blocks,
        }

        try:
            await self.web_client.views_update(view_id=view_id, view=updated_view)
        except SlackApiError as e:
            logger.debug(f"Failed to update resume modal: {e}")

    def _get_default_opencode_agent_name(self, opencode_agents: list) -> Optional[str]:
        """Resolve the default OpenCode agent name."""
        for agent in opencode_agents:
            name = agent.get("name")
            if name == "build":
                return name
        for agent in opencode_agents:
            name = agent.get("name")
            if name:
                return name
        return None

    def _resolve_opencode_default_model(
        self,
        opencode_default_config: dict,
        opencode_agents: list,
        selected_agent: Optional[str],
    ) -> Optional[str]:
        """Resolve the default model for a selected OpenCode agent."""
        agent_name = selected_agent or self._get_default_opencode_agent_name(opencode_agents)
        if isinstance(opencode_default_config, dict):
            agents_config = opencode_default_config.get("agent", {})
            if isinstance(agents_config, dict) and agent_name:
                agent_config = agents_config.get(agent_name, {})
                if isinstance(agent_config, dict):
                    model = agent_config.get("model")
                    if isinstance(model, str) and model:
                        return model
            model = opencode_default_config.get("model")
            if isinstance(model, str) and model:
                return model
        return None

    def _build_routing_modal_view(
        self,
        channel_id: str,
        registered_backends: list,
        current_backend: str,
        current_routing,
        opencode_agents: list,
        opencode_models: dict,
        opencode_default_config: dict,
        claude_agents: list = None,
        claude_models: list = None,
        codex_agents: list = None,
        codex_models: list = None,
        selected_backend: object = _UNSET,
        selected_opencode_agent: object = _UNSET,
        selected_opencode_model: object = _UNSET,
        selected_opencode_reasoning: object = _UNSET,
        selected_claude_agent: object = _UNSET,
        selected_claude_model: object = _UNSET,
        selected_claude_reasoning: object = _UNSET,
        selected_codex_agent: object = _UNSET,
        selected_codex_model: object = _UNSET,
        selected_codex_reasoning: object = _UNSET,
    ) -> dict:
        """Build modal view for agent/model routing settings."""
        # Build backend options
        backend_display_names = {
            "claude": "ClaudeCode",
            "codex": "Codex",
            "opencode": "OpenCode",
        }
        backend_options = []
        for backend in registered_backends:
            display_name = backend_display_names.get(backend, backend.capitalize())
            backend_options.append(
                {
                    "text": {"type": "plain_text", "text": display_name},
                    "value": backend,
                }
            )

        # Find initial backend option
        selected_backend_value = current_backend if selected_backend is _UNSET else selected_backend
        initial_backend = None
        for option in backend_options:
            if option["value"] == selected_backend_value:
                initial_backend = option
                break
        if initial_backend is None and backend_options:
            initial_backend = backend_options[0]

        backend_select = {
            "type": "static_select",
            "action_id": "backend_select",
            "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectBackend")},
            "options": backend_options,
            "initial_option": initial_backend,
        }

        # Build modal blocks
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{self._t('modal.routing.currentBackend')}* {backend_display_names.get(current_backend, current_backend)}",
                },
            },
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "backend_block",
                "dispatch_action": True,
                "element": backend_select,
                "label": {"type": "plain_text", "text": self._t("modal.routing.backend")},
            },
        ]

        # Determine effective backend for showing backend-specific options
        effective_backend = selected_backend_value or current_backend or "opencode"
        canonical_model = getattr(current_routing, "model", None) if current_routing else None
        canonical_reasoning = getattr(current_routing, "reasoning_effort", None) if current_routing else None

        def _canonical_applies_to_backend(backend: str) -> bool:
            return backend == (current_backend or "opencode")

        def _current_model_for_backend(field_name: str, backend: str) -> Optional[str]:
            value = getattr(current_routing, field_name, None) if current_routing else None
            if value is not None:
                return value
            if effective_backend == backend and _canonical_applies_to_backend(backend):
                return canonical_model
            return None

        def _current_reasoning_for_backend(field_name: str, backend: str) -> Optional[str]:
            value = getattr(current_routing, field_name, None) if current_routing else None
            if value is not None:
                return value
            if effective_backend == backend and _canonical_applies_to_backend(backend):
                return canonical_reasoning
            return None

        # OpenCode-specific options (only if opencode is selected)
        if effective_backend == "opencode" and "opencode" in registered_backends:
            # Get current opencode settings
            if selected_opencode_agent is _UNSET:
                current_oc_agent = current_routing.opencode_agent if current_routing else None
            else:
                current_oc_agent = selected_opencode_agent

            if selected_opencode_model is _UNSET:
                current_oc_model = _current_model_for_backend("opencode_model", "opencode")
            else:
                current_oc_model = selected_opencode_model

            if selected_opencode_reasoning is _UNSET:
                current_oc_reasoning = _current_reasoning_for_backend("opencode_reasoning_effort", "opencode")
            else:
                current_oc_reasoning = selected_opencode_reasoning

            # Determine default agent/model from OpenCode config
            default_model_str = self._resolve_opencode_default_model(
                opencode_default_config, opencode_agents, current_oc_agent
            )

            # Build agent options
            agent_options = [
                {"text": {"type": "plain_text", "text": self._t("common.default")}, "value": "__default__"}
            ]
            for agent in opencode_agents:
                agent_name = agent.get("name", "")
                if agent_name:
                    agent_options.append(
                        {
                            "text": {"type": "plain_text", "text": agent_name},
                            "value": agent_name,
                        }
                    )

            # Find initial agent
            initial_agent = agent_options[0]  # Default
            if current_oc_agent:
                for opt in agent_options:
                    if opt["value"] == current_oc_agent:
                        initial_agent = opt
                        break

            agent_select = {
                "type": "static_select",
                "action_id": "opencode_agent_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectOpencodeAgent")},
                "options": agent_options,
                "initial_option": initial_agent,
            }

            # Build model options
            default_label = self._t("common.default")
            if default_model_str:
                default_label = f"{self._t('common.default')} - {default_model_str}"
            model_options = [{"text": {"type": "plain_text", "text": default_label}, "value": "__default__"}]

            # Add models from providers (sorted, filtered, truncated)
            preferred_providers = resolve_opencode_provider_preferences(
                opencode_default_config,
                current_oc_model or default_model_str,
            )
            allowed_providers = resolve_opencode_allowed_providers(
                opencode_default_config,
                opencode_models,
            )
            model_entries = build_opencode_model_option_items(
                opencode_models,
                max_total=99,
                preferred_providers=preferred_providers,
                allowed_providers=allowed_providers,
            )
            for entry in model_entries:
                label = entry.get("label", "")
                value = entry.get("value", "")
                if not label or not value:
                    continue
                model_options.append(
                    {
                        "text": {"type": "plain_text", "text": label[:75]},  # Slack limit
                        "value": value,
                    }
                )

            # Find initial model
            initial_model = model_options[0]  # Default
            if current_oc_model:
                for opt in model_options:
                    if opt["value"] == current_oc_model:
                        initial_model = opt
                        break

            model_select = {
                "type": "static_select",
                "action_id": "opencode_model_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectModel")},
                "options": model_options,
                "initial_option": initial_model,
            }

            # Build reasoning effort options dynamically based on model variants
            target_model = current_oc_model or default_model_str

            reasoning_model_key = target_model or "__default__"
            reasoning_action_id = (
                "opencode_reasoning_select__" + hashlib.sha1(reasoning_model_key.encode("utf-8")).hexdigest()[:8]
            )

            reasoning_entries = build_reasoning_effort_options(opencode_models, target_model)
            reasoning_effort_options = []
            for entry in reasoning_entries:
                value = entry.get("value")
                if not value:
                    continue
                if value == "__default__":
                    label = self._t("common.default")
                else:
                    translated = self._t(f"reasoning.{value}")
                    label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                reasoning_effort_options.append(
                    {
                        "text": {"type": "plain_text", "text": label},
                        "value": value,
                    }
                )

            # Find initial reasoning effort
            initial_reasoning = reasoning_effort_options[0]  # Default
            if current_oc_reasoning:
                for opt in reasoning_effort_options:
                    if opt["value"] == current_oc_reasoning:
                        initial_reasoning = opt
                        break

            reasoning_select = {
                "type": "static_select",
                "action_id": reasoning_action_id,
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectReasoningEffort")},
                "options": reasoning_effort_options,
                "initial_option": initial_reasoning,
            }

            # Add OpenCode section
            blocks.extend(
                [
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{self._t('modal.routing.opencodeSettings')}*",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "opencode_agent_block",
                        "optional": True,
                        "dispatch_action": True,
                        "element": agent_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.opencodeAgent")},
                    },
                    {
                        "type": "input",
                        "block_id": "opencode_model_block",
                        "optional": True,
                        "dispatch_action": True,
                        "element": model_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.model")},
                    },
                    {
                        "type": "input",
                        "block_id": "opencode_reasoning_block",
                        "optional": True,
                        "element": reasoning_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.reasoningEffort")},
                    },
                ]
            )

        # Claude-specific options (only if claude is selected)
        if effective_backend == "claude" and "claude" in registered_backends:
            claude_agents = claude_agents or []
            claude_models = claude_models or []

            # Get current claude settings
            if selected_claude_agent is _UNSET:
                current_cl_agent = current_routing.claude_agent if current_routing else None
            else:
                current_cl_agent = selected_claude_agent

            if selected_claude_model is _UNSET:
                current_cl_model = _current_model_for_backend("claude_model", "claude")
            else:
                current_cl_model = selected_claude_model

            if selected_claude_reasoning is _UNSET:
                current_cl_reasoning = _current_reasoning_for_backend("claude_reasoning_effort", "claude")
            else:
                current_cl_reasoning = selected_claude_reasoning

            # Build agent options
            cl_agent_options = [
                {"text": {"type": "plain_text", "text": self._t("common.default")}, "value": "__default__"}
            ]
            for agent in claude_agents:
                agent_id = agent.get("id", "")
                agent_name = agent.get("name", agent_id)
                if agent_id:
                    cl_agent_options.append(
                        {
                            "text": {"type": "plain_text", "text": agent_name[:75]},
                            "value": agent_id,
                        }
                    )

            # Find initial agent
            initial_cl_agent = cl_agent_options[0]
            if current_cl_agent:
                for opt in cl_agent_options:
                    if opt["value"] == current_cl_agent:
                        initial_cl_agent = opt
                        break

            cl_agent_select = {
                "type": "static_select",
                "action_id": "claude_agent_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectClaudeAgent")},
                "options": cl_agent_options,
                "initial_option": initial_cl_agent,
            }

            # Build model options
            cl_model_options = [
                {"text": {"type": "plain_text", "text": self._t("common.default")}, "value": "__default__"}
            ]
            for model in claude_models:
                if model:
                    model_label = format_claude_model_label(model)
                    cl_model_options.append(
                        {
                            "text": {"type": "plain_text", "text": model_label[:75]},
                            "value": model,
                        }
                    )

            # Add current model if not in list (preserve custom models)
            if current_cl_model and not any(opt["value"] == current_cl_model for opt in cl_model_options):
                model_label = format_claude_model_label(current_cl_model)
                cl_model_options.append(
                    {
                        "text": {"type": "plain_text", "text": model_label[:75]},
                        "value": current_cl_model,
                    }
                )

            # Limit to 100 options
            if len(cl_model_options) > 100:
                cl_model_options = cl_model_options[:100]

            # Find initial model
            initial_cl_model = cl_model_options[0]
            if current_cl_model:
                for opt in cl_model_options:
                    if opt["value"] == current_cl_model:
                        initial_cl_model = opt
                        break

            cl_model_select = {
                "type": "static_select",
                "action_id": "claude_model_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectModel")},
                "options": cl_model_options,
                "initial_option": initial_cl_model,
            }

            cl_reasoning_entries = build_claude_reasoning_options(current_cl_model)
            selected_cl_reasoning = (
                current_cl_reasoning if current_cl_reasoning not in (None, "__default__") else "__default__"
            )
            available_cl_reasoning = {entry.get("value") for entry in cl_reasoning_entries}
            if selected_cl_reasoning not in available_cl_reasoning:
                selected_cl_reasoning = "__default__"

            cl_reasoning_options = []
            for entry in cl_reasoning_entries:
                value = entry.get("value")
                if not value:
                    continue
                if value == "__default__":
                    label = self._t("common.default")
                else:
                    translated = self._t(f"reasoning.{value}")
                    label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                cl_reasoning_options.append(
                    {
                        "text": {"type": "plain_text", "text": label},
                        "value": value,
                    }
                )

            initial_cl_reasoning = next(
                (opt for opt in cl_reasoning_options if opt["value"] == selected_cl_reasoning),
                cl_reasoning_options[0],
            )

            cl_reasoning_select = {
                "type": "static_select",
                "action_id": "claude_reasoning_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectReasoningEffort")},
                "options": cl_reasoning_options,
                "initial_option": initial_cl_reasoning,
            }

            # Add Claude section
            blocks.extend(
                [
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{self._t('modal.routing.claudeSettings')}*",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "claude_agent_block",
                        "optional": True,
                        "element": cl_agent_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.claudeAgent")},
                    },
                    {
                        "type": "input",
                        "block_id": "claude_model_block",
                        "optional": True,
                        "dispatch_action": True,
                        "element": cl_model_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.model")},
                    },
                    {
                        "type": "input",
                        "block_id": "claude_reasoning_block",
                        "optional": True,
                        "element": cl_reasoning_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.reasoningEffort")},
                    },
                ]
            )

        # Codex-specific options (only if codex is selected)
        if effective_backend == "codex" and "codex" in registered_backends:
            codex_agents = codex_agents or []
            codex_models = codex_models or []

            # Get current codex settings
            if selected_codex_agent is _UNSET:
                current_cx_agent = getattr(current_routing, "codex_agent", None) if current_routing else None
            else:
                current_cx_agent = selected_codex_agent

            if selected_codex_model is _UNSET:
                current_cx_model = _current_model_for_backend("codex_model", "codex")
            else:
                current_cx_model = selected_codex_model

            if selected_codex_reasoning is _UNSET:
                current_cx_reasoning = _current_reasoning_for_backend("codex_reasoning_effort", "codex")
            else:
                current_cx_reasoning = selected_codex_reasoning

            cx_agent_options = [
                {"text": {"type": "plain_text", "text": self._t("common.default")}, "value": "__default__"}
            ]
            for agent in codex_agents:
                agent_id = agent.get("id", "")
                agent_name = agent.get("name", agent_id)
                if agent_id:
                    cx_agent_options.append(
                        {
                            "text": {"type": "plain_text", "text": agent_name[:75]},
                            "value": agent_id,
                        }
                    )

            initial_cx_agent = cx_agent_options[0]
            if current_cx_agent:
                for opt in cx_agent_options:
                    if opt["value"] == current_cx_agent:
                        initial_cx_agent = opt
                        break

            cx_agent_select = {
                "type": "static_select",
                "action_id": "codex_agent_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectCodexAgent")},
                "options": cx_agent_options,
                "initial_option": initial_cx_agent,
            }

            # Build model options
            cx_model_options = [
                {"text": {"type": "plain_text", "text": self._t("common.default")}, "value": "__default__"}
            ]
            for model in codex_models:
                if model:
                    cx_model_options.append(
                        {
                            "text": {"type": "plain_text", "text": model[:75]},
                            "value": model,
                        }
                    )

            # Add current model if not in list (preserve custom models)
            if current_cx_model and not any(opt["value"] == current_cx_model for opt in cx_model_options):
                cx_model_options.append(
                    {
                        "text": {"type": "plain_text", "text": current_cx_model[:75]},
                        "value": current_cx_model,
                    }
                )

            # Limit to 100 options
            if len(cx_model_options) > 100:
                cx_model_options = cx_model_options[:100]

            # Find initial model
            initial_cx_model = cx_model_options[0]
            if current_cx_model:
                for opt in cx_model_options:
                    if opt["value"] == current_cx_model:
                        initial_cx_model = opt
                        break

            cx_model_select = {
                "type": "static_select",
                "action_id": "codex_model_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectModel")},
                "options": cx_model_options,
                "initial_option": initial_cx_model,
            }

            # Build reasoning effort options from centralized Codex definition
            codex_reasoning_entries = build_codex_reasoning_options()
            selected_cx_reasoning = (
                current_cx_reasoning if current_cx_reasoning not in (None, "__default__") else "__default__"
            )
            available_cx_reasoning = {entry.get("value") for entry in codex_reasoning_entries}
            if selected_cx_reasoning not in available_cx_reasoning:
                selected_cx_reasoning = "__default__"

            cx_reasoning_options = []
            for entry in codex_reasoning_entries:
                value = entry.get("value")
                if not value:
                    continue
                if value == "__default__":
                    label = self._t("common.default")
                else:
                    translated = self._t(f"reasoning.{value}")
                    label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                cx_reasoning_options.append(
                    {
                        "text": {"type": "plain_text", "text": label},
                        "value": value,
                    }
                )

            # Find initial reasoning
            initial_cx_reasoning = next(
                (opt for opt in cx_reasoning_options if opt["value"] == selected_cx_reasoning),
                cx_reasoning_options[0],
            )

            cx_reasoning_select = {
                "type": "static_select",
                "action_id": "codex_reasoning_select",
                "placeholder": {"type": "plain_text", "text": self._t("modal.routing.selectReasoningEffort")},
                "options": cx_reasoning_options,
                "initial_option": initial_cx_reasoning,
            }

            # Add Codex section
            blocks.extend(
                [
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{self._t('modal.routing.codexSettings')}*",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "codex_agent_block",
                        "optional": True,
                        "element": cx_agent_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.codexAgent")},
                    },
                    {
                        "type": "input",
                        "block_id": "codex_model_block",
                        "optional": True,
                        "element": cx_model_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.model")},
                    },
                    {
                        "type": "input",
                        "block_id": "codex_reasoning_block",
                        "optional": True,
                        "element": cx_reasoning_select,
                        "label": {"type": "plain_text", "text": self._t("modal.routing.codexReasoningEffort")},
                    },
                ]
            )

        # Add tip
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"_💡 {self._t('modal.routing.tip')}_",
                    }
                ],
            }
        )

        return {
            "type": "modal",
            "callback_id": "routing_modal",
            "private_metadata": channel_id,
            "title": {"type": "plain_text", "text": self._t("modal.routing.title")},
            "submit": {"type": "plain_text", "text": self._t("common.save")},
            "close": {"type": "plain_text", "text": self._t("common.cancel")},
            "blocks": blocks,
        }

    async def open_question_modal(
        self,
        trigger_id: str,
        context: MessageContext,
        pending: Any,  # PendingQuestion from question_ui module
        callback_prefix: str = "claude_question",
    ):
        """Open a question modal for any agent backend.

        This supports different callback prefixes for agent question flows.

        Args:
            trigger_id: Slack trigger ID for opening the modal
            context: Message context
            pending: PendingQuestion instance with questions data
            callback_prefix: Prefix for callback routing (e.g., "claude_question")
        """
        self._ensure_clients()

        # Support both PendingQuestion dataclass and dict format
        if hasattr(pending, "questions"):
            questions = pending.questions
        else:
            questions = pending.get("questions") if isinstance(pending, dict) else []

        if not questions:
            raise ValueError("No questions available")

        import json

        private_metadata = json.dumps(
            {
                "channel_id": context.channel_id,
                "thread_id": context.thread_id,
                "question_count": len(questions),
                "callback_prefix": callback_prefix,
            }
        )

        blocks: list[Dict[str, Any]] = []
        for idx, q in enumerate(questions):
            # Support both Question dataclass and dict format
            if hasattr(q, "header"):
                header = (q.header or f"Question {idx + 1}").strip()
                prompt = (q.question or "").strip()
                multiple = bool(q.multiple)
                options = q.options
            elif isinstance(q, dict):
                header = (q.get("header") or f"Question {idx + 1}").strip()
                prompt = (q.get("question") or "").strip()
                multiple = bool(q.get("multiple") or q.get("multiSelect"))
                options = q.get("options") if isinstance(q.get("options"), list) else []
            else:
                continue

            option_items = []
            for opt in options:
                # Support both QuestionOption dataclass and dict format
                if hasattr(opt, "label"):
                    label = opt.label
                    desc = opt.description
                elif isinstance(opt, dict):
                    label = opt.get("label")
                    desc = opt.get("description")
                else:
                    continue

                if label is None:
                    continue
                item: Dict[str, Any] = {
                    "text": {
                        "type": "plain_text",
                        "text": str(label)[:75],
                        "emoji": True,
                    },
                    "value": str(label),
                }
                if desc:
                    item["description"] = {
                        "type": "plain_text",
                        "text": str(desc)[:75],
                        "emoji": True,
                    }
                option_items.append(item)

            element: Dict[str, Any]
            if multiple:
                element = {
                    "type": "multi_static_select",
                    "action_id": "select",
                    "options": option_items,
                    "placeholder": {
                        "type": "plain_text",
                        "text": self._t("common.selectOneOrMore"),
                        "emoji": True,
                    },
                }
            else:
                element = {
                    "type": "static_select",
                    "action_id": "select",
                    "options": option_items,
                    "placeholder": {
                        "type": "plain_text",
                        "text": self._t("common.selectOne"),
                        "emoji": True,
                    },
                }

            label_text = header
            if prompt:
                label_text = f"{header}: {prompt}"[:150]

            blocks.append(
                {
                    "type": "input",
                    "block_id": f"q{idx}",
                    "label": {
                        "type": "plain_text",
                        "text": label_text,
                        "emoji": True,
                    },
                    "element": element,
                }
            )

        # Use callback_prefix to generate callback_id
        callback_id = f"{callback_prefix}_modal"
        title = (
            self._t("modal.question.claudeCode")
            if callback_prefix.startswith("claude")
            else self._t("modal.question.claudeCode")
        )

        view = {
            "type": "modal",
            "callback_id": callback_id,
            "private_metadata": private_metadata,
            "title": {"type": "plain_text", "text": title, "emoji": True},
            "submit": {"type": "plain_text", "text": self._t("common.submit"), "emoji": True},
            "close": {"type": "plain_text", "text": self._t("common.cancel"), "emoji": True},
            "blocks": blocks,
        }

        await self.web_client.views_open(trigger_id=trigger_id, view=view)

    async def open_routing_modal(
        self,
        trigger_id: str,
        channel_id: str,
        registered_backends: list,
        current_backend: str,
        current_routing,  # Optional[ChannelRouting]
        opencode_agents: list,
        opencode_models: dict,
        opencode_default_config: dict,
        claude_agents: list = None,
        claude_models: list = None,
        codex_agents: list = None,
        codex_models: list = None,
    ):
        """Open a modal dialog for agent/model routing settings"""
        self._ensure_clients()

        view = self._build_routing_modal_view(
            channel_id=channel_id,
            registered_backends=registered_backends,
            current_backend=current_backend,
            current_routing=current_routing,
            opencode_agents=opencode_agents,
            opencode_models=opencode_models,
            opencode_default_config=opencode_default_config,
            claude_agents=claude_agents,
            claude_models=claude_models,
            codex_agents=codex_agents,
            codex_models=codex_models,
        )

        try:
            await self.web_client.views_open(trigger_id=trigger_id, view=view)
        except SlackApiError as e:
            logger.error(f"Error opening routing modal: {e}")
            raise

    async def update_routing_modal(
        self,
        view_id: str,
        view_hash: str,
        channel_id: str,
        registered_backends: list,
        current_backend: str,
        current_routing,
        opencode_agents: list,
        opencode_models: dict,
        opencode_default_config: dict,
        claude_agents: list = None,
        claude_models: list = None,
        codex_agents: list = None,
        codex_models: list = None,
        selected_backend: Optional[str] = None,
        selected_opencode_agent: Optional[str] = None,
        selected_opencode_model: Optional[str] = None,
        selected_opencode_reasoning: Optional[str] = None,
        selected_claude_agent: Optional[str] = None,
        selected_claude_model: Optional[str] = None,
        selected_claude_reasoning: Optional[str] = None,
        selected_codex_agent: Optional[str] = None,
        selected_codex_model: Optional[str] = None,
        selected_codex_reasoning: Optional[str] = None,
    ) -> None:
        """Update routing modal when selections change."""
        self._ensure_clients()

        view = self._build_routing_modal_view(
            channel_id=channel_id,
            registered_backends=registered_backends,
            current_backend=current_backend,
            current_routing=current_routing,
            opencode_agents=opencode_agents,
            opencode_models=opencode_models,
            opencode_default_config=opencode_default_config,
            claude_agents=claude_agents,
            claude_models=claude_models,
            codex_agents=codex_agents,
            codex_models=codex_models,
            selected_backend=selected_backend,
            selected_opencode_agent=selected_opencode_agent,
            selected_opencode_model=selected_opencode_model,
            selected_opencode_reasoning=selected_opencode_reasoning,
            selected_claude_agent=selected_claude_agent,
            selected_claude_model=selected_claude_model,
            selected_claude_reasoning=selected_claude_reasoning,
            selected_codex_agent=selected_codex_agent,
            selected_codex_model=selected_codex_model,
            selected_codex_reasoning=selected_codex_reasoning,
        )

        try:
            await self.web_client.views_update(view_id=view_id, hash=view_hash, view=view)
        except SlackApiError as e:
            logger.error(f"Error updating routing modal: {e}")
            raise

    def register_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_command: Optional[Dict[str, Callable]] = None,
        on_callback_query: Optional[Callable] = None,
        **kwargs,
    ):
        """Register callback functions for different events"""
        super().register_callbacks(on_message, on_command, on_callback_query, **kwargs)

        # Register command handlers
        if on_command:
            self.command_handlers.update(on_command)

        # Register any slash command handlers passed in kwargs
        if "on_slash_command" in kwargs:
            slash_commands = kwargs["on_slash_command"]
            if isinstance(slash_commands, dict):
                self.slash_command_handlers.update(slash_commands)

        # Register settings update handler
        if "on_settings_update" in kwargs:
            self._on_settings_update = kwargs["on_settings_update"]

        # Register change CWD handler
        if "on_change_cwd" in kwargs:
            self._on_change_cwd = kwargs["on_change_cwd"]

        # Register routing update handler
        if "on_routing_update" in kwargs:
            self._on_routing_update = kwargs["on_routing_update"]

        # Register routing modal update handler
        if "on_routing_modal_update" in kwargs:
            self._on_routing_modal_update = kwargs["on_routing_modal_update"]

        # Register resume session handler
        if "on_resume_session" in kwargs:
            self._on_resume_session = kwargs["on_resume_session"]

        # Register on_ready handler (called when connected)
        if "on_ready" in kwargs:
            self._on_ready = kwargs["on_ready"]

    async def get_or_create_thread(self, channel_id: str, user_id: str) -> Optional[str]:
        """Get existing thread timestamp or return None for new thread"""
        # Deprecated: Thread handling now uses user's message timestamp directly
        return None

    async def send_slash_response(self, response_url: str, text: str, ephemeral: bool = True) -> bool:
        """Send response to a slash command via response_url"""
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                await session.post(
                    response_url,
                    json={
                        "text": text,
                        "response_type": "ephemeral" if ephemeral else "in_channel",
                    },
                )
            return True
        except Exception as e:
            logger.error(f"Error sending slash command response: {e}")
            return False

    async def _is_authorized_channel(self, channel_id: str) -> bool:
        """Check if a channel is authorized based on whitelist configuration"""
        if not self.settings_manager:
            logger.warning("No settings_manager configured; rejecting by default")
            return False

        settings = self.settings_manager.get_channel_settings(channel_id)
        if settings is None:
            logger.warning("No channel settings found; rejecting by default")
            return False

        if settings.enabled:
            return True

        logger.info("Channel not enabled in settings.json: %s", channel_id)
        return False

    async def _send_auth_denial(self, channel_id: str, user_id: str, auth: AuthResult, response_url: str = None):
        """Send appropriate denial message based on auth result."""
        msg = self.build_auth_denial_text(auth.denial, channel_id)
        if not msg:
            return

        if response_url:
            await self.send_slash_response(response_url, msg)
            return

        try:
            self._ensure_clients()
            await self._chat_post_message(channel=channel_id, text=msg)
        except Exception as e:
            logger.error(f"Failed to send auth denial message: {e}")

    async def _send_unauthorized_message(self, channel_id: str):
        """Send unauthorized access message to channel"""
        try:
            self._ensure_clients()
            await self._chat_post_message(
                channel=channel_id,
                text=f"❌ {self._t('error.channelNotEnabled', channel_id)}",
            )
        except Exception as e:
            logger.error(f"Failed to send unauthorized message to {channel_id}: {e}")
