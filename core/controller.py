"""Core controller that coordinates between modules and handlers"""

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Optional, Dict, Any, TypeVar
from config import paths
from config.platform_registry import get_platform_descriptor
from config.v2_config import (
    DEFAULT_AGENT_BACKEND,
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_PROGRESS_STYLE,
    MemoryConfig,
    atomic_update_memory,
)
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
from core.message_output import MessageOutput
from core.processing_indicator import ProcessingIndicatorService
from core.run_settlement import SETTLED_BY_NO_TERMINAL_RESULT
from core.runtime_commands import RuntimeCommandWatcher
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import RuntimeOwnershipProvider
from core.runtime_recovery import SessionDeliveryRecoveryHandler
from core.runtime_work import RuntimeWorkLane, RuntimeWorkSupervisor
from core.scheduled_tasks import ScheduledTaskService
from core.show_git import ShowGitCheckpointService
from core.update_checker import UpdateChecker
from core.watches import ManagedWatchService
from core.vibe_agents import VibeAgent, VibeAgentStore
from core.memory import CaptureReceipt, CaptureRequest, CaptureSkipped
from core.memory.admission import (
    WORKBENCH_PLATFORM,
    CaptureAdmission,
    InboundTurnFacts,
)
from core.memory.blocking import run_blocking
from core.memory.operation_lock import MemoryOperationBusy, MemoryOperationLease
from vibe.i18n import get_supported_languages, t as i18n_t

logger = logging.getLogger(__name__)

_RUNTIME_WORK_SHUTDOWN_GRACE_SECONDS = 10.0
_MemorySessionLifecycleResult = TypeVar("_MemorySessionLifecycleResult")


