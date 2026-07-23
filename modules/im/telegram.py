from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from config.v2_config import TelegramConfig
from core import chat_discovery
from core.message_context import resolve_context_scope_settings_key, resolve_context_thread_id
from vibe.i18n import get_supported_languages, t as i18n_t
from vibe.proxy import resolve_proxy
from modules.agents.native_sessions import AgentNativeSessionService, NativeResumeSession
from modules.agents.opencode.utils import format_claude_model_label

from .base import BaseIMClient, FileAttachment, MessageContext, InlineButton, InlineKeyboard
from .formatters import TelegramFormatter
from . import telegram_api

logger = logging.getLogger(__name__)


@dataclass
class _TelegramCwdPrompt:
    message_id: str
    current_cwd: str


@dataclass
class _TelegramResumeSessionState:
    message_id: str
    options: list[tuple[str, str]]
    is_dm: bool
    thread_id: Optional[str]


@dataclass
class _TelegramRoutingState:
    message_id: str
    channel_id: str
    thread_id: Optional[str]
    user_id: str
    is_dm: bool
    registered_backends: list[str]
    opencode_agents: list[Any]
    opencode_models: dict[str, Any]
    opencode_default_config: dict[str, Any]
    claude_agents: list[Any]
    claude_models: list[Any]
    codex_models: list[Any]
    backend_reasoning_options: dict[str, dict[str, list[dict[str, str]]]]
    backend: str
    opencode_agent: Optional[str] = None
    opencode_model: Optional[str] = None
    opencode_reasoning_effort: Optional[str] = None
    claude_agent: Optional[str] = None
    claude_model: Optional[str] = None
    claude_reasoning_effort: Optional[str] = None
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None
    picker_field: Optional[str] = None
    picker_page: int = 0


@dataclass
class _TelegramQuestionState:
    message_id: str
    callback_prefix: str
    questions: list[Any]
    answers: list[list[str]]
    index: int = 0


@dataclass
class _TelegramSettingsState:
    message_id: str
    show_message_types: list[str]
    current_require_mention: Optional[bool]
    global_require_mention: bool
    current_language: str
    is_dm: bool
    thread_id: Optional[str]


@dataclass
class _TelegramUpdateScopeGate:
    lock: asyncio.Lock
    waiters: int = 0


