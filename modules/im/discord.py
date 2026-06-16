import asyncio
import io
import json
import logging
import os
import time
from typing import Dict, Any, Optional, Callable, List

import aiohttp
import discord

from .base import (
    BaseIMClient,
    FileDownloadResult,
    MessageContext,
    InlineKeyboard,
    InlineButton,
    FileAttachment,
)
from config.v2_config import DiscordConfig
from .formatters import DiscordFormatter
from vibe.i18n import get_supported_languages, t as i18n_t
from modules.agents.opencode.utils import (
    build_claude_reasoning_options,
    build_opencode_model_option_items,
    build_codex_reasoning_options,
    format_claude_model_label,
    build_reasoning_effort_options,
    resolve_opencode_allowed_providers,
    resolve_opencode_default_model,
    resolve_opencode_provider_preferences,
)
from modules.agents.native_sessions.display import format_display_summary, format_display_time
from modules.agents.native_sessions.types import NativeResumeSession
from vibe.claude_model_catalog import DEFAULT_CLAUDE_MODEL_ALIASES

logger = logging.getLogger(__name__)


def _prioritize_claude_model_choices(models: List[str], current_model: Optional[str]) -> List[str]:
    """Order Claude model ids so the active selection and the canonical bare
    aliases (opus/sonnet/haiku) survive Discord's 25-option select-menu cap.

    ``api.claude_models()`` appends the bare aliases after the full catalog, so a
    plain truncation silently drops the most useful, always-valid picks — and the
    user's current selection. Surfacing them first means the cap only trims the
    long tail of dated snapshots instead.
    """
    priority: List[str] = []
    seen: set[str] = set()
    for candidate in (current_model, *DEFAULT_CLAUDE_MODEL_ALIASES):
        if candidate and candidate in models and candidate not in seen:
            priority.append(candidate)
            seen.add(candidate)
    return priority + [model for model in models if model not in seen]