class _SettingsUserBindings:
    """Answer Memory's binding question from the per-platform settings stores."""

    def __init__(self, managers: object) -> None:
        self._managers = managers if isinstance(managers, dict) else {}

    def is_enabled_user(self, platform: str, user_id: str) -> bool:
        manager = self._managers.get(platform)
        if manager is None:
            return False
        store = manager.get_store()
        store.maybe_reload()
        user = store.get_user(user_id, platform=platform)
        return bool(user is not None and user.enabled)


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
        self._shutdown_requested = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._runtime_work_shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_tainted = False
        self._service_lock_safe_to_release = False
        self._runtime_work_shutdown_grace_seconds = (
            _RUNTIME_WORK_SHUTDOWN_GRACE_SECONDS
        )
        self.enabled_platforms = list(getattr(config, "enabled_platforms", lambda: [config.platform])())
        self.primary_platform = getattr(getattr(config, "platforms", None), "primary", config.platform)
        self._reconcile_lock: Optional[asyncio.Lock] = None
        self._removed_im_clients: Dict[str, BaseIMClient] = {}
        self._memory_scopes_by_session: Dict[str, tuple[str, str]] = {}
        self._memory_cli_facts_by_session: Dict[str, InboundTurnFacts] = {}

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
        # once the internal server is built on the loop. Persisted Session inputs
        # route through it so their source policy can queue, steer, or replace via
        # the same durable lifecycle (in_flight + turn.start / turn.end + Stop).
        # ``None`` until the server is up; callers then fall back to the direct path.
        self.session_turn_gate: Optional[Any] = None

        # Per-session turn owner (FSM). Created here so the controller owns it from
        # birth — boot stale-reset (below) and the OpenCode poll restore both run
        # before the internal server binds. ``core.internal_server.create_app`` later
        # binds the routing-context builder + exposes the gate endpoints; the gate,
        # dispatcher, and scheduler all share this one owner's in_flight + flush state.
        from core.session_turns import SessionTurnManager

        self.runtime_activation = RuntimeActivationRegistry()
        self.session_turns = SessionTurnManager(self)
        self.runtime_ownership = RuntimeOwnershipProvider(
            self.session_turns._sqlite_engine()
        )
        self.runtime_work_supervisor = RuntimeWorkSupervisor(
            on_lease_lost=lambda: self.request_shutdown("service lease lost")
        )
        self._runtime_work_tokens = [
            self.runtime_work_supervisor.register(
                RuntimeWorkLane.SESSION_DELIVERIES,
                SessionDeliveryRecoveryHandler(self.session_turns),
            )
        ]
        # The internal server publishes the Session gate before waiting on this
        # event. Controller startup owns backend restoration, durable owner
        # recovery, and supervisor activation, then releases HTTP serving and
        # the scheduler/watch services together.
        self._delivery_recovery_complete = asyncio.Event()

        self._init_model_hub()

        # Initialize core modules
        self._init_modules()

        # Initialize handlers
        self._init_handlers()

        # Initialize agents (depends on handlers/session handler)
        self._init_agents()
        self.agent_auth_service = AgentAuthService(self)
        from core.backend_restart import BackendRestartCoordinator

        self.backend_restart_coordinator = BackendRestartCoordinator(
            self,
            self.agent_auth_service._apply_backend_runtime_refresh,
        )

        self.vibe_agent_store = VibeAgentStore()
        self.vibe_agent_store.ensure_builtin_default_agents(
            self._enabled_agent_backends(),
        )

        # Setup callbacks
        self._setup_callbacks()

        # Consolidated message dispatcher
        self.message_dispatcher = ConsolidatedMessageDispatcher(self)
        self.scheduled_task_service = ScheduledTaskService(self)
        self._runtime_work_tokens.extend(
            self.scheduled_task_service.register_controller_runtime_work_lanes()
        )
        self.watch_service = ManagedWatchService(self)
        self.runtime_command_watcher = RuntimeCommandWatcher(self)
        self.show_git_checkpoint_service = ShowGitCheckpointService()

        # Background task for cleanup
        self.cleanup_task: Optional[asyncio.Task] = None
        self._memory_reconcile_task: Optional[asyncio.Task] = None
        self._memory_replacement_gate = asyncio.Lock()
        self._memory_factory_reset_task: Optional[asyncio.Task[dict[str, Any]]] = None

        # Initialize update checker (use default config if not present)
        from config.v2_config import UpdateConfig

        update_config = getattr(config, "update", None) or UpdateConfig()
        self.update_checker = UpdateChecker(self, update_config)

        # Restore session mappings on startup (after handlers are initialized)
        self.session_handler.restore_session_mappings()

        # Clean only pre-durable status projections. Durable Turn owners remain
        # running until backend restoration and exact reconciliation complete.
        self.session_turns.reset_legacy_ownerless_status()

    def _init_model_hub(self) -> None:
        """Create the Model Hub aggregate only for an explicit release opt-in."""

        from config.v2_config import is_model_hub_enabled

        self.model_hub_service = None
        self.model_hub_turn_gateway = None
        self.model_hub_runtime = None
        if not is_model_hub_enabled():
            return

        # The controller is the single Model Hub aggregate and engine owner.
        # The UI process reaches this instance through the internal Unix socket.
        from core.handlers.model_hub import create_default_service
        from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
        from modules.agents.model_hub import ModelHubRuntimeRouter

        def default_vibe_agent_model(backend: str) -> Optional[str]:
            agent = self.vibe_agent_store.get_default_agent()
            if agent is None or agent.backend != backend:
                return None
            return agent.model

        def default_vibe_agent_name(backend: str) -> Optional[str]:
            agent = self.vibe_agent_store.get_default_agent()
            if agent is None or agent.backend != backend or not str(agent.model or "").strip():
                return None
            return agent.name

        def named_vibe_agents(backend: str) -> list[tuple[str, Optional[str]]]:
            return [
                (agent.name, agent.model)
                for agent in self.vibe_agent_store.list_agents(include_disabled=False)
                if agent.backend == backend
            ]

        cli_presence: dict[str, bool] = {}

        def cli_present(backend: str) -> bool:
            # Payload assembly runs on the controller loop. Read only the last
            # worker-produced snapshot here; a missing/erroring probe is false.
            return cli_presence.get(backend, False)

        def refresh_cli_presence() -> None:
            from config.v2_config import V2Config
            from vibe.api import resolve_cli_path

            try:
                v2_config = V2Config.load()
            except FileNotFoundError:
                v2_config = None
            for backend in ("claude", "codex", "opencode"):
                backend_config = getattr(getattr(v2_config, "agents", None), backend, None)
                configured_path = getattr(backend_config, "cli_path", None) or backend
                try:
                    cli_presence[backend] = resolve_cli_path(str(configured_path)) is not None
                except Exception:
                    logger.warning("Model Hub CLI presence probe failed for %s", backend, exc_info=True)
                    cli_presence[backend] = False

        self.model_hub_service = create_default_service(
            requested_model_override=default_vibe_agent_model,
            selected_agent_override=default_vibe_agent_name,
            named_agents_override=named_vibe_agents,
            cli_present_override=cli_present,
            cli_presence_refresh=refresh_cli_presence,
        )
        self.model_hub_turn_gateway = ModelHubTurnGateway(
            self.model_hub_service,
            language_provider=lambda: self.config.language,
        )
        self.model_hub_runtime = ModelHubRuntimeRouter(
            service=self.model_hub_service,
            turn_gateway=self.model_hub_turn_gateway,
        )

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
        # The runtime serves controller UDS reads/capture and the shared
        # private-IM Memory admission path.
        from core.memory.runtime import create_memory_runtime

        self.memory_runtime = create_memory_runtime(
            getattr(self.config, "memory", None) or MemoryConfig(),
            processing_event=self._send_memory_processing_event,
            on_config_settled=self._adopt_settled_memory_config,
        )
        self.memory_module = self.memory_runtime.module
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

    def _adopt_settled_memory_config(self, memory_config: MemoryConfig) -> None:
        """Publish a rebuild settlement into the live Controller snapshot."""

        self.config.memory = deepcopy(memory_config)

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

    async def reconcile_agent_backends(self, backends: list[str]) -> dict[str, Any]:
        """Hot-apply persisted config for the requested Agent backends."""
        from modules.agents.catalog import AGENT_BACKENDS

        requested: list[str] = []
        for backend in backends:
            if backend not in AGENT_BACKENDS:
                raise ValueError(f"Unsupported agent backend: {backend}")
            if backend not in requested:
                requested.append(backend)

        states: dict[str, str] = {}
        for backend in requested:
            states[backend] = await self.backend_restart_coordinator.request_restart(backend)

        logger.info("Hot-reconciled Agent backends: %s", states)
        return {
            "ok": True,
            "backends": requested,
            "states": states,
        }

    async def reconcile_memory(self, memory_config: MemoryConfig) -> dict[str, Any]:
        """Hot-apply persisted Memory settings without restarting Avibe."""

        if getattr(memory_config, "recovery_intent", None) == "factory_reset":
            return await self.factory_reset_memory()
        async with self._memory_replacement_lock():
            runtime = getattr(self, "memory_runtime", None)
            if runtime is None:
                return {"ok": False, "error": "memory_factory_reset_failed"}
            result = await runtime.reconcile(memory_config)
            self.memory_module = runtime.module
            if result.get("ok") is True:
                self.config.memory = memory_config
            return result

    async def capture_memory(self, request: CaptureRequest) -> CaptureReceipt:
        """Capture through the replacement gate so stale modules cannot write."""

        observed_runtime = getattr(self, "memory_runtime", None)
        if self._memory_factory_reset_pending(observed_runtime):
            return CaptureSkipped(reason="memory_operation_in_progress")
        if observed_runtime is None or getattr(observed_runtime, "retired", False):
            return CaptureSkipped(reason="memory_operation_in_progress")
        async with self._memory_replacement_lock():
            runtime = getattr(self, "memory_runtime", None)
            if (
                self._memory_factory_reset_pending(runtime)
                or runtime is not observed_runtime
                or runtime is None
                or getattr(runtime, "retired", False)
            ):
                return CaptureSkipped(reason="memory_operation_in_progress")
            if not runtime.available:
                return CaptureSkipped(reason="memory_store_unavailable")
            return await runtime.module.capture(request)

    async def factory_reset_memory(self) -> dict[str, Any]:
        """Delete Memory's two mutable roots and atomically publish a fresh Runtime.

        Calls join one retained task so a request cancellation cannot abandon the
        reset or let a second mutation race the replacement gate.
        """

        task = getattr(self, "_memory_factory_reset_task", None)
        if task is None or task.done():
            task = asyncio.create_task(
                self._factory_reset_memory_once(),
                name="memory-factory-reset",
            )
            self._memory_factory_reset_task = task

            def clear_factory_reset(completed: asyncio.Task[dict[str, Any]]) -> None:
                if self._memory_factory_reset_task is completed:
                    self._memory_factory_reset_task = None

            task.add_done_callback(clear_factory_reset)
        return await asyncio.shield(task)

    async def _factory_reset_memory_once(self) -> dict[str, Any]:
        from core.memory.factory_reset import delete_memory_roots, unchanged_memory_reset_result
        from core.memory.runtime import create_memory_runtime

        async with self._memory_replacement_lock():
            runtime = getattr(self, "memory_runtime", None)
            if runtime is None:
                return unchanged_memory_reset_result(
                    reason="memory_runtime_unavailable"
                )

            lease = MemoryOperationLease(runtime.effective_home)
            try:
                await run_blocking(lease.acquire)
            except MemoryOperationBusy:
                return {
                    "ok": False,
                    "error": "memory_operation_in_progress",
                    "result": "failed",
                }
            except Exception:
                logger.exception("Memory factory reset could not acquire operation lease")
                return unchanged_memory_reset_result(
                    runtime.effective_home,
                    reason="operation_lock_failed",
                )

            try:
                try:
                    repair_running = getattr(runtime, "_repair_running", None)
                    if callable(repair_running) and repair_running():
                        return {
                            "ok": False,
                            "error": "memory_operation_in_progress",
                            "result": "failed",
                        }
                    reap_sync = getattr(runtime, "_reap_recorded_sync_if_unowned", None)
                    if callable(reap_sync):
                        await reap_sync(fail_closed=True)
                except Exception:
                    logger.exception(
                        "Memory factory reset could not reap a recorded cascade sync"
                    )
                    return unchanged_memory_reset_result(
                        runtime.effective_home,
                        reason="sync_recovery_failed",
                    )

                try:
                    # A restart can construct a Runtime with no in-memory
                    # supervisor even while its recorded sidecar survives.
                    # Reap that ownership under the operation/root fence before
                    # any reset path is allowed to delete the provider root.
                    await runtime._reap_recorded_sidecar_if_unowned(
                        fail_closed=True,
                    )
                except Exception:
                    logger.exception("Memory factory reset could not reap a recorded sidecar")
                    return unchanged_memory_reset_result(
                        runtime.effective_home,
                        reason="sidecar_recovery_failed",
                    )

                # Keep a defensive admission check for runtimes that expose the
                # in-flight installer flag before they acquire the shared lease.
                if getattr(runtime, "_artifact_installing", False):
                    return {
                        "ok": False,
                        "error": "memory_operation_in_progress",
                        "result": "failed",
                    }
                if not runtime.artifact_admitted():
                    return unchanged_memory_reset_result(
                        runtime.effective_home,
                        reason="artifact_repair_required",
                    )

                try:
                    candidate = await asyncio.to_thread(self._mark_factory_reset_intent)
                except Exception:
                    logger.exception("Memory factory reset could not persist intent")
                    return unchanged_memory_reset_result(
                        runtime.effective_home,
                        reason="intent_persist_failed",
                    )

                # A retry may arrive with the prior aggregate already tombstoned.
                if not runtime.retired:
                    runtime.adopt_recovery_intent(candidate)
                    runtime.retire()
                if not getattr(runtime, "closed", False):
                    try:
                        await runtime.close()
                    except Exception:
                        logger.exception("Memory factory reset could not retire Runtime")
                        return unchanged_memory_reset_result(
                            runtime.effective_home,
                            reason="runtime_retirement_failed",
                        )

                deletion = await asyncio.to_thread(
                    delete_memory_roots,
                    runtime.effective_home,
                )
                payload = deletion.payload()
                if deletion.data_remaining:
                    repairable = None
                    try:
                        repairable = create_memory_runtime(
                            candidate,
                            artifact_manager=getattr(runtime, "artifact_manager", None),
                            process_factory=getattr(runtime, "process_factory", None),
                            effective_home=runtime.effective_home,
                            processing_event=self._send_memory_processing_event,
                            on_config_settled=self._adopt_settled_memory_config,
                        )
                        if not await self._retain_failed_factory_reset_runtime(
                            repairable,
                            candidate,
                        ):
                            repairable.retire()
                            await repairable.close()
                    except Exception:
                        logger.exception(
                            "Memory factory reset could not retain a repairable Runtime"
                        )
                        if repairable is not None and not getattr(repairable, "closed", False):
                            try:
                                repairable.retire()
                                await repairable.close()
                            except Exception:
                                logger.exception(
                                    "Failed partial-reset Runtime could not close"
                                )
                    return {
                        "ok": False,
                        "error": "memory_factory_reset_failed",
                        "result": "partial",
                        **payload,
                    }

                settled = replace(candidate, recovery_intent=None)
                fresh = None
                try:
                    fresh = create_memory_runtime(
                        settled,
                        artifact_manager=runtime.artifact_manager,
                        process_factory=runtime.process_factory,
                        effective_home=runtime.effective_home,
                        processing_event=self._send_memory_processing_event,
                        on_config_settled=self._adopt_settled_memory_config,
                    )
                    activation = await fresh.activate_fresh(settled)
                except Exception:
                    logger.exception("Memory factory reset could not activate fresh Runtime")
                    if fresh is not None:
                        if not await self._retain_failed_factory_reset_runtime(fresh, candidate):
                            fresh.retire()
                            try:
                                await fresh.close()
                            except Exception:
                                logger.exception("Failed fresh Memory Runtime could not close")
                    return {
                        "ok": False,
                        "error": "memory_factory_reset_failed",
                        "result": "deleted_activation_failed",
                        **payload,
                    }
                self.memory_runtime = fresh
                self.memory_module = fresh.module
                if activation.get("ok") is not True:
                    if not await self._retain_failed_factory_reset_runtime(fresh, candidate):
                        fresh.retire()
                        try:
                            await fresh.close()
                        except Exception:
                            logger.exception("Failed fresh Memory Runtime could not close")
                    return {
                        "ok": False,
                        "error": "memory_factory_reset_failed",
                        "result": "deleted_activation_failed",
                        **payload,
                    }
                try:
                    persisted = await asyncio.to_thread(self._clear_factory_reset_intent)
                except Exception:
                    fresh.retire()
                    try:
                        await fresh.close()
                    except Exception:
                        logger.exception("Unsettled fresh Memory Runtime could not close")
                    return {
                        "ok": False,
                        "error": "memory_factory_reset_failed",
                        "result": "deleted_activation_failed",
                        **payload,
                    }
                fresh.adopt_recovery_intent(persisted)
                self.config.memory = persisted
                return {"ok": True, "result": "completed", **payload}
            finally:
                await run_blocking(lease.release)

    @staticmethod
    def _mark_factory_reset_intent() -> MemoryConfig:
        def mark(current: MemoryConfig) -> MemoryConfig:
            return replace(current, recovery_intent="factory_reset")

        return atomic_update_memory(mark).memory

    @staticmethod
    def _clear_factory_reset_intent() -> MemoryConfig:
        def clear(current: MemoryConfig) -> MemoryConfig:
            if current.recovery_intent != "factory_reset":
                raise ValueError("factory reset intent is no longer pending")
            return replace(current, recovery_intent=None)

        return atomic_update_memory(clear).memory

    def _memory_replacement_lock(self) -> asyncio.Lock:
        """Lazily provide the gate for lightweight Controller test doubles."""

        gate = getattr(self, "_memory_replacement_gate", None)
        if gate is None:
            gate = asyncio.Lock()
            self._memory_replacement_gate = gate
        return gate

    def _memory_factory_reset_pending(self, runtime: object | None = None) -> bool:
        """Reject captures while an in-flight or durable reset fence remains."""

        if self._memory_factory_reset_running():
            return True
        runtime = runtime if runtime is not None else getattr(self, "memory_runtime", None)
        if getattr(runtime, "factory_reset_pending", False) is True:
            return True
        for snapshot in (
            getattr(runtime, "_config", None),
            getattr(runtime, "_restart_config", None),
            getattr(getattr(self, "config", None), "memory", None),
        ):
            if getattr(snapshot, "recovery_intent", None) == "factory_reset":
                return True
        return False

    async def _retain_failed_factory_reset_runtime(
        self,
        runtime: object,
        candidate: MemoryConfig,
    ) -> bool:
        """Publish a fenced fresh Runtime so Repair can recover it in place."""

        try:
            retain = getattr(runtime, "retain_factory_reset_recovery", None)
            if callable(retain):
                result = retain(candidate)
                if hasattr(result, "__await__"):
                    await result
            else:
                adopt = getattr(runtime, "adopt_recovery_intent", None)
                if not callable(adopt):
                    return False
                adopt(candidate)
                pause_claims = getattr(getattr(runtime, "module", None), "pause_claims", None)
                if callable(pause_claims):
                    pause_claims()
            self.memory_runtime = runtime
            self.memory_module = getattr(runtime, "module", getattr(self, "memory_module", None))
            return True
        except Exception:
            logger.exception("Failed to retain fresh Memory Runtime for factory-reset repair")
            return False

    def _memory_factory_reset_running(self) -> bool:
        """Return whether the retained factory-reset task still owns admission."""

        task = getattr(self, "_memory_factory_reset_task", None)
        done = getattr(task, "done", None)
        return task is not None and callable(done) and not done()

    async def _join_memory_factory_reset_task(self) -> None:
        """Settle the retained reset before selecting the runtime to close."""

        task = getattr(self, "_memory_factory_reset_task", None)
        if task is None or task is asyncio.current_task():
            return
        cancellation: asyncio.CancelledError | None = None
        current = asyncio.current_task()
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    if current is not None and current.cancelling():
                        cancellation = cancellation or error
                except Exception as error:
                    logger.debug("Memory factory reset already failed during shutdown: %s", error)
                    break
        finally:
            if getattr(self, "_memory_factory_reset_task", None) is task and task.done():
                self._memory_factory_reset_task = None
        if cancellation is not None:
            raise cancellation

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
        settings_manager.require_mention_default = lambda: bool(
            getattr(getattr(client, "config", None), "require_mention", False)
        )
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
                self.config.harness_prompt_echo = v2_config.runtime.harness_prompt_echo
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
        from core.session_activities import SessionActivityRegistry
        from modules.agents.claude_agent import ClaudeAgent
        from modules.agents.codex import CodexAgent
        from modules.agents.opencode import OpenCodeAgent
        from storage.db import get_cached_sqlite_engine
        from storage.session_activities import SQLiteSessionActivityStore

        activity_store = SQLiteSessionActivityStore(get_cached_sqlite_engine())
        self.agent_service = AgentService(
            self,
            activities=SessionActivityRegistry(
                activity_store,
                activation_registry=self.runtime_activation,
            ),
            activation_registry=self.runtime_activation,
        )
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

        def inbound(callback):
            return self._dispatch_to_controller_loop(
                callback,
                wait_for_owner_recovery=True,
            )

        # Command handlers dict
        # Admin protection for "set_cwd" and "settings" is now handled by
        # the centralized auth pipeline (core.auth.check_auth) in IM entry points.
        command_handlers = {
            "start": inbound(self.command_handler.handle_start),
            "new": inbound(self.command_handler.handle_new),
            "cwd": inbound(self.command_handler.handle_cwd),
            "set_cwd": inbound(self.command_handler.handle_set_cwd),
            "resume": inbound(self.command_handler.handle_resume),
            "setup": inbound(self.command_handler.handle_setup),
            "settings": inbound(self.settings_handler.handle_settings),
            "stop": inbound(self.command_handler.handle_stop),
            "bind": inbound(self.command_handler.handle_bind),
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
            on_message=self._dispatch_im_message_to_controller_loop(
                _on_im_message,
                wait_for_owner_recovery=True,
            ),
            on_command=command_handlers,
            on_callback_query=inbound(self.message_handler.handle_callback_query),
            on_settings_update=inbound(self.settings_handler.handle_settings_update),
            on_change_cwd=inbound(self.command_handler.handle_change_cwd_submission),
            on_routing_update=inbound(self.settings_handler.handle_routing_update),
            on_routing_modal_update=inbound(
                self.settings_handler.handle_routing_modal_update
            ),
            on_resume_session=inbound(
                self.session_handler.handle_resume_session_submission
            ),
            on_ready=self._dispatch_to_controller_loop(self._on_runtime_ready),
            on_transport_ready=self._dispatch_to_controller_loop(self._on_im_ready),
        )

    async def _await_runtime_owner_recovery(self) -> None:
        recovery_complete = getattr(self, "_delivery_recovery_complete", None)
        if recovery_complete is not None:
            await recovery_complete.wait()

    def _dispatch_to_controller_loop(
        self,
        callback,
        *,
        wait_for_owner_recovery: bool = False,
    ):
        async def _wrapped(*args, **kwargs):
            async def _invoke():
                if wait_for_owner_recovery:
                    await self._await_runtime_owner_recovery()
                return await callback(*args, **kwargs)

            loop = self._loop
            if loop is None:
                return await _invoke()

            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None

            if current_loop is loop:
                return await _invoke()

            future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
            return await asyncio.wrap_future(future)

        return _wrapped

    def _dispatch_im_message_to_controller_loop(
        self,
        callback,
        *,
        wait_for_owner_recovery: bool = False,
    ):
        tracked_platforms = {"telegram", "wechat"}

        async def _wrapped(context, *args, **kwargs):
            async def _invoke():
                if wait_for_owner_recovery:
                    await self._await_runtime_owner_recovery()
                return await callback(context, *args, **kwargs)

            platform = self._platform_for_im_callback_context(context)
            if platform in tracked_platforms:
                return await self._run_on_controller_loop(_invoke)
            self._schedule_controller_callback(_invoke)
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

    async def _restore_active_polls(self, platforms: set[str]) -> None:
        opencode_agent = self.agent_service.agents.get("opencode")
        if opencode_agent and hasattr(opencode_agent, "restore_active_polls"):
            try:
                restored = await opencode_agent.restore_active_polls(platforms)  # type: ignore[attr-defined]
                if restored > 0:
                    logger.info(f"Restored {restored} active OpenCode poll(s)")
            except Exception as e:
                logger.error(f"Failed to restore active polls: {e}", exc_info=True)

    async def _on_im_ready(self, *, platform: str) -> None:
        """Restore transport-owned state only after that transport can deliver."""
        logger.info("IM transport ready, restoring state for %s", platform)
        platforms = {platform}
        if platform == self.primary_platform:
            platforms.add("")
        await self._restore_active_polls(platforms)
        # Poll registration is durable-owner evidence needed by startup recovery.
        # All work admission and user-visible delivery remain behind the barrier.
        await self._await_runtime_owner_recovery()
        # Interruption reports for turns on this platform were held back during
        # recovery precisely because it could not deliver them yet.
        notify_turns = getattr(self.session_turns, "notify_transport_ready", None)
        if callable(notify_turns):
            try:
                reported = await notify_turns(platform)
                if reported:
                    logger.info(
                        "Reported %d interrupted turn(s) on %s", reported, platform
                    )
            except Exception:
                logger.exception("Failed to report interrupted turns for %s", platform)
        self.scheduled_task_service.notify_transport_ready(platform)
        notify_update_checker = getattr(self.update_checker, "notify_transport_ready", None)
        if callable(notify_update_checker):
            notify_update_checker(platform)
        try:
            await self.update_checker.check_and_send_post_update_notification(ready_platform=platform)
        except Exception as e:
            logger.error(f"Failed to send post-update notification: {e}", exc_info=True)

    def is_im_transport_ready(self, platform: str) -> bool:
        return self.im_client.is_transport_ready(platform)

    async def _on_runtime_ready(self) -> None:
        """Start aggregate services without waiting for external connectivity."""
        logger.info("IM runtime ready, starting core services")
        workbench_platforms = {"avibe"}
        if self.primary_platform == "avibe":
            workbench_platforms.add("")
        await self._restore_active_polls(workbench_platforms)
        try:
            await self._recover_runtime_owners()
        except Exception:
            self.request_shutdown("runtime owner recovery failed")
            raise
        try:
            await self.update_checker.check_and_send_post_update_notification(ready_platform="avibe")
        except Exception as e:
            logger.error(f"Failed to send post-update notification: {e}", exc_info=True)
        try:
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

    async def _recover_runtime_owners(self) -> None:
        """Restore durable execution owners before any producer can admit work."""

        recover_deliveries = getattr(
            self.session_turns,
            "recover_durable_delivery_state",
            None,
        )
        if callable(recover_deliveries):
            try:
                recovered = await recover_deliveries(service_restart=True)
                if recovered:
                    logger.info(
                        "Recovered durable Session delivery owners for %s",
                        ",".join(recovered),
                    )
            except Exception:
                logger.exception("Failed to recover durable Session delivery owners")
                raise

        recover_queue = getattr(
            self.session_turns,
            "recover_persisted_agent_run_queue",
            None,
        )
        if callable(recover_queue):
            try:
                recovered = await recover_queue()
                if recovered:
                    logger.info(
                        "Recovered persisted Workbench Agent Run queues for %s",
                        ",".join(recovered),
                    )
            except Exception:
                logger.exception("Failed to recover persisted Workbench Agent Run queues")
                raise

        try:
            self.scheduled_task_service.recover_processing_requests()
        except Exception:
            logger.exception("Failed to recover fallback request owners")
            raise

        await self.runtime_work_supervisor.activate()
        self._delivery_recovery_complete.set()

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
        For channel contexts, returns channel_id for per-channel settings. A
        Telegram forum topic returns a runtime thread key so its explicit
        override can win before the parent channel fallback.

        Relies on the ``is_dm`` flag set by the IM layer in
        ``context.platform_specific`` (see Phase 2 of the refactoring).
        """
        from core.message_context import resolve_context_scope_settings_key

        return resolve_context_scope_settings_key(context)

    def _get_session_key(self, context: MessageContext) -> str:
        """Get a globally unique session-scope key.

        Unlike ``_get_settings_key`` (which returns a raw ID for settings
        lookup routed by platform), this key must be unique across all
        platforms so that sessions, polls, and message-consolidation
        tracking never collide.
        """
        platform = context.platform or (context.platform_specific or {}).get("platform") or self.primary_platform
        from core.message_context import resolve_context_settings_key

        settings_key = resolve_context_settings_key(context)
        return build_context_session_key(context, platform=platform, settings_key=settings_key)

    def _get_turn_sink_key(self, context: MessageContext) -> str:
        """Get the live turn sink's key for ``context``.

        Thread-scoped, unlike ``_get_session_key``: the sink is one agent
        session's turn-concurrency slot, so sharing it across a channel's
        threads made ``dispatch_turn`` refuse unrelated sessions' turns. See
        ``core.message_context.build_context_turn_sink_key``.
        """
        from core.message_context import build_context_turn_sink_key

        return build_context_turn_sink_key(context, session_key=self._get_session_key(context))

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

    def mark_turn_complete(
        self,
        context: Optional[MessageContext] = None,
        *,
        settled_by: str = SETTLED_BY_NO_TERMINAL_RESULT,
    ) -> None:
        """Release a streaming turn sink whose turn finished WITHOUT emitting a
        result (missing/disabled backend, dedup, inline-stop, error, or any
        synchronous no-agent path) so the SSE dispatch closes promptly instead
        of waiting out the safety timeout. No-op for non-streaming turns or
        when an agent turn is genuinely in flight (the result emit releases it)."""
        if context is None:
            return
        sink = self.get_turn_sink(self._get_turn_sink_key(context))
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
        # Record WHY the waiter is being released, so ``dispatch_turn`` can tell its
        # caller. An ``agent_run`` released with a no-result settlement must be
        # terminalized by that caller: nothing else will ever do it (see
        # docs/plans/agent-run-zombie-settlement.md). ``setdefault`` keeps a real
        # terminal result — which always runs before this ``finally`` — as the
        # winning settlement.
        #
        # The default is the no-dispatch case this method was written for (blank
        # prompt, dedup, inline stop). A caller that IS a terminal output overrides
        # it: the dispatcher passes ``SETTLED_BY_TURN_ONLY_RESULT`` when the output
        # completes the turn but deliberately leaves the run to another owner (a
        # requeued Claude Activity), which must NOT be settled here (Codex P1).
        sink.setdefault("settled_by", settled_by or SETTLED_BY_NO_TERMINAL_RESULT)
        done = sink.get("done_event")
        if done is not None:
            done.set()

    # ----- Live agent-runtime status (workbench sidebar dot) -------------
    #
    # ``agent_sessions.agent_status`` is a projection of durable Turn ownership.
    # Admission projects running; the terminal transaction projects the exact
    # successor state. Legacy non-durable paths use this writer directly.

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

    # ----- Direct Memory entry admission ---------------------------------
    #
    # The policy lives in ``core.memory.admission``. The controller only
    # collects the facts one turn carries and acts on the verdict.

    def _memory_admission(self) -> CaptureAdmission:
        # ``getattr`` keeps a controller assembled without a Memory runtime
        # failing closed inside the admission module rather than raising here.
        return CaptureAdmission(
            principals=getattr(self, "memory_runtime", None),
            bindings=_SettingsUserBindings(getattr(self, "platform_settings_managers", None)),
        )

    def _memory_turn_facts(
        self,
        context: MessageContext,
        *,
        text: object = None,
        session_id: object = None,
        attachment_lease: object = None,
        attachment_capture_status: object = None,
        attachment_config_generation: object = None,
        attachment_failures: object = None,
        include_workdir: bool = True,
    ) -> InboundTurnFacts:
        payload = context.platform_specific if isinstance(context.platform_specific, dict) else {}
        workdir = None
        if include_workdir:
            try:
                workdir = self.get_cwd(context)
            except Exception:
                workdir = None
        return InboundTurnFacts(
            platform=context.platform or payload.get("platform"),
            user_id=getattr(context, "user_id", None),
            message_id=getattr(context, "message_id", None),
            session_id=session_id,
            workdir=workdir,
            text=text,
            files=getattr(context, "files", None),
            is_dm=payload.get("is_dm") is True,
            is_ordinary_text=context.is_ordinary_text,
            is_ordinary_attachment=context.is_ordinary_attachment,
            attachment_lease=attachment_lease,
            attachment_capture_status=attachment_capture_status,
            attachment_config_generation=attachment_config_generation,
            attachment_failures=attachment_failures,
            memory_enabled=bool(
                getattr(getattr(getattr(self, "config", None), "memory", None), "enabled", False)
            ),
        )

    def memory_capture_admitted(self, context: MessageContext) -> bool:
        return self._memory_admission().admits(
            self._memory_turn_facts(context, include_workdir=False)
        )

    def memory_attachment_capture_admitted(
        self,
        context: MessageContext,
        session_id: str,
    ) -> int | None:
        """Return the local config generation authorizing a Memory lease."""

        admission = self._memory_admission()
        facts = self._memory_turn_facts(
            context,
            text="",
            session_id=session_id,
            include_workdir=False,
        )
        if not admission.admits_attachment_turn(facts):
            return None
        runtime = getattr(self, "memory_runtime", None)
        read_generation = getattr(
            runtime,
            "attachment_capture_config_generation",
            None,
        )
        if not callable(read_generation):
            return None
        try:
            generation = read_generation()
        except Exception:
            return None
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return generation if generation >= 0 else None

    def memory_principal_for_context(self, context: MessageContext) -> Optional[str]:
        return self._memory_admission().principal_for(
            self._memory_turn_facts(context, include_workdir=False)
        )

    def configure_memory_cli_session(self, context: MessageContext, *, admitted: bool) -> bool:
        """Associate an admitted Agent session with its Memory read/write scope."""

        from core.caller_context import caller_context_from_platform_payload

        payload = context.platform_specific if isinstance(context.platform_specific, dict) else {}
        caller = caller_context_from_platform_payload(payload)
        if caller is None:
            return False
        facts_by_session = getattr(self, "_memory_cli_facts_by_session", None)
        if not isinstance(facts_by_session, dict):
            facts_by_session = {}
            self._memory_cli_facts_by_session = facts_by_session
        admission = self._memory_admission()
        facts = self._memory_turn_facts(context)
        principal_id = admission.principal_for(facts) if admitted else None
        project_id = admission.project_for(facts) if admitted else None
        if principal_id is None or project_id is None:
            self._memory_scopes_by_session.pop(caller.session_id, None)
            facts_by_session.pop(caller.session_id, None)
            return False
        self._memory_scopes_by_session[caller.session_id] = (principal_id, project_id)
        facts_by_session[caller.session_id] = facts
        return True

    def memory_scope_for_cli_session(self, session_id: str) -> Optional[tuple[str, str]]:
        """Return the principal and project owned by an admitted Agent session."""

        from core.memory.project_ids import DEFAULT_MEMORY_PROJECT_ID
        from core.memory.store import is_principal_id, is_project_id

        session_key = str(session_id or "").strip()
        scope = self._memory_scopes_by_session.get(session_key)
        if (
            isinstance(scope, tuple)
            and len(scope) == 2
            and is_principal_id(scope[0])
            and is_project_id(scope[1])
        ):
            facts = getattr(self, "_memory_cli_facts_by_session", {}).get(session_key)
            if facts is not None:
                admission = self._memory_admission()
                memory_enabled = bool(
                    getattr(getattr(getattr(self, "config", None), "memory", None), "enabled", False)
                )
                current_scope = (
                    admission.principal_for(facts),
                    admission.project_for(facts),
                )
                if (
                    not memory_enabled
                    or not admission.admits(facts)
                    or current_scope != scope
                ):
                    self._memory_scopes_by_session.pop(session_key, None)
                    self._memory_cli_facts_by_session.pop(session_key, None)
                    return None
            return scope
        return None

    def memory_principal_for_cli_session(self, session_id: str) -> Optional[str]:
        """Return the principal associated with an admitted Agent session."""

        scope = self.memory_scope_for_cli_session(session_id)
        return scope[0] if scope is not None else None

    def memory_project_for_cli_session(self, session_id: str) -> Optional[str]:
        """Return the project associated with an admitted Agent session."""

        scope = self.memory_scope_for_cli_session(session_id)
        return scope[1] if scope is not None else None

    def default_memory_project_id(self) -> str:
        """Return the Memory project used by Settings and default Agent search."""

        from core.memory.project_ids import DEFAULT_MEMORY_PROJECT_ID

        return DEFAULT_MEMORY_PROJECT_ID

    async def final_flush_memory_session(
        self,
        context: MessageContext,
        raw_session_id: str,
        *,
        deadline_seconds: float = 5.0,
    ) -> bool:
        """Best-effort final Memory flush for a trusted IM session lifecycle event.

        The raw anchor must come from ``SessionHandler.get_base_session_id``.  The
        handler deliberately never constructs a provider session identifier: this
        controller boundary reuses capture admission and lets Memory derive its
        current epoch-scoped identity.
        """

        scope = self._memory_scope_for_im_session(context, raw_session_id)
        if scope is None:
            return False

        return await self._final_flush_memory_scope(
            raw_session_id,
            scope[0],
            scope[1],
            deadline_seconds=deadline_seconds,
        )

    async def run_memory_session_lifecycle(
        self,
        context: MessageContext,
        raw_session_id: str,
        operation: Callable[[], Awaitable[_MemorySessionLifecycleResult]],
        *,
        deadline_seconds: float = 5.0,
    ) -> _MemorySessionLifecycleResult:
        """Run an IM session reset behind Memory's exact capture fence."""

        scope = self._memory_scope_for_im_session(context, raw_session_id)
        if scope is None:
            return await operation()

        runtime = getattr(self, "memory_runtime", None)
        run_lifecycle = getattr(runtime, "run_session_lifecycle", None)
        if not callable(run_lifecycle):
            await self._final_flush_memory_scope(
                raw_session_id,
                scope[0],
                scope[1],
                deadline_seconds=deadline_seconds,
            )
            return await operation()

        return await run_lifecycle(
            principal_id=scope[0],
            project_id=scope[1],
            raw_session_id=raw_session_id,
            operation=operation,
            deadline_seconds=deadline_seconds,
        )

    def _memory_scope_for_im_session(
        self,
        context: MessageContext,
        raw_session_id: object,
    ) -> Optional[tuple[str, str]]:
        """Resolve the same complete, admitted scope used by IM capture."""

        from core.memory.store import is_principal_id, is_project_id

        if not isinstance(raw_session_id, str) or not raw_session_id:
            return None
        facts = self._memory_turn_facts(context, session_id=raw_session_id)
        if not facts.memory_enabled:
            return None
        admission = self._memory_admission()
        try:
            if not admission.admits(facts):
                return None
            principal_id = admission.principal_for(facts)
            project_id = admission.project_for(facts)
        except Exception:
            logger.debug("Memory session lifecycle admission failed", exc_info=True)
            return None
        if not is_principal_id(principal_id) or not is_project_id(project_id):
            return None
        return principal_id, project_id

    async def final_flush_memory_cli_session(
        self,
        raw_session_id: str,
        *,
        deadline_seconds: float = 5.0,
    ) -> bool:
        """Best-effort final flush for a trusted Workbench session ID.

        The controller owns the stored scope and resolves it here; callers must
        not reconstruct one from a Workbench row or synthesize a
        ``MessageContext``.
        """

        if not isinstance(raw_session_id, str) or not raw_session_id:
            return False
        from core.memory.project_ids import (
            DEFAULT_MEMORY_PROJECT_ID,
            is_persisted_memory_project_id,
        )
        from core.memory.store import is_principal_id

        live_scope = self.memory_scope_for_cli_session(raw_session_id)
        runtime = getattr(self, "memory_runtime", None)
        resolve_scopes = getattr(runtime, "resolve_current_session_scopes", None)
        durable_scopes: tuple[tuple[str, str], ...] | None = ()
        if callable(resolve_scopes):
            try:
                durable_scopes = await resolve_scopes(raw_session_id)
            except Exception:
                logger.debug("Memory session scope recovery failed", exc_info=True)
                durable_scopes = None
        if durable_scopes is None:
            return False
        scopes = set(durable_scopes)
        if live_scope is not None:
            scopes.add(live_scope)
        if not scopes:
            return False
        ordered = tuple(
            sorted(
                scopes,
                key=lambda scope: (scope[1] != DEFAULT_MEMORY_PROJECT_ID, scope),
            )
        )
        if any(
            not is_principal_id(principal_id)
            or not is_persisted_memory_project_id(project_id)
            for principal_id, project_id in ordered
        ):
            return False
        run_lifecycle = getattr(runtime, "run_session_scopes_lifecycle", None)
        if not callable(run_lifecycle):
            return False

        async def _flushed() -> bool:
            return True

        try:
            return bool(
                await run_lifecycle(
                    scopes=ordered,
                    raw_session_id=raw_session_id,
                    operation=_flushed,
                    deadline_seconds=deadline_seconds,
                )
            )
        except Exception:
            logger.debug("Memory multi-scope final flush failed", exc_info=True)
            return False

    async def archive_memory_cli_session(
        self,
        raw_session_id: str,
        *,
        deadline_seconds: float = 5.0,
    ) -> dict[str, Any]:
        """Archive one Workbench session inside its exact Memory capture fence.

        This is a closed controller-owned use case for the UI process. The UI
        supplies only the durable Workbench session ID; canonical Memory identity
        is resolved here, and the terminal database mutation stays inside the same
        runtime lifecycle operation as final flush.
        """

        from core.memory.project_ids import DEFAULT_MEMORY_PROJECT_ID
        from core.memory.store import is_principal_id, is_project_id
        from core.services import sessions as workbench_sessions_service
        from storage.agent_session_rows import WORKSPACE_NOTICE_SESSION_ID
        from storage.db import create_sqlite_engine

        if (
            not isinstance(raw_session_id, str)
            or not raw_session_id
            or raw_session_id != raw_session_id.strip()
        ):
            raise ValueError("invalid Workbench session ID")
        if raw_session_id == WORKSPACE_NOTICE_SESSION_ID:
            raise workbench_sessions_service.ReservedSessionError(raw_session_id)

        def read_session() -> dict[str, Any]:
            engine = create_sqlite_engine()
            try:
                with engine.connect() as conn:
                    return workbench_sessions_service.get_session(
                        conn,
                        raw_session_id,
                    )
            finally:
                engine.dispose()

        existing = await asyncio.to_thread(read_session)
        if existing.get("status") == "archived":
            return existing

        def archive_session() -> dict[str, Any]:
            engine = create_sqlite_engine()
            try:
                with engine.begin() as conn:
                    return workbench_sessions_service.archive_session(
                        conn,
                        raw_session_id,
                    )
            finally:
                engine.dispose()

        async def archive_operation() -> dict[str, Any]:
            # Cancelling the socket request must not release Memory admission
            # while SQLite is still committing the terminal transition.
            return await run_blocking(archive_session)

        async def run_memory_lifecycle() -> dict[str, Any]:
            live_scope = self.memory_scope_for_cli_session(raw_session_id)
            runtime = getattr(self, "memory_runtime", None)
            resolve_scopes = getattr(runtime, "resolve_current_session_scopes", None)
            if not callable(resolve_scopes):
                raise RuntimeError("Memory session scope recovery is unavailable")
            durable_scopes = await resolve_scopes(raw_session_id)
            if durable_scopes is None:
                raise RuntimeError("Memory session scopes could not be recovered safely")

            scopes = set(durable_scopes)
            if live_scope is not None:
                scopes.add(live_scope)
            if not scopes:
                return await archive_operation()
            for scope in scopes:
                if (
                    not isinstance(scope, tuple)
                    or len(scope) != 2
                    or not is_principal_id(scope[0])
                    or not is_project_id(scope[1])
                ):
                    raise RuntimeError("invalid canonical Memory session scope")
            canonical_scopes = tuple(
                sorted(
                    scopes,
                    key=lambda scope: (scope[1] != DEFAULT_MEMORY_PROJECT_ID, scope),
                )
            )

            run_lifecycle = getattr(runtime, "run_session_scopes_lifecycle", None)
            if not callable(run_lifecycle):
                raise RuntimeError("Memory session lifecycle is unavailable")
            return await run_lifecycle(
                scopes=canonical_scopes,
                raw_session_id=raw_session_id,
                operation=archive_operation,
                deadline_seconds=deadline_seconds,
            )

        turn_manager = getattr(self, "session_turns", None)
        turn_lifecycle = getattr(turn_manager, "run_session_lifecycle", None)
        if callable(turn_lifecycle):
            return await turn_lifecycle(
                raw_session_id,
                run_memory_lifecycle,
                deadline_seconds=deadline_seconds,
            )
        return await run_memory_lifecycle()

    async def _final_flush_memory_scope(
        self,
        raw_session_id: str,
        principal_id: str,
        project_id: str,
        *,
        deadline_seconds: float,
    ) -> bool:
        """Call the Runtime with one already-admitted canonical scope."""

        from core.memory.store import is_principal_id, is_project_id

        if (
            not isinstance(raw_session_id, str)
            or not raw_session_id
            or not is_principal_id(principal_id)
            or not is_project_id(project_id)
        ):
            return False

        async with self._memory_replacement_lock():
            runtime = getattr(self, "memory_runtime", None)
            final_flush = getattr(runtime, "final_flush", None)
            if not callable(final_flush):
                return False
            try:
                return bool(
                    await final_flush(
                        principal_id=principal_id,
                        project_id=project_id,
                        raw_session_id=raw_session_id,
                        deadline_seconds=deadline_seconds,
                    )
                )
            except Exception:
                logger.debug("Memory final flush failed", exc_info=True)
                return False

    def capture_user_memory(
        self,
        context: MessageContext,
        text: str,
        session_id: str,
        *,
        attachment_lease: object = None,
        attachment_config_generation: int | None = None,
        attachment_failure_reasons: object = None,
        admission_ready: asyncio.Event | None = None,
    ) -> Awaitable[None]:
        """Submit one eligible attributed human turn after session resolution.

        This is deliberately best effort. It is scheduled by ``MessageHandler``
        and never participates in an agent turn's completion path. Bind the
        aggregate before task scheduling can yield to a concurrent reset.
        """

        return self._capture_user_memory_bound(
            context,
            text,
            session_id,
            attachment_lease=attachment_lease,
            attachment_config_generation=attachment_config_generation,
            attachment_failure_reasons=attachment_failure_reasons,
            admission_ready=admission_ready,
            observed_runtime=getattr(self, "memory_runtime", None),
        )

    async def _capture_user_memory_bound(
        self,
        context: MessageContext,
        text: str,
        session_id: str,
        *,
        attachment_lease: object = None,
        attachment_config_generation: int | None = None,
        attachment_failure_reasons: object = None,
        admission_ready: asyncio.Event | None = None,
        observed_runtime: object,
    ) -> None:
        admission = self._memory_admission()
        facts = self._memory_turn_facts(
            context,
            text=text,
            session_id=session_id,
            attachment_lease=attachment_lease,
            attachment_config_generation=attachment_config_generation,
            attachment_failures=attachment_failure_reasons,
        )
        try:
            if admission.admits_attachment_turn(facts):
                principal_id = admission.principal_for(facts)
                project_id = admission.project_for(facts)
                module = getattr(observed_runtime, "module", None)
                acquire_admission = getattr(module, "capture_admission", None)
                if (
                    isinstance(principal_id, str)
                    and isinstance(project_id, str)
                    and callable(acquire_admission)
                ):
                    async with acquire_admission(
                        principal_id=principal_id,
                        project_id=project_id,
                        session_id=session_id,
                    ) as held_admission:
                        if admission_ready is not None:
                            admission_ready.set()
                        await self._capture_user_memory_decided(
                            admission,
                            facts,
                            attachment_lease=attachment_lease,
                            observed_runtime=observed_runtime,
                            held_admission=held_admission,
                        )
                    return
            if admission_ready is not None:
                admission_ready.set()
            await self._capture_user_memory_decided(
                admission,
                facts,
                attachment_lease=attachment_lease,
                observed_runtime=observed_runtime,
            )
        finally:
            if admission_ready is not None:
                admission_ready.set()

    async def _capture_user_memory_decided(
        self,
        admission: CaptureAdmission,
        facts: InboundTurnFacts,
        *,
        attachment_lease: object,
        observed_runtime: object,
        held_admission: object = None,
    ) -> None:
        if admission.admits_attachment_turn(facts):
            status = "unavailable"
            read_status = getattr(observed_runtime, "attachment_capture_status", None)
            if callable(read_status):
                try:
                    observed_status = await read_status()
                except Exception:
                    observed_status = "unavailable"
                if observed_status in {"ready", "not_configured", "unavailable"}:
                    status = observed_status
            facts = replace(facts, attachment_capture_status=status)
        request = admission.decide(facts)
        if not isinstance(request, CaptureRequest):
            return

        platform = CaptureAdmission.platform_of(facts)
        started_at = time.monotonic()
        try:
            if self._memory_factory_reset_pending(observed_runtime) or observed_runtime is None:
                return
            if getattr(observed_runtime, "retired", False):
                return
            async with self._memory_replacement_lock():
                runtime = getattr(self, "memory_runtime", None)
                if (
                    self._memory_factory_reset_pending(runtime)
                    or runtime is not observed_runtime
                    or runtime is None
                    or getattr(runtime, "retired", False)
                    or not runtime.available
                ):
                    return
                if request.attachments and platform != WORKBENCH_PLATFORM:
                    read_generation = getattr(
                        runtime,
                        "attachment_capture_config_generation",
                        None,
                    )
                    current_generation = (
                        read_generation() if callable(read_generation) else None
                    )
                    if (
                        isinstance(current_generation, bool)
                        or current_generation != request.attachment_config_generation
                    ):
                        request = replace(
                            request,
                            attachments=(),
                            attachment_config_generation=None,
                        )
                        if not request.text.strip():
                            return
                capture_options = {}
                if held_admission is not None:
                    capture_options["admission"] = held_admission
                if request.attachments and attachment_lease is not None:
                    capture_options["source_lease"] = attachment_lease
                await runtime.module.capture(request, **capture_options)
            logger.info(
                "Memory capture platform=%s latency_ms=%d",
                platform,
                int((time.monotonic() - started_at) * 1000),
            )
        except Exception:
            logger.warning(
                "Memory capture failed platform=%s latency_ms=%d",
                platform,
                int((time.monotonic() - started_at) * 1000),
            )

    async def _send_memory_processing_event(
        self,
        event: str,
        kind: str | None,
        occurred_at: str,
        queued: int,
    ) -> bool:
        from core.handlers.admin_notifications import send_admin_text

        store = self.settings_manager.get_store()
        admin_ids = list(store.get_admins().keys()) if store else []
        if not admin_ids:
            return True
        if event == "recovered":
            key = "memory.alert.recovered"
        else:
            key = "memory.alert.credential" if kind == "credential" else "memory.alert.engine"
        text = self._t(key, occurred_at=occurred_at, queued=queued)
        delivered = await send_admin_text(
            self,
            admin_ids,
            text,
            log_label="Memory processing notification",
        )
        return bool(delivered)

    def update_thread_message_id(self, context: MessageContext) -> None:
        """Run real-turn-start hooks after the runtime gate is acquired."""
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
        payload = context.platform_specific or {}
        target_agent_id = payload.get("vibe_agent_id") or (target.get("agent_id") if target else None)
        target_agent_name = target.get("agent_name") if target else None
        target_backend = target.get("agent_backend") if target else None
        if target_agent_id or target_agent_name:
            vibe_agent = self.resolve_vibe_agent_for_context(
                context,
                override_agent_id=str(target_agent_id) if target_agent_id else None,
                override_agent_name=str(target_agent_name) if target_agent_name else None,
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
        override_agent_id: Optional[str] = None,
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
        agent_id = override_agent_id or (target.get("agent_id") if target else None)
        try:
            if agent_id:
                return self.vibe_agent_store.require_reference_by_id(str(agent_id))
            if agent_name:
                return self.vibe_agent_store.require_reference(agent_name)
            default_agent = self.vibe_agent_store.get_default_agent()
            if default_agent is not None:
                return default_agent
            if required:
                return self.vibe_agent_store.require("default")
            return None
        except Exception as exc:
            if required:
                raise
            logger.warning(
                "Scope references Vibe Agent '%s' but it cannot be resolved: %s",
                agent_id or agent_name or "default",
                exc,
            )
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
        result_footer: Optional[str] = None,
        output: MessageOutput | None = None,
        terminal_error: Optional[str] = None,
        delivery: Any = None,
    ):
        """Backward-compatible entrypoint; delegated to message dispatcher."""
        result = await self.message_dispatcher.emit_agent_message(
            context=context,
            message_type=message_type,
            text=text,
            parse_mode=parse_mode,
            is_error=is_error,
            level=level,
            status_label=status_label,
            result_footer=result_footer,
            output=output,
            terminal_error=terminal_error,
            # Forwarded ONLY when a caller asked for it, for the same reason
            # ``emit_backend_failure`` does: ``message_dispatcher`` is a
            # substitutable collaborator (six test suites replace it), so passing
            # an optional diagnostic unconditionally would change the required
            # signature of every stand-in.
            **({"delivery": delivery} if delivery is not None else {}),
        )
        manager = getattr(self, "session_turns", None)
        complete = getattr(manager, "on_terminal_delivery_complete", None)
        if callable(complete):
            complete(context)
        return result

    def note_session_tokens(self, context: MessageContext, *, total: int) -> None:
        """Report the session's current context-window occupancy for the status
        footer (backend-agnostic). SETs an absolute snapshot; the next footer render
        shows it. No-op if the dispatcher is unavailable (partially-wired test
        controllers)."""
        dispatcher = getattr(self, "message_dispatcher", None)
        if dispatcher is None:
            return
        dispatcher.note_session_tokens(context, total=total)

    def session_token_field(self, context: MessageContext) -> str:
        """The compact ``{n} tok`` footer field for the session's current
        context-window occupancy, or "" when unknown/zero or the dispatcher is
        unavailable (partially-wired test controllers)."""
        dispatcher = getattr(self, "message_dispatcher", None)
        if dispatcher is None:
            return ""
        return dispatcher.session_token_field(context)

    # Main run method
    @property
    def service_lock_safe_to_release(self) -> bool:
        return bool(getattr(self, "_service_lock_safe_to_release", False))

    def request_shutdown(self, reason: str = "requested") -> None:
        """Schedule shutdown on the controller loop without blocking its owner."""

        if getattr(self, "_shutdown_requested", False):
            return
        self._shutdown_requested = True
        self._service_lock_safe_to_release = False
        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._ensure_shutdown_task, reason)
        except RuntimeError:
            logger.exception("Failed to schedule controller shutdown")
            self._shutdown_tainted = True

    def _ensure_shutdown_task(self, reason: str = "requested") -> None:
        task = getattr(self, "_shutdown_task", None)
        if task is not None and not task.done():
            return
        self._shutdown_task = asyncio.create_task(
            self._shutdown_on_loop(reason),
            name="controller-shutdown",
        )

    async def _shutdown_on_loop(self, reason: str) -> None:
        """Join passive recovery owners before allowing the loop to stop."""

        logger.info("Controller shutdown started: %s", reason)
        try:
            stop_task = self._begin_runtime_work_stack_shutdown()
            grace = max(
                0.0,
                float(
                    getattr(
                        self,
                        "_runtime_work_shutdown_grace_seconds",
                        _RUNTIME_WORK_SHUTDOWN_GRACE_SECONDS,
                    )
                ),
            )
            done, _ = await asyncio.wait({stop_task}, timeout=grace)
            if not done:
                self._shutdown_tainted = True
                logger.critical(
                    "Runtime work shutdown exceeded %.1fs; retaining the "
                    "service lease until exact workers join",
                    grace,
                )
            await asyncio.shield(stop_task)
        except Exception:
            self._shutdown_tainted = True
            logger.exception("Controller shutdown could not join runtime work")
        finally:
            loop = self._loop
            if loop is not None and loop.is_running():
                loop.call_soon(loop.stop)

    def _begin_runtime_work_stack_shutdown(self) -> asyncio.Task[None]:
        task = getattr(self, "_runtime_work_shutdown_task", None)
        if task is None:
            task = asyncio.create_task(
                self._stop_runtime_work_stack(),
                name="controller-runtime-work-stack-stop",
            )
            self._runtime_work_shutdown_task = task
        return task

    async def _join_runtime_work_stack_shutdown(self) -> None:
        await asyncio.shield(self._begin_runtime_work_stack_shutdown())

    async def _stop_runtime_work_stack(self) -> None:
        """Stop lane consumers before disposing their shared executor."""

        supervisor = getattr(self, "runtime_work_supervisor", None)
        quiesce = getattr(supervisor, "quiesce", None)
        if callable(quiesce):
            quiesce()

        # Controller-generation lanes can still be finishing work after
        # quiesce. Join them before ScheduledTaskService releases durable Turn
        # owners; otherwise a delivery-recovery worker can admit a new Turn
        # immediately after the final owner snapshot.
        controller_tokens = tuple(getattr(self, "_runtime_work_tokens", ()))
        if controller_tokens:
            begin_unregister = getattr(supervisor, "begin_unregister", None)
            if not callable(begin_unregister):
                self._shutdown_tainted = True
                raise RuntimeError(
                    "runtime work supervisor cannot join controller lanes"
                )
            controller_lane_joins = [
                begin_unregister(token) for token in controller_tokens
            ]
            self._runtime_work_tokens = []
            controller_results = await asyncio.gather(
                *controller_lane_joins,
                return_exceptions=True,
            )
            controller_errors = [
                result
                for result in controller_results
                if isinstance(result, BaseException)
            ]
            if controller_errors:
                self._shutdown_tainted = True
                raise RuntimeError(
                    "controller runtime work lane shutdown failed"
                ) from controller_errors[0]

        dispatcher = getattr(self, "message_dispatcher", None)
        drain_activity = getattr(dispatcher, "drain_agent_run_activity", None)
        if callable(drain_activity):
            await drain_activity()

        service_stops: list[asyncio.Task[None]] = []
        for service_name in ("scheduled_task_service", "watch_service"):
            service = getattr(self, service_name, None)
            stop = getattr(service, "stop", None)
            if callable(stop):
                service_stops.append(
                    asyncio.create_task(
                        stop(),
                        name=f"controller-{service_name}-stop",
                    )
                )
        results = await asyncio.gather(*service_stops, return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        stop_supervisor = getattr(supervisor, "stop", None)
        if callable(stop_supervisor):
            try:
                await stop_supervisor()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            self._shutdown_tainted = True
            raise RuntimeError("runtime work stack shutdown failed") from errors[0]

    def run(self):
        """Run the controller"""
        logger.info("Starting Claude Proxy Controller with platforms: %s", ", ".join(self.enabled_platforms))

        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            memory_config = getattr(self.config, "memory", None)
            if memory_config is not None:
                self._memory_reconcile_task = self._loop.create_task(
                    self.reconcile_memory(memory_config),
                    name="memory-runtime-reconcile",
                )
            self.show_git_checkpoint_service.start()
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
            self._im_thread = threading.Thread(target=self._run_im_runtime, name="im-runtime", daemon=True)
            self._im_thread.start()
            if self._shutdown_requested:
                self._ensure_shutdown_task("pre-loop request")
            self._loop.run_forever()
            if self._im_run_exception and not isinstance(self._im_run_exception, (KeyboardInterrupt, SystemExit)):
                raise self._im_run_exception
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Error in main run loop: {e}", exc_info=True)
        finally:
            self.cleanup_sync()
            if not getattr(self, "_shutdown_tainted", False):
                self._service_lock_safe_to_release = True
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

        def _stop_loop_coroutine(coro, label: str, *, timeout: float | None = 5) -> None:
            try:
                loop = self._loop
                if not loop or loop.is_closed():
                    return
                if loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(coro, loop)
                    future.result(timeout=timeout)
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

        async def _cancel_internal_server_task() -> None:
            task = getattr(self, "_internal_server_task", None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._internal_server_task = None

        async def _cancel_memory_reconcile_task() -> None:
            task = getattr(self, "_memory_reconcile_task", None)
            try:
                if task is not None:
                    if not task.done():
                        task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception as error:
                        logger.debug("Memory startup reconciliation already failed: %s", error)
            finally:
                self._memory_reconcile_task = None

        # Without this the task is never settled, so the done callback that
        # records "stopped" never runs and internal-server.json keeps saying
        # "ready" after the service exits.
        _stop_loop_coroutine(_cancel_internal_server_task(), "Internal dispatch server")
        try:
            from core import internal_server as _internal_server

            _internal_server.note_stopped()
        except Exception as e:
            logger.debug(f"Internal dispatch server status write skipped: {e}")

        _stop_loop_coroutine(_cancel_cleanup_task(), "Idle cleanup task")
        _stop_loop_coroutine(
            self._join_runtime_work_stack_shutdown(),
            "Runtime work stack",
        )
        _stop_loop_coroutine(self.runtime_command_watcher.stop(), "Runtime command watcher")
        # Reconciliation can start the sidecar, so settle it before closing the
        # runtime or it could race shutdown and leave a process behind.
        _stop_loop_coroutine(_cancel_memory_reconcile_task(), "Memory startup reconciliation")
        # Factory reset retains its inner task behind a shield; join it before
        # reading ``memory_runtime`` so a fresh aggregate cannot appear after
        # shutdown has started closing the old one.
        _stop_loop_coroutine(
            self._join_memory_factory_reset_task(),
            "Memory factory reset",
            timeout=None,
        )
        async def _drain_memory_capture_tasks() -> None:
            handler = getattr(self, "message_handler", None)
            drain = getattr(handler, "drain_memory_capture_tasks", None)
            if callable(drain):
                await drain()

        _stop_loop_coroutine(_drain_memory_capture_tasks(), "Memory capture tasks")
        memory_runtime = getattr(self, "memory_runtime", None)
        if memory_runtime is not None:
            _stop_loop_coroutine(memory_runtime.close(), "Memory runtime")
        model_hub_turn_gateway = getattr(self, "model_hub_turn_gateway", None)
        if model_hub_turn_gateway is not None:
            _stop_loop_coroutine(model_hub_turn_gateway.close(), "Model Hub turn gateway")
        show_git_checkpoint_service = getattr(self, "show_git_checkpoint_service", None)
        if show_git_checkpoint_service is not None:
            show_git_checkpoint_service.stop()

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

        # An explicit Avibe stop/restart must not leave an adopted OpenCode
        # generation behind. Active turns have already reached shutdown cleanup.
        try:
            from modules.agents.opencode import OpenCodeServerManager

            OpenCodeServerManager.terminate_instance_sync()
        except Exception as e:
            logger.debug(f"OpenCode server cleanup skipped: {e}")

        logger.info("Controller cleanup (sync) complete")
