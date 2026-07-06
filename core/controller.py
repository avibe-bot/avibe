"""Core controller that coordinates between modules and handlers"""

import asyncio
import concurrent.futures
import json
import logging
import threading
from typing import Optional, Dict, Any
from config import paths
from config.platform_registry import get_platform_descriptor
from config.v2_config import DEFAULT_AGENT_BACKEND, DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS, DEFAULT_AGENT_PROGRESS_STYLE
from modules.im import BaseIMClient, MessageContext, IMFactory
from modules.im.multi import MultiIMClient
from modules.agent_router import AgentRouter
from modules.agents.service import AgentService
from modules.claude_client import ClaudeClient
from modules.session_manager import SessionManager
from modules.settings_manager import SettingsManager, MultiSettingsManager
from core.handlers import (
    CommandHandlers,
    SessionHandler,
    SettingsHandler,
    MessageHandler,
)
from core.agent_auth_service import AgentAuthService
from core.audio_asr import AudioAsrService
from core.message_context import build_context_session_key
from core.message_dispatcher import ConsolidatedMessageDispatcher
from core.processing_indicator import ProcessingIndicatorService
from core.runtime_commands import RuntimeCommandWatcher
from core.scheduled_tasks import ScheduledTaskService
from core.update_checker import UpdateChecker
from core.watches import ManagedWatchService
from core.vibe_agents import VibeAgent, VibeAgentStore
from vibe.i18n import get_supported_languages, t as i18n_t

logger = logging.getLogger(__name__)