class DiscordBot(BaseIMClient):
    """Discord implementation of the IM client."""

    def __init__(self, config: DiscordConfig):
        super().__init__(config)
        self.config = config

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.messages = True
        intents.dm_messages = True
        intents.reactions = True

        self.client = discord.Client(intents=intents)
        self.formatter = DiscordFormatter()

        self.settings_manager = None
        self.sessions = None
        self._controller = None
        self._on_ready: Optional[Callable] = None
        self._user_info_cache: Dict[str, Dict[str, Any]] = {}
        self._recent_interaction_ids: Dict[str, float] = {}
        self._recent_callback_keys: Dict[str, float] = {}
        self._callback_dedupe_ttl_seconds = 3.0

        self.client.on_ready = self._on_ready_event
        self.client.on_message = self._on_message_event

    def set_settings_manager(self, settings_manager):
        self.settings_manager = settings_manager
        self.sessions = getattr(settings_manager, "sessions", None)

    def set_controller(self, controller):
        self._controller = controller

    def register_callbacks(
        self,
        on_message: Optional[Callable] = None,
        on_command: Optional[Dict[str, Callable]] = None,
        on_callback_query: Optional[Callable] = None,
        **kwargs,
    ):
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

    def _get_lang(self, channel_id: Optional[str] = None) -> str:
        if self._controller and hasattr(self._controller, "config"):
            if hasattr(self._controller, "_get_lang"):
                return self._controller._get_lang()
            return getattr(self._controller.config, "language", "en")
        return "en"

    def _t(self, key: str, channel_id: Optional[str] = None, **kwargs) -> str:
        lang = self._get_lang(channel_id)
        return i18n_t(key, lang, **kwargs)

    def _prune_recent_interactions(self) -> None:
        cutoff = time.monotonic() - self._callback_dedupe_ttl_seconds
        self._recent_interaction_ids = {key: ts for key, ts in self._recent_interaction_ids.items() if ts >= cutoff}
        self._recent_callback_keys = {key: ts for key, ts in self._recent_callback_keys.items() if ts >= cutoff}

    def _mark_interaction_seen(self, interaction: discord.Interaction, callback_data: str) -> bool:
        self._prune_recent_interactions()

        now = time.monotonic()
        interaction_id = str(interaction.id)
        if interaction_id in self._recent_interaction_ids:
            return False

        message_id = str(interaction.message.id) if interaction.message else ""
        callback_key = f"{interaction.user.id}:{message_id}:{callback_data}"
        last_seen = self._recent_callback_keys.get(callback_key)
        self._recent_interaction_ids[interaction_id] = now
        self._recent_callback_keys[callback_key] = now
        return last_seen is None or (now - last_seen) > self._callback_dedupe_ttl_seconds

    def _build_interaction_context(self, interaction: discord.Interaction) -> MessageContext | None:
        channel_id, thread_id = self._extract_context_ids(interaction.channel)
        guild_id = str(interaction.guild_id) if interaction.guild_id else None
        if guild_id and not self._is_allowed_guild(guild_id):
            return None

        is_dm = interaction.guild is None or isinstance(interaction.channel, discord.DMChannel)
        return MessageContext(
            user_id=str(interaction.user.id),
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=str(interaction.message.id) if interaction.message else None,
            platform_specific={"interaction": interaction, "is_dm": is_dm},
        )

    async def _dispatch_callback_query(self, context: MessageContext, data: str) -> None:
        if self.on_callback_query_callback:
            await self.on_callback_query_callback(context, data)

    def _spawn_callback_query_task(self, context: MessageContext, data: str) -> None:
        task = asyncio.create_task(self._dispatch_callback_query(context, data))

        def _log_task_result(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except Exception:
                logger.exception("Discord callback task failed: %s", data)

        task.add_done_callback(_log_task_result)

    def get_default_parse_mode(self) -> str:
        return "markdown"

    def should_use_thread_for_reply(self) -> bool:
        return True

    def should_use_thread_for_dm_session(self) -> bool:
        return False

    async def prepare_resume_context(
        self,
        context: MessageContext,
        *,
        host_message_ts: Optional[str] = None,
        is_dm: bool = False,
    ) -> MessageContext:
        if context.thread_id or is_dm or not host_message_ts:
            return context

        async def _impl() -> MessageContext:
            channel = await self._fetch_channel(context.channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return context
            try:
                message = await channel.fetch_message(int(host_message_ts))
            except Exception as err:
                logger.warning("Failed to fetch Discord host message %s: %s", host_message_ts, err)
                return context

            thread = getattr(message, "thread", None)
            if thread is None:
                thread = await self._maybe_create_thread(message)
            if thread is None:
                return context

            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                platform=context.platform,
                thread_id=str(thread.id),
                message_id=context.message_id,
                platform_specific=context.platform_specific,
                files=context.files,
            )

        return await self._run_on_client_loop(_impl())

    async def prepare_turn_context(self, context: MessageContext, source: str) -> MessageContext:
        if source != "human" or context.thread_id:
            return context
        payload = context.platform_specific or {}
        if payload.get("is_dm"):
            return context
        message = payload.get("message")
        if message is None or getattr(message, "guild", None) is None:
            return context

        reply_anchor_message_id = self._get_reference_message_id(message)
        reply_anchor_base_session_id = self._get_reply_anchor_base(context.channel_id, reply_anchor_message_id)
        if reply_anchor_base_session_id:
            async def _reply_impl() -> MessageContext:
                channel = await self._fetch_channel(context.channel_id)
                if channel is None or not hasattr(channel, "fetch_message"):
                    return context
                try:
                    anchor_message = await channel.fetch_message(int(reply_anchor_message_id))
                except Exception as err:
                    logger.warning(
                        "Failed to fetch Discord reply anchor %s: %s",
                        reply_anchor_message_id,
                        err,
                    )
                    return context

                thread = getattr(anchor_message, "thread", None)
                if thread is None:
                    thread = await self._maybe_create_thread(anchor_message)
                if thread is None:
                    return context

                next_payload = dict(context.platform_specific or {})
                next_payload["reply_anchor_base_session_id"] = reply_anchor_base_session_id
                next_payload["reply_anchor_message_id"] = reply_anchor_message_id
                return MessageContext(
                    user_id=context.user_id,
                    channel_id=context.channel_id,
                    platform=context.platform,
                    thread_id=str(thread.id),
                    message_id=context.message_id,
                    platform_specific=next_payload,
                    files=context.files,
                )

            return await self._run_on_client_loop(_reply_impl())

        async def _impl() -> MessageContext:
            thread = await self._maybe_create_thread(message)
            if thread is None:
                return context
            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                platform=context.platform,
                thread_id=str(thread.id),
                message_id=context.message_id,
                platform_specific=context.platform_specific,
                files=context.files,
            )

        return await self._run_on_client_loop(_impl())

    def _is_thread_reply_allowed(self, author_id: str, channel_id: str, thread_id: str) -> bool:
        if not self.settings_manager or not self.sessions:
            return False
        if self.sessions.is_thread_active(author_id, channel_id, thread_id):
            return True
        return self.sessions.is_thread_active("scheduled", channel_id, thread_id)

    @staticmethod
    def _get_reference_message_id(message: discord.Message) -> Optional[str]:
        reference = getattr(message, "reference", None)
        message_id = getattr(reference, "message_id", None) if reference is not None else None
        return str(message_id) if message_id is not None else None

    def _get_reply_anchor_base(self, channel_id: str, message_id: Optional[str]) -> Optional[str]:
        if not self.sessions or not channel_id or not message_id:
            return None
        session_key = f"discord::{channel_id}"
        base_session_id = f"discord_{message_id}"
        if self.sessions.has_any_agent_session_base(session_key, base_session_id):
            return base_session_id
        return None

    def format_markdown(self, text: str) -> str:
        return text

    async def _run_on_client_loop(self, coro):
        loop = getattr(self, "_loop", None)
        if loop is None:
            return await coro
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            return await coro
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return await asyncio.wrap_future(future)

    async def run_on_client_loop(self, coro):
        return await self._run_on_client_loop(coro)

    def register_handlers(self):
        return

    def run(self):
        if not self.config.bot_token:
            raise ValueError("Discord bot token is required")

        async def _run():
            self._loop = asyncio.get_running_loop()
            # Inject proxy connector inside the event loop (required by
            # aiohttp). Must happen before login() creates the session.
            from vibe.proxy import redact_proxy_url, resolve_proxy

            proxy_url = resolve_proxy(self.config.proxy_url)
            if proxy_url:
                try:
                    from aiohttp_socks import ProxyConnector

                    self.client.http.connector = ProxyConnector.from_url(proxy_url, rdns=True)
                    logger.info("Discord using proxy: %s", redact_proxy_url(proxy_url))
                except ImportError:
                    logger.warning("Proxy configured but aiohttp_socks not installed")

            async with self.client:
                await self.client.start(self.config.bot_token)

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            return

    def stop(self) -> None:
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: loop.create_task(self.client.close()))
        except Exception:
            logger.exception("Failed to stop Discord client")

    async def shutdown(self) -> None:
        loop = getattr(self, "_loop", None)
        current_loop = asyncio.get_running_loop()
        if loop is None or loop is current_loop:
            await self.client.close()

    # ---------------------------------------------------------------------
    # Message helpers
    # ---------------------------------------------------------------------
    def _to_int_id(self, value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _fetch_channel(self, channel_id: Optional[str]) -> Optional[discord.abc.Messageable]:
        cid = self._to_int_id(channel_id)
        if cid is None:
            return None
        channel = self.client.get_channel(cid)
        if channel is not None:
            return channel
        try:
            return await self.client.fetch_channel(cid)
        except Exception as err:
            logger.debug("Failed to fetch channel %s: %s", channel_id, err)
            return None

    def _get_context_channel(self, context: MessageContext):
        payload = context.platform_specific or {}
        interaction = payload.get("interaction") if isinstance(payload, dict) else None
        if interaction is not None and getattr(interaction, "channel", None) is not None:
            return interaction.channel
        message = payload.get("message") if isinstance(payload, dict) else None
        if message is not None and getattr(message, "channel", None) is not None:
            return message.channel
        return None

    async def _resolve_target(self, context: MessageContext) -> Optional[discord.abc.Messageable]:
        direct_channel = self._get_context_channel(context)
        if isinstance(direct_channel, discord.Thread):
            return direct_channel

        if context.thread_id:
            target = await self._fetch_channel(context.thread_id)
            if isinstance(target, discord.Thread):
                return target

        if direct_channel is not None:
            return direct_channel

        return await self._fetch_channel(context.channel_id)

    def _extract_context_ids(self, channel: discord.abc.GuildChannel | discord.Thread) -> tuple[str, Optional[str]]:
        if isinstance(channel, discord.Thread):
            parent_id = str(channel.parent_id) if channel.parent_id else str(channel.id)
            return parent_id, str(channel.id)
        return str(channel.id), None

    def _clean_message_text(self, text: str) -> str:
        return (text or "").strip()

    def _is_allowed_guild(self, guild_id: Optional[str]) -> bool:
        if guild_id and self.settings_manager and hasattr(self.settings_manager, "has_guild_scope"):
            try:
                if self.settings_manager.has_guild_scope():
                    allowed = bool(self.settings_manager.is_guild_enabled(guild_id))
                    if not allowed:
                        logger.debug("Ignoring Discord message from disabled guild %s", guild_id)
                    return allowed
            except Exception:
                logger.debug("Failed to resolve Discord guild access settings", exc_info=True)

        allow = set(self.config.guild_allowlist or [])
        deny = set(self.config.guild_denylist or [])
        if guild_id and guild_id in deny:
            return False
        if allow and (not guild_id or guild_id not in allow):
            return False
        return True

    async def send_dm(self, user_id: str, text: str, **kwargs) -> Optional[str]:
        """Send a direct message to a Discord user."""

        async def _impl() -> Optional[str]:
            try:
                uid = self._to_int_id(user_id)
                if uid is None:
                    return None
                user = self.client.get_user(uid)
                if user is None:
                    user = await self.client.fetch_user(uid)
                if user is None:
                    return None
                dm_channel = user.dm_channel or await user.create_dm()
                keyboard = kwargs.get("keyboard")
                view = None
                if keyboard is not None:
                    context = MessageContext(
                        user_id=user_id,
                        channel_id=str(dm_channel.id),
                        platform="discord",
                        platform_specific={"is_dm": True},
                    )
                    view = _DiscordButtonView(self, context, keyboard, owner_id=str(uid))
                message = await dm_channel.send(content=text, view=view)
                return str(message.id)
            except Exception as e:
                logger.error("Failed to send DM to Discord user %s: %s", user_id, e)
                return None

        return await self._run_on_client_loop(_impl())

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        async def _impl() -> str:
            if not text:
                raise ValueError("Discord send_message requires non-empty text")
            target = await self._resolve_target(context)
            if target is None:
                raise RuntimeError("Discord channel not found")
            message = await target.send(content=text)
            if self.settings_manager and context.thread_id:
                try:
                    if self.sessions:
                        self.sessions.mark_thread_active(context.user_id, context.channel_id, context.thread_id)
                except Exception:
                    pass
            return str(message.id)

        return await self._run_on_client_loop(_impl())

    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        parse_mode: Optional[str] = None,
    ) -> str:
        async def _impl() -> str:
            target = await self._resolve_target(context)
            if target is None:
                raise RuntimeError("Discord channel not found")

            view = (
                _PersistentStartView(self, keyboard)
                if _PersistentStartView.is_all_static(keyboard)
                else _DiscordButtonView(self, context, keyboard)
            )
            message = await target.send(content=text, view=view)
            if self.settings_manager and context.thread_id:
                try:
                    if self.sessions:
                        self.sessions.mark_thread_active(context.user_id, context.channel_id, context.thread_id)
                except Exception:
                    pass
            return str(message.id)

        return await self._run_on_client_loop(_impl())

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        async def _impl() -> bool:
            target = await self._resolve_target(context)
            if target is None:
                return False
            try:
                msg = await target.fetch_message(int(message_id))
                view = None
                if keyboard:
                    view = (
                        _PersistentStartView(self, keyboard)
                        if _PersistentStartView.is_all_static(keyboard)
                        else _DiscordButtonView(self, context, keyboard)
                    )
                await msg.edit(content=text, view=view)
                return True
            except Exception as err:
                logger.debug("Failed to edit Discord message: %s", err)
                return False

        return await self._run_on_client_loop(_impl())

    async def remove_inline_keyboard(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        async def _impl() -> bool:
            # When called from a button callback, use the interaction's message
            # object to edit directly (the interaction response is already deferred).
            interaction = (context.platform_specific or {}).get("interaction") if context.platform_specific else None
            if interaction is not None and interaction.message is not None:
                try:
                    kwargs: dict = {"view": None}
                    if text is not None:
                        kwargs["content"] = text
                    await interaction.message.edit(**kwargs)
                    return True
                except Exception as err:
                    logger.info("Failed to remove Discord keyboard via interaction.message: %s", err)
                    return False
            return await self.edit_message(context, message_id, text=text, keyboard=None, parse_mode=parse_mode)

        return await self._run_on_client_loop(_impl())

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        return True

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        async def _impl() -> bool:
            target = await self._resolve_target(context)
            if target is None:
                return False
            try:
                msg = await target.fetch_message(int(message_id))
                normalized = emoji
                if normalized in [":eyes:", "eyes", "eye", "👀"]:
                    normalized = "👀"
                await msg.add_reaction(normalized)
                return True
            except Exception as err:
                logger.debug("Failed to add Discord reaction: %s", err)
                return False

        return await self._run_on_client_loop(_impl())

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        async def _impl() -> bool:
            target = await self._resolve_target(context)
            if target is None:
                return False
            try:
                msg = await target.fetch_message(int(message_id))
                normalized = emoji
                if normalized in [":eyes:", "eyes", "eye", "👀"]:
                    normalized = "👀"
                await msg.remove_reaction(normalized, self.client.user)
                return True
            except Exception as err:
                logger.debug("Failed to remove Discord reaction: %s", err)
                return False

        return await self._run_on_client_loop(_impl())

    async def upload_markdown(
        self,
        context: MessageContext,
        title: str,
        content: str,
        filetype: str = "markdown",
    ) -> str:
        async def _impl() -> str:
            target = await self._resolve_target(context)
            if target is None:
                raise RuntimeError("Discord channel not found")
            data = (content or "").encode("utf-8")
            file_obj = discord.File(io.BytesIO(data), filename=title)
            message = await target.send(file=file_obj)
            return str(message.id)

        return await self._run_on_client_loop(_impl())

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        async def _impl() -> bool:
            target = await self._resolve_target(context)
            if target is None:
                return False

            typing_method = getattr(target, "typing", None)
            if not callable(typing_method):
                return False

            try:
                await typing_method()
                return True
            except Exception as err:
                logger.debug("Failed to trigger Discord typing indicator: %s", err)
                return False

        return await self._run_on_client_loop(_impl())

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        """Discord typing indicators expire automatically without explicit cancel."""

        return True

    async def upload_file_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        """Upload a local file to the Discord conversation."""

        async def _impl() -> str:
            target = await self._resolve_target(context)
            if target is None:
                raise RuntimeError("Discord channel not found")

            # Keep original extension for proper Discord preview handling.
            filename = os.path.basename(file_path)
            file_obj = discord.File(file_path, filename=filename)
            message = await target.send(file=file_obj)
            return str(message.id)

        return await self._run_on_client_loop(_impl())

    async def download_file(
        self,
        file_info: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> Optional[bytes]:
        url = file_info.get("url") or file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            return None
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    content_length = response.headers.get("Content-Length")
                    if max_bytes is not None and content_length and int(content_length) > max_bytes:
                        return None
                    chunks = []
                    total_size = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total_size += len(chunk)
                        if max_bytes is not None and total_size > max_bytes:
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception as err:
            logger.debug("Failed to download Discord file: %s", err)
            return None

    async def download_file_to_path(
        self,
        file_info: Dict[str, Any],
        target_path: str,
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> FileDownloadResult:
        url = file_info.get("url") or file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            return FileDownloadResult(False, "No download URL available")
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return FileDownloadResult(False, f"Download failed with HTTP {response.status}")
                    content_length = response.headers.get("Content-Length")
                    if max_bytes is not None and content_length and int(content_length) > max_bytes:
                        return FileDownloadResult(False, f"File exceeds the allowed size limit ({max_bytes} bytes)")

                    total_size = 0
                    with open(target_path, "wb") as file_obj:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            total_size += len(chunk)
                            if max_bytes is not None and total_size > max_bytes:
                                return FileDownloadResult(
                                    False, f"File exceeds the allowed size limit ({max_bytes} bytes)"
                                )
                            file_obj.write(chunk)
                    return FileDownloadResult(True)
        except Exception as err:
            logger.debug("Failed to download Discord file to path: %s", err)
            return FileDownloadResult(False, f"Download error: {err}")

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        cached = self._user_info_cache.get(user_id)
        if cached is not None:
            return cached

        async def _impl() -> Dict[str, Any]:
            uid = self._to_int_id(user_id)
            if uid is None:
                return {"id": user_id}
            user = self.client.get_user(uid)
            if user is None:
                try:
                    user = await self.client.fetch_user(uid)
                except Exception:
                    user = None
            if user is None:
                return {"id": user_id}
            info = {"id": str(user.id), "name": user.name, "display_name": user.display_name}
            self._user_info_cache[user_id] = info
            return info

        return await self._run_on_client_loop(_impl())

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            channel = await self._fetch_channel(channel_id)
            if channel is None:
                return {"id": channel_id, "name": channel_id}
            name = getattr(channel, "name", None) or channel_id
            return {"id": str(channel.id), "name": name}

        return await self._run_on_client_loop(_impl())

    # ---------------------------------------------------------------------
    # Discord-specific interaction helpers
    # ---------------------------------------------------------------------
    async def _on_ready_event(self):
        logger.info("Discord client ready")
        # Register persistent view so /start menu buttons survive restarts.
        try:
            self.client.add_view(_PersistentStartView(self))
        except Exception as err:
            logger.error("Failed to register persistent start view: %s", err, exc_info=True)
        if self._on_ready:
            try:
                await self._on_ready()
            except Exception as err:
                logger.error("Discord on_ready callback failed: %s", err, exc_info=True)

    async def _is_authorized_channel(self, channel_id: str) -> bool:
        if not self.settings_manager:
            logger.warning("No settings_manager configured; rejecting by default")
            return False
        settings = self.settings_manager.get_channel_settings(channel_id)
        if settings is None:
            logger.warning("No channel settings found; rejecting by default")
            return False
        return settings.enabled

    async def _send_unauthorized_message(self, channel_id: str):
        try:
            channel = await self._fetch_channel(channel_id)
            if channel is None:
                return
            await channel.send(content=f"❌ {self._t('error.channelNotEnabled', channel_id)}")
        except Exception as err:
            logger.debug("Failed to send unauthorized message: %s", err)

    async def _send_auth_denial(self, channel_id: str, user_id: str, auth_result, interaction=None):
        """Send denial message for failed auth check."""
        msg = self.build_auth_denial_text(auth_result.denial, channel_id)
        if not msg:
            if interaction:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.defer(ephemeral=True)
                except Exception as err:
                    logger.debug("Failed to acknowledge silent interaction auth denial: %s", err)
            return

        if interaction:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(msg, ephemeral=True)
                else:
                    await interaction.followup.send(msg, ephemeral=True)
            except Exception as err:
                logger.debug("Failed to send interaction auth denial: %s", err)
            return

        channel = await self._fetch_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.send(content=msg)
        except Exception as err:
            logger.debug("Failed to send channel auth denial: %s", err)

    async def _dismiss_interaction_message(self, interaction: discord.Interaction, fallback_text: str) -> None:
        """Delete the source interaction message, with edit fallback when delete is unavailable."""
        if interaction.message is not None:
            try:
                await interaction.message.delete()
                return
            except Exception as err:
                logger.debug("Failed to delete Discord interaction message directly: %s", err)

        try:
            await interaction.delete_original_response()
            return
        except Exception as err:
            logger.debug("Failed to delete Discord original interaction response: %s", err)

        try:
            await interaction.edit_original_response(content=fallback_text, embed=None, view=None)
        except Exception as err:
            logger.warning("Failed to dismiss Discord interaction message after successful submit: %s", err)

    async def _maybe_create_thread(self, message: discord.Message) -> Optional[discord.Thread]:
        if isinstance(message.channel, discord.Thread):
            return message.channel
        if message.guild is None:
            return None
        try:
            snippet = (message.content or "").strip()
            if snippet:
                snippet = snippet[:50]
            name = snippet or "vibe-remote session"
            thread = await message.create_thread(
                name=name,
                auto_archive_duration=self.config.thread_auto_archive_minutes,
            )
            return thread
        except Exception as err:
            logger.warning("Failed to create thread: %s", err)
            return None

    async def _on_message_event(self, message: discord.Message):
        if message.author and message.author.bot:
            return

        # Hot-reload config BEFORE reading any config values (require_mention, etc.)
        if self._controller and hasattr(self._controller, "_refresh_config_from_disk"):
            self._controller._refresh_config_from_disk()

        content = self._clean_message_text(message.content)

        channel = message.channel
        channel_id, thread_id = self._extract_context_ids(channel)

        if message.guild and not self._is_allowed_guild(str(message.guild.id)):
            return

        # File attachments
        files = None
        if message.attachments:
            files = []
            for attachment in message.attachments:
                files.append(
                    FileAttachment(
                        name=attachment.filename,
                        mimetype=attachment.content_type or "application/octet-stream",
                        url=attachment.url,
                        size=attachment.size,
                    )
                )

        if not content and not files:
            return

        # Determine if this is a DM
        is_dm = isinstance(channel, discord.DMChannel) or message.guild is None
        referenced_anchor_base = None if is_dm else self._get_reply_anchor_base(channel_id, self._get_reference_message_id(message))

        auth_result = self.check_authorization(
            user_id=str(message.author.id),
            channel_id=channel_id,
            is_dm=is_dm,
            text=content,
            settings_manager=self.settings_manager,
        )
        if not auth_result.allowed:
            await self._send_auth_denial(channel_id, str(message.author.id), auth_result)
            return

        # Mention logic for guild channels
        effective_require_mention = self.config.require_mention
        if self.settings_manager:
            effective_require_mention = self.settings_manager.get_require_mention(
                channel_id, global_default=self.config.require_mention
            )

        mentioned_bot = bool(self.client.user and message.mentions and self.client.user in message.mentions)

        if effective_require_mention and not is_dm:
            if isinstance(channel, discord.Thread):
                if self.settings_manager:
                    thread_active = self._is_thread_reply_allowed(str(message.author.id), channel_id, str(channel.id))
                    if not thread_active:
                        return
                else:
                    return
            else:
                if referenced_anchor_base:
                    pass
                elif not message.mentions or (self.client.user and self.client.user not in message.mentions):
                    return

        # Strip bot mention from content
        if self.client.user:
            bot_id = str(self.client.user.id)
            content = content.replace(f"<@{bot_id}>", "").replace(f"<@!{bot_id}>", "").strip()

        allow_plain_bind = self.should_allow_plain_bind(
            user_id=str(message.author.id),
            is_dm=is_dm,
            settings_manager=self.settings_manager,
        )

        # Handle slash-like commands in plain messages
        if self.parse_text_command(content, allow_plain_bind=allow_plain_bind):
            command_context = MessageContext(
                user_id=str(message.author.id),
                channel_id=channel_id,
                thread_id=thread_id,
                message_id=str(message.id),
                platform_specific={"message": message, "is_dm": is_dm},
                files=files,
            )
            if await self.dispatch_text_command(command_context, content, allow_plain_bind=allow_plain_bind):
                return

        if not content and not files:
            if mentioned_bot and self.on_message_callback:
                context = MessageContext(
                    user_id=str(message.author.id),
                    channel_id=channel_id,
                    thread_id=thread_id,
                    message_id=str(message.id),
                    platform_specific={"message": message, "is_dm": is_dm},
                    files=files,
                )
                await self.on_message_callback(context, "")
            return

        context = MessageContext(
            user_id=str(message.author.id),
            channel_id=channel_id,
            thread_id=thread_id,
            message_id=str(message.id),
            platform_specific={"message": message, "is_dm": is_dm},
            files=files,
        )

        if self.on_message_callback:
            await self.on_message_callback(context, content)

    # ---------------------------------------------------------------------
    # Discord UI helpers (modals and selects)
    # ---------------------------------------------------------------------
    async def open_change_cwd_modal(self, interaction: discord.Interaction, current_cwd: str, channel_id: str):
        class ChangeCwdModal(discord.ui.Modal, title="Change Working Directory"):
            new_cwd = discord.ui.TextInput(label="New working directory", default=current_cwd or "", required=True)

            async def on_submit(self, submit_interaction: discord.Interaction):
                if not hasattr(self, "_outer"):
                    return
                outer: DiscordBot = getattr(self, "_outer")
                if hasattr(outer, "_on_change_cwd"):
                    _is_dm = submit_interaction.guild is None
                    await outer._on_change_cwd(
                        str(submit_interaction.user.id),
                        str(self.new_cwd.value or ""),
                        channel_id,
                        _is_dm,
                    )
                await submit_interaction.response.defer(ephemeral=True)

        modal = ChangeCwdModal()
        modal._outer = self
        await interaction.response.send_modal(modal)

    async def open_settings_modal(
        self,
        trigger_id: Any,
        user_settings: Any,
        message_types: list,
        display_names: dict,
        channel_id: str = None,
        current_require_mention: object = None,
        global_require_mention: bool = False,
        current_language: str = None,
        owner_user_id: Optional[str] = None,
    ):
        interaction = trigger_id if isinstance(trigger_id, discord.Interaction) else None

        def _prefixed_label(prefix_key: str, label: str, limit: int = 100) -> str:
            prefix = self._t(prefix_key)
            combined = f"{prefix}: {label}" if prefix else label
            if len(combined) > limit:
                return combined[:limit]
            return combined

        class SettingsView(discord.ui.View):
            def __init__(self, outer: DiscordBot, owner_id: Optional[str]):
                super().__init__(timeout=900)
                self.outer = outer
                self.owner_id = owner_id
                self.selected_types = set(user_settings.show_message_types or [])
                if current_require_mention is None:
                    self.require_value = "__default__"
                elif current_require_mention is True:
                    self.require_value = "true"
                else:
                    self.require_value = "false"
                self.language_value = current_language or outer._get_lang()
                self._save_callback = None
                type_options = []
                for mt in message_types:
                    display_name = display_names.get(mt, mt)
                    label = _prefixed_label("discord.labels.messageTypes", str(display_name))
                    type_options.append(
                        discord.SelectOption(
                            label=label,
                            value=mt,
                            default=mt in self.selected_types,
                        )
                    )
                default_status = (
                    self.outer._t("modal.settings.mentionStatusOn")
                    if global_require_mention
                    else self.outer._t("modal.settings.mentionStatusOff")
                )
                require_options = [
                    discord.SelectOption(
                        label=_prefixed_label(
                            "discord.labels.mentionPolicy",
                            self.outer._t("modal.settings.optionDefault", status=default_status),
                        ),
                        value="__default__",
                        default=self.require_value == "__default__",
                    ),
                    discord.SelectOption(
                        label=_prefixed_label(
                            "discord.labels.mentionPolicy",
                            self.outer._t("modal.settings.optionRequireMention"),
                        ),
                        value="true",
                        default=self.require_value == "true",
                    ),
                    discord.SelectOption(
                        label=_prefixed_label(
                            "discord.labels.mentionPolicy",
                            self.outer._t("modal.settings.optionDontRequireMention"),
                        ),
                        value="false",
                        default=self.require_value == "false",
                    ),
                ]
                language_options = [
                    discord.SelectOption(
                        label=_prefixed_label("discord.labels.language", lang),
                        value=lang,
                        default=lang == self.language_value,
                    )
                    for lang in get_supported_languages()
                ]

                self.types_select = discord.ui.Select(
                    placeholder=self.outer._t("modal.settings.showMessageTypesPlaceholder"),
                    options=type_options,
                    min_values=0,
                    max_values=len(type_options) if type_options else 1,
                )
                self.require_select = discord.ui.Select(
                    placeholder=self.outer._t("modal.settings.selectMentionBehavior"),
                    options=require_options,
                    min_values=1,
                    max_values=1,
                )
                self.lang_select = discord.ui.Select(
                    placeholder=self.outer._t("modal.settings.language"),
                    options=language_options,
                    min_values=1,
                    max_values=1,
                )

                async def types_callback(select_interaction: discord.Interaction):
                    self.selected_types = set(self.types_select.values or [])
                    await select_interaction.response.defer()

                async def require_callback(select_interaction: discord.Interaction):
                    if self.require_select.values:
                        self.require_value = self.require_select.values[0]
                    await select_interaction.response.defer()

                async def language_callback(select_interaction: discord.Interaction):
                    if self.lang_select.values:
                        self.language_value = self.lang_select.values[0]
                    await select_interaction.response.defer()

                self.types_select.callback = types_callback
                self.require_select.callback = require_callback
                self.lang_select.callback = language_callback
                self.add_item(self.types_select)
                self.add_item(self.require_select)
                self.add_item(self.lang_select)
                save_button = discord.ui.Button(
                    label=self.outer._t("common.save"),
                    style=discord.ButtonStyle.primary,
                )
                save_button.callback = self._on_save
                self.add_item(save_button)

            async def _on_save(self, interaction: discord.Interaction):
                if self._save_callback:
                    await self._save_callback(interaction)

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if self.owner_id and str(interaction.user.id) != self.owner_id:
                    return False
                return True

            async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
                logger.debug("SettingsView error: %s", error)

            async def on_timeout(self) -> None:
                return

        owner_id = owner_user_id or (str(interaction.user.id) if interaction else None)
        view = SettingsView(self, owner_id)

        async def save_callback(save_interaction: discord.Interaction):
            # Re-check auth before saving (defense-in-depth against shared views)
            save_uid = str(save_interaction.user.id)
            save_cid = channel_id or str(save_interaction.channel_id or "")
            save_is_dm = save_interaction.guild is None
            auth = self.check_authorization(
                user_id=save_uid,
                channel_id=save_cid,
                is_dm=save_is_dm,
                action="settings",
                settings_manager=self.settings_manager,
            )
            if not auth.allowed:
                await self._send_auth_denial(save_cid, save_uid, auth, interaction=save_interaction)
                return

            show_types = list(view.selected_types or [])
            require_value = view.require_value
            if require_value == "__default__":
                require_mention = None
            elif require_value == "true":
                require_mention = True
            else:
                require_mention = False
            language = view.language_value
            try:
                await save_interaction.response.defer()
                if hasattr(self, "_on_settings_update"):
                    await self._on_settings_update(
                        str(save_interaction.user.id),
                        show_types,
                        channel_id or str(save_interaction.channel_id or ""),
                        require_mention,
                        language,
                        notify_user=True,
                        is_dm=save_interaction.guild is None,
                    )
                await self._dismiss_interaction_message(save_interaction, f"✅ {self._t('common.submitted')}")
            except Exception as err:
                await save_interaction.edit_original_response(
                    content=f"❌ {self._t('error.settingsUpdateFailed', error=str(err))}",
                    embed=None,
                    view=None,
                )

        view._save_callback = save_callback
        settings_title = f"⚙️ {self._t('modal.settings.title')}"
        settings_embed = discord.Embed(
            title=settings_title,
            description=self._t("discord.settingsSubtitle"),
        )
        if interaction:
            await interaction.response.send_message(
                embed=settings_embed,
                view=view,
                ephemeral=True,
            )
        else:
            channel = await self._fetch_channel(channel_id)
            if channel is None:
                raise RuntimeError("Discord channel not found")
            await channel.send(embed=settings_embed, view=view)

    async def open_resume_session_modal(
        self,
        trigger_id: Any,
        sessions: List[NativeResumeSession],
        channel_id: str,
        thread_id: str,
        host_message_ts: Optional[str] = None,
    ):
        interaction = trigger_id if isinstance(trigger_id, discord.Interaction) else None
        t = lambda key, **kw: self._t(key, channel_id, **kw)
        common_agents = ["claude", "codex", "opencode"]
        registered_backends = None
        if getattr(self, "_controller", None) and getattr(self._controller, "agent_service", None):
            registered_backends = list(self._controller.agent_service.agents.keys())
        allowed_agents = set(registered_backends or common_agents)
        sessions = [item for item in sessions if item.agent in allowed_agents]

        options = []
        for item in sessions:
            label = format_display_summary(item)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=f"{item.agent}|{item.native_session_id}",
                    description=format_display_time(item)[:100],
                )
            )
        if len(options) > 25:
            options = options[:25]
        has_recent_sessions = bool(options)
        if not options:
            options = [discord.SelectOption(label=t("modal.resume.noRecentSessionsOption"), value="__none__")]

        agent_options = [discord.SelectOption(label=agent, value=agent) for agent in sorted(allowed_agents)]
        if len(agent_options) > 25:
            agent_options = agent_options[:25]
        if not agent_options:
            agent_options = [discord.SelectOption(label="default", value="opencode")]

        class ManualSessionModal(discord.ui.Modal):
            def __init__(self):
                super().__init__(title=t("modal.resume.manualInputTitle"))
                self.session_id = discord.ui.TextInput(
                    label=t("modal.resume.sessionIdLabel"),
                    placeholder=t("modal.resume.pasteIdPlaceholder"),
                    required=True,
                )
                self.add_item(self.session_id)

            async def on_submit(self, submit_interaction: discord.Interaction):
                if not hasattr(self, "_view"):
                    return
                view: ResumeView = getattr(self, "_view")
                view.manual_session = str(self.session_id.value)
                await submit_interaction.response.send_message(
                    f"✅ {t('modal.resume.manualCaptured')}",
                    ephemeral=True,
                )

        class ResumeView(discord.ui.View):
            def __init__(self, outer: DiscordBot, owner_id: Optional[str]):
                super().__init__(timeout=900)
                self.outer = outer
                self.owner_id = owner_id
                self.manual_session: Optional[str] = None
                self.session_select = discord.ui.Select(
                    placeholder=t("modal.resume.selectSession"),
                    options=options,
                    min_values=1,
                    max_values=1,
                )
                self.agent_select = discord.ui.Select(
                    placeholder=t("modal.resume.selectAgentBackend"),
                    options=agent_options,
                    min_values=1,
                    max_values=1,
                )
                self.manual_button = discord.ui.Button(
                    label=t("modal.resume.manualInputButton"),
                    style=discord.ButtonStyle.secondary,
                )
                self.resume_button = discord.ui.Button(label=t("common.resume"), style=discord.ButtonStyle.primary)

                async def _defer(interaction: discord.Interaction):
                    await interaction.response.defer()

                self.session_select.callback = _defer
                self.agent_select.callback = _defer
                self.add_item(self.session_select)
                self.add_item(self.agent_select)
                self.add_item(self.manual_button)
                self.add_item(self.resume_button)

        owner_id = str(interaction.user.id) if interaction else None
        view = ResumeView(self, owner_id)

        async def manual_callback(manual_interaction: discord.Interaction):
            modal = ManualSessionModal()
            modal._view = view
            await manual_interaction.response.send_modal(modal)

        async def resume_callback(resume_interaction: discord.Interaction):
            try:
                if not resume_interaction.response.is_done():
                    await resume_interaction.response.defer(ephemeral=True)
            except Exception as err:
                logger.debug("Failed to defer Discord resume interaction: %s", err)

            selected = view.session_select.values[0] if view.session_select.values else None
            chosen_agent = view.agent_select.values[0] if view.agent_select.values else None
            chosen_session = None
            if selected and selected != "__none__" and "|" in selected:
                chosen_agent, chosen_session = selected.split("|", 1)
            if view.manual_session:
                chosen_session = view.manual_session
            if hasattr(self, "_on_resume_session"):
                await self._on_resume_session(
                    str(resume_interaction.user.id),
                    channel_id,
                    thread_id,
                    chosen_agent,
                    chosen_session,
                    host_message_ts,
                    resume_interaction.guild is None,
                )
            try:
                await self._dismiss_interaction_message(resume_interaction, " ")
            except Exception as err:
                logger.debug("Failed to dismiss Discord resume message: %s", err)

        view.manual_button.callback = manual_callback
        view.resume_button.callback = resume_callback

        intro_text = "\n".join(
            [
                f"⏮️ {t('modal.resume.title')}",
                t("modal.resume.chooseOneOf"),
                (
                    t("modal.resume.discordPickOrPaste")
                    if has_recent_sessions
                    else t("modal.resume.noSessionsFound")
                ),
            ]
        )

        if interaction:
            await interaction.response.send_message(intro_text, view=view, ephemeral=True)
        else:
            channel = await self._fetch_channel(channel_id)
            if channel is None:
                raise RuntimeError("Discord channel not found")
            await channel.send(intro_text, view=view)

    async def open_routing_modal(
        self,
        trigger_id: Any,
        channel_id: str,
        registered_backends: list,
        current_backend: str,
        current_routing: Any,
        opencode_agents: list,
        opencode_models: dict,
        opencode_default_config: dict,
        claude_agents: list,
        claude_models: list,
        codex_agents: list,
        codex_models: list,
    ):
        interaction = trigger_id if isinstance(trigger_id, discord.Interaction) else None

        backend_display_names = {
            "claude": "ClaudeCode",
            "codex": "Codex",
            "opencode": "OpenCode",
        }

        def _prefixed_label(prefix_key: str, label: str, limit: int = 100) -> str:
            prefix = self._t(prefix_key)
            combined = f"{prefix}: {label}" if prefix else label
            if len(combined) > limit:
                return combined[:limit]
            return combined

        def _normalize_agent_name(agent: Any) -> Optional[str]:
            if isinstance(agent, str):
                return agent
            if isinstance(agent, dict):
                for key in ("name", "id", "label", "agent"):
                    value = agent.get(key)
                    if isinstance(value, str) and value:
                        return value
            return None

        def _unique_agent_names(agents: list) -> list:
            seen = set()
            names = []
            for agent in agents or []:
                name = _normalize_agent_name(agent)
                if not name or name in seen:
                    continue
                seen.add(name)
                names.append(name)
            return names

        class RoutingView(discord.ui.View):
            def __init__(self, outer: DiscordBot, owner_id: Optional[str]):
                super().__init__(timeout=900)
                self.outer = outer
                self.owner_id = owner_id
                self.step = "backend"
                self.selected_backend = current_backend or (
                    registered_backends[0] if registered_backends else "opencode"
                )
                stored_backend = getattr(current_routing, "agent_backend", None) if current_routing else None
                canonical_model = getattr(current_routing, "model", None) if current_routing else None
                canonical_reasoning = getattr(current_routing, "reasoning_effort", None) if current_routing else None

                def _canonical_applies_to_backend(backend: str) -> bool:
                    if stored_backend:
                        return stored_backend == backend
                    return backend == (current_backend or "opencode")

                def _current_model(field_name: str, backend: str) -> Optional[str]:
                    value = getattr(current_routing, field_name, None) if current_routing else None
                    if value is not None:
                        return value
                    if self.selected_backend == backend and _canonical_applies_to_backend(backend):
                        return canonical_model
                    return None

                def _current_reasoning(field_name: str, backend: str) -> Optional[str]:
                    value = getattr(current_routing, field_name, None) if current_routing else None
                    if value is not None:
                        return value
                    if self.selected_backend == backend and _canonical_applies_to_backend(backend):
                        return canonical_reasoning
                    return None

                self.oc_agent = getattr(current_routing, "opencode_agent", None) if current_routing else None
                self.oc_model = _current_model("opencode_model", "opencode")
                self.oc_reasoning = _current_reasoning("opencode_reasoning_effort", "opencode")
                self.claude_agent = getattr(current_routing, "claude_agent", None) if current_routing else None
                self.claude_model = _current_model("claude_model", "claude")
                self.claude_reasoning = _current_reasoning("claude_reasoning_effort", "claude")
                self.codex_agent = getattr(current_routing, "codex_agent", None) if current_routing else None
                self.codex_model = _current_model("codex_model", "codex")
                self.codex_reasoning = _current_reasoning("codex_reasoning_effort", "codex")
                self._render()

            def _render(self):
                self.clear_items()
                options = []
                for backend in registered_backends:
                    display = backend_display_names.get(backend, backend.capitalize())
                    label = _prefixed_label("discord.labels.backend", display)
                    options.append(
                        discord.SelectOption(
                            label=label,
                            value=backend,
                            default=backend == self.selected_backend,
                        )
                    )
                if len(options) > 25:
                    options = options[:25]
                backend_select = discord.ui.Select(
                    placeholder=self.outer._t("modal.routing.selectBackend"),
                    options=options,
                    min_values=1,
                    max_values=1,
                )

                async def backend_callback(select_interaction: discord.Interaction):
                    if backend_select.values:
                        self.selected_backend = backend_select.values[0]
                    self._render()
                    updated_embed = discord.Embed(
                        title=self._content(),
                        description=self.outer._t("discord.routingSubtitle"),
                    )
                    await select_interaction.response.edit_message(embed=updated_embed, view=self)

                backend_select.callback = backend_callback
                self.add_item(backend_select)

                if self.selected_backend == "opencode":
                    opencode_agent_names = _unique_agent_names(opencode_agents)
                    default_model_str = resolve_opencode_default_model(
                        opencode_default_config,
                        opencode_agents,
                        self.oc_agent if self.oc_agent not in ("__default__", None) else None,
                    )
                    target_model = self.oc_model if self.oc_model not in (None, "__default__") else default_model_str
                    preferred_providers = resolve_opencode_provider_preferences(
                        opencode_default_config,
                        target_model,
                    )
                    allowed_providers = resolve_opencode_allowed_providers(
                        opencode_default_config,
                        opencode_models,
                    )
                    agent_options = [
                        discord.SelectOption(
                            label=_prefixed_label(
                                "discord.labels.opencodeAgent",
                                self.outer._t("common.default"),
                            ),
                            value="__default__",
                            default=self.oc_agent in (None, "__default__"),
                        )
                    ]
                    agent_options += [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.opencodeAgent", a),
                            value=a,
                            default=a == self.oc_agent,
                        )
                        for a in opencode_agent_names
                    ]
                    if len(agent_options) > 25:
                        agent_options = agent_options[:25]
                    agent_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectOpencodeAgent"),
                        options=agent_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def agent_callback(select_interaction: discord.Interaction):
                        if agent_select.values:
                            self.oc_agent = agent_select.values[0]
                        await select_interaction.response.defer()

                    agent_select.callback = agent_callback
                    self.add_item(agent_select)

                    default_label = self.outer._t("common.default")
                    if default_model_str:
                        default_label = f"{default_label} - {default_model_str}"
                    model_options = [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.model", default_label),
                            value="__default__",
                            default=self.oc_model in (None, "__default__"),
                        )
                    ]
                    model_entries = build_opencode_model_option_items(
                        opencode_models,
                        max_total=24,
                        preferred_providers=preferred_providers,
                        allowed_providers=allowed_providers,
                    )
                    for entry in model_entries:
                        label = entry.get("label", "")
                        value = entry.get("value", "")
                        if not label or not value:
                            continue
                        model_options.append(
                            discord.SelectOption(
                                label=_prefixed_label("discord.labels.model", label),
                                value=value,
                                default=value == self.oc_model,
                            )
                        )
                    model_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectModel"),
                        options=model_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def model_callback(select_interaction: discord.Interaction):
                        if model_select.values:
                            self.oc_model = model_select.values[0]
                        self._render()
                        updated_embed = discord.Embed(
                            title=self._content(),
                            description=self.outer._t("discord.routingSubtitle"),
                        )
                        await select_interaction.response.edit_message(embed=updated_embed, view=self)

                    model_select.callback = model_callback
                    self.add_item(model_select)

                    reasoning_entries = build_reasoning_effort_options(opencode_models, target_model)
                    selected_reasoning = (
                        self.oc_reasoning if self.oc_reasoning not in (None, "__default__") else "__default__"
                    )
                    available_reasoning = {entry.get("value") for entry in reasoning_entries}
                    if selected_reasoning not in available_reasoning:
                        selected_reasoning = "__default__"
                    reasoning_options = []
                    for entry in reasoning_entries:
                        value = entry.get("value")
                        if not value:
                            continue
                        if value == "__default__":
                            label = self.outer._t("common.default")
                        else:
                            translated = self.outer._t(f"reasoning.{value}")
                            label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                        reasoning_options.append(
                            discord.SelectOption(
                                label=_prefixed_label("discord.labels.reasoningEffort", label),
                                value=value,
                                default=value == selected_reasoning,
                            )
                        )
                    reasoning_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectReasoningEffort"),
                        options=reasoning_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def reasoning_callback(select_interaction: discord.Interaction):
                        if reasoning_select.values:
                            self.oc_reasoning = reasoning_select.values[0]
                        await select_interaction.response.defer()

                    reasoning_select.callback = reasoning_callback
                    self.add_item(reasoning_select)

                if self.selected_backend == "claude":
                    claude_agent_names = _unique_agent_names(claude_agents)
                    agent_options = [
                        discord.SelectOption(
                            label=_prefixed_label(
                                "discord.labels.claudeAgent",
                                self.outer._t("common.default"),
                            ),
                            value="__default__",
                            default=self.claude_agent in (None, "__default__"),
                        )
                    ]
                    agent_options += [
                        discord.SelectOption(label=a, value=a, default=a == self.claude_agent)
                        for a in claude_agent_names
                    ]
                    if len(agent_options) > 25:
                        agent_options = agent_options[:25]
                    agent_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectClaudeAgent"),
                        options=agent_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def claude_agent_callback(select_interaction: discord.Interaction):
                        if agent_select.values:
                            self.claude_agent = agent_select.values[0]
                        await select_interaction.response.defer()

                    agent_select.callback = claude_agent_callback
                    self.add_item(agent_select)

                    model_options = [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.model", self.outer._t("common.default")),
                            value="__default__",
                            default=self.claude_model in (None, "__default__"),
                        )
                    ]
                    # Discord select menus cap at 25 options; prioritize the active
                    # pick and the bare aliases so the truncation below only trims the
                    # long tail of dated snapshots (see _prioritize_claude_model_choices).
                    model_options += [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.model", format_claude_model_label(m)),
                            value=m,
                            default=m == self.claude_model,
                        )
                        for m in _prioritize_claude_model_choices(claude_models, self.claude_model)
                    ]
                    if len(model_options) > 25:
                        model_options = model_options[:25]
                    model_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectModel"),
                        options=model_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def claude_model_callback(select_interaction: discord.Interaction):
                        if model_select.values:
                            self.claude_model = model_select.values[0]
                            self.claude_reasoning = None
                        self._render()
                        updated_embed = discord.Embed(
                            title=self._content(),
                            description=self.outer._t("discord.routingSubtitle"),
                        )
                        await select_interaction.response.edit_message(embed=updated_embed, view=self)

                    model_select.callback = claude_model_callback
                    self.add_item(model_select)

                    claude_reasoning_entries = build_claude_reasoning_options(
                        self.claude_model if self.claude_model not in (None, "__default__") else None
                    )
                    selected_cl_reasoning = (
                        self.claude_reasoning if self.claude_reasoning not in (None, "__default__") else "__default__"
                    )
                    available_cl_reasoning = {entry.get("value") for entry in claude_reasoning_entries}
                    if selected_cl_reasoning not in available_cl_reasoning:
                        selected_cl_reasoning = "__default__"
                    reasoning_options = []
                    for entry in claude_reasoning_entries:
                        value = entry.get("value")
                        if not value:
                            continue
                        if value == "__default__":
                            label = self.outer._t("common.default")
                        else:
                            translated = self.outer._t(f"reasoning.{value}")
                            label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                        reasoning_options.append(
                            discord.SelectOption(
                                label=_prefixed_label("discord.labels.reasoningEffort", label),
                                value=value,
                                default=value == selected_cl_reasoning,
                            )
                        )
                    reasoning_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectReasoningEffort"),
                        options=reasoning_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def claude_reasoning_callback(select_interaction: discord.Interaction):
                        if reasoning_select.values:
                            self.claude_reasoning = reasoning_select.values[0]
                        await select_interaction.response.defer()

                    reasoning_select.callback = claude_reasoning_callback
                    self.add_item(reasoning_select)

                if self.selected_backend == "codex":
                    codex_agent_names = _unique_agent_names(codex_agents)
                    agent_options = [
                        discord.SelectOption(
                            label=_prefixed_label(
                                "discord.labels.codexAgent",
                                self.outer._t("common.default"),
                            ),
                            value="__default__",
                            default=self.codex_agent in (None, "__default__"),
                        )
                    ]
                    agent_options += [
                        discord.SelectOption(label=a, value=a, default=a == self.codex_agent)
                        for a in codex_agent_names
                    ]
                    if len(agent_options) > 25:
                        agent_options = agent_options[:25]
                    agent_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectCodexAgent"),
                        options=agent_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def codex_agent_callback(select_interaction: discord.Interaction):
                        if agent_select.values:
                            self.codex_agent = agent_select.values[0]
                        await select_interaction.response.defer()

                    agent_select.callback = codex_agent_callback
                    self.add_item(agent_select)

                    model_options = [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.model", self.outer._t("common.default")),
                            value="__default__",
                            default=self.codex_model in (None, "__default__"),
                        )
                    ]
                    model_options += [
                        discord.SelectOption(
                            label=_prefixed_label("discord.labels.model", m),
                            value=m,
                            default=m == self.codex_model,
                        )
                        for m in codex_models
                    ]
                    if len(model_options) > 25:
                        model_options = model_options[:25]
                    model_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectModel"),
                        options=model_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def codex_model_callback(select_interaction: discord.Interaction):
                        if model_select.values:
                            self.codex_model = model_select.values[0]
                        await select_interaction.response.defer()

                    model_select.callback = codex_model_callback
                    self.add_item(model_select)

                    codex_reasoning_entries = build_codex_reasoning_options()
                    selected_cx_reasoning = (
                        self.codex_reasoning if self.codex_reasoning not in (None, "__default__") else "__default__"
                    )
                    available_cx_reasoning = {entry.get("value") for entry in codex_reasoning_entries}
                    if selected_cx_reasoning not in available_cx_reasoning:
                        selected_cx_reasoning = "__default__"
                    reasoning_options = []
                    for entry in codex_reasoning_entries:
                        value = entry.get("value")
                        if not value:
                            continue
                        if value == "__default__":
                            label = self.outer._t("common.default")
                        else:
                            translated = self.outer._t(f"reasoning.{value}")
                            label = translated if translated != f"reasoning.{value}" else entry.get("label", value)
                        reasoning_options.append(
                            discord.SelectOption(
                                label=_prefixed_label("discord.labels.reasoningEffort", label),
                                value=value,
                                default=value == selected_cx_reasoning,
                            )
                        )
                    reasoning_select = discord.ui.Select(
                        placeholder=self.outer._t("modal.routing.selectReasoningEffort"),
                        options=reasoning_options,
                        min_values=1,
                        max_values=1,
                    )

                    async def codex_reasoning_callback(select_interaction: discord.Interaction):
                        if reasoning_select.values:
                            self.codex_reasoning = reasoning_select.values[0]
                        await select_interaction.response.defer()

                    reasoning_select.callback = codex_reasoning_callback
                    self.add_item(reasoning_select)

                save_button = discord.ui.Button(
                    label=self.outer._t("common.save"),
                    style=discord.ButtonStyle.primary,
                )
                save_button.callback = self._on_save
                self.add_item(save_button)

            def _content(self) -> str:
                return f"🤖 {self.outer._t('modal.routing.title')}"

            async def _on_save(self, interaction: discord.Interaction):
                def _normalize(value: Optional[str]) -> Optional[str]:
                    if value in (None, "__default__"):
                        return None
                    return value

                try:
                    await interaction.response.defer()
                    # Re-check auth before saving (defense-in-depth)
                    save_uid = str(interaction.user.id)
                    save_cid = channel_id or str(interaction.channel_id or "")
                    save_is_dm = interaction.guild is None
                    auth = self.outer.check_authorization(
                        user_id=save_uid,
                        channel_id=save_cid,
                        is_dm=save_is_dm,
                        action="cmd_routing",
                        settings_manager=self.outer.settings_manager,
                    )
                    if not auth.allowed:
                        await self.outer._send_auth_denial(save_cid, save_uid, auth, interaction=interaction)
                        return

                    if hasattr(self.outer, "_on_routing_update"):
                        await self.outer._on_routing_update(
                            str(interaction.user.id),
                            channel_id,
                            self.selected_backend,
                            _normalize(self.oc_agent),
                            _normalize(self.oc_model),
                            _normalize(self.oc_reasoning),
                            _normalize(self.claude_agent),
                            _normalize(self.claude_model),
                            _normalize(self.claude_reasoning),
                            _normalize(self.codex_agent),
                            _normalize(self.codex_model),
                            _normalize(self.codex_reasoning),
                            notify_user=True,
                            is_dm=interaction.guild is None,
                        )
                    await self.outer._dismiss_interaction_message(
                        interaction,
                        f"✅ {self.outer._t('common.submitted')}",
                    )
                except Exception as err:
                    await interaction.edit_original_response(
                        content=f"❌ {self.outer._t('error.routingUpdateFailed', error=str(err))}",
                        embed=None,
                        view=None,
                    )

        owner_id = str(interaction.user.id) if interaction else None
        view = RoutingView(self, owner_id)

        routing_embed = discord.Embed(
            title=view._content(),
            description=self._t("discord.routingSubtitle"),
        )
        if interaction:
            await interaction.response.send_message(embed=routing_embed, view=view, ephemeral=True)
        else:
            channel = await self._fetch_channel(channel_id)
            if channel is None:
                raise RuntimeError("Discord channel not found")
            await channel.send(embed=routing_embed, view=view)

    async def open_question_modal(
        self,
        trigger_id: Any,
        context: MessageContext,
        pending: Any,
        callback_prefix: str = "claude_question",
    ):
        interaction = trigger_id if isinstance(trigger_id, discord.Interaction) else None
        if isinstance(pending, dict):
            questions = pending.get("questions") or []
        else:
            questions = getattr(pending, "questions", None) or []
        if not questions or len(questions) > 4:
            await self.send_message(
                context,
                "Too many questions for Discord UI. Please reply with a custom message.",
            )
            return

        def _normalize_question(raw: Any) -> tuple[str, list, bool]:
            if isinstance(raw, dict):
                header = raw.get("header") or raw.get("question") or raw.get("title") or ""
                options = raw.get("options") or []
                multiple = bool(raw.get("multiple"))
                return str(header), options, multiple
            header = getattr(raw, "header", None) or getattr(raw, "question", None) or ""
            options = getattr(raw, "options", None) or []
            multiple = bool(getattr(raw, "multiple", False))
            return str(header), options, multiple

        def _normalize_option(option: Any) -> str:
            if isinstance(option, dict):
                label = option.get("label") or option.get("value") or option.get("name")
                if label is not None:
                    return str(label)
            return str(option)

        class QuestionView(discord.ui.View):
            def __init__(self, outer: DiscordBot):
                super().__init__(timeout=900)
                self.outer = outer
                self.answers: list[list[str]] = [[] for _ in questions]
                for idx, q in enumerate(questions):
                    header, options_raw, multiple = _normalize_question(q)
                    option_labels = [_normalize_option(opt) for opt in options_raw]
                    option_labels = [label for label in option_labels if label]
                    if len(option_labels) > 25:
                        option_labels = option_labels[:25]
                    options = [discord.SelectOption(label=label[:100], value=label[:100]) for label in option_labels]
                    max_values = len(options) if multiple else 1
                    select = discord.ui.Select(
                        placeholder=header or f"Question {idx + 1}",
                        options=options,
                        min_values=1,
                        max_values=max_values,
                    )

                    async def make_callback(select_interaction: discord.Interaction, i=idx, sel=select):
                        self.answers[i] = list(sel.values)
                        await select_interaction.response.defer()

                    select.callback = make_callback
                    self.add_item(select)
                self.add_item(discord.ui.Button(label="Submit", style=discord.ButtonStyle.primary))

        view = QuestionView(self)

        async def submit_callback(submit_interaction: discord.Interaction):
            payload = {"answers": view.answers}
            if self.on_callback_query_callback:
                callback_data = f"{callback_prefix}:modal:" + json.dumps(payload)
                ctx = MessageContext(
                    user_id=str(submit_interaction.user.id),
                    channel_id=context.channel_id,
                    thread_id=context.thread_id,
                    message_id=context.message_id,
                    platform_specific={
                        "interaction": submit_interaction,
                        "is_dm": (context.platform_specific or {}).get("is_dm", False),
                    },
                )
                await self.on_callback_query_callback(ctx, callback_data)
            await submit_interaction.response.edit_message(content="✅ Answer submitted.", view=None)

        for item in view.children:
            if isinstance(item, discord.ui.Button) and item.label == "Submit":
                item.callback = submit_callback

        if interaction:
            if interaction.response.is_done():
                await interaction.followup.send("Please answer:", view=view, ephemeral=True)
            else:
                await interaction.response.send_message("Please answer:", view=view, ephemeral=True)
        else:
            channel = await self._fetch_channel(context.thread_id or context.channel_id)
            if channel is None:
                raise RuntimeError("Discord channel not found")
            await channel.send("Please answer:", view=view)