class TelegramBot(BaseIMClient):
    """Telegram adapter using Bot API long polling."""

    _MAX_IN_FLIGHT_UPDATE_TASKS = 100
    _MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS = 100
    _COMMAND_MENU = (
        ("start", "telegram.commandMenu.start"),
        ("new", "telegram.commandMenu.new"),
        ("cwd", "telegram.commandMenu.cwd"),
        ("setcwd", "telegram.commandMenu.setcwd"),
        ("resume", "telegram.commandMenu.resume"),
        ("setup", "telegram.commandMenu.setup"),
        ("settings", "telegram.commandMenu.settings"),
        ("stop", "telegram.commandMenu.stop"),
    )
    _RICH_MARKDOWN_BLOCK_RE = re.compile(
        r"(?m)^(?:#{1,6}\s+\S|[-*+]\s+\S|\d+\.\s+\S|>\s?\S|---\s*$|\|.*\|\s*$|\$\$)"
        r"|```|<details\b|<tg-|!\[[^\]]*\]\(\s*<?https?://",
        re.IGNORECASE,
    )
    _REMOTE_MARKDOWN_IMAGE_RE = re.compile(
        r"!\[([^\]]*)\]\(\s*(?:<(?P<angle_url>https?://[^>\n]+)>|"
        r"(?P<bare_url>https?://(?:[^()\\\s]|\([^()]*\))+))"
        r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)",
        re.IGNORECASE,
    )
    _REMOTE_MARKDOWN_LINKED_IMAGE_RE = re.compile(
        r"\[!\[([^\]]*)\]\(\s*(?:<(?P<image_angle_url>https?://[^>\n]+)>|"
        r"(?P<image_bare_url>https?://(?:[^()\\\s]|\([^()]*\))+))"
        r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)\]"
        r"\(\s*(?P<link_destination><[^>\n]+>|(?:[^()\\\s]|\([^()]*\))+)"
        r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)",
        re.IGNORECASE,
    )
    _MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
        r"^[ \t]{0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?:<(?P<angle_url>https?://[^>\n]+)>|"
        r"(?P<bare_url>https?://\S+))",
        re.IGNORECASE,
    )
    _MARKDOWN_REFERENCE_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\[(?P<label>[^\]]+)\]")
    _MARKDOWN_REFERENCE_LINKED_IMAGE_RE = re.compile(
        r"\[!\[([^\]]*)\]\(\s*(?:<(?P<image_angle_url>https?://[^>\n]+)>|"
        r"(?P<image_bare_url>https?://(?:[^()\\\s]|\([^()]*\))+))"
        r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)\]\[(?P<label>[^\]]+)\]",
        re.IGNORECASE,
    )
    _MARKDOWN_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
    _MARKDOWN_LIST_ITEM_RE = re.compile(r"^(?P<indent>[ \t]*)(?:[-*+]|\d+\.)(?P<marker_space>\s+)")

    def __init__(self, config: TelegramConfig):
        super().__init__(config)
        self.config = config
        self.formatter = TelegramFormatter()
        self.settings_manager = None
        self.sessions = None
        self._controller = None
        self._stop_event = threading.Event()
        self._offset: Optional[int] = None
        self._bot_user: Optional[dict[str, Any]] = None
        self._on_ready: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_task: Optional[asyncio.Task[Any]] = None
        self._command_menu_task: Optional[asyncio.Task[Any]] = None
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._message_callback_tasks: set[asyncio.Task[Any]] = set()
        self._update_scope_gates: dict[str, _TelegramUpdateScopeGate] = {}
        self._cwd_prompts: dict[str, _TelegramCwdPrompt] = {}
        self._resume_states: dict[str, _TelegramResumeSessionState] = {}
        self._routing_states: dict[str, _TelegramRoutingState] = {}
        self._question_states: dict[str, _TelegramQuestionState] = {}
        self._settings_states: dict[str, _TelegramSettingsState] = {}
        self._rich_markdown_supported: Optional[bool] = None

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
        if "on_ready" in kwargs:
            self._on_ready = kwargs["on_ready"]

    def _t(self, key: str, **kwargs) -> str:
        lang = "en"
        if self._controller and hasattr(self._controller, "_get_lang"):
            lang = self._controller._get_lang()
        return i18n_t(key, lang, **kwargs)

    @property
    def _proxy_url(self) -> Optional[str]:
        return resolve_proxy(self.config.proxy_url)

    def get_default_parse_mode(self) -> Optional[str]:
        return "HTML"

    def should_use_thread_for_reply(self) -> bool:
        return True

    def should_use_message_id_for_channel_session(self, context: Optional[MessageContext] = None) -> bool:
        return False

    def format_markdown(self, text: str) -> str:
        return self.formatter.render(text)

    def register_handlers(self):
        return None

    def run(self):
        if not self.config.bot_token:
            raise ValueError("Telegram bot token is required")
        self._stop_event.clear()
        asyncio.run(self._run())

    def stop(self):
        self._stop_event.set()
        loop = self._loop
        poll_task = self._poll_task
        if loop is not None and loop.is_running() and poll_task is not None and not poll_task.done():
            loop.call_soon_threadsafe(poll_task.cancel)

    async def shutdown(self) -> None:
        self.stop()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._bot_user = (await telegram_api.get_me(self.config.bot_token, proxy_url=self._proxy_url)).get("result")
            logger.info("Telegram bot connected as @%s", self._bot_user.get("username") if self._bot_user else "unknown")
            self._command_menu_task = asyncio.create_task(self._sync_command_menu())
            if self._on_ready:
                await self._on_ready()

            while not self._stop_event.is_set():
                try:
                    self._poll_task = asyncio.create_task(telegram_api.get_updates(
                        self.config.bot_token,
                        self._offset,
                        proxy_url=self._proxy_url,
                    ))
                    try:
                        updates = await self._poll_task
                    except asyncio.CancelledError:
                        if self._stop_event.is_set():
                            break
                        raise
                    finally:
                        self._poll_task = None
                    for update in updates.get("result", []):
                        await self._wait_for_update_capacity()
                        self._offset = int(update["update_id"]) + 1
                        self._spawn_update_task(update)
                except Exception as err:
                    if self._stop_event.is_set():
                        break
                    logger.warning("Telegram poll loop error: %s", err, exc_info=True)
                    await asyncio.sleep(2)
        finally:
            self._poll_task = None
            command_menu_task = self._command_menu_task
            self._command_menu_task = None
            if command_menu_task is not None:
                if not command_menu_task.done():
                    command_menu_task.cancel()
                await asyncio.gather(command_menu_task, return_exceptions=True)
            await self._drain_background_tasks()

    def _build_command_menu(self, language: str) -> list[dict[str, str]]:
        return [
            {"command": command, "description": i18n_t(key, language)}
            for command, key in self._COMMAND_MENU
        ]

    async def _sync_command_menu(self) -> None:
        registrations: list[tuple[Optional[str], str]] = [(None, "en")]
        registrations.extend(
            (language, language)
            for language in get_supported_languages()
            if re.fullmatch(r"[a-z]{2}", language)
        )

        for language_code, translation_language in registrations:
            try:
                await telegram_api.set_my_commands(
                    self.config.bot_token,
                    self._build_command_menu(translation_language),
                    language_code=language_code,
                    proxy_url=self._proxy_url,
                )
            except Exception as err:
                label = language_code or "default"
                logger.warning("Failed to sync Telegram command menu for %s: %s", label, err)

        try:
            await telegram_api.set_chat_menu_button(
                self.config.bot_token,
                menu_button={"type": "commands"},
                proxy_url=self._proxy_url,
            )
        except Exception as err:
            logger.warning("Failed to enable Telegram command menu button: %s", err)

    def _spawn_update_task(self, update: dict[str, Any]) -> None:
        scope_key = self._extract_update_scope_key(update)
        if scope_key:
            task = asyncio.create_task(self._handle_scoped_update(update, scope_key))
        else:
            task = asyncio.create_task(self._handle_update(update))
        self._update_tasks.add(task)
        task.add_done_callback(self._handle_update_task_done)

    async def _handle_scoped_update(self, update: dict[str, Any], scope_key: str) -> None:
        gate = self._update_scope_gates.get(scope_key)
        if gate is None:
            gate = _TelegramUpdateScopeGate(lock=asyncio.Lock())
            self._update_scope_gates[scope_key] = gate
        gate.waiters += 1
        try:
            async with gate.lock:
                await self._handle_update(update)
        finally:
            gate.waiters -= 1
            if gate.waiters == 0 and self._update_scope_gates.get(scope_key) is gate:
                self._update_scope_gates.pop(scope_key, None)

    def _handle_update_task_done(self, task: asyncio.Task[Any]) -> None:
        self._update_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Telegram update task failed")

    async def _wait_for_update_capacity(self) -> None:
        if len(self._update_tasks) < self._MAX_IN_FLIGHT_UPDATE_TASKS:
            return
        pending = tuple(self._update_tasks)
        if pending:
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _wait_for_message_callback_capacity(self) -> None:
        while len(self._message_callback_tasks) >= self._MAX_IN_FLIGHT_MESSAGE_CALLBACK_TASKS:
            pending = tuple(self._message_callback_tasks)
            if not pending:
                return
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    async def _drain_task_set(self, tasks: set[asyncio.Task[Any]]) -> None:
        if not tasks:
            return
        pending = tuple(tasks)
        await asyncio.gather(*pending, return_exceptions=True)

    async def _drain_background_tasks(self) -> None:
        await self._drain_task_set(self._update_tasks)
        await self._drain_task_set(self._message_callback_tasks)

    def _extract_update_scope_key(self, update: dict[str, Any]) -> Optional[str]:
        callback_query = update.get("callback_query") or {}
        if callback_query:
            message = callback_query.get("message") or {}
            chat = message.get("chat") or {}
            from_user = callback_query.get("from") or {}
            return self._raw_interaction_scope_key(chat=chat, from_user=from_user)

        message = update.get("message") or {}
        if message:
            chat = message.get("chat") or {}
            from_user = message.get("from") or {}
            return self._raw_interaction_scope_key(chat=chat, from_user=from_user)

        return None

    def _raw_interaction_scope_key(self, *, chat: dict[str, Any], from_user: dict[str, Any]) -> Optional[str]:
        chat_id = str(chat.get("id") or "").strip()
        user_id = str(from_user.get("id") or "").strip()
        if not chat_id or not user_id:
            return None
        is_dm = chat.get("type") == "private"
        scope = user_id if is_dm else chat_id
        return f"{scope}:{user_id}"

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
            logger.exception("Telegram message callback task failed")

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if update.get("callback_query"):
            await self._handle_callback_query(update["callback_query"])
            return
        message = update.get("message")
        if message:
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if self._controller and hasattr(self._controller, "_refresh_config_from_disk"):
            self._controller._refresh_config_from_disk()

        context = self._build_message_context(message)
        if context is None:
            return

        raw_text = message.get("text") or message.get("caption") or ""
        text = self._normalize_command_text(raw_text)
        if self._is_command_for_other_bot(text):
            return

        explicitly_addressed = self._is_explicitly_addressed(message, text)

        effective_require_mention = self._effective_require_mention(context)

        text = self._strip_leading_bot_mention(message, text)

        if await self._consume_cwd_prompt(context, text):
            return

        if effective_require_mention and not context.platform_specific.get("is_dm", False):
            if not explicitly_addressed:
                return

        denial = self.check_authorization(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=resolve_context_thread_id(context),
            is_dm=bool(context.platform_specific.get("is_dm")),
            text=text,
            settings_manager=self.settings_manager,
        )
        if not denial.allowed:
            denial_text = self.build_auth_denial_text(denial.denial, context.channel_id)
            if denial_text:
                await self.send_message(context, denial_text)
            return

        allow_plain_bind = self.should_allow_plain_bind(
            user_id=context.user_id,
            is_dm=bool(context.platform_specific.get("is_dm")),
            settings_manager=self.settings_manager,
        )
        if await self.dispatch_text_command(context, text, allow_plain_bind=allow_plain_bind):
            return

        original_thread_id = resolve_context_thread_id(context)
        context = await self._maybe_route_to_forum_topic(context, message, text)
        if resolve_context_thread_id(context) != original_thread_id:
            if (
                self._effective_require_mention(context)
                and not context.platform_specific.get("is_dm", False)
                and not explicitly_addressed
            ):
                return
            denial = self.check_authorization(
                user_id=context.user_id,
                channel_id=context.channel_id,
                thread_id=resolve_context_thread_id(context),
                is_dm=bool(context.platform_specific.get("is_dm")),
                text=text,
                settings_manager=self.settings_manager,
            )
            if not denial.allowed:
                denial_text = self.build_auth_denial_text(denial.denial, context.channel_id)
                if denial_text:
                    await self.send_message(context, denial_text)
                return

        await self._spawn_message_callback_task(context, text)

    def _effective_require_mention(self, context: MessageContext) -> bool:
        effective = self.config.require_mention
        if self.settings_manager is None or context.platform_specific.get("is_dm", False):
            return bool(effective)
        try:
            return bool(
                self.settings_manager.get_require_mention(
                    resolve_context_scope_settings_key(context),
                    global_default=self.config.require_mention,
                )
            )
        except Exception:
            logger.debug("Failed to resolve Telegram effective require_mention", exc_info=True)
            return bool(effective)

    async def _maybe_route_to_forum_topic(
        self,
        context: MessageContext,
        message: dict[str, Any],
        text: str,
    ) -> MessageContext:
        if not self._should_auto_create_topic(context, message, text):
            return context

        try:
            new_context = await self.start_new_topic_session(context, seed_text=text, message=message)
            if new_context is not None:
                return new_context
        except Exception as err:
            logger.warning("Telegram forum auto-topic failed, falling back to current topic: %s", err, exc_info=True)
        return context

    def _is_forum_chat(self, context: MessageContext, message: Optional[dict[str, Any]] = None) -> bool:
        payload = context.platform_specific or {}
        chat = (message or {}).get("chat") or {}
        return (
            bool(payload.get("is_forum"))
            or bool(payload.get("is_topic_message"))
            or bool((message or {}).get("is_topic_message"))
            or bool(chat.get("is_forum"))
        )

    def _is_general_forum_context(self, context: MessageContext, message: dict[str, Any]) -> bool:
        if not self._is_forum_chat(context, message):
            return False
        thread_id = str(context.thread_id or message.get("message_thread_id") or "").strip()
        return thread_id in {"", "1"}

    def _has_topic_seed_content(self, context: MessageContext, text: str) -> bool:
        if (text or "").strip():
            return True
        return bool(getattr(context, "files", None))

    def _should_auto_create_topic(self, context: MessageContext, message: dict[str, Any], text: str) -> bool:
        if not self.config.forum_auto_topic:
            return False
        if (context.platform_specific or {}).get("chat_type") != "supergroup":
            return False
        if not self._is_general_forum_context(context, message):
            return False
        if not self._has_topic_seed_content(context, text):
            return False
        if message.get("reply_to_message"):
            return False
        if text.startswith("/"):
            return False
        return True

    def _derive_topic_title(self, text: str, message: dict[str, Any]) -> str:
        first_line = ""
        if text:
            first_line = text.strip().splitlines()[0].strip()
        if first_line.startswith("/"):
            first_line = ""
        if first_line:
            if len(first_line) > 60:
                return first_line[:57].rstrip() + "..."
            return first_line
        sender = (message.get("from") or {}).get("first_name") or "Session"
        return f"{sender} {datetime.now().strftime('%m-%d %H:%M')}"

    async def start_new_topic_session(
        self,
        context: MessageContext,
        *,
        seed_text: str = "",
        message: Optional[dict[str, Any]] = None,
    ) -> Optional[MessageContext]:
        payload = context.platform_specific or {}
        if payload.get("chat_type") != "supergroup":
            return None
        if not context.thread_id and not self._is_forum_chat(context, message):
            return None

        topic_name = self._derive_topic_title(seed_text, message or {})
        created = await telegram_api.create_forum_topic(self.config.bot_token, context.channel_id, topic_name, proxy_url=self._proxy_url)
        topic = created.get("result") or {}
        topic_id = topic.get("message_thread_id")
        if topic_id is None:
            raise RuntimeError("Telegram createForumTopic returned no message_thread_id")

        chat_discovery.remember_thread(
            "telegram",
            context.channel_id,
            str(topic_id),
            name=topic_name,
            native_type="forum_topic",
            metadata={"auto_created": True, "is_general": False},
        )

        topic_context = MessageContext(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=str(topic_id),
            message_id=context.message_id,
            platform="telegram",
            files=context.files,
            platform_specific={
                **payload,
                "is_topic_message": True,
                "is_forum": True,
                "auto_topic_created": True,
                "topic_name": topic_name,
            },
        )

        if self._is_general_forum_context(context, message or {}):
            try:
                await self.send_message(
                    context,
                    self._t("telegram.autoTopicGeneralNotice", topic=topic_name),
                    reply_to=context.message_id,
                )
            except Exception:
                logger.debug("Failed to send Telegram General handoff notice", exc_info=True)

        return topic_context

    async def _handle_callback_query(self, payload: dict[str, Any]) -> None:
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        from_user = payload.get("from") or {}
        if not chat or not from_user:
            return
        self._remember_discovered_chat(chat, message)
        thread_id = message.get("message_thread_id")
        context = MessageContext(
            user_id=str(from_user.get("id")),
            channel_id=str(chat.get("id")),
            thread_id=str(thread_id) if thread_id is not None else None,
            message_id=str(message.get("message_id")),
            platform="telegram",
            platform_specific={
                "is_dm": chat.get("type") == "private",
                "chat_type": chat.get("type"),
                "chat_title": chat.get("title") or chat.get("username"),
                "is_forum": bool(chat.get("is_forum")) or bool(message.get("is_topic_message")),
                "is_topic_message": bool(message.get("is_topic_message")),
                "raw_message": message,
            },
        )
        callback_id = str(payload.get("id"))
        context.platform_specific = {
            **(context.platform_specific or {}),
            "callback_id": callback_id,
            "callback_query": payload,
        }
        callback_data = str(payload.get("data", ""))
        primary_action = self._resolve_callback_action(callback_data)
        is_internal_callback = callback_data.startswith(("tg_cwd:", "tg_resume:", "tg_route:", "tg_question:", "tg_settings:"))
        if is_internal_callback:
            auth_result = self.check_authorization(
                user_id=context.user_id,
                channel_id=context.channel_id,
                thread_id=resolve_context_thread_id(context),
                is_dm=bool(context.platform_specific.get("is_dm")),
                action=primary_action,
                settings_manager=self.settings_manager,
            )
            if not auth_result.allowed:
                denial_text = self.build_auth_denial_text(auth_result.denial, context.channel_id)
                await self.answer_callback(
                    callback_id,
                    denial_text,
                    show_alert=bool(denial_text),
                )
                return
            if await self._handle_internal_callback(context, callback_data):
                await self.answer_callback(callback_id)
                return
        if self.on_callback_query_callback:
            auth_result = self.check_authorization(
                user_id=context.user_id,
                channel_id=context.channel_id,
                thread_id=resolve_context_thread_id(context),
                is_dm=bool(context.platform_specific.get("is_dm")),
                action=primary_action,
                settings_manager=self.settings_manager,
            )
            if not auth_result.allowed:
                denial_text = self.build_auth_denial_text(auth_result.denial, context.channel_id)
                await self.answer_callback(
                    callback_id,
                    denial_text,
                    show_alert=bool(denial_text),
                )
                return
            await self.on_callback_query_callback(context, callback_data)
        await self.answer_callback(callback_id)

    async def _handle_internal_callback(self, context: MessageContext, callback_data: str) -> bool:
        if callback_data.startswith("tg_cwd:"):
            await self._handle_cwd_callback(context, callback_data)
            return True
        if callback_data.startswith("tg_resume:"):
            await self._handle_resume_callback(context, callback_data)
            return True
        if callback_data.startswith("tg_route:"):
            await self._handle_routing_callback(context, callback_data)
            return True
        if callback_data.startswith("tg_settings:"):
            await self._handle_settings_callback(context, callback_data)
            return True
        if callback_data.startswith("tg_question:"):
            await self._handle_question_callback(context, callback_data)
            return True
        return False

    def _build_message_context(self, message: dict[str, Any]) -> Optional[MessageContext]:
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        if not chat or not from_user:
            return None
        self._remember_discovered_chat(chat, message)

        chat_id = str(chat.get("id"))
        user_id = str(from_user.get("id"))
        thread_id = message.get("message_thread_id")
        files = self._extract_files(message)

        return MessageContext(
            user_id=user_id,
            channel_id=chat_id,
            thread_id=str(thread_id) if thread_id is not None else None,
            message_id=str(message.get("message_id")),
            files=files,
            platform="telegram",
            platform_specific={
                "is_dm": chat.get("type") == "private",
                "chat_type": chat.get("type"),
                "chat_title": chat.get("title") or chat.get("username"),
                "is_forum": bool(chat.get("is_forum")),
                "is_topic_message": bool(message.get("is_topic_message")),
                "raw_message": message,
            },
        )

    def _remember_discovered_chat(self, chat: dict[str, Any], message: Optional[dict[str, Any]] = None) -> None:
        try:
            parts = [str(chat.get("first_name") or "").strip(), str(chat.get("last_name") or "").strip()]
            display_name = " ".join(part for part in parts if part).strip()
            name = chat.get("title") or chat.get("username") or display_name or str(chat.get("id") or "")
            chat_type = str(chat.get("type") or "")
            is_topic_message = bool((message or {}).get("is_topic_message"))
            is_forum = bool(chat.get("is_forum")) or is_topic_message
            chat_discovery.remember_chat(
                platform="telegram",
                chat_id=str(chat.get("id")),
                name=name,
                native_type=chat_type,
                is_private=chat_type == "private",
                supports_threads=chat_type == "supergroup" and is_forum,
                metadata={
                    chat_discovery.METADATA_USERNAME: str(chat.get("username") or ""),
                    chat_discovery.METADATA_IS_FORUM: is_forum,
                    chat_discovery.METADATA_SUPPORTS_TOPICS: chat_type == "supergroup" and is_forum,
                },
            )
            topic_id = (message or {}).get("message_thread_id")
            if topic_id is None and is_forum:
                topic_id = 1
            if topic_id is not None and is_forum:
                created = (message or {}).get("forum_topic_created") or {}
                edited = (message or {}).get("forum_topic_edited") or {}
                topic_name = created.get("name") or edited.get("name") or ""
                chat_discovery.remember_thread(
                    "telegram",
                    str(chat.get("id")),
                    str(topic_id),
                    name=str(topic_name or ""),
                    native_type="forum_topic",
                    metadata={"is_general": str(topic_id) == "1"},
                )
        except Exception:
            logger.debug("Failed to remember Telegram discovered chat", exc_info=True)

    def _extract_files(self, message: dict[str, Any]) -> list[FileAttachment]:
        files: list[FileAttachment] = []
        document = message.get("document")
        if document:
            files.append(
                FileAttachment(
                    name=document.get("file_name") or "telegram-document",
                    mimetype=document.get("mime_type") or "application/octet-stream",
                    url=document.get("file_id"),
                    size=document.get("file_size"),
                )
            )
        photo = message.get("photo") or []
        if photo:
            best = photo[-1]
            files.append(
                FileAttachment(
                    name="telegram-photo.jpg",
                    mimetype="image/jpeg",
                    url=best.get("file_id"),
                    size=best.get("file_size"),
                )
            )
        voice = message.get("voice")
        if voice:
            files.append(
                FileAttachment(
                    name="telegram-voice.ogg",
                    mimetype=voice.get("mime_type") or "audio/ogg",
                    url=voice.get("file_id"),
                    size=voice.get("file_size"),
                )
            )
        audio = message.get("audio")
        if audio:
            files.append(
                FileAttachment(
                    name=audio.get("file_name") or "telegram-audio.mp3",
                    mimetype=audio.get("mime_type") or "audio/mpeg",
                    url=audio.get("file_id"),
                    size=audio.get("file_size"),
                )
            )
        return files

    def _normalize_command_text(self, text: str) -> str:
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return stripped
        head, *tail = stripped.split(maxsplit=1)
        command, username = self._split_command_target(head)
        bot_username = str((self._bot_user or {}).get("username") or "")
        if username and bot_username and username.lower() == bot_username.lower():
            head = command
        return " ".join([head, *tail]).strip()

    def _split_command_target(self, head: str) -> tuple[str, str]:
        command, sep, username = str(head or "").partition("@")
        if not sep:
            return command, ""
        return command, username

    def _is_command_for_other_bot(self, text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return False
        head = stripped.split(maxsplit=1)[0]
        _, username = self._split_command_target(head)
        if not username:
            return False
        bot_username = str((self._bot_user or {}).get("username") or "")
        return bool(bot_username) and username.lower() != bot_username.lower()

    def _strip_leading_bot_mention(self, message: dict[str, Any], text: str) -> str:
        stripped = (text or "").strip()
        if not stripped or stripped.startswith("/"):
            return stripped

        username = str((self._bot_user or {}).get("username") or "")
        if not username:
            return stripped

        entities = message.get("entities") or []
        candidate = stripped
        for entity in entities:
            if entity.get("type") != "mention":
                continue
            offset = int(entity.get("offset", 0))
            length = int(entity.get("length", 0))
            if offset != 0 or length <= 0:
                continue
            mention_text = candidate[offset : offset + length]
            if mention_text.lower() != f"@{username.lower()}":
                continue
            remainder = candidate[offset + length :].lstrip(" \t\r\n,:-")
            return remainder.strip()
        return stripped

    def _resolve_callback_action(self, callback_data: str) -> str:
        if callback_data.startswith("tg_cwd:"):
            return "cmd_change_cwd"
        if callback_data.startswith("tg_route:"):
            return "cmd_routing"
        if callback_data.startswith("tg_settings:"):
            return "cmd_settings"
        if callback_data.startswith("toggle_msg_") or callback_data in {"open_settings_modal", "info_msg_types"}:
            return "cmd_settings"
        return callback_data

    def _interaction_scope_key(self, context: MessageContext) -> str:
        payload = context.platform_specific or {}
        is_dm = bool(payload.get("is_dm"))
        scope = context.user_id if is_dm else context.channel_id
        thread_id = resolve_context_thread_id(context)
        thread_suffix = f":{thread_id}" if not is_dm and thread_id is not None else ""
        return f"{scope}:{context.user_id}{thread_suffix}"

    async def _consume_cwd_prompt(self, context: MessageContext, text: str) -> bool:
        prompt = self._cwd_prompts.get(self._interaction_scope_key(context))
        if prompt is None:
            return False
        stripped = text.strip()
        if not stripped:
            return False
        if stripped == "/cancel":
            self._cwd_prompts.pop(self._interaction_scope_key(context), None)
            await self._delete_interaction_message(context, prompt.message_id)
            return True
        known_commands = {
            "start",
            "new",
            "clear",
            "resume",
            "settings",
            "routing",
            "cwd",
            "setcwd",
            "set_cwd",
            "bind",
            "stop",
        }
        parsed_command = self.parse_text_command(stripped, allow_plain_bind=True)
        if parsed_command and parsed_command[0] in known_commands:
            return False
        self._cwd_prompts.pop(self._interaction_scope_key(context), None)
        await self._delete_interaction_message(context, prompt.message_id)
        if self._controller is None or not hasattr(self._controller, "command_handler"):
            await self.send_message(context, f"❌ {self._t('error.cwdChangeFailed')}")
            return True
        await self._controller.command_handler.handle_set_cwd(context, stripped)
        return True

    def _is_explicitly_addressed(self, message: dict[str, Any], text: str) -> bool:
        if text.startswith("/"):
            head = text.split(maxsplit=1)[0]
            _, username = self._split_command_target(head)
            if not username:
                return True
            bot_username = str((self._bot_user or {}).get("username") or "")
            return bool(bot_username) and username.lower() == bot_username.lower()
        reply_to = message.get("reply_to_message") or {}
        reply_from = reply_to.get("from") or {}
        if self._bot_user and str(reply_from.get("id")) == str(self._bot_user.get("id")):
            return True
        username = str((self._bot_user or {}).get("username") or "")
        if not username:
            return False
        entities = message.get("entities") or []
        for entity in entities:
            if entity.get("type") != "mention":
                continue
            offset = int(entity.get("offset", 0))
            length = int(entity.get("length", 0))
            if text[offset : offset + length].lower() == f"@{username.lower()}":
                return True
        return False

    def _build_payload(
        self,
        context: MessageContext,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        *,
        reply_to: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": context.channel_id}
        if context.thread_id:
            payload["message_thread_id"] = int(context.thread_id)
        if text is not None:
            payload["text"] = text
        resolved_parse_mode = self._resolve_parse_mode(parse_mode)
        if text is not None and resolved_parse_mode:
            payload["parse_mode"] = resolved_parse_mode
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to)}
        if keyboard is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.text, "callback_data": button.callback_data} for button in row]
                    for row in keyboard.buttons
                ]
            }
        return payload

    def _build_rich_message_payload(
        self,
        context: MessageContext,
        text: str,
        keyboard: Optional[InlineKeyboard] = None,
        *,
        reply_to: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": context.channel_id,
            "rich_message": {
                "markdown": text,
            },
        }
        if context.thread_id:
            payload["message_thread_id"] = int(context.thread_id)
        if reply_to:
            payload["reply_parameters"] = {"message_id": int(reply_to)}
        if keyboard is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.text, "callback_data": button.callback_data} for button in row]
                    for row in keyboard.buttons
                ]
            }
        return payload

    def _should_send_rich_markdown(self, text: str) -> bool:
        if not text or not text.strip():
            return False
        if self._rich_markdown_supported is False:
            return False
        return bool(self._RICH_MARKDOWN_BLOCK_RE.search(text))

    @staticmethod
    def _is_rich_message_method_unavailable(err: Exception) -> bool:
        description = str(err).lower()
        return "method not found" in description

    @staticmethod
    def _is_rich_message_remote_media_error(err: Exception) -> bool:
        description = str(err)
        return "RICH_MESSAGE_PHOTO_NO_MEDIA_FOUND" in description

    @classmethod
    def _degrade_remote_markdown_images(cls, text: str) -> str:
        reference_urls: dict[str, str] = {}
        for line in text.splitlines():
            match = cls._MARKDOWN_REFERENCE_DEFINITION_RE.match(line)
            if match:
                label = " ".join(match.group("label").casefold().split())
                angle_url = match.group("angle_url")
                reference_urls[label] = f"<{angle_url}>" if angle_url is not None else match.group("bare_url")

        def is_escaped(segment: str, pos: int) -> bool:
            backslashes = 0
            cursor = pos - 1
            while cursor >= 0 and segment[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            return backslashes % 2 == 1

        def replace(match: re.Match[str]) -> str:
            if is_escaped(match.string, match.start()):
                return match.group(0)
            angle_url = match.group("angle_url")
            url = angle_url or match.group("bare_url")
            label = match.group(1).strip() or url
            destination = f"<{angle_url}>" if angle_url is not None else url
            return f"[{label}]({destination})"

        def replace_linked(match: re.Match[str]) -> str:
            if is_escaped(match.string, match.start()):
                return match.group(0)
            image_url = match.group("image_angle_url") or match.group("image_bare_url")
            label = match.group(1).strip() or image_url
            destination = match.group("link_destination")
            return f"[{label}]({destination})"

        def replace_reference_linked(match: re.Match[str]) -> str:
            if is_escaped(match.string, match.start()):
                return match.group(0)
            image_url = match.group("image_angle_url") or match.group("image_bare_url")
            label = match.group(1).strip() or image_url
            destination_label = match.group("label")
            return f"[{label}][{destination_label}]"

        def replace_reference(match: re.Match[str]) -> str:
            if is_escaped(match.string, match.start()):
                return match.group(0)
            destination_label = " ".join(match.group("label").casefold().split())
            destination = reference_urls.get(destination_label)
            if destination is None:
                return match.group(0)
            label = match.group(1).strip() or destination
            return f"[{label}]({destination})"

        def degrade_images(segment: str) -> str:
            segment = cls._MARKDOWN_REFERENCE_LINKED_IMAGE_RE.sub(replace_reference_linked, segment)
            segment = cls._REMOTE_MARKDOWN_LINKED_IMAGE_RE.sub(replace_linked, segment)
            segment = cls._REMOTE_MARKDOWN_IMAGE_RE.sub(replace, segment)
            return cls._MARKDOWN_REFERENCE_IMAGE_RE.sub(replace_reference, segment)

        def replace_outside_inline_code(segment: str) -> str:
            parts: list[str] = []
            pos = 0
            while pos < len(segment):
                if segment[pos] != "`":
                    next_code = segment.find("`", pos)
                    end = len(segment) if next_code == -1 else next_code
                    parts.append(degrade_images(segment[pos:end]))
                    pos = end
                    continue

                marker_end = pos + 1
                while marker_end < len(segment) and segment[marker_end] == "`":
                    marker_end += 1
                marker = segment[pos:marker_end]
                close = segment.find(marker, marker_end)
                if close == -1:
                    parts.append(segment[pos])
                    pos += 1
                    continue
                close_end = close + len(marker)
                parts.append(segment[pos:close_end])
                pos = close_end
            return "".join(parts)

        parts: list[str] = []
        fence_char: Optional[str] = None
        fence_len = 0
        list_content_indent: Optional[int] = None
        for line in text.splitlines(keepends=True):
            fence_match = cls._MARKDOWN_FENCE_RE.match(line)
            if fence_char is not None:
                parts.append(line)
                if fence_match:
                    marker = fence_match.group(1)
                    if marker[0] == fence_char and len(marker) >= fence_len:
                        fence_char = None
                        fence_len = 0
                continue

            if fence_match:
                marker = fence_match.group(1)
                fence_char = marker[0]
                fence_len = len(marker)
                parts.append(line)
                continue

            list_match = cls._MARKDOWN_LIST_ITEM_RE.match(line)
            if list_match:
                list_content_indent = len(line[: list_match.end()].replace("\t", "    "))
            elif line.strip():
                indent = len(line) - len(line.lstrip(" \t"))
                if list_content_indent is not None and indent < list_content_indent:
                    list_content_indent = None

            indent = len(line) - len(line.lstrip(" \t"))
            is_list_continuation = list_content_indent is not None and indent >= list_content_indent
            is_list_code = list_content_indent is not None and indent >= list_content_indent + 4
            if line.startswith(("    ", "\t")) and (not is_list_continuation or is_list_code):
                parts.append(line)
                continue

            parts.append(replace_outside_inline_code(line))
        return "".join(parts)

    async def _send_rich_markdown_payload(
        self,
        context: MessageContext,
        text: str,
        keyboard: Optional[InlineKeyboard] = None,
        *,
        reply_to: Optional[str] = None,
    ) -> str:
        payload = self._build_rich_message_payload(context, text, keyboard=keyboard, reply_to=reply_to)
        result = await telegram_api.call_api(
            self.config.bot_token,
            "sendRichMessage",
            payload,
            proxy_url=self._proxy_url,
        )
        return str(result["result"]["message_id"])

    async def _send_message_with_buttons_payload(
        self,
        context: MessageContext,
        text: str,
        keyboard: InlineKeyboard,
        *,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        payload = self._build_payload(
            context,
            self.format_markdown(text),
            keyboard=keyboard,
            reply_to=reply_to,
            parse_mode=parse_mode,
        )
        result = await telegram_api.call_api(self.config.bot_token, "sendMessage", payload, proxy_url=self._proxy_url)
        return str(result["result"]["message_id"])

    async def send_markdown_message(
        self,
        context: MessageContext,
        text: str,
        keyboard: Optional[InlineKeyboard] = None,
        reply_to: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> str:
        """Send LLM-authored Markdown through Telegram Rich Messages when useful.

        Bot API 10.1 rich messages understand GFM-like block structure. Plain
        short markdown still uses the legacy HTML conversion path because it is
        older, broadly deployed, and sufficient for simple emphasis/links/code.

        ``subtext`` is accepted for the BaseIMClient contract and ignored:
        Telegram has no native de-emphasized footer, so the dispatcher folds any
        footnote into ``text`` for this platform instead.
        """
        if not self._should_send_rich_markdown(text):
            if keyboard is not None:
                return await self._send_message_with_buttons_payload(
                    context,
                    text,
                    keyboard,
                    parse_mode="markdown",
                    reply_to=reply_to,
                )
            return await self.send_message(context, text, parse_mode="markdown", reply_to=reply_to)

        try:
            message_id = await self._send_rich_markdown_payload(context, text, keyboard=keyboard, reply_to=reply_to)
        except Exception as err:
            if self._is_rich_message_method_unavailable(err):
                self._rich_markdown_supported = False
            elif self._is_rich_message_remote_media_error(err):
                degraded = self._degrade_remote_markdown_images(text)
                if degraded != text:
                    try:
                        message_id = await self._send_rich_markdown_payload(
                            context,
                            degraded,
                            keyboard=keyboard,
                            reply_to=reply_to,
                        )
                    except Exception:
                        logger.warning("Telegram sendRichMessage retry without remote image media failed", exc_info=True)
                    else:
                        self._rich_markdown_supported = True
                        return message_id
            logger.warning("Telegram sendRichMessage failed; falling back to sendMessage", exc_info=True)
            if keyboard is not None:
                return await self._send_message_with_buttons_payload(
                    context,
                    text,
                    keyboard,
                    parse_mode="markdown",
                    reply_to=reply_to,
                )
            return await self.send_message(context, text, parse_mode="markdown", reply_to=reply_to)
        self._rich_markdown_supported = True
        return message_id

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> str:
        # ``subtext`` (concise status-bubble footer) is part of the BaseIMClient
        # contract; Telegram has no native footer styling, so it is accepted and
        # ignored (no behavior change).
        payload = self._build_payload(
            context,
            self.format_markdown(text),
            reply_to=reply_to,
            parse_mode=parse_mode,
        )
        result = await telegram_api.call_api(self.config.bot_token, "sendMessage", payload, proxy_url=self._proxy_url)
        return str(result["result"]["message_id"])

    async def send_message_with_buttons(
        self, context: MessageContext, text: str, keyboard: InlineKeyboard, parse_mode: Optional[str] = None
    ) -> str:
        payload = self._build_payload(
            context,
            self.format_markdown(text),
            keyboard=keyboard,
            parse_mode=parse_mode,
        )
        result = await telegram_api.call_api(self.config.bot_token, "sendMessage", payload, proxy_url=self._proxy_url)
        return str(result["result"]["message_id"])

    async def upload_markdown(
        self,
        context: MessageContext,
        title: str,
        content: str,
        filetype: str = "markdown",
    ) -> str:
        suffix = ".md" if filetype == "markdown" else f".{filetype.lstrip('.')}"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=suffix, delete=False) as tmp:
            tmp.write(content or "")
            tmp_path = tmp.name
        try:
            return await self.upload_file_from_path(context, tmp_path, title=title)
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                logger.debug("Failed to clean up temporary markdown file", exc_info=True)

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[InlineKeyboard] = None,
        parse_mode: Optional[str] = None,
        subtext: Optional[str] = None,
    ) -> bool:
        # ``subtext`` is accepted for the BaseIMClient contract and ignored:
        # Telegram has no native de-emphasized footer styling (no behavior change).
        payload = {
            "chat_id": context.channel_id,
            "message_id": int(message_id),
        }
        if keyboard is not None:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": button.text, "callback_data": button.callback_data} for button in row]
                    for row in keyboard.buttons
                ]
            }
        elif text is None:
            payload["reply_markup"] = {"inline_keyboard": []}
        if text is not None:
            payload["text"] = self.format_markdown(text)
            resolved_parse_mode = self._resolve_parse_mode(parse_mode)
            if resolved_parse_mode:
                payload["parse_mode"] = resolved_parse_mode
            if keyboard is None:
                payload["reply_markup"] = {"inline_keyboard": []}
            await telegram_api.call_api(self.config.bot_token, "editMessageText", payload, proxy_url=self._proxy_url)
            return True
        await telegram_api.call_api(self.config.bot_token, "editMessageReplyMarkup", payload, proxy_url=self._proxy_url)
        return True

    def _resolve_parse_mode(self, parse_mode: Optional[str]) -> Optional[str]:
        if not parse_mode:
            return self.get_default_parse_mode()
        if parse_mode.lower() == "markdown":
            return self.get_default_parse_mode()
        return parse_mode

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        payload = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text
        await telegram_api.call_api(self.config.bot_token, "answerCallbackQuery", payload, proxy_url=self._proxy_url)
        return True

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        result = await telegram_api.call_api(self.config.bot_token, "getChat", {"chat_id": user_id}, proxy_url=self._proxy_url)
        chat = result["result"]
        display_name = chat.get("first_name") or chat.get("username") or "Telegram User"
        return {"id": user_id, "name": display_name, "display_name": display_name, "real_name": display_name}

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        result = await telegram_api.call_api(self.config.bot_token, "getChat", {"chat_id": channel_id}, proxy_url=self._proxy_url)
        chat = result["result"]
        name = chat.get("title") or chat.get("username") or channel_id
        return {"id": channel_id, "name": name, "type": chat.get("type")}

    async def send_dm(self, user_id: str, text: str, **kwargs) -> Optional[str]:
        context = MessageContext(
            user_id=user_id,
            channel_id=user_id,
            platform="telegram",
            platform_specific={"is_dm": True},
        )
        keyboard = kwargs.get("keyboard")
        parse_mode = kwargs.get("parse_mode")
        if keyboard is not None:
            return await self.send_message_with_buttons(context, text, keyboard, parse_mode=parse_mode)
        return await self.send_message(context, text, parse_mode=parse_mode)

    def _normalize_reaction_emoji(self, emoji: str) -> Optional[str]:
        normalized = (emoji or "").strip()
        if not normalized:
            return None
        aliases = {
            ":eyes:": "👀",
            "eyes": "👀",
            "eye": "👀",
            "👀": "👀",
            ":robot_face:": "🤖",
            "robot_face": "🤖",
            "robot": "🤖",
            "🤖": "🤖",
        }
        return aliases.get(normalized, normalized)

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        normalized = self._normalize_reaction_emoji(emoji)
        if not normalized or not message_id:
            return False
        try:
            await telegram_api.set_message_reaction(self.config.bot_token, context.channel_id, message_id, normalized, proxy_url=self._proxy_url)
            return True
        except Exception as err:
            logger.debug("Failed to add Telegram reaction: %s", err)
            return False

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        if not message_id or not self._normalize_reaction_emoji(emoji):
            return False
        try:
            await telegram_api.clear_message_reaction(self.config.bot_token, context.channel_id, message_id, proxy_url=self._proxy_url)
            return True
        except Exception as err:
            logger.debug("Failed to remove Telegram reaction: %s", err)
            return False

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        payload = {"chat_id": context.channel_id, "action": "typing"}
        if context.thread_id:
            payload["message_thread_id"] = int(context.thread_id)
        await telegram_api.call_api(self.config.bot_token, "sendChatAction", payload, proxy_url=self._proxy_url)
        return True

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        return True

    async def delete_message(self, context: MessageContext, message_id: str) -> bool:
        if not message_id:
            return False
        await telegram_api.delete_message(self.config.bot_token, context.channel_id, message_id, proxy_url=self._proxy_url)
        return True

    async def _delete_interaction_message(self, context: MessageContext, message_id: str) -> None:
        if not message_id:
            return
        try:
            await self.delete_message(context, message_id)
        except Exception:
            logger.debug("Failed to delete Telegram interaction message %s", message_id, exc_info=True)

    async def upload_file_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        payload = self._build_payload(context)
        if title:
            payload["caption"] = title
        result = await telegram_api.send_multipart_file(
            self.config.bot_token,
            "sendDocument",
            payload,
            file_path,
            "document",
            proxy_url=self._proxy_url,
        )
        return str(result["result"]["message_id"])

    async def upload_image_from_path(
        self,
        context: MessageContext,
        file_path: str,
        title: Optional[str] = None,
    ) -> str:
        payload = self._build_payload(context)
        if title:
            payload["caption"] = title
        result = await telegram_api.send_multipart_file(
            self.config.bot_token,
            "sendPhoto",
            payload,
            file_path,
            "photo",
            proxy_url=self._proxy_url,
        )
        return str(result["result"]["message_id"])

    async def download_file(
        self,
        file_info: Dict[str, Any],
        max_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
    ) -> Optional[bytes]:
        file_id = (
            file_info.get("telegram_file_id")
            or file_info.get("url")
            or file_info.get("file_id")
        )
        if not file_id:
            raise ValueError("Telegram file_id is required")
        file_result = await telegram_api.get_file(self.config.bot_token, str(file_id), proxy_url=self._proxy_url)
        file_path = file_result["result"]["file_path"]
        content = await telegram_api.download_file(self.config.bot_token, file_path, timeout_seconds=timeout_seconds, proxy_url=self._proxy_url)
        if max_bytes is not None and len(content) > max_bytes:
            raise ValueError("Downloaded file exceeds max_bytes")
        return content

    async def open_change_cwd_modal(self, trigger_id: Any, current_cwd: str, channel_id: str = None):
        context = trigger_id if isinstance(trigger_id, MessageContext) else None
        if context is None:
            raise ValueError("Telegram change-cwd flow requires a message context")
        keyboard = InlineKeyboard(
            buttons=[[InlineButton(text=f"✖️ {self._t('common.cancel')}", callback_data="tg_cwd:cancel")]]
        )
        text = "\n".join(
            [
                f"📂 {self._t('telegram.cwdPromptTitle')}",
                "",
                f"{self._t('modal.cwd.current')} `{current_cwd}`",
                self._t("telegram.cwdPromptBody"),
            ]
        )
        prompt_message_id = await self.send_message_with_buttons(context, text, keyboard)
        self._cwd_prompts[self._interaction_scope_key(context)] = _TelegramCwdPrompt(
            message_id=prompt_message_id,
            current_cwd=current_cwd,
        )

    async def _handle_cwd_callback(self, context: MessageContext, callback_data: str) -> None:
        scope_key = self._interaction_scope_key(context)
        if callback_data == "tg_cwd:cancel":
            prompt = self._cwd_prompts.pop(scope_key, None)
            await self._delete_interaction_message(context, (prompt.message_id if prompt else context.message_id or ""))

    async def open_resume_session_modal(
        self,
        trigger_id: Any,
        sessions: list[NativeResumeSession],
        channel_id: str,
        thread_id: Optional[str],
        host_message_ts: Optional[str],
    ):
        context = trigger_id if isinstance(trigger_id, MessageContext) else None
        if context is None:
            raise ValueError("Telegram resume flow requires a message context")

        options: list[tuple[str, str]] = []
        rows: list[list[InlineButton]] = []
        summary_lines = [
            f"⏮️ {self._t('telegram.resumeTitle')}",
            self._t("telegram.resumeBody"),
        ]
        for item in list(sessions)[:12]:
            idx = len(options)
            options.append((item.agent, item.native_session_id))
            label = AgentNativeSessionService.format_display_summary(item)
            rows.append([InlineButton(text=label[:40], callback_data=f"tg_resume:{idx}")])
            summary_lines.append(
                f"{idx + 1}. {label} ({AgentNativeSessionService.format_display_time(item)})"
            )
            if len(options) >= 12:
                break

        rows.append([InlineButton(text=f"✖️ {self._t('common.cancel')}", callback_data="tg_resume:cancel")])
        text = "\n".join(summary_lines)
        if not options:
            text += f"\n\nℹ️ {self._t('telegram.resumeNoStoredSessions')}"
        message_id = await self.send_message_with_buttons(context, text, InlineKeyboard(buttons=rows))
        self._resume_states[self._interaction_scope_key(context)] = _TelegramResumeSessionState(
            message_id=message_id,
            options=options,
            is_dm=bool((context.platform_specific or {}).get("is_dm")),
            thread_id=resolve_context_thread_id(context),
        )

    async def _handle_resume_callback(self, context: MessageContext, callback_data: str) -> None:
        scope_key = self._interaction_scope_key(context)
        state = self._resume_states.get(scope_key)
        if state is None or state.message_id != (context.message_id or ""):
            return
        if callback_data == "tg_resume:cancel":
            self._resume_states.pop(scope_key, None)
            await self._delete_interaction_message(context, state.message_id)
            return

        try:
            option_index = int(callback_data.split(":", 1)[1])
        except Exception:
            return
        if option_index < 0 or option_index >= len(state.options):
            return

        agent, session_id = state.options[option_index]
        self._resume_states.pop(scope_key, None)
        if self._controller is None or not hasattr(self._controller, "session_handler"):
            await self.send_message(context, f"❌ {self._t('error.resumeFailed')}")
            return
        await self._delete_interaction_message(context, state.message_id)
        await self._controller.session_handler.handle_resume_session_submission(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=getattr(state, "thread_id", resolve_context_thread_id(context)),
            agent=agent,
            session_id=session_id,
            is_dm=state.is_dm,
            platform="telegram",
        )

    async def open_routing_modal(self, trigger_id: Any, channel_id: str, **kwargs):
        context = trigger_id if isinstance(trigger_id, MessageContext) else None
        if context is None:
            raise ValueError("Telegram routing flow requires a message context")
        current_routing = kwargs.get("current_routing")
        current_backend = kwargs.get("current_backend") or "opencode"
        backend = current_backend or "opencode"
        current_model = getattr(current_routing, "model", None)
        current_effort = getattr(current_routing, "reasoning_effort", None)
        state = _TelegramRoutingState(
            message_id="",
            channel_id=channel_id,
            thread_id=resolve_context_thread_id(context),
            user_id=context.user_id,
            is_dm=bool((context.platform_specific or {}).get("is_dm")),
            registered_backends=list(kwargs.get("registered_backends") or []),
            opencode_agents=list(kwargs.get("opencode_agents") or []),
            opencode_models=dict(kwargs.get("opencode_models") or {}),
            opencode_default_config=dict(kwargs.get("opencode_default_config") or {}),
            claude_agents=list(kwargs.get("claude_agents") or []),
            claude_models=list(kwargs.get("claude_models") or []),
            codex_models=list(kwargs.get("codex_models") or []),
            backend_reasoning_options=dict(kwargs.get("backend_reasoning_options") or {}),
            backend=backend,
            opencode_agent=getattr(current_routing, "opencode_agent", None),
            opencode_model=current_model if backend == "opencode" else None,
            opencode_reasoning_effort=current_effort if backend == "opencode" else None,
            claude_agent=getattr(current_routing, "claude_agent", None),
            claude_model=current_model if backend == "claude" else None,
            claude_reasoning_effort=current_effort if backend == "claude" else None,
            codex_model=current_model if backend == "codex" else None,
            codex_reasoning_effort=current_effort if backend == "codex" else None,
        )
        if backend in {"claude", "codex"}:
            from modules.agents.opencode.utils import (
                build_claude_reasoning_options,
                build_codex_reasoning_options,
                resolve_model_reasoning_options,
            )

            target_model = state.claude_model if backend == "claude" else state.codex_model
            fallback = (
                build_claude_reasoning_options(target_model)
                if backend == "claude"
                else build_codex_reasoning_options()
            )
            available_efforts = {
                entry.get("value")
                for entry in resolve_model_reasoning_options(
                    state.backend_reasoning_options.get(backend),
                    target_model,
                    fallback,
                )
            }
            effort_field = f"{backend}_reasoning_effort"
            if getattr(state, effort_field) not in available_efforts:
                setattr(state, effort_field, None)
        text, keyboard = self._render_routing_state(state)
        message_id = await self.send_message_with_buttons(context, text, keyboard)
        state.message_id = message_id
        self._routing_states[self._interaction_scope_key(context)] = state

    async def open_question_modal(self, trigger_id: Any, context: MessageContext, pending: Any, callback_prefix: str):
        target_context = trigger_id if isinstance(trigger_id, MessageContext) else context
        if target_context is None or not isinstance(target_context, MessageContext):
            raise ValueError("Telegram question flow requires a message context")

        raw_questions = getattr(pending, "questions", None)
        if raw_questions is None and isinstance(pending, dict):
            raw_questions = pending.get("questions")
        questions = list(raw_questions or [])
        if not questions:
            raise ValueError("Pending question has no questions")

        state = _TelegramQuestionState(
            message_id=str(target_context.message_id or ""),
            callback_prefix=callback_prefix,
            questions=questions,
            answers=[[] for _ in questions],
        )
        self._question_states[self._interaction_scope_key(target_context)] = state
        text, keyboard = self._render_question_state(target_context, state)
        if state.message_id:
            await self.edit_message(target_context, state.message_id, text=text, keyboard=keyboard)
        else:
            state.message_id = await self.send_message_with_buttons(target_context, text, keyboard)

    def _get_backend_label(self, backend: str) -> str:
        translated = self._t(f"backend.{backend}")
        return translated if translated != f"backend.{backend}" else backend

    def _option_label(self, value: Optional[str], default_label: Optional[str] = None) -> str:
        if not value:
            return self._t("common.default")
        if value in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            translated = self._t(f"reasoning.{value}")
            if translated != f"reasoning.{value}":
                return translated
        return default_label or value

    def _routing_field_label(self, field: Optional[str]) -> str:
        mapping = {
            "backend": self._t("routing.label.backend"),
            "opencode_agent": self._t("routing.label.agent"),
            "opencode_model": self._t("routing.label.model"),
            "opencode_reasoning_effort": self._t("routing.label.reasoningEffort"),
            "claude_agent": self._t("routing.label.agent"),
            "claude_model": self._t("routing.label.model"),
            "claude_reasoning_effort": self._t("routing.label.reasoningEffort"),
            "codex_model": self._t("routing.label.model"),
            "codex_reasoning_effort": self._t("routing.label.reasoningEffort"),
        }
        return mapping.get(field or "", field or "")

    def _question_definition(self, question: Any) -> tuple[str, str, list[tuple[str, str]], bool]:
        if isinstance(question, dict):
            header = str(question.get("header") or "").strip()
            prompt = str(question.get("question") or "").strip()
            options_raw = list(question.get("options") or [])
            multiple = bool(question.get("multiple") or question.get("multiSelect"))
        else:
            header = str(getattr(question, "header", "") or "").strip()
            prompt = str(getattr(question, "question", "") or "").strip()
            options_raw = list(getattr(question, "options", []) or [])
            multiple = bool(getattr(question, "multiple", False))

        options: list[tuple[str, str]] = []
        for option in options_raw:
            if isinstance(option, dict):
                label = str(option.get("label") or "").strip()
                description = str(option.get("description") or "").strip()
            else:
                label = str(getattr(option, "label", "") or "").strip()
                description = str(getattr(option, "description", "") or "").strip()
            if label:
                options.append((label, description))
        return header, prompt, options, multiple

    def _render_question_state(
        self,
        context: MessageContext,
        state: _TelegramQuestionState,
    ) -> tuple[str, InlineKeyboard]:
        header, prompt, options, multiple = self._question_definition(state.questions[state.index])
        current_answers = set(state.answers[state.index])
        lines = []
        title = header or f"Question {state.index + 1}"
        lines.append(f"❓ {title}")
        if prompt:
            lines.append(prompt)
        if len(state.questions) > 1:
            lines.append(f"{state.index + 1}/{len(state.questions)}")
        rows: list[list[InlineButton]] = []
        for idx, (label, description) in enumerate(options, start=1):
            prefix = "☑️ " if label in current_answers else ""
            text = f"{prefix}{label}"
            if description:
                lines.append(f"{idx}. {label} - {description}")
            rows.append(
                [
                    InlineButton(
                        text=text[:40],
                        callback_data=f"tg_question:{'toggle' if multiple else 'choose'}:{idx}",
                    )
                ]
            )
        footer_row: list[InlineButton] = []
        if multiple:
            action_label = self._t("common.submit") if state.index + 1 >= len(state.questions) else self._t("common.next")
            footer_row.append(InlineButton(text=action_label, callback_data="tg_question:advance"))
        footer_row.append(InlineButton(text=f"✖️ {self._t('common.cancel')}", callback_data="tg_question:cancel"))
        rows.append(footer_row)
        return "\n".join(lines), InlineKeyboard(buttons=rows)

    async def _handle_question_callback(self, context: MessageContext, callback_data: str) -> None:
        scope_key = self._interaction_scope_key(context)
        state = self._question_states.get(scope_key)
        if state is None or state.message_id != (context.message_id or ""):
            return

        _, action, *rest = callback_data.split(":")
        header, prompt, options, multiple = self._question_definition(state.questions[state.index])
        del header, prompt
        if action == "cancel":
            self._question_states.pop(scope_key, None)
            await self._delete_interaction_message(context, state.message_id)
            return

        if action in {"choose", "toggle"} and rest:
            try:
                option_idx = int(rest[0]) - 1
            except Exception:
                option_idx = -1
            if 0 <= option_idx < len(options):
                label = options[option_idx][0]
                if action == "choose":
                    state.answers[state.index] = [label]
                    if state.index + 1 < len(state.questions):
                        state.index += 1
                    else:
                        await self._finalize_question_state(context, scope_key, state)
                        return
                else:
                    answers = state.answers[state.index]
                    if label in answers:
                        state.answers[state.index] = [value for value in answers if value != label]
                    else:
                        answers.append(label)
        elif action == "advance":
            if multiple and state.index + 1 < len(state.questions):
                state.index += 1
            else:
                await self._finalize_question_state(context, scope_key, state)
                return

        text, keyboard = self._render_question_state(context, state)
        await self.edit_message(context, state.message_id, text=text, keyboard=keyboard)

    async def _finalize_question_state(
        self,
        context: MessageContext,
        scope_key: str,
        state: _TelegramQuestionState,
    ) -> None:
        self._question_states.pop(scope_key, None)
        answers_payload = state.answers
        await self._delete_interaction_message(context, state.message_id)
        auth_result = self.check_authorization(
            user_id=context.user_id,
            channel_id=context.channel_id,
            thread_id=resolve_context_thread_id(context),
            is_dm=bool((context.platform_specific or {}).get("is_dm")),
            action=state.callback_prefix,
            settings_manager=self.settings_manager,
        )
        if not auth_result.allowed:
            denial_text = self.build_auth_denial_text(auth_result.denial, context.channel_id)
            if denial_text:
                await self.send_message(context, denial_text)
            return
        if self.on_callback_query_callback:
            synthetic_payload = f"{state.callback_prefix}:modal:{json.dumps(answers_payload, ensure_ascii=True)}"
            await self.on_callback_query_callback(context, synthetic_payload)

    def _routing_summary_lines(self, state: _TelegramRoutingState) -> list[str]:
        lines = [
            f"🤖 {self._t('telegram.routingTitle')}",
            "",
            f"{self._t('routing.label.backend')}: {self._get_backend_label(state.backend)}",
        ]
        if state.backend == "opencode":
            lines.append(
                f"{self._t('routing.label.agent')}: {self._option_label(state.opencode_agent)}"
            )
            lines.append(
                f"{self._t('routing.label.model')}: {self._option_label(state.opencode_model)}"
            )
            lines.append(
                f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.opencode_reasoning_effort)}"
            )
        elif state.backend == "claude":
            lines.append(
                f"{self._t('routing.label.agent')}: {self._option_label(state.claude_agent)}"
            )
            lines.append(
                f"{self._t('routing.label.model')}: {self._option_label(state.claude_model)}"
            )
            lines.append(
                f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.claude_reasoning_effort)}"
            )
        elif state.backend == "codex":
            lines.append(
                f"{self._t('routing.label.model')}: {self._option_label(state.codex_model)}"
            )
            lines.append(
                f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.codex_reasoning_effort)}"
            )
        return lines

    def _routing_picker_options(self, state: _TelegramRoutingState) -> list[tuple[str, Optional[str]]]:
        from modules.agents.opencode.utils import (
            build_claude_reasoning_options,
            build_codex_reasoning_options,
            build_opencode_model_option_items,
            build_reasoning_effort_options,
            resolve_model_reasoning_options,
            resolve_opencode_allowed_providers,
            resolve_opencode_default_model,
            resolve_opencode_provider_preferences,
        )

        field = state.picker_field
        if field == "backend":
            return [(self._get_backend_label(backend), backend) for backend in state.registered_backends]
        if field == "opencode_agent":
            names = []
            for item in state.opencode_agents:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(item).strip()
                if name and name not in names:
                    names.append(name)
            return [(self._t("common.default"), None)] + [(name, name) for name in names]
        if field == "opencode_model":
            target_model = state.opencode_model
            preferred = resolve_opencode_provider_preferences(state.opencode_default_config, target_model)
            allowed = resolve_opencode_allowed_providers(state.opencode_default_config, state.opencode_models)
            default_model = resolve_opencode_default_model(
                state.opencode_default_config,
                state.opencode_agents,
                state.opencode_agent,
            )
            entries = build_opencode_model_option_items(
                state.opencode_models,
                max_total=24,
                preferred_providers=preferred,
                allowed_providers=allowed,
            )
            options = [(self._t("common.default"), None)]
            if default_model:
                options[0] = (f"{self._t('common.default')} - {default_model}", None)
            options.extend((str(entry.get("label")), str(entry.get("value"))) for entry in entries if entry.get("value"))
            return options
        if field == "opencode_reasoning_effort":
            target_model = state.opencode_model
            return [
                (
                    self._option_label(None if entry.get("value") == "__default__" else str(entry.get("value"))),
                    None if entry.get("value") == "__default__" else str(entry.get("value")),
                )
                for entry in build_reasoning_effort_options(state.opencode_models, target_model)
                if entry.get("value")
            ]
        if field == "claude_agent":
            names = []
            for item in state.claude_agents:
                if isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                else:
                    name = str(item).strip()
                if name and name not in names:
                    names.append(name)
            return [(self._t("common.default"), None)] + [(name, name) for name in names]
        if field == "claude_model":
            return [(self._t("common.default"), None)] + [
                (format_claude_model_label(model), str(model)) for model in state.claude_models
            ]
        if field == "claude_reasoning_effort":
            return [
                (
                    self._option_label(None if entry.get("value") == "__default__" else str(entry.get("value"))),
                    None if entry.get("value") == "__default__" else str(entry.get("value")),
                )
                for entry in resolve_model_reasoning_options(
                    state.backend_reasoning_options.get("claude"),
                    state.claude_model,
                    build_claude_reasoning_options(state.claude_model),
                )
                if entry.get("value")
            ]
        if field == "codex_model":
            return [(self._t("common.default"), None)] + [(str(model), str(model)) for model in state.codex_models]
        if field == "codex_reasoning_effort":
            return [
                (
                    self._option_label(
                        None if entry.get("value") == "__default__" else str(entry.get("value")),
                        str(entry.get("label") or entry.get("value") or ""),
                    ),
                    None if entry.get("value") == "__default__" else str(entry.get("value")),
                )
                for entry in resolve_model_reasoning_options(
                    state.backend_reasoning_options.get("codex"),
                    state.codex_model,
                    build_codex_reasoning_options(),
                )
                if entry.get("value")
            ]
        return []

    def _apply_routing_option(self, state: _TelegramRoutingState, value: Optional[str]) -> None:
        field = state.picker_field
        if not field:
            return
        setattr(state, field, value)
        if field == "claude_model" and value is None:
            state.claude_reasoning_effort = None
        if field == "claude_model" and value is not None:
            state.claude_reasoning_effort = None
        if field == "codex_model":
            state.codex_reasoning_effort = None
        state.picker_field = None
        state.picker_page = 0

    def _render_routing_state(self, state: _TelegramRoutingState) -> tuple[str, InlineKeyboard]:
        if state.picker_field:
            return self._render_routing_picker(state)

        text = "\n".join(self._routing_summary_lines(state))
        backend_row = [
            InlineButton(
                text=(f"☑️ {self._get_backend_label(backend)}" if backend == state.backend else self._get_backend_label(backend))[
                    :40
                ],
                callback_data=f"tg_route:backend:{backend}",
            )
            for backend in state.registered_backends[:3]
        ]
        rows: list[list[InlineButton]] = []
        if backend_row:
            rows.append(backend_row)
        if len(state.registered_backends) > 3:
            rows.append(
                [
                    InlineButton(
                        text=f"… {self._t('routing.label.backend')}"[:40],
                        callback_data="tg_route:field:backend",
                    )
                ]
            )

        if state.backend == "opencode":
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.agent')}: {self._option_label(state.opencode_agent)}"[:40],
                        callback_data="tg_route:field:opencode_agent",
                    )
                ]
            )
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.model')}: {self._option_label(state.opencode_model)}"[:40],
                        callback_data="tg_route:field:opencode_model",
                    )
                ]
            )
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.opencode_reasoning_effort)}"[
                            :40
                        ],
                        callback_data="tg_route:field:opencode_reasoning_effort",
                    )
                ]
            )
        elif state.backend == "claude":
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.agent')}: {self._option_label(state.claude_agent)}"[:40],
                        callback_data="tg_route:field:claude_agent",
                    )
                ]
            )
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.model')}: {self._option_label(state.claude_model)}"[:40],
                        callback_data="tg_route:field:claude_model",
                    )
                ]
            )
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.claude_reasoning_effort)}"[
                            :40
                        ],
                        callback_data="tg_route:field:claude_reasoning_effort",
                    )
                ]
            )
        elif state.backend == "codex":
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.model')}: {self._option_label(state.codex_model)}"[:40],
                        callback_data="tg_route:field:codex_model",
                    )
                ]
            )
            rows.append(
                [
                    InlineButton(
                        text=f"{self._t('routing.label.reasoningEffort')}: {self._option_label(state.codex_reasoning_effort)}"[
                            :40
                        ],
                        callback_data="tg_route:field:codex_reasoning_effort",
                    )
                ]
            )

        rows.append(
            [
                InlineButton(text=f"💾 {self._t('common.save')}", callback_data="tg_route:save"),
                InlineButton(text=f"✖️ {self._t('common.cancel')}", callback_data="tg_route:cancel"),
            ]
        )
        return text, InlineKeyboard(buttons=rows)

    def _render_routing_picker(self, state: _TelegramRoutingState) -> tuple[str, InlineKeyboard]:
        options = self._routing_picker_options(state)
        page_size = 6
        total_pages = max(1, (len(options) + page_size - 1) // page_size)
        page = max(0, min(state.picker_page, total_pages - 1))
        state.picker_page = page
        page_options = options[page * page_size : (page + 1) * page_size]

        lines = self._routing_summary_lines(state)
        lines.extend(
            [
                "",
                f"{self._t('telegram.routingChoosePrefix')} {self._routing_field_label(state.picker_field)}",
            ]
        )
        rows = [
            [InlineButton(text=(label or self._t("common.default"))[:40], callback_data=f"tg_route:option:{index}")]
            for index, (label, _) in enumerate(page_options, start=page * page_size)
        ]
        nav_row: list[InlineButton] = []
        if page > 0:
            nav_row.append(InlineButton(text="◀️", callback_data="tg_route:page:prev"))
        if page + 1 < total_pages:
            nav_row.append(InlineButton(text="▶️", callback_data="tg_route:page:next"))
        if nav_row:
            rows.append(nav_row)
        rows.append([InlineButton(text=f"↩️ {self._t('common.back')}", callback_data="tg_route:back")])
        return "\n".join(lines), InlineKeyboard(buttons=rows)

    async def _handle_routing_callback(self, context: MessageContext, callback_data: str) -> None:
        scope_key = self._interaction_scope_key(context)
        state = self._routing_states.get(scope_key)
        if state is None or state.message_id != (context.message_id or ""):
            return

        parts = callback_data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "cancel":
            self._routing_states.pop(scope_key, None)
            await self._delete_interaction_message(context, state.message_id)
            return
        if action == "save":
            self._routing_states.pop(scope_key, None)
            if self._controller is None or not hasattr(self._controller, "settings_handler"):
                await self.send_message(context, f"❌ {self._t('error.routingModalFailed')}")
                return
            await self._delete_interaction_message(context, state.message_id)
            routing_update = {
                "user_id": state.user_id,
                "channel_id": state.channel_id,
                "backend": state.backend,
                "opencode_agent": state.opencode_agent,
                "opencode_model": state.opencode_model,
                "opencode_reasoning_effort": state.opencode_reasoning_effort,
                "claude_agent": state.claude_agent,
                "claude_model": state.claude_model,
                "claude_reasoning_effort": state.claude_reasoning_effort,
                "codex_model": state.codex_model,
                "codex_reasoning_effort": state.codex_reasoning_effort,
                "is_dm": state.is_dm,
                "platform": "telegram",
            }
            state_thread_id = getattr(state, "thread_id", None)
            if state_thread_id is not None:
                routing_update["thread_id"] = state_thread_id
            await self._controller.settings_handler.handle_routing_update(**routing_update)
            return
        if action == "back":
            state.picker_field = None
            state.picker_page = 0
        elif action == "backend" and len(parts) > 2:
            backend = parts[2]
            if backend in state.registered_backends:
                state.backend = backend
                state.picker_field = None
                state.picker_page = 0
        elif action == "page" and len(parts) > 2:
            state.picker_page += -1 if parts[2] == "prev" else 1
        elif action == "field" and len(parts) > 2:
            field = parts[2]
            if field == "backend":
                state.picker_field = "backend"
            else:
                state.picker_field = field
            state.picker_page = 0
        elif action == "option" and len(parts) > 2:
            try:
                option_index = int(parts[2])
            except Exception:
                option_index = -1
            options = self._routing_picker_options(state)
            if 0 <= option_index < len(options):
                _, value = options[option_index]
                if state.picker_field == "backend" and value:
                    state.backend = value
                    state.picker_field = None
                    state.picker_page = 0
                else:
                    self._apply_routing_option(state, value)

        text, keyboard = self._render_routing_state(state)
        await self.edit_message(context, state.message_id, text=text, keyboard=keyboard)

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
        del display_names, owner_user_id
        context = trigger_id if isinstance(trigger_id, MessageContext) else None
        if context is None:
            raise ValueError("Telegram settings flow requires a message context")

        state = _TelegramSettingsState(
            message_id="",
            show_message_types=list(getattr(user_settings, "show_message_types", []) or []),
            current_require_mention=current_require_mention,
            global_require_mention=global_require_mention,
            current_language=current_language or self._get_lang(),
            is_dm=bool((context.platform_specific or {}).get("is_dm")),
            thread_id=resolve_context_thread_id(context),
        )
        text, keyboard = self._render_settings_state(state, list(message_types or []))
        message_id = await self.send_message_with_buttons(context, text, keyboard)
        state.message_id = message_id
        self._settings_states[self._interaction_scope_key(context)] = state

    def _settings_mention_label(self, value: Optional[bool], global_require_mention: bool) -> str:
        if value is None:
            default_status = (
                self._t("modal.settings.mentionStatusOn")
                if global_require_mention
                else self._t("modal.settings.mentionStatusOff")
            )
            return f"{self._t('common.default')} ({default_status})"
        return self._t("modal.settings.optionRequireMention") if value else self._t("modal.settings.optionDontRequireMention")

    def _settings_message_type_label(self, msg_type: str) -> str:
        display_names = {
            "assistant": self._t("messageType.assistant"),
            "toolcall": self._t("messageType.toolcall"),
        }
        return display_names.get(msg_type, msg_type)

    def _render_settings_state(
        self,
        state: _TelegramSettingsState,
        message_types: list[str],
    ) -> tuple[str, InlineKeyboard]:
        selected = [self._settings_message_type_label(msg_type) for msg_type in state.show_message_types]
        selected_text = ", ".join(selected) if selected else "-"
        language_label = self._t(f"language.{state.current_language}")
        if language_label == f"language.{state.current_language}":
            language_label = state.current_language

        lines = [
            f"⚙️ {self._t('modal.settings.title')}",
            "",
            f"1. {self._t('modal.settings.showMessageTypes')}",
            f"   {self._t('modal.settings.current')}: {selected_text}",
            "",
            f"2. {self._t('modal.settings.requireMention')}",
            f"   {self._t('modal.settings.current')}: {self._settings_mention_label(state.current_require_mention, state.global_require_mention)}",
            "",
            f"3. {self._t('modal.settings.language')}",
            f"   {self._t('modal.settings.current')}: {language_label}",
        ]

        rows: list[list[InlineButton]] = []
        row: list[InlineButton] = []
        for index, msg_type in enumerate(message_types):
            is_shown = msg_type in state.show_message_types
            checkbox = "☑️" if is_shown else "⬜"
            row.append(
                InlineButton(
                    text=f"{checkbox} {self._settings_message_type_label(msg_type)}"[:40],
                    callback_data=f"tg_settings:toggle:{msg_type}",
                )
            )
            if len(row) == 2 or index == len(message_types) - 1:
                rows.append(row)
                row = []

        mention_options = [
            ("default", self._t("common.default"), state.current_require_mention is None),
            ("on", self._t("modal.settings.optionRequireMention"), state.current_require_mention is True),
            ("off", self._t("modal.settings.optionDontRequireMention"), state.current_require_mention is False),
        ]
        rows.append(
            [
                InlineButton(
                    text=(f"☑️ {label}" if selected_option else label)[:40],
                    callback_data=f"tg_settings:mention:{value}",
                )
                for value, label, selected_option in mention_options
            ]
        )
        rows.append(
            [
                InlineButton(
                    text=(f"☑️ {self._t(f'language.{lang}')}" if lang == state.current_language else self._t(f"language.{lang}"))[:40],
                    callback_data=f"tg_settings:lang:{lang}",
                )
                for lang in get_supported_languages()
            ]
        )
        rows.append([InlineButton(text=f"ℹ️ {self._t('button.aboutMessageTypes')}", callback_data="info_msg_types")])
        rows.append(
            [
                InlineButton(text=f"💾 {self._t('common.save')}", callback_data="tg_settings:save"),
                InlineButton(text=f"✖️ {self._t('common.cancel')}", callback_data="tg_settings:cancel"),
            ]
        )
        return "\n".join(lines), InlineKeyboard(buttons=rows)

    async def _handle_settings_callback(self, context: MessageContext, callback_data: str) -> None:
        scope_key = self._interaction_scope_key(context)
        state = self._settings_states.get(scope_key)
        if state is None or state.message_id != (context.message_id or ""):
            return

        settings_manager = self.settings_manager
        message_types = list(settings_manager.get_available_message_types()) if settings_manager else []
        parts = callback_data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "cancel":
            self._settings_states.pop(scope_key, None)
            await self._delete_interaction_message(context, state.message_id)
            return

        if action == "save":
            self._settings_states.pop(scope_key, None)
            if self._controller is None or not hasattr(self._controller, "settings_handler"):
                await self.send_message(context, f"❌ {self._t('error.settingsFailed')}")
                return
            await self._delete_interaction_message(context, state.message_id)
            settings_update = {
                "user_id": context.user_id,
                "show_message_types": state.show_message_types,
                "channel_id": context.channel_id,
                "require_mention": state.current_require_mention,
                "language": state.current_language,
                "notify_user": True,
                "is_dm": state.is_dm,
                "platform": "telegram",
            }
            state_thread_id = getattr(state, "thread_id", None)
            if state_thread_id is not None:
                settings_update["thread_id"] = state_thread_id
            await self._controller.settings_handler.handle_settings_update(**settings_update)
            return

        if action == "toggle" and len(parts) > 2:
            msg_type = parts[2]
            if msg_type in state.show_message_types:
                state.show_message_types = [item for item in state.show_message_types if item != msg_type]
            elif msg_type in message_types:
                state.show_message_types.append(msg_type)
        elif action == "mention" and len(parts) > 2:
            value = parts[2]
            if value == "default":
                state.current_require_mention = None
            elif value == "on":
                state.current_require_mention = True
            elif value == "off":
                state.current_require_mention = False
        elif action == "lang" and len(parts) > 2 and parts[2] in get_supported_languages():
            state.current_language = parts[2]

        text, keyboard = self._render_settings_state(state, message_types)
        await self.edit_message(context, state.message_id, text=text, keyboard=keyboard)