class RemovedPlatformIMClient(BaseIMClient):
    """No-op sink for stale replies after an IM platform is hot-disabled."""

    def __init__(self, platform: str):
        from config.v2_config import AvibeConfig
        from modules.im.formatters.avibe_formatter import AvibeFormatter

        super().__init__(AvibeConfig())
        self.platform = platform
        self.formatter = AvibeFormatter()

    def get_default_parse_mode(self) -> Optional[str]:
        return None

    def should_use_thread_for_reply(self) -> bool:
        return False

    def supports_message_editing(self, context: Optional[MessageContext] = None) -> bool:
        return False

    async def send_message(
        self,
        context: MessageContext,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> Optional[str]:
        logger.info("Dropping stale outbound message for removed IM platform %s", self.platform)
        return None

    async def send_message_with_buttons(
        self,
        context: MessageContext,
        text: str,
        keyboard,
        parse_mode: Optional[str] = None,
    ) -> Optional[str]:
        logger.info("Dropping stale outbound button message for removed IM platform %s", self.platform)
        return None

    async def edit_message(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        keyboard: Optional[Any] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        return False

    async def remove_inline_keyboard(
        self,
        context: MessageContext,
        message_id: str,
        text: Optional[str] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        return False

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False) -> bool:
        return False

    def register_handlers(self):
        return None

    def run(self):
        return None

    def stop(self):
        return None

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        return {"id": user_id, "platform": self.platform, "removed": True}

    async def get_channel_info(self, channel_id: str) -> Dict[str, Any]:
        return {"id": channel_id, "platform": self.platform, "removed": True}

    async def add_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        return False

    async def remove_reaction(self, context: MessageContext, message_id: str, emoji: str) -> bool:
        return False

    async def send_typing_indicator(self, context: MessageContext) -> bool:
        return False

    async def clear_typing_indicator(self, context: MessageContext) -> bool:
        return False

    async def delete_message(self, context: MessageContext, message_id: str) -> bool:
        return False

    async def send_dm(self, user_id: str, text: str, **kwargs):
        return None

    def format_markdown(self, text: str) -> str:
        return text


def _optional_target_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _target_agent_variant(value: Any, backend: Any = None, agent_name: Any = None) -> Optional[str]:
    variant = _optional_target_str(value)
    if variant is None:
        return None
    sentinel_values = {"default", "claude", "codex", "opencode"}
    backend_text = _optional_target_str(backend)
    if backend_text:
        sentinel_values.add(backend_text)
    agent_name_text = _optional_target_str(agent_name)
    if agent_name_text:
        sentinel_values.add(agent_name_text)
    return None if variant in sentinel_values else variant


def _refresh_status_bubble_config(controller: Any) -> None:
    """Best-effort, mtime-guarded reload so Web UI changes to the status-bubble
    settings (progress style + heartbeat/no-output thresholds) take effect for
    turns that never pass through an IM inbound handler first (e.g. scheduled /
    background agent runs), where nothing else calls ``_refresh_config_from_disk``
    before the getters read ``controller.config``.

    Implemented as a module-level helper taking the controller explicitly and
    resolving the reload via ``getattr`` so lightweight test stubs that invoke the
    getters unbound (a bare ``SimpleNamespace`` self) simply skip the refresh
    instead of raising ``AttributeError``.
    """
    refresh = getattr(controller, "_refresh_config_from_disk", None)
    if callable(refresh):
        refresh()


class Controller:
    """Main controller that coordinates all bot operations"""

    def __init__(self, config):
        """Initialize controller with configuration"""
        self.config = config
        self._config_mtime: Optional[float] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._im_thread: Optional[threading.Thread] = None
        self._im_run_exception: Optional[BaseException] = None
        self.enabled_platforms = list(getattr(config, "enabled_platforms", lambda: [config.platform])())
        self.primary_platform = getattr(getattr(config, "platforms", None), "primary", config.platform)
        self._reconcile_lock: Optional[asyncio.Lock] = None
        self._removed_im_clients: Dict[str, BaseIMClient] = {}

        # Session tracking (must be initialized before handlers)
        self.claude_sessions: Dict[str, Any] = {}
        self.receiver_tasks: Dict[str, asyncio.Task] = {}
        self.stored_session_mappings: Dict[str, str] = {}
        self.session_last_activity: Dict[str, float] = {}
        # Monotonic baseline of when each session's CURRENT turn went active
        # (idle→active transition). Unlike ``session_last_activity`` — which is
        # bumped on every streamed event — this is NOT touched mid-turn, so the
        # Running tab can report an accurate "busy for" duration instead of
        # seconds-since-last-chunk.
        self.session_turn_started: Dict[str, float] = {}
        self.claude_active_sessions: set[str] = set()

        # The live streaming turn-sink registry now lives on the turn owner
        # (``self.session_turns.active_turn_sinks``); the register/pop/get methods +
        # the ``active_turn_sinks`` property below delegate to it.

        # Per-session turn gate, published by ``core.internal_server.create_app``
        # once the internal server is built on the loop. The scheduler routes
        # avibe scheduled / watch turns through it so they QUEUE behind an active
        # Chat turn (never preempt it) and get the Chat path's turn lifecycle
        # (in_flight + turn.start / turn.end + Stop). ``None`` until the server is
        # up — callers must treat its absence as "fall back to the direct path".
        self.session_turn_gate: Optional[Any] = None

        # Per-session turn owner (FSM). Created here so the controller owns it from
        # birth — boot stale-reset (below) and the OpenCode poll restore both run
        # before the internal server binds. ``core.internal_server.create_app`` later
        # binds the routing-context builder + exposes the gate endpoints; the gate,
        # dispatcher, and scheduler all share this one owner's in_flight + flush state.
        from core.session_turns import SessionTurnManager

        self.session_turns = SessionTurnManager(self)

        # Initialize core modules
        self._init_modules()

        # Initialize handlers
        self._init_handlers()

        # Initialize agents (depends on handlers/session handler)
        self._init_agents()
        self.agent_auth_service = AgentAuthService(self)

        self.vibe_agent_store = VibeAgentStore()
        self.vibe_agent_store.ensure_builtin_default_agents(
            self._enabled_agent_backends(),
        )

        # Setup callbacks
        self._setup_callbacks()

        # Consolidated message dispatcher
        self.message_dispatcher = ConsolidatedMessageDispatcher(self)
        self.scheduled_task_service = ScheduledTaskService(self)
        self.watch_service = ManagedWatchService(self)
        self.runtime_command_watcher = RuntimeCommandWatcher(self)

        # Background task for cleanup
        self.cleanup_task: Optional[asyncio.Task] = None

        # Initialize update checker (use default config if not present)
        from config.v2_config import UpdateConfig

        update_config = getattr(config, "update", None) or UpdateConfig()
        self.update_checker = UpdateChecker(self, update_config)

        # Restore session mappings on startup (after handlers are initialized)
        self.session_handler.restore_session_mappings()

        # Crash recovery: no turn survives a restart, so any session left
        # ``running`` in the table is stale — reset it to ``idle`` so the
        # workbench sidebar dot doesn't show a phantom green forever.
        self.session_turns.reset_stale()

    def _init_modules(self):
        """Initialize core modules"""
        runtime_clients: Dict[str, BaseIMClient] = IMFactory.create_clients(self.config)
        for platform, client in runtime_clients.items():
            client.formatter = self._create_formatter(platform)
        self.primary_platform = self._derive_primary_platform(self.config)
        self.im_clients = dict(runtime_clients)

        from modules.im.avibe import AvibeBot, AvibeConfig

        self.im_clients["avibe"] = AvibeBot(AvibeConfig())
        self.im_client = MultiIMClient(
            dict(runtime_clients),
            primary_platform=self.primary_platform,
            auxiliary_clients={"avibe": self.im_clients["avibe"]},
        )
        self._removed_im_clients = {}
        formatter = self.im_clients.get(self.primary_platform, self.im_clients["avibe"]).formatter
        self.claude_client = ClaudeClient(self.config.claude, formatter)

        # Initialize managers
        self.session_manager = SessionManager()
        self.settings_manager = MultiSettingsManager(
            self._settings_platforms_for(self.enabled_platforms, self.primary_platform),
            primary_platform=self.primary_platform,
        )
        self.platform_settings_managers = self.settings_manager.managers
        self.sessions = self.settings_manager.sessions
        self.native_session_service = None
        self.processing_indicator = ProcessingIndicatorService(self)
        self.audio_asr_service = AudioAsrService(self.config)
        self._migrate_discord_guild_scope_from_config()

        # Migrate legacy per-channel language into global config
        self._migrate_language_from_settings()

        # Legacy backend router. It is kept for platform runtime compatibility;
        # product routing is resolved through VibeAgentStore.
        self.agent_router = AgentRouter.from_file(None, platform=self.primary_platform)
        for platform in self.enabled_platforms:
            if platform not in self.agent_router.platform_routes:
                self.agent_router.platform_routes[platform] = self.agent_router.platform_routes[self.primary_platform]
        if "avibe" not in self.agent_router.platform_routes:
            self.agent_router.platform_routes["avibe"] = self.agent_router.platform_routes[self.primary_platform]

        # Inject settings_manager into IM client if supported
        for platform, client in runtime_clients.items():
            self._inject_runtime_dependencies(platform, client)

    @staticmethod
    def _derive_primary_platform(config) -> str:
        enabled = list(getattr(config, "enabled_platforms", lambda: [getattr(config, "platform", "slack")])())
        configured_primary = getattr(getattr(config, "platforms", None), "primary", getattr(config, "platform", "slack"))
        if enabled:
            return configured_primary if configured_primary in enabled else enabled[0]
        return "avibe"

    @staticmethod
    def _settings_platforms_for(enabled_platforms: list[str], primary_platform: str) -> list[str]:
        platforms = list(enabled_platforms)
        if primary_platform not in platforms:
            platforms.append(primary_platform)
        if "avibe" not in platforms:
            platforms.append("avibe")
        return platforms

    def _enabled_agent_backends(self) -> list[str]:
        result: list[str] = []
        agent_config = getattr(self.config, "agents", None)
        if agent_config is None:
            return list(getattr(self.agent_service, "agents", {}).keys()) or [DEFAULT_AGENT_BACKEND]
        for backend in ("opencode", "claude", "codex"):
            cfg = getattr(agent_config, backend, None)
            if bool(getattr(cfg, "enabled", False)):
                result.append(backend)
        return result

    def get_native_session_service(self):
        if self.native_session_service is None:
            from modules.agents.native_sessions.service import AgentNativeSessionService

            self.native_session_service = AgentNativeSessionService()
        return self.native_session_service

    def _create_formatter(self, platform: str):
        return get_platform_descriptor(platform).create_formatter()

    @staticmethod
    def _runtime_reconcile_signature(config, platform: str) -> tuple[Any, ...]:
        descriptor = get_platform_descriptor(platform)
        platform_config = descriptor.get_config(config)
        if platform_config is None:
            return ()
        return tuple(getattr(platform_config, field, None) for field in descriptor.runtime_reconcile_field_names())

    def _ensure_agent_route_for_platform(self, platform: str) -> None:
        if platform in self.agent_router.platform_routes:
            return
        fallback = self.agent_router.platform_routes.get(self.primary_platform)
        if fallback is None:
            from modules.agent_router import PlatformRoute

            fallback = PlatformRoute(default=self.agent_router.global_default)
        self.agent_router.platform_routes[platform] = fallback

    def _register_client_runtime(self, platform: str, client: BaseIMClient) -> None:
        client.formatter = self._create_formatter(platform)
        if platform not in self.platform_settings_managers:
            self.settings_manager.add_platform(platform)
            self.platform_settings_managers = self.settings_manager.managers
        self._ensure_agent_route_for_platform(platform)
        self._inject_runtime_dependencies(platform, client)

    def _build_platform_client(self, platform: str, config) -> BaseIMClient:
        descriptor = get_platform_descriptor(platform)
        client = descriptor.create_client(config)
        self._register_client_runtime(platform, client)
        return client

    def _sync_config_references(self, new_config) -> None:
        self.config = new_config
        governor = getattr(self, "_agent_resource_governor", None)
        if governor is not None:
            governor.update_config(getattr(new_config, "resource_governance", {"mode": "auto"}))
        self.processing_indicator.config = new_config
        self.audio_asr_service.config = new_config
        for handler_name in ("command_handler", "settings_handler", "message_handler", "session_handler"):
            handler = getattr(self, handler_name, None)
            if handler is not None:
                handler.config = new_config
                handler.im_client = self.im_client
                handler.settings_manager = self.settings_manager
                handler.sessions = self.sessions
        for agent in getattr(getattr(self, "agent_service", None), "agents", {}).values():
            agent.config = new_config
            agent.im_client = self.im_client
            agent.settings_manager = self.settings_manager
            agent.sessions = self.sessions
        self.claude_client.config = new_config.claude
        primary_formatter = self.im_clients.get(self.primary_platform, self.im_clients["avibe"]).formatter
        if primary_formatter is not None:
            self.claude_client.formatter = primary_formatter

    async def reconcile_platforms(self, new_config) -> dict[str, Any]:
        """Hot-apply IM platform enablement and runtime credential changes."""
        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()

        async with self._reconcile_lock:
            current_enabled = list(self.enabled_platforms)
            next_enabled = list(getattr(new_config, "enabled_platforms", lambda: [])())
            current_set = set(current_enabled)
            next_set = set(next_enabled)
            removed = [platform for platform in current_enabled if platform not in next_set]
            added = [platform for platform in next_enabled if platform not in current_set]
            rebuilt = [
                platform
                for platform in next_enabled
                if platform in current_set
                and self._runtime_reconcile_signature(self.config, platform)
                != self._runtime_reconcile_signature(new_config, platform)
            ]
            next_primary = self._derive_primary_platform(new_config)

            for platform in removed + rebuilt:
                self.im_clients.pop(platform, None)
                self._removed_im_clients[platform] = RemovedPlatformIMClient(platform)
                await asyncio.to_thread(self.im_client.remove_client, platform)

            self.enabled_platforms = next_enabled
            self.primary_platform = next_primary
            self.settings_manager.set_primary_platform(next_primary)
            self.platform_settings_managers = self.settings_manager.managers
            for platform in removed:
                self.settings_manager.remove_platform(platform)
                self.agent_router.platform_routes.pop(platform, None)

            for platform in next_enabled:
                self._ensure_agent_route_for_platform(platform)
            self._ensure_agent_route_for_platform("avibe")

            for platform in rebuilt + added:
                client = self._build_platform_client(platform, new_config)
                self.im_clients[platform] = client
                self.im_client.add_client(platform, client)
                self._removed_im_clients.pop(platform, None)

            self.im_client.set_primary_platform(next_primary)
            self._sync_config_references(new_config)

            logger.info(
                "Hot-reconciled IM platforms: added=%s removed=%s rebuilt=%s primary=%s",
                added,
                removed,
                rebuilt,
                next_primary,
            )
            return {
                "ok": True,
                "added": added,
                "removed": removed,
                "rebuilt": rebuilt,
                "enabled": next_enabled,
                "primary": next_primary,
            }

    def _migrate_discord_guild_scope_from_config(self) -> None:
        if "discord" not in self.platform_settings_managers:
            return
        discord_config = getattr(self.config, "discord", None)
        if not discord_config:
            return
        allowlist = getattr(discord_config, "guild_allowlist", None) or []
        denylist = getattr(discord_config, "guild_denylist", None) or []
        if not allowlist and not denylist:
            return
        manager = self.platform_settings_managers["discord"]
        if manager.has_guild_scope():
            return
        from config.v2_settings import GuildSettings

        store = manager.get_store()
        default_enabled = not bool(allowlist)
        guilds = {str(guild_id): GuildSettings(enabled=True) for guild_id in allowlist if str(guild_id)}
        for guild_id in denylist:
            guilds[str(guild_id)] = GuildSettings(enabled=False)
        store.set_guilds_for_platform("discord", guilds, default_enabled=default_enabled)
        store.save()
        logger.info("Migrated Discord guild access from config to settings")

    def _inject_runtime_dependencies(self, platform: str, client: BaseIMClient) -> None:
        settings_manager = self.platform_settings_managers[platform]
        setter = getattr(client, "set_settings_manager", None)
        if callable(setter):
            setter(settings_manager)
        controller_setter = getattr(client, "set_controller", None)
        if callable(controller_setter):
            controller_setter(self)
        logger.info("Injected settings_manager and controller into %s client", platform)

    def _get_lang(self) -> str:
        self._refresh_config_from_disk()
        return getattr(self.config, "language", "en")

    def _t(self, key: str, **kwargs) -> str:
        return i18n_t(key, self._get_lang(), **kwargs)

    def _refresh_config_from_disk(self) -> None:
        """Hot-reload mutable message-processing settings from config.json.

        Called on every ``_t()`` invocation (guarded by mtime check).
        Refreshes: language, show_duration, ack_mode, include_time_info, include_user_info,
        reply_enhancements, agent_progress_style, agent_status_heartbeat_ms,
        agent_status_no_output_ms, and mutable platform message filters.
        """
        try:
            config_path = paths.get_config_path()
            if not config_path.exists():
                return
            mtime = config_path.stat().st_mtime
            if self._config_mtime != mtime:
                from config.v2_config import V2Config

                v2_config = V2Config.load()
                self.config.language = v2_config.language
                self.config.show_duration = v2_config.show_duration
                self.config.ack_mode = v2_config.ack_mode
                self.config.include_time_info = v2_config.include_time_info
                self.config.include_user_info = v2_config.include_user_info
                self.config.reply_enhancements = v2_config.reply_enhancements
                self.config.agent_progress_style = v2_config.agent_progress_style
                self.config.agent_status_heartbeat_ms = v2_config.agent_status_heartbeat_ms
                self.config.agent_status_no_output_ms = v2_config.agent_status_no_output_ms
                self.config.resource_governance = v2_config.runtime.resource_governance
                governor = getattr(self, "_agent_resource_governor", None)
                if governor is not None:
                    governor.update_config(self.config.resource_governance)
                self.config.audio_asr = v2_config.audio_asr
                self.config.remote_access = v2_config.remote_access
                audio_asr_service = getattr(self, "audio_asr_service", None)
                if audio_asr_service is not None:
                    audio_asr_service.config = self.config

                mutable_platform_attrs = (
                    "require_mention",
                    "guild_allowlist",
                    "guild_denylist",
                    "thread_auto_archive_minutes",
                    "allowed_chat_ids",
                    "allowed_user_ids",
                    "disable_link_unfurl",
                    "forum_auto_topic",
                )
                for platform, client in self.im_clients.items():
                    im_cfg = getattr(client, "config", None)
                    if im_cfg is None:
                        continue
                    latest_platform_config = get_platform_descriptor(platform).get_config(v2_config)
                    if latest_platform_config is None:
                        continue
                    for attr in mutable_platform_attrs:
                        if hasattr(im_cfg, attr) and hasattr(latest_platform_config, attr):
                            setattr(im_cfg, attr, getattr(latest_platform_config, attr))

                self._config_mtime = mtime
        except Exception as err:
            logger.debug("Failed to reload config from disk: %s", err)

    def _migrate_language_from_settings(self) -> None:
        """Persist legacy per-channel language into global config if missing."""
        try:
            config_path = paths.get_config_path()
            if not config_path.exists():
                return
            config_payload = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(config_payload, dict) and "language" in config_payload:
                return

            settings_path = paths.get_settings_path()
            if not settings_path.exists():
                return
            settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
            channels = settings_payload.get("channels") if isinstance(settings_payload, dict) else None
            if not isinstance(channels, dict):
                return

            counts: dict[str, int] = {}
            supported_languages = set(get_supported_languages())
            for payload in channels.values():
                if not isinstance(payload, dict):
                    continue
                value = payload.get("language")
                if value in supported_languages:
                    counts[value] = counts.get(value, 0) + 1

            if not counts:
                return

            chosen = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if len(counts) > 1:
                logger.warning(
                    "Multiple per-channel languages found; using '%s' for global config (%s)",
                    chosen,
                    counts,
                )

            from config.v2_config import V2Config

            v2_config = V2Config.load()
            v2_config.language = chosen
            v2_config.save()
            self.config.language = chosen
            logger.info("Migrated legacy per-channel language to global config: %s", chosen)
        except Exception as err:
            logger.warning("Failed to migrate legacy language setting: %s", err)

    def _init_handlers(self):
        """Initialize all handlers with controller reference"""
        # Initialize session_handler first as other handlers depend on it
        self.session_handler = SessionHandler(self)
        self.command_handler = CommandHandlers(self)
        self.settings_handler = SettingsHandler(self)
        self.message_handler = MessageHandler(self)

        # Set cross-references between handlers
        self.message_handler.set_session_handler(self.session_handler)

    def _init_agents(self):
        from modules.agents.claude_agent import ClaudeAgent
        from modules.agents.codex import CodexAgent
        from modules.agents.opencode import OpenCodeAgent

        self.agent_service = AgentService(self)
        self.agent_service.register(ClaudeAgent(self))
        if self.config.codex:
            try:
                self.agent_service.register(CodexAgent(self, self.config.codex))
            except Exception as e:
                logger.error(f"Failed to initialize Codex agent: {e}")
        if self.config.opencode:
            try:
                self.agent_service.register(OpenCodeAgent(self, self.config.opencode))
            except Exception as e:
                logger.error(f"Failed to initialize OpenCode agent: {e}")

    def _setup_callbacks(self):
        """Setup callback connections between modules"""

        # Command handlers dict
        # Admin protection for "set_cwd" and "settings" is now handled by
        # the centralized auth pipeline (core.auth.check_auth) in IM entry points.
        command_handlers = {
            "start": self._dispatch_to_controller_loop(self.command_handler.handle_start),
            "new": self._dispatch_to_controller_loop(self.command_handler.handle_new),
            "cwd": self._dispatch_to_controller_loop(self.command_handler.handle_cwd),
            "set_cwd": self._dispatch_to_controller_loop(self.command_handler.handle_set_cwd),
            "resume": self._dispatch_to_controller_loop(self.command_handler.handle_resume),
            "setup": self._dispatch_to_controller_loop(self.command_handler.handle_setup),
            "settings": self._dispatch_to_controller_loop(self.settings_handler.handle_settings),
            "stop": self._dispatch_to_controller_loop(self.command_handler.handle_stop),
            "bind": self._dispatch_to_controller_loop(self.command_handler.handle_bind),
        }

        # IM inbound messages funnel through ``core.services.dispatch``
        # alongside the CLI and the upcoming Web UI / N3 socket path so all
        # three callers exercise the same business API. The lambda preserves
        # the existing ``(context, text)`` callback shape that the IM clients
        # know how to invoke.
        from core.services.dispatch import dispatch_turn

        async def _on_im_message(context, text):
            await dispatch_turn(self, context, text)

        # Register callbacks with the IM client
        self.im_client.register_callbacks(
            on_message=self._dispatch_im_message_to_controller_loop(_on_im_message),
            on_command=command_handlers,
            on_callback_query=self._dispatch_to_controller_loop(self.message_handler.handle_callback_query),
            on_settings_update=self._dispatch_to_controller_loop(self.settings_handler.handle_settings_update),
            on_change_cwd=self._dispatch_to_controller_loop(self.command_handler.handle_change_cwd_submission),
            on_routing_update=self._dispatch_to_controller_loop(self.settings_handler.handle_routing_update),
            on_routing_modal_update=self._dispatch_to_controller_loop(
                self.settings_handler.handle_routing_modal_update
            ),
            on_resume_session=self._dispatch_to_controller_loop(self.session_handler.handle_resume_session_submission),
            on_ready=self._dispatch_to_controller_loop(self._on_im_ready),
        )

    def _dispatch_to_controller_loop(self, callback):
        async def _wrapped(*args, **kwargs):
            loop = self._loop
            if loop is None:
                return await callback(*args, **kwargs)

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if current_loop is loop:
                return await callback(*args, **kwargs)

            future = asyncio.run_coroutine_threadsafe(callback(*args, **kwargs), loop)
            return await asyncio.wrap_future(future)

        return _wrapped

    def _dispatch_im_message_to_controller_loop(self, callback):
        tracked_platforms = {"telegram", "wechat"}

        async def _wrapped(context, *args, **kwargs):
            platform = self._platform_for_im_callback_context(context)
            if platform in tracked_platforms:
                return await self._run_on_controller_loop(callback, context, *args, **kwargs)
            self._schedule_controller_callback(callback, context, *args, **kwargs)
            return None

        return _wrapped

    def _platform_for_im_callback_context(self, context) -> str:
        platform = str(
            getattr(context, "platform", None)
            or (getattr(context, "platform_specific", None) or {}).get("platform")
            or ""
        ).strip()
        if platform:
            return platform
        im_client = getattr(self, "im_client", None)
        primary_platform = str(getattr(im_client, "primary_platform", "") or "").strip()
        if primary_platform:
            return primary_platform
        module = str(getattr(type(im_client), "__module__", "") or "")
        if module.startswith("modules.im.wechat"):
            return "wechat"
        if module.startswith("modules.im.telegram"):
            return "telegram"
        return ""

    def _dispatch_to_controller_loop_background(self, callback):
        async def _wrapped(*args, **kwargs):
            self._schedule_controller_callback(callback, *args, **kwargs)

        return _wrapped

    async def _run_on_controller_loop(self, callback, *args, **kwargs):
        loop = self._loop
        if loop is None:
            return await callback(*args, **kwargs)

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            return await callback(*args, **kwargs)

        future = asyncio.run_coroutine_threadsafe(callback(*args, **kwargs), loop)
        return await asyncio.wrap_future(future)

    def _schedule_controller_callback(self, callback, *args, **kwargs) -> None:
        async def _runner():
            await callback(*args, **kwargs)

        loop = self._loop
        if loop is None:
            task = asyncio.create_task(_runner())
            task.add_done_callback(self._log_background_callback_result)
            return

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is loop:
            task = loop.create_task(_runner())
            task.add_done_callback(self._log_background_callback_result)
            return

        future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        future.add_done_callback(self._log_background_callback_result)

    @staticmethod
    def _log_background_callback_result(future) -> None:
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            return
        except Exception:
            logger.error("Background IM message callback failed", exc_info=True)

    def _run_im_runtime(self) -> None:
        try:
            self.im_client.run()
        except BaseException as exc:  # noqa: BLE001
            self._im_run_exception = exc
            logger.error("IM runtime thread exited with error: %s", exc, exc_info=True)
        finally:
            loop = self._loop
            if loop and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)

    async def _on_im_ready(self):
        """Called when IM client is connected and ready.

        Used to restore active poll loops that were interrupted by restart.
        """
        logger.info("IM client ready, checking for active polls to restore...")
        opencode_agent = self.agent_service.agents.get("opencode")
        if opencode_agent and hasattr(opencode_agent, "restore_active_polls"):
            try:
                restored = await opencode_agent.restore_active_polls()  # type: ignore[attr-defined]
                if restored > 0:
                    logger.info(f"Restored {restored} active OpenCode poll(s)")
            except Exception as e:
                logger.error(f"Failed to restore active polls: {e}", exc_info=True)

        # Start update checker and send any pending post-update notification
        try:
            await self.update_checker.check_and_send_post_update_notification()
            self.update_checker.start()
        except Exception as e:
            logger.error(f"Failed to start update checker: {e}", exc_info=True)

        try:
            self.scheduled_task_service.start()
        except Exception as e:
            logger.error("Failed to start scheduled task service: %s", e, exc_info=True)
        try:
            self.watch_service.start()
        except Exception as e:
            logger.error("Failed to start watch service: %s", e, exc_info=True)
        try:
            await self.runtime_command_watcher.start()
        except Exception as e:
            logger.error("Failed to start runtime command watcher: %s", e, exc_info=True)

        claude_timeout, codex_timeout = self._get_idle_cleanup_timeouts()
        if (claude_timeout > 0 or codex_timeout > 0) and (
            self.cleanup_task is None or self.cleanup_task.done()
        ):
            self.cleanup_task = asyncio.create_task(self.periodic_cleanup())

    # Utility methods used by handlers

    def get_cwd(self, context: MessageContext) -> str:
        """Get the current cwd without creating an Agent Session row."""
        payload = context.platform_specific or {}
        source = str(payload.get("turn_source") or "human")
        return self.resolve_agent_run_target(context, source=source, create_session=False).workdir

    def resolve_agent_run_target(
        self,
        context: MessageContext,
        *,
        base_session_id: Optional[str] = None,
        source: str = "human",
        create_session: bool = True,
    ):
        """Resolve the shared execution target for one agent turn."""
        from core.services.agent_run_target import resolve_agent_run_target

        return resolve_agent_run_target(
            context,
            controller=self,
            base_session_id=base_session_id,
            source=source,
            create_session=create_session,
        )

    def _get_settings_key(self, context: MessageContext) -> str:
        """Get settings key based on context.

        For DM contexts, returns user_id so per-user settings apply.
        For channel contexts, returns channel_id for per-channel settings.

        Relies on the ``is_dm`` flag set by the IM layer in
        ``context.platform_specific`` (see Phase 2 of the refactoring).
        """
        is_dm = (context.platform_specific or {}).get("is_dm", False)
        return context.user_id if is_dm else context.channel_id

    def _get_session_key(self, context: MessageContext) -> str:
        """Get a globally unique session-scope key.

        Unlike ``_get_settings_key`` (which returns a raw ID for settings
        lookup routed by platform), this key must be unique across all
        platforms so that sessions, polls, and message-consolidation
        tracking never collide.
        """
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        settings_key = self._get_settings_key(context)
        return build_context_session_key(context, platform=platform, settings_key=settings_key)

    def backend_alive(self, context: MessageContext) -> Optional[bool]:
        """Best-effort backend liveness for the concise status bubble's footer.

        Delegates to ``AgentService.backend_alive`` (which dispatches to the
        per-backend probe). Returns ``None`` when unknown — the dispatcher
        treats ``None``/missing as alive so it never false-alarms ⚠️.
        """
        service = getattr(self, "agent_service", None)
        probe = getattr(service, "backend_alive", None)
        if not callable(probe):
            return None
        try:
            return probe(context)
        except Exception:
            logger.debug("backend_alive delegation failed", exc_info=True)
            return None

    # ---- concise status-bubble settings (read by the message dispatcher) ----

    def get_progress_style_for_context(self, context: MessageContext) -> str:
        """Resolve the process-message UX style for this context: concise|verbose|off.

        Currently a global config setting; per-channel overrides can layer on top
        here later without touching the dispatcher.
        """
        _refresh_status_bubble_config(self)
        value = getattr(self.config, "agent_progress_style", DEFAULT_AGENT_PROGRESS_STYLE)
        return value if value in ("concise", "verbose", "off") else DEFAULT_AGENT_PROGRESS_STYLE

    def uses_concise_status_bubble(self, context: MessageContext) -> bool:
        """True when this turn renders a concise status bubble (Slack/Discord +
        progress_style=concise). The single source of truth shared by the message
        dispatcher (which creates the bubble) and the processing indicator (which
        suppresses its ack-message/reaction so there is no duplicate signal)."""
        # Resolve platform with the SAME fallback the dispatcher's _get_platform
        # uses (config.platform) so the bubble-creation gate and this suppression
        # gate never disagree on an edge config; both then read the SAME
        # ``supports_status_bubble`` capability rather than a hardcoded platform set.
        platform = (
            context.platform
            or (context.platform_specific or {}).get("platform")
            or getattr(self.config, "platform", None)
            or self.primary_platform
        )
        if not get_platform_descriptor(platform).capabilities.supports_status_bubble:
            return False
        return self.get_progress_style_for_context(context) == "concise"

    def get_heartbeat_interval_ms_for_context(self, context: MessageContext) -> int:
        _refresh_status_bubble_config(self)
        value = getattr(self.config, "agent_status_heartbeat_ms", 8000)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 8000

    def get_no_output_hint_after_ms_for_context(self, context: MessageContext) -> int:
        _refresh_status_bubble_config(self)
        value = getattr(self.config, "agent_status_no_output_ms", 180000)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 180000

    def get_im_client_for_context(self, context: Optional[MessageContext] = None) -> BaseIMClient:
        if context is None:
            return self.im_clients[self.primary_platform]
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        client = self.im_clients.get(platform)
        if client is not None:
            return client
        removed_client = self._removed_im_clients.get(platform)
        if removed_client is not None:
            return removed_client
        return self.im_clients[self.primary_platform]

    def _get_im_client_for_platform(self, platform: str) -> BaseIMClient:
        client = self.im_clients.get(platform)
        if client is not None:
            return client
        removed_client = self._removed_im_clients.get(platform)
        if removed_client is not None:
            return removed_client
        return self.im_clients[self.primary_platform]

    # --- Streaming turn sinks -------------------------------------------
    # A live SSE caller registers a sink before dispatching a turn so the
    # async agent receiver can forward chunks to the open stream and mark the
    # turn complete. See ``core/services/dispatch.py`` and the
    # ``ConsolidatedMessageDispatcher._stream_chunk`` consumer.

    @property
    def active_turn_sinks(self) -> Dict[str, Dict[str, Any]]:
        # Owned by the turn owner (FSM); exposed here for back-compat readers.
        return self.session_turns.active_turn_sinks

    def register_turn_sink(self, session_key: str, *, on_chunk, done_event, turn_token=None, context=None) -> None:
        self.session_turns.register_turn_sink(
            session_key,
            on_chunk=on_chunk,
            done_event=done_event,
            turn_token=turn_token,
            context=context,
        )

    def pop_turn_sink(self, session_key: str, done_event=None) -> None:
        self.session_turns.pop_turn_sink(session_key, done_event)

    def get_turn_sink(self, session_key: str) -> Optional[Dict[str, Any]]:
        return self.session_turns.get_turn_sink(session_key)

    def bind_context_to_turn_sink(
        self,
        context: MessageContext,
        *,
        agent_session_id: Optional[str] = None,
        backend_base_session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self.session_turns.bind_context_to_turn_sink(
            context,
            agent_session_id=agent_session_id,
            backend_base_session_id=backend_base_session_id,
        )

    def settle_bound_turn_sink(self, binding: Optional[Dict[str, Any]]) -> bool:
        return self.session_turns.settle_bound_turn_sink(binding)

    def mark_turn_complete(self, context: Optional[MessageContext] = None) -> None:
        """Release a streaming turn sink whose turn finished WITHOUT emitting a
        result (missing/disabled backend, dedup, inline-stop, error, or any
        synchronous no-agent path) so the SSE dispatch closes promptly instead
        of waiting out the safety timeout. No-op for non-streaming turns or
        when an agent turn is genuinely in flight (the result emit releases it)."""
        if context is None:
            return
        sink = self.get_turn_sink(self._get_session_key(context))
        if sink is None:
            return
        # Turn-token guard (mirrors ``_stream_chunk`` / ``_is_active_turn``): a
        # SUPERSEDED or OLDER turn ending (a stopped turn whose backend later fires
        # turn/completed, or a scheduled/watch run that carries no token) must not
        # close the CURRENT turn's stream — the ONE active-turn token rule (shared
        # with _stream_chunk + _is_active_turn) decides if this emit is the live
        # turn's; a different OR absent token is stale, fail-open when tokenless.
        from core.session_turns import emit_matches_active_turn

        if not emit_matches_active_turn(sink, context):
            return
        done = sink.get("done_event")
        if done is not None:
            done.set()

    # ----- Live agent-runtime status (workbench sidebar dot) -------------
    #
    # ``agent_sessions.agent_status`` is idle/running/failed, written at EXACTLY
    # two chokepoints every turn funnels through — no per-path / per-backend
    # instrumentation:
    #   * inbound  — ``AgentService.handle_message`` flips the session to
    #     ``running`` (every source/backend dispatches through it).
    #   * outbound — ``MessageDispatcher.emit_agent_message`` settles the terminal
    #     ``result`` to ``idle`` (or ``failed`` when ``is_error``).
    # A fire-and-forget backend error surfaces as an emitted message, not an
    # exception, so terminal failures are emitted as ``result`` + ``is_error`` and
    # ride the same outbound chokepoint. ``set_agent_status`` is the shared writer;
    # ``SessionTurnManager.reset_stale`` recovers ``running`` rows to ``idle`` on
    # startup (a turn whose process died never reached the outbound chokepoint).

    @staticmethod
    def _session_id_from_context(context: Optional[MessageContext]) -> Optional[str]:
        spec = getattr(context, "platform_specific", None) or {}
        sid = spec.get("agent_session_id")
        return sid if isinstance(sid, str) and sid else None

    def set_agent_status(self, session_id: Optional[str], status: str) -> None:
        """Persist a session's agent_status and broadcast ``session.status``.

        Best-effort + idempotent: a no-op when the value is unchanged (the
        service reports it), when ``session_id`` is empty, or when the DB write
        fails. The realtime event rides the same controller→browser bus as
        ``turn.start`` / ``turn.end`` so the sidebar dot updates without a refetch.
        """

        if not session_id:
            return
        try:
            from core.services import sessions as workbench_sessions_service
            from storage.db import create_sqlite_engine

            engine = create_sqlite_engine()
            try:
                with engine.begin() as conn:
                    changed = workbench_sessions_service.set_agent_status(conn, session_id, status)
            finally:
                # Dispose the per-turn engine promptly: this fires on every
                # workbench turn start/end, so leaking it would pin SQLite
                # connections/FDs until GC under active Chat use (Codex P3).
                engine.dispose()
            if changed:
                from core.inbox_events import bus

                bus.publish("session.status", {"session_id": session_id, "agent_status": status})
        except Exception:
            logger.debug("set_agent_status failed for session=%s", session_id, exc_info=True)

    def get_settings_manager_for_context(self, context: Optional[MessageContext] = None) -> SettingsManager:
        if context is None:
            return self.platform_settings_managers[self.primary_platform]
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        return self.platform_settings_managers.get(platform, self.platform_settings_managers[self.primary_platform])

    def update_thread_message_id(self, context: MessageContext) -> None:
        """Update message tracking for consolidated log dispatch."""
        self.message_dispatcher.update_thread_message_id(context)

    async def clear_consolidated_message_id(
        self, context: MessageContext, trigger_message_id: Optional[str] = None
    ) -> None:
        """Clear consolidated message anchor so next log chunk starts fresh."""
        await self.message_dispatcher.clear_consolidated_message_id(context, trigger_message_id)

    def resolve_agent_for_context(self, context: MessageContext) -> str:
        """Unified agent resolution with dynamic override support.

        Priority:
        1. explicit/session Vibe Agent target
        2. existing session backend snapshot
        3. default Vibe Agent route
        4. AgentService.default_agent / first registered backend compatibility fallback
        """
        target = self._agent_run_target_payload(context)
        target_agent_name = target.get("agent_name") if target else None
        target_backend = target.get("agent_backend") if target else None
        if target_agent_name:
            vibe_agent = self.resolve_vibe_agent_for_context(
                context,
                override_agent_name=str(target_agent_name),
                required=False,
            )
            if vibe_agent:
                return vibe_agent.backend
        if target_backend and str(target_backend) in {"opencode", "claude", "codex"}:
            return str(target_backend)

        vibe_agent = self.resolve_vibe_agent_for_context(context, required=False)
        if vibe_agent:
            return vibe_agent.backend

        return self._fallback_registered_agent_backend()

    def _fallback_registered_agent_backend(self) -> str:
        default_agent = getattr(self.agent_service, "default_agent", None)
        registered = getattr(self.agent_service, "agents", {})
        if default_agent in registered:
            return str(default_agent)
        if registered:
            return next(iter(registered))
        return DEFAULT_AGENT_BACKEND

    def resolve_vibe_agent_for_context(
        self,
        context: MessageContext,
        *,
        override_agent_name: Optional[str] = None,
        required: bool = True,
    ) -> Optional[VibeAgent]:
        target = self._agent_run_target_payload(context)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        if platform == "avibe" and target:
            routing = None
        else:
            settings_key = self._get_settings_key(context)
            settings_manager = self.get_settings_manager_for_context(context)
            routing = settings_manager.get_channel_routing(settings_key)
        agent_name = override_agent_name or (target.get("agent_name") if target else None) or (
            routing.agent_name if routing else None
        )
        try:
            if agent_name:
                return self.vibe_agent_store.require_enabled(agent_name)
            default_agent = self.vibe_agent_store.get_default_agent()
            if default_agent is not None:
                return default_agent
            if required:
                return self.vibe_agent_store.require("default")
            return None
        except Exception as exc:
            if required:
                raise
            logger.warning("Scope references Vibe Agent '%s' but it cannot be resolved: %s", agent_name or "default", exc)
            return None

    @staticmethod
    def _agent_run_target_payload(context: MessageContext) -> dict[str, Any]:
        payload = context.platform_specific or {}
        target = payload.get("agent_run_target")
        if isinstance(target, dict):
            return target
        session_target = payload.get("agent_session_target")
        return session_target if isinstance(session_target, dict) else {}

    def get_opencode_overrides(self, context: MessageContext) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Get OpenCode agent, model, and reasoning effort overrides for this channel.

        Returns:
            Tuple of (opencode_agent, opencode_model, opencode_reasoning_effort)
            or (None, None, None) if no overrides.
        """
        target = self._agent_run_target_payload(context)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        if platform == "avibe" and target:
            return (
                _target_agent_variant(
                    target.get("agent_variant"),
                    target.get("agent_backend"),
                    target.get("agent_name"),
                ),
                _optional_target_str(target.get("model")),
                _optional_target_str(target.get("reasoning_effort")),
            )
        settings_key = self._get_settings_key(context)
        settings_manager = self.get_settings_manager_for_context(context)
        routing = settings_manager.get_channel_routing(settings_key)
        if routing:
            from config.v2_settings import routing_model_for_backend, routing_reasoning_effort_for_backend

            return (
                routing.opencode_agent,
                routing_model_for_backend(routing, "opencode"),
                routing_reasoning_effort_for_backend(routing, "opencode"),
            )
        return None, None, None

    def get_codex_overrides(self, context: MessageContext) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Get Codex agent, model, and reasoning effort overrides for this channel."""
        target = self._agent_run_target_payload(context)
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        if platform == "avibe" and target:
            return (
                _target_agent_variant(
                    target.get("agent_variant"),
                    target.get("agent_backend"),
                    target.get("agent_name"),
                ),
                _optional_target_str(target.get("model")),
                _optional_target_str(target.get("reasoning_effort")),
            )
        settings_key = self._get_settings_key(context)
        settings_manager = self.get_settings_manager_for_context(context)
        routing = settings_manager.get_channel_routing(settings_key)
        if routing:
            from config.v2_settings import routing_model_for_backend, routing_reasoning_effort_for_backend

            return (
                routing.codex_agent,
                routing_model_for_backend(routing, "codex"),
                routing_reasoning_effort_for_backend(routing, "codex"),
            )
        return None, None, None

    async def emit_agent_message(
        self,
        context: MessageContext,
        message_type: str,
        text: str,
        parse_mode: Optional[str] = "markdown",
        *,
        is_error: bool = False,
        level: str = "normal",
        status_label: Optional[str] = None,
    ):
        """Backward-compatible entrypoint; delegated to message dispatcher."""
        return await self.message_dispatcher.emit_agent_message(
            context=context,
            message_type=message_type,
            text=text,
            parse_mode=parse_mode,
            is_error=is_error,
            level=level,
            status_label=status_label,
        )

    def note_session_tokens(self, context: MessageContext, *, total: int) -> None:
        """Report the session's current context-window occupancy for the status
        footer (backend-agnostic). SETs an absolute snapshot; the next footer render
        shows it. No-op if the dispatcher is unavailable (partially-wired test
        controllers)."""
        dispatcher = getattr(self, "message_dispatcher", None)
        if dispatcher is None:
            return
        dispatcher.note_session_tokens(context, total=total)

    # Main run method
    def run(self):
        """Run the controller"""
        logger.info("Starting Claude Proxy Controller with platforms: %s", ", ".join(self.enabled_platforms))

        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._im_thread = threading.Thread(target=self._run_im_runtime, name="im-runtime", daemon=True)
            self._im_thread.start()
            # Internal Unix-socket ASGI server for the Web UI / future
            # ``vibe agent run --sync`` cross-process callers. Lives on
            # the same loop as the IM dispatch path so they share one
            # asyncio scheduler. See core/internal_server.py.
            try:
                from core import internal_server as _internal_server

                self._internal_server_task = _internal_server.start(self)
            except Exception:
                logger.exception("internal dispatch server failed to schedule; UI fallback will use the queue path")
                self._internal_server_task = None
            self._loop.run_forever()
            if self._im_run_exception and not isinstance(self._im_run_exception, (KeyboardInterrupt, SystemExit)):
                raise self._im_run_exception
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Error in main run loop: {e}", exc_info=True)
        finally:
            self.cleanup_sync()
            # Best-effort: remove the dispatch socket so the next controller
            # boot starts from a clean filesystem state. uvicorn unlinks
            # the path on exit when it bound the socket itself, but it
            # can be left behind on hard crashes.
            try:
                from core import internal_server as _internal_server

                sock_path = _internal_server.default_socket_path()
                if sock_path.exists():
                    sock_path.unlink()
            except Exception:
                pass
            if self._loop is not None:
                try:
                    self._loop.stop()
                except Exception:
                    pass
                self._loop.close()
                self._loop = None

    def _get_idle_cleanup_timeouts(self) -> tuple[int, int]:
        """Return normalized idle cleanup timeouts for Claude and Codex."""
        claude_config = getattr(self.config, "claude", None)
        codex_config = getattr(self.config, "codex", None)
        claude_timeout = int(
            max(0, getattr(claude_config, "idle_timeout_seconds", DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS) or 0)
        )
        codex_timeout = (
            int(max(0, getattr(codex_config, "idle_timeout_seconds", DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS) or 0))
            if codex_config is not None
            else 0
        )
        return claude_timeout, codex_timeout

    async def periodic_cleanup(self):
        """Sweep idle backend runtime state without interrupting active work."""
        claude_timeout, codex_timeout = self._get_idle_cleanup_timeouts()
        enabled_timeouts = [timeout for timeout in (claude_timeout, codex_timeout) if timeout > 0]
        if not enabled_timeouts:
            logger.info("Idle cleanup disabled for Claude and Codex.")
            return

        sweep_interval = max(min(enabled_timeouts) // 6, 60)
        logger.info(
            "Starting idle cleanup loop (interval=%ss, claude_timeout=%ss, codex_timeout=%ss)",
            sweep_interval,
            claude_timeout,
            codex_timeout,
        )

        try:
            while True:
                await asyncio.sleep(sweep_interval)

                if claude_timeout > 0:
                    try:
                        await self.session_handler.evict_idle_sessions(claude_timeout)
                    except Exception as e:
                        logger.error("Claude idle cleanup failed: %s", e, exc_info=True)
                    try:
                        # Defense-in-depth: reconcile live claude subprocesses
                        # against tracked sessions and reap orphans (no-owner /
                        # cross-restart) the idle-eviction path cannot see.
                        await self.session_handler.reap_orphaned_claude_sessions()
                    except Exception as e:
                        logger.error("Claude orphan reaper failed: %s", e, exc_info=True)

                if codex_timeout > 0:
                    codex_agent = self.agent_service.agents.get("codex")
                    if codex_agent and hasattr(codex_agent, "evict_idle_transports"):
                        try:
                            await codex_agent.evict_idle_transports(codex_timeout)
                        except Exception as e:
                            logger.error("Codex idle cleanup failed: %s", e, exc_info=True)
        except asyncio.CancelledError:
            logger.info("Idle cleanup loop stopped.")
            raise

    def cleanup_sync(self):
        """Best-effort synchronous cleanup without cross-loop awaits"""
        logger.info("Cleaning up controller resources (sync, best-effort)...")

        def _stop_loop_coroutine(coro, label: str) -> None:
            try:
                loop = self._loop
                if not loop or loop.is_closed():
                    return
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(coro, loop)
                    future.result(timeout=5)
                    return
                loop.run_until_complete(coro)
            except Exception as e:
                logger.debug(f"{label} cleanup skipped: {e}")

        # Stop update checker
        try:
            update_task = self.update_checker.stop()
            if update_task and not update_task.done():
                loop = self._loop
                if loop and not loop.is_running() and not loop.is_closed():
                    try:
                        loop.run_until_complete(self.update_checker.wait_stopped(update_task))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Update checker cleanup skipped: {e}")

        async def _cancel_cleanup_task() -> None:
            if self.cleanup_task and not self.cleanup_task.done():
                self.cleanup_task.cancel()
                try:
                    await self.cleanup_task
                except asyncio.CancelledError:
                    pass
            self.cleanup_task = None

        _stop_loop_coroutine(_cancel_cleanup_task(), "Idle cleanup task")
        _stop_loop_coroutine(self.scheduled_task_service.stop(), "Scheduled task service")
        _stop_loop_coroutine(self.watch_service.stop(), "Watch service")
        _stop_loop_coroutine(self.runtime_command_watcher.stop(), "Runtime command watcher")

        try:
            codex_agent = self.agent_service.agents.get("codex")
            if codex_agent and hasattr(codex_agent, "shutdown_runtime"):
                _stop_loop_coroutine(codex_agent.shutdown_runtime(), "Codex runtime")
        except Exception as e:
            logger.debug(f"Codex runtime cleanup skipped: {e}")

        # Cancel receiver tasks without awaiting (they may belong to other loops)
        try:
            for session_id, task in list(self.receiver_tasks.items()):
                if not task.done():
                    task.cancel()
                # Remove from registry regardless
                del self.receiver_tasks[session_id]
        except Exception as e:
            logger.debug(f"Receiver tasks cleanup skipped due to: {e}")

        # Do not attempt to await SessionHandler cleanup here to avoid cross-loop issues.
        # Active connections will be closed by process exit; mappings are persisted separately.

        # Attempt to call stop if it's a plain function; skip if coroutine to avoid cross-loop awaits
        try:
            stop_attr = getattr(self.im_client, "stop", None)
            if callable(stop_attr):
                import inspect

                if not inspect.iscoroutinefunction(stop_attr):
                    stop_attr()
        except Exception as e:
            logger.warning("Failed to stop IM client: %s", e)

        # Best-effort async shutdown for IM clients
        try:
            shutdown_attr = getattr(self.im_client, "shutdown", None)
            if callable(shutdown_attr):
                import inspect

                if inspect.iscoroutinefunction(shutdown_attr):
                    loop = self._loop
                    if loop and loop.is_running():
                        try:
                            future = asyncio.run_coroutine_threadsafe(shutdown_attr(), loop)
                            future.result(timeout=5)
                        except Exception:
                            pass
                else:
                    shutdown_attr()
        except Exception as e:
            logger.warning("Failed to shutdown IM client: %s", e)

        if self._im_thread and self._im_thread.is_alive():
            self._im_thread.join(timeout=5)
        self._im_thread = None

        # Stop OpenCode server if running
        try:
            from modules.agents.opencode import OpenCodeServerManager

            OpenCodeServerManager.stop_instance_sync()
        except Exception as e:
            logger.debug(f"OpenCode server cleanup skipped: {e}")

        logger.info("Controller cleanup (sync) complete")