class _PersistentStartView(discord.ui.View):
    """Persistent view for /start menu buttons.

    Survives bot restarts because:
    1. timeout=None
    2. All buttons have explicit static custom_ids
    3. Registered via client.add_view() on startup
    """

    # Static custom_ids used by the /start menu
    KNOWN_IDS = frozenset(
        {
            "cmd_cwd",
            "cmd_change_cwd",
            "cmd_new",
            "cmd_clear",
            "cmd_settings",
            "cmd_resume",
            "cmd_routing",
            "info_how_it_works",
        }
    )

    def __init__(self, outer: "DiscordBot", keyboard: Optional[InlineKeyboard] = None):
        super().__init__(timeout=None)
        self.outer = outer
        if keyboard is not None:
            for row_idx, row in enumerate(keyboard.buttons):
                for button in row:
                    item = discord.ui.Button(
                        label=button.text,
                        style=discord.ButtonStyle.secondary,
                        custom_id=button.callback_data,
                        row=row_idx,
                    )
                    item.callback = self._make_callback(button.callback_data)
                    self.add_item(item)
        else:
            # Skeleton mode: register callbacks for known IDs so that
            # interactions on old messages are routed correctly after restart.
            for cid in sorted(self.KNOWN_IDS):
                item = discord.ui.Button(
                    label=cid,  # label is ignored for persistent views
                    style=discord.ButtonStyle.secondary,
                    custom_id=cid,
                )
                item.callback = self._make_callback(cid)
                self.add_item(item)

    def _make_callback(self, data: str):
        async def on_click(interaction: discord.Interaction):
            needs_modal = data.endswith(":open_modal") or data in {
                "cmd_change_cwd",
                "cmd_settings",
                "cmd_routing",
                "cmd_resume",
            }
            if not needs_modal:
                try:
                    await interaction.response.defer(ephemeral=True)
                except Exception:
                    pass

            if not self.outer._mark_interaction_seen(interaction, data):
                logger.info("Ignoring duplicate Discord interaction: %s", data)
                return

            context = self.outer._build_interaction_context(interaction)
            if context is None:
                return
            auth_result = self.outer.check_authorization(
                user_id=context.user_id,
                channel_id=context.channel_id,
                is_dm=bool((context.platform_specific or {}).get("is_dm", False)),
                action=data,
                settings_manager=self.outer.settings_manager,
            )
            if not auth_result.allowed:
                await self.outer._send_auth_denial(
                    context.channel_id, context.user_id, auth_result, interaction=interaction
                )
                return

            if needs_modal:
                await self.outer._dispatch_callback_query(context, data)
            else:
                self.outer._spawn_callback_query_task(context, data)

        return on_click

    @staticmethod
    def is_all_static(keyboard: InlineKeyboard) -> bool:
        """Return True if every button in *keyboard* uses a known static custom_id."""
        for row in keyboard.buttons:
            for button in row:
                if button.callback_data not in _PersistentStartView.KNOWN_IDS:
                    return False
        return True


class _DiscordButtonView(discord.ui.View):
    """Non-persistent view for dynamic buttons (update prompts, question modals, etc.)."""

    def __init__(
        self,
        outer: DiscordBot,
        base_context: MessageContext,
        keyboard: InlineKeyboard,
        owner_id: Optional[str] = None,
    ):
        super().__init__(timeout=900)
        self.outer = outer
        self.base_context = base_context
        self.owner_id = owner_id
        for row_idx, row in enumerate(keyboard.buttons):
            for button in row:
                item = discord.ui.Button(
                    label=button.text,
                    style=discord.ButtonStyle.secondary,
                    custom_id=button.callback_data,
                    row=row_idx,
                )

                async def on_click(interaction: discord.Interaction, data=button.callback_data):
                    needs_modal = data.endswith(":open_modal") or data in {
                        "cmd_change_cwd",
                        "cmd_settings",
                        "cmd_routing",
                        "cmd_resume",
                    }
                    if data.startswith("opencode_question:") or not needs_modal:
                        try:
                            await interaction.response.defer(ephemeral=True)
                        except Exception:
                            pass

                    if not self.outer._mark_interaction_seen(interaction, data):
                        logger.info("Ignoring duplicate Discord interaction: %s", data)
                        return

                    context = self.outer._build_interaction_context(interaction)
                    if context is None:
                        return
                    auth_result = self.outer.check_authorization(
                        user_id=context.user_id,
                        channel_id=context.channel_id,
                        is_dm=bool((context.platform_specific or {}).get("is_dm", False)),
                        action=data,
                        settings_manager=self.outer.settings_manager,
                    )
                    if not auth_result.allowed:
                        await self.outer._send_auth_denial(
                            context.channel_id, context.user_id, auth_result, interaction=interaction
                        )
                        return

                    if needs_modal:
                        await self.outer._dispatch_callback_query(context, data)
                    else:
                        self.outer._spawn_callback_query_task(context, data)

                item.callback = on_click
                self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id and str(interaction.user.id) != self.owner_id:
            return False
        return True
