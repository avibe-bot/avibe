import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.resource_governance import AgentResourceFailure
from core.runtime_activation import RuntimeActivationRegistry
from core.runtime_ownership import RuntimeTargetOwnershipSnapshot, SessionRuntimeDisposition
from modules.agents.base import BaseAgent as RealBaseAgent
from modules.agents.codex.transport import CodexResponseTooLargeError, CodexRPCError
from modules.agents.codex.session import CodexSessionManager as RealCodexSessionManager
from tests.codex_generation_support import (
    acquire_returning,
    bind_installed,
    codex_transports,
    init_generation_state,
    install_codex_transport,
)
import modules.agents.runtime_generations  # noqa: F401 -- real module before the stubs below
from core.native_dispatch_phase import (
    DISPATCH_PHASE_ATTEMPTING,
    DISPATCH_PHASE_PREWRITE,
    prewrite_failure_evidence,
    set_dispatch_phase,
)

_AGENT_PATH = Path(__file__).resolve().parents[1] / "modules/agents/codex/agent.py"


def _catalog_reference(path):
    catalog = SimpleNamespace(path=Path(path), close=Mock())
    catalog.retain = Mock(return_value=catalog)
    return catalog


def _fork_transport():
    def send_request(method, _params):
        if method == "thread/turns/list":
            return {
                "data": [
                    {"id": "turn-source", "status": "inProgress"},
                    {"id": "turn-before", "status": "completed"},
                ],
            }
        return {"thread": {"id": "thread-fork"}}

    return SimpleNamespace(send_request=AsyncMock(side_effect=send_request))


_modules_pkg = types.ModuleType("modules")
_agents_pkg = types.ModuleType("modules.agents")
_codex_pkg = types.ModuleType("modules.agents.codex")

_base_module = types.ModuleType("modules.agents.base")
setattr(_base_module, "AgentRequest", object)


class _BaseAgent:
    render_input = RealBaseAgent.render_input

    def __init__(self, controller):
        self.controller = controller

    def ensure_agent_session_id(self, request, *, session_anchor=None):
        anchor = session_anchor or request.base_session_id
        sessions = getattr(self, "sessions", None)
        ensure = getattr(sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            session_id = ensure(request.session_key, self.name, anchor)
        else:
            getter = getattr(sessions, "get_agent_session_row_id", None)
            session_id = getter(request.session_key, anchor, self.name) if callable(getter) else None
        if session_id:
            request.context.platform_specific["agent_session_id"] = session_id
        return session_id

    def bind_agent_session_id(self, request, native_session_id, *, session_anchor=None):
        anchor = session_anchor or request.base_session_id
        binder = getattr(self.sessions, "bind_agent_session", None)
        if callable(binder):
            session_id = binder(request.session_key, self.name, anchor, native_session_id)
        else:
            setter = getattr(self.sessions, "set_agent_session_mapping", None)
            if callable(setter):
                setter(request.session_key, self.name, anchor, native_session_id)
            session_id = None
        return session_id or self.ensure_agent_session_id(request, session_anchor=anchor)

    @staticmethod
    def _uses_namespaced_backend_session(context, *, subagent_name=None):
        payload = getattr(context, "platform_specific", None) or {}
        return bool(subagent_name or payload.get("routing_subagent"))

    @staticmethod
    def _reserved_native_session_id(context, backend=None):
        # Mirrors the real BaseAgent helper: native session bound to the reserved
        # workbench row (by PK), carried in agent_session_target; gated by backend
        # match. None for the IM-style turns these tests exercise (no reserved
        # target).
        payload = getattr(context, "platform_specific", None) or {}
        target = payload.get("agent_session_target")
        if not isinstance(target, dict):
            return None
        native = str(target.get("native_session_id") or "").strip()
        if not native:
            return None
        if backend:
            target_backend = str(target.get("agent_backend") or "").strip()
            if target_backend and target_backend != backend:
                return None
        return native


setattr(_base_module, "BaseAgent", _BaseAgent)

_event_handler_module = types.ModuleType("modules.agents.codex.event_handler")
setattr(_event_handler_module, "CodexEventHandler", object)

_session_module = types.ModuleType("modules.agents.codex.session")
setattr(_session_module, "CodexSessionManager", object)

_transport_module = types.ModuleType("modules.agents.codex.transport")
setattr(_transport_module, "CodexTransport", object)
setattr(_transport_module, "CodexRPCError", CodexRPCError)
setattr(_transport_module, "CodexResponseTooLargeError", CodexResponseTooLargeError)

_turn_state_module = types.ModuleType("modules.agents.codex.turn_state")
setattr(_turn_state_module, "CodexTurnRegistry", object)

_subagent_router_module = types.ModuleType("modules.agents.subagent_router")


class _StubSubagentDefinition:
    def __init__(
        self,
        name=None,
        description=None,
        developer_instructions=None,
        model=None,
        reasoning_effort=None,
        path=None,
        source=None,
    ):
        self.name = name
        self.description = description
        self.developer_instructions = developer_instructions
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.path = path
        self.source = source


setattr(_subagent_router_module, "SubagentDefinition", _StubSubagentDefinition)
setattr(_subagent_router_module, "load_codex_subagent", lambda *args, **kwargs: None)

_STUBBED_MODULES = {
    "modules": _modules_pkg,
    "modules.agents": _agents_pkg,
    "modules.agents.codex": _codex_pkg,
    "modules.agents.base": _base_module,
    "modules.agents.subagent_router": _subagent_router_module,
    "modules.agents.codex.event_handler": _event_handler_module,
    "modules.agents.codex.session": _session_module,
    "modules.agents.codex.transport": _transport_module,
    "modules.agents.codex.turn_state": _turn_state_module,
}
# Prime the real ``modules.agents.catalog`` before installing the bare (no
# ``__path__``) ``modules.agents`` stub below. Loading agent.py pulls in
# core.show_pages -> config.v2_config -> ``from modules.agents.catalog import``;
# without the real submodule cached first, the stub shadows it and standalone
# collection fails with "modules.agents is not a package". Sibling test modules
# import core.controller (which primes this), so a group run masks the issue.
import modules.agents.catalog  # noqa: E402,F401

_saved_modules = {name: sys.modules.get(name) for name in _STUBBED_MODULES}

for name, module in _STUBBED_MODULES.items():
    sys.modules[name] = module

_SPEC = importlib.util.spec_from_file_location("test_codex_agent_module", _AGENT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
try:
    _SPEC.loader.exec_module(_MODULE)
finally:
    sys.modules.pop(_SPEC.name, None)
CodexAgent = _MODULE.CodexAgent
CODEX_PROMPT_STRATEGY_METADATA_KEY = _MODULE.CODEX_PROMPT_STRATEGY_METADATA_KEY
CodexConnectionProbeRuntimeMismatchError = (
    _MODULE.CodexConnectionProbeRuntimeMismatchError
)
CodexPromptRefreshUnavailableError = _MODULE.CodexPromptRefreshUnavailableError
CodexForkBoundaryUnavailableError = _MODULE.CodexForkBoundaryUnavailableError
CodexResumeUnavailableError = _MODULE.CodexResumeUnavailableError

for name, module in _saved_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


class CodexExitPressureCacheTests(unittest.TestCase):
    def _agent_with_transport(self, transport):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(language="en"))
        agent._session_mgr = SimpleNamespace(get_cwd=lambda _base: "/work")
        install_codex_transport(agent, "/work", transport, sessions={"one": "t1", "two": "t2"})
        return agent

    def test_concurrent_sessions_share_positive_exit_observation(self):
        transport = SimpleNamespace(_process=SimpleNamespace(returncode=137))
        agent = self._agent_with_transport(transport)
        failure = AgentResourceFailure(
            kind="pids", message="shared PID event", pids_current=10, pids_max=32
        )
        first = agent.capture_backend_exit_failure(
            SimpleNamespace(platform_specific={"turn_base_session_id": "one"})
        )
        second = agent.capture_backend_exit_failure(
            SimpleNamespace(platform_specific={"turn_base_session_id": "two"})
        )

        with patch.object(
            _MODULE, "observe_agent_resource_pressure", return_value=failure
        ) as observe:
            assert first() == second()
        observe.assert_called_once()

    def test_negative_exit_observation_is_cached_only_for_that_transport(self):
        old_transport = SimpleNamespace(_process=SimpleNamespace(returncode=137))
        agent = self._agent_with_transport(old_transport)
        first = agent.capture_backend_exit_failure(
            SimpleNamespace(platform_specific={"turn_base_session_id": "one"})
        )
        second = agent.capture_backend_exit_failure(
            SimpleNamespace(platform_specific={"turn_base_session_id": "two"})
        )
        newer_failure = AgentResourceFailure(kind="memory", message="new event")

        with patch.object(
            _MODULE,
            "observe_agent_resource_pressure",
            side_effect=[None, newer_failure],
        ) as observe:
            assert first() is None
            assert second() is None
            install_codex_transport(
                agent,
                "/work",
                SimpleNamespace(_process=SimpleNamespace(returncode=137)),
                sessions={"three": "t3"},
            )
            third = agent.capture_backend_exit_failure(
                SimpleNamespace(platform_specific={"turn_base_session_id": "three"})
            )
            assert "new event" in third()[0]
        assert observe.call_count == 2


class _StubSessionManager:
    def __init__(self):
        self._threads = {}

    def find_base_session_id_for_thread(self, thread_id: str):
        for base_session_id, stored_thread_id in self._threads.items():
            if stored_thread_id == thread_id:
                return base_session_id
        return None


class _StubTurnRegistry:
    def __init__(self):
        self._turn_requests = {}
        self._latest_requests = {}
        self._pending_requests = {}
        self._active_turns = {}

    def get_request_for_turn(self, turn_id: str):
        return self._turn_requests.get(turn_id)

    def get_latest_request(self, base_session_id: str):
        return self._latest_requests.get(base_session_id)

    def bootstrap_turn(self, turn_id: str, base_session_id: str, thread_id: str):
        request = self._pending_requests.get(base_session_id)
        if not request:
            return None
        self._turn_requests[turn_id] = request
        return SimpleNamespace(request=request)

    def get_active_turn(self, base_session_id: str):
        return self._active_turns.get(base_session_id)

    def finalize_turn_start_response(self, turn_id: str, request):
        self._turn_requests[turn_id] = request
        return SimpleNamespace(request=request)


class CodexAgentNotificationRoutingTests(unittest.TestCase):
    def test_find_request_prefers_turn_mapping_over_replaced_active_request(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        old_request = SimpleNamespace(base_session_id="session-1", context="old")
        new_request = SimpleNamespace(base_session_id="session-1", context="new")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = new_request
        agent._turn_registry._turn_requests["turn-1"] = old_request

        request = agent._find_request_for_notification("item/completed", {"threadId": "thread-1", "turnId": "turn-1"})

        self.assertIs(request, old_request)

    def test_find_request_falls_back_to_thread_mapping_without_turn_id(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request

        resolved = agent._find_request_for_notification("thread/started", {"threadId": "thread-1"})

        self.assertIs(resolved, request)

    def test_find_request_does_not_fall_back_to_thread_when_turn_is_unknown(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request

        resolved = agent._find_request_for_notification(
            "item/completed", {"threadId": "thread-1", "turnId": "turn-old"}
        )

        self.assertIsNone(resolved)

    def test_find_request_bootstraps_pending_turn_start(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request
        agent._turn_registry._pending_requests["session-1"] = request

        resolved = agent._find_request_for_notification(
            "turn/started", {"threadId": "thread-1", "turn": {"id": "turn-1"}}
        )

        self.assertIs(resolved, request)

    def test_find_request_does_not_bootstrap_items_for_pending_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request
        agent._turn_registry._pending_requests["session-1"] = request

        resolved = agent._find_request_for_notification("item/completed", {"threadId": "thread-1", "turnId": "turn-1"})

        self.assertIsNone(resolved)


class CodexAgentConnectionProbeTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self, cwd: str, transport, *, direct=True):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._connection_probes = {}
        agent._connection_probe_turns = {}
        self.generation = install_codex_transport(agent, cwd, transport, hub=not direct)
        agent._direct_probe_generation = Mock(return_value=self.generation if direct else None)
        return agent

    def test_live_probe_requires_a_direct_generation_and_existing_cwd(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.codex_config = SimpleNamespace(binary="codex-probe-fixture", extra_args=[])
        with tempfile.TemporaryDirectory() as cwd:
            self.assertFalse(agent.can_reuse_direct_connection_probe(cwd))
            direct_digest = asyncio.run(agent._launch_spec(cwd)).digest

            install_codex_transport(
                agent, cwd, SimpleNamespace(is_initialized=True), digest="hub-spec", hub=True
            )
            self.assertFalse(agent.can_reuse_direct_connection_probe(cwd))

            install_codex_transport(
                agent, cwd, SimpleNamespace(is_initialized=True), digest=direct_digest
            )
            self.assertTrue(agent.can_reuse_direct_connection_probe(cwd))
        self.assertFalse(agent.can_reuse_direct_connection_probe(cwd))

    async def test_reuses_existing_transport_with_isolated_ephemeral_thread(self):
        requests = []
        cwd = "/tmp/user-project"
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)

        class _Transport:
            is_initialized = True

            async def send_request(inner_self, method, params):
                requests.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                await agent._on_notification(
                    "item/completed",
                    {
                        "threadId": "thread-probe",
                        "turnId": "turn-probe",
                        "item": {"type": "agentMessage", "text": "hello"},
                    },
                )
                await agent._on_notification(
                    "turn/completed",
                    {
                        "threadId": "thread-probe",
                        "turn": {"id": "turn-probe", "status": "completed"},
                    },
                )
                return {"turn": {"id": "turn-probe"}}

            async def wait_closed(inner_self):
                await asyncio.Event().wait()

        transport = _Transport()
        agent = self._agent(cwd, transport)
        diagnostics = []

        with patch.object(
            _MODULE.paths,
            "get_runtime_dir",
            return_value=Path(runtime_dir.name),
        ):
            result = await agent.probe_connection(
                cwd,
                model="gpt-5.4-mini",
                on_diagnostic=diagnostics.append,
            )

        self.assertEqual(result, "hello")
        self.assertIs(agent._current_generation(cwd), self.generation)
        self.assertEqual(self.generation.bindings, 0)
        self.assertEqual(requests[0][0], "thread/start")
        self.assertEqual(
            requests[0][1],
            {
                "cwd": str(Path(runtime_dir.name) / "codex-connection-probe"),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "model": "gpt-5.4-mini",
                "developerInstructions": (
                    "This is a connection probe. Do not use tools. "
                    "Reply with a short greeting."
                ),
            },
        )
        self.assertEqual(
            requests[1],
            (
                "turn/start",
                {
                    "threadId": "thread-probe",
                    "input": [{"type": "text", "text": "Hi"}],
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly",
                        "networkAccess": False,
                    },
                    "effort": "low",
                    "model": "gpt-5.4-mini",
                },
            ),
        )
        self.assertEqual(agent._connection_probes, {})
        self.assertEqual(agent._connection_probe_turns, {})
        self.assertEqual(diagnostics, [])

    async def test_transport_exit_settles_probe_without_outer_timeout(self):
        cwd = "/tmp/user-project"
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)

        class _Transport:
            is_initialized = True

            async def send_request(inner_self, method, _params):
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                return {"turn": {"id": "turn-probe"}}

            async def wait_closed(inner_self):
                return None

        agent = self._agent(cwd, _Transport())

        with (
            patch.object(
                _MODULE.paths,
                "get_runtime_dir",
                return_value=Path(runtime_dir.name),
            ),
            self.assertRaisesRegex(
                ConnectionError,
                "app-server exited during the connection probe",
            ),
        ):
            await agent.probe_connection(cwd, model="gpt-fixture")

        self.assertEqual(agent._connection_probes, {})
        self.assertEqual(agent._connection_probe_turns, {})
        self.assertEqual(self.generation.bindings, 0)

    async def test_cancellation_interrupts_started_probe_and_clears_ownership(self):
        cwd = "/tmp/user-project"
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)
        turn_started = asyncio.Event()
        requests = []

        class _Transport:
            is_initialized = True

            async def send_request(inner_self, method, params):
                requests.append((method, params))
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                if method == "turn/start":
                    turn_started.set()
                    return {"turn": {"id": "turn-probe"}}
                if method == "turn/interrupt":
                    return {}
                raise AssertionError(method)

            async def wait_closed(inner_self):
                await asyncio.Event().wait()

        agent = self._agent(cwd, _Transport())
        with patch.object(
            _MODULE.paths,
            "get_runtime_dir",
            return_value=Path(runtime_dir.name),
        ):
            task = asyncio.create_task(agent.probe_connection(cwd, model="gpt-fixture"))
            await turn_started.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertIn(
            (
                "turn/interrupt",
                {"threadId": "thread-probe", "turnId": "turn-probe"},
            ),
            requests,
        )
        self.assertEqual(agent._connection_probes, {})
        self.assertEqual(agent._connection_probe_turns, {})
        self.assertEqual(self.generation.bindings, 0)

    async def test_retriable_errors_are_preserved_as_diagnostics(self):
        cwd = "/tmp/user-project"
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)
        diagnostics = []

        class _Transport:
            is_initialized = True

            async def send_request(inner_self, method, _params):
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                await agent._on_notification(
                    "error",
                    {
                        "threadId": "thread-probe",
                        "turnId": "turn-probe",
                        "willRetry": True,
                        "error": {"message": "401 Unauthorized"},
                    },
                )
                await agent._on_notification(
                    "turn/completed",
                    {
                        "threadId": "thread-probe",
                        "turn": {"id": "turn-probe", "status": "completed"},
                    },
                )
                return {"turn": {"id": "turn-probe"}}

            async def wait_closed(inner_self):
                await asyncio.Event().wait()

        agent = self._agent(cwd, _Transport())
        with patch.object(
            _MODULE.paths,
            "get_runtime_dir",
            return_value=Path(runtime_dir.name),
        ):
            with self.assertRaisesRegex(RuntimeError, "returned no response"):
                await agent.probe_connection(
                    cwd,
                    model="gpt-fixture",
                    on_diagnostic=diagnostics.append,
                )

        self.assertEqual(diagnostics, ["401 Unauthorized"])

    async def test_rejects_initialized_model_hub_transport(self):
        cwd = "/tmp/user-project"
        transport = SimpleNamespace(is_initialized=True)
        agent = self._agent(cwd, transport, direct=False)

        with self.assertRaises(CodexConnectionProbeRuntimeMismatchError):
            await agent.probe_connection(cwd, model="gpt-fixture")

        self.assertEqual(self.generation.bindings, 0)

    async def test_probe_binds_a_retiring_direct_generation_without_promoting_it(self):
        cwd = "/tmp/user-project"
        runtime_dir = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_dir.cleanup)

        class _Transport:
            is_initialized = True

            async def send_request(inner_self, method, _params):
                if method == "thread/start":
                    return {"thread": {"id": "thread-probe"}}
                await agent._on_notification(
                    "item/completed",
                    {"threadId": "thread-probe", "turnId": "turn-probe", "item": {"type": "agentMessage", "text": "hi"}},
                )
                await agent._on_notification(
                    "turn/completed",
                    {"threadId": "thread-probe", "turn": {"id": "turn-probe", "status": "completed"}},
                )
                return {"turn": {"id": "turn-probe"}}

            async def wait_closed(inner_self):
                await asyncio.Event().wait()

        agent = self._agent(cwd, _Transport())
        direct = self.generation
        hub = install_codex_transport(agent, cwd, SimpleNamespace(is_initialized=True), digest="hub", hub=True)
        self.assertTrue(direct.retiring)

        with patch.object(_MODULE.paths, "get_runtime_dir", return_value=Path(runtime_dir.name)):
            self.assertEqual(await agent.probe_connection(cwd, model="gpt-fixture"), "hi")

        self.assertIs(agent._current_generation(cwd), hub)
        self.assertTrue(direct.retiring)
        self.assertEqual(direct.bindings, 0)

    async def test_unregistered_probe_runtime_preserves_live_activation(self):
        cwd = "/tmp/user-project"
        activation = RuntimeActivationRegistry()
        live_identity = activation.attach("codex", cwd)
        retire_scope = Mock()
        transport = SimpleNamespace(stop=AsyncMock(), _process=None)
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._registered_runtime = False
        agent._session_last_activity = {}
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.sessions = SimpleNamespace()
        agent.controller = SimpleNamespace(
            runtime_activation=activation,
            model_hub_runtime=SimpleNamespace(retire_process_scope=retire_scope),
        )
        generation = install_codex_transport(agent, cwd, transport, hub=True)

        self.assertIsNone(agent._attach_runtime_activation(generation.runtime))
        await agent.shutdown_runtime()

        transport.stop.assert_awaited_once_with()
        self.assertIs(activation.current("codex", cwd), live_identity)
        retire_scope.assert_not_called()


class CodexAgentStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ownership = SimpleNamespace(
            disposition="reclaimable",
            blocks_reclamation=False,
            blocks_transport_replacement=False,
            blocks_dead_transport_replacement=False,
        )
        self.snapshot_calls = 0
        # Holding this gate pauses the given snapshot call, the way a racing
        # turn could act while ownership is read off the event loop.
        self.snapshot_gate = None
        self.gated_call = 2

        async def snapshots(_agent, generations):
            self.snapshot_calls += 1
            if self.snapshot_gate is not None and self.snapshot_calls == self.gated_call:
                await self.snapshot_gate.wait()
            return tuple(self.ownership for _ in generations)

        patcher = patch.object(CodexAgent, "_ownership_snapshots", new=snapshots)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_handle_stop_does_not_hide_turn_before_interrupt_succeeds(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()
        transport = SimpleNamespace(is_alive=True, send_request=AsyncMock(side_effect=RuntimeError("boom")))
        install_codex_transport(agent, "/tmp", transport, sessions={"session-1": "thread-1"})
        agent._event_handler = SimpleNamespace(clear_pending=Mock(return_value=SimpleNamespace()))
        agent._remove_ack_reaction = AsyncMock()
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())

        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        result = await agent.handle_stop(request)

        self.assertFalse(result)
        agent._event_handler.clear_pending.assert_not_called()
        agent._remove_ack_reaction.assert_not_awaited()
        # Nothing was stopped, so no later ending may inherit a stopped receipt.
        self.assertEqual(agent._user_stopped_turn_ids, set())

    async def test_handle_stop_hides_turn_after_interrupt_succeeds(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()

        events = []

        async def send_request(method, payload):
            events.append(("send", method, payload))
            return {}

        def clear_pending(turn_id):
            events.append(("clear", turn_id))
            return SimpleNamespace()

        install_codex_transport(
            agent,
            "/tmp",
            SimpleNamespace(is_alive=True, send_request=send_request),
            sessions={"session-1": "thread-1"},
        )
        agent._event_handler = SimpleNamespace(clear_pending=clear_pending)
        agent._remove_ack_reaction = AsyncMock(
            side_effect=lambda request, *, terminal_emoji=None: events.append(("ack", terminal_emoji))
        )
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())

        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        result = await agent.handle_stop(request)

        self.assertTrue(result)
        self.assertEqual(events[0][0], "send")
        self.assertEqual(events[1][0], "clear")
        # The stop is silent, so the ⏹️ receipt replacing the running 👀 is the
        # only thing that tells the user the turn ended on their command.
        self.assertEqual(events[2], ("ack", STOPPED_REACTION_EMOJI))
        self.assertEqual(agent._user_stopped_turn_ids, set())

    async def test_handle_stop_uses_the_generation_running_the_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()
        running = SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={}))
        install_codex_transport(agent, "/tmp", running, sessions={"session-1": "thread-1"})
        newer = SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={}))
        install_codex_transport(agent, "/tmp", newer, digest="newer")
        agent._event_handler = SimpleNamespace(clear_pending=Mock(return_value=SimpleNamespace()))
        agent._remove_ack_reaction = AsyncMock()
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())

        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        self.assertTrue(await agent.handle_stop(request))
        running.send_request.assert_awaited_once_with(
            "turn/interrupt", {"threadId": "thread-1", "turnId": "turn-1"}
        )
        newer.send_request.assert_not_awaited()

    async def test_handle_stop_does_not_cancel_a_turn_that_completed_during_rpc(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()
        install_codex_transport(
            agent,
            "/tmp",
            SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={})),
            sessions={"session-1": "thread-1"},
        )
        # None with the intent still present means a normal/failed completion
        # popped the turn; an interrupted completion would consume the intent.
        agent._event_handler = SimpleNamespace(clear_pending=Mock(return_value=None))
        agent._remove_ack_reaction = AsyncMock()
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())

        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        result = await agent.handle_stop(request)

        self.assertTrue(result)
        agent._remove_ack_reaction.assert_not_awaited()
        agent.controller.emit_agent_message.assert_not_awaited()
        self.assertEqual(agent._user_stopped_turn_ids, set())

    async def test_refresh_auth_state_stops_transports_and_invalidates_threads(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        stop_calls = []
        retire_scope = Mock()

        async def stop_a():
            stop_calls.append("a")

        async def stop_b():
            stop_calls.append("b")

        invalidated = []
        cleared_sessions = []
        install_codex_transport(agent, "/tmp/a", SimpleNamespace(stop=stop_a), hub=True)
        install_codex_transport(agent, "/tmp/b", SimpleNamespace(stop=stop_b), hub=True)
        agent._session_mgr = SimpleNamespace(
            all_base_sessions=lambda: ["session-1", "session-2"],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(clear_session=lambda base_session_id: cleared_sessions.append(base_session_id))
        release_calls = []

        async def release_for_backend_refresh(*, backend, base_session_ids):
            release_calls.append((backend, set(base_session_ids)))

        agent.controller = SimpleNamespace(
            session_turns=SimpleNamespace(release_for_backend_refresh=release_for_backend_refresh),
            model_hub_runtime=SimpleNamespace(
                retire_process_scope=retire_scope,
            ),
        )

        await agent.refresh_auth_state()

        self.assertEqual(release_calls, [("codex", {"session-1", "session-2"})])
        self.assertEqual(stop_calls, ["a", "b"])
        self.assertEqual(codex_transports(agent), {})
        self.assertEqual(invalidated, ["session-1", "session-2"])
        self.assertEqual(cleared_sessions, ["session-1", "session-2"])
        self.assertEqual(
            retire_scope.call_args_list,
            [
                call("codex", agent._hub_process_scope("/tmp/a")),
                call("codex", agent._hub_process_scope("/tmp/b")),
            ],
        )

    async def test_refresh_auth_state_retires_generation_before_transport_stop(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        transport = SimpleNamespace()
        agent._session_last_activity = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = activation.attach("codex", "/tmp/work#1")
        install_codex_transport(agent, "/tmp/work", transport, activation=identity)
        late_commit = Mock(return_value="started")

        async def stop_transport():
            result = activation.commit_if_current(identity, late_commit)
            self.assertFalse(result.admitted)

        transport.stop = stop_transport

        await agent.refresh_auth_state()

        late_commit.assert_not_called()
        self.assertEqual(codex_transports(agent), {})
        self.assertIsNone(activation.current("codex", "/tmp/work#1"))

    def _resume_agent(self, transport, *, sessions):
        agent = init_generation_state(object.__new__(CodexAgent))
        self.invalidated = []
        self.cleared_sessions = []
        self.retire_scope = Mock()
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: list(sessions) if cwd == "/tmp/work" else [],
            get_thread_id=lambda base_session_id: sessions.get(base_session_id),
            invalidate_thread=lambda base_session_id: self.invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: None,
            clear_session=lambda base_session_id: self.cleared_sessions.append(base_session_id),
        )
        agent.controller = SimpleNamespace(
            model_hub_runtime=SimpleNamespace(retire_process_scope=self.retire_scope)
        )
        self.generation = install_codex_transport(agent, "/tmp/work", transport, sessions=sessions)
        return agent

    async def test_prepare_resume_binding_releases_only_the_sessions_thread(self):
        transport = SimpleNamespace(
            is_alive=True,
            stop=AsyncMock(),
            send_request=AsyncMock(return_value={"status": "notLoaded"}),
        )
        agent = self._resume_agent(transport, sessions={"session-1": "thread-1", "session-2": "thread-2"})

        await agent.prepare_resume_binding(
            base_session_id="session-1",
            session_key="scope-1",
            working_path="/tmp/work",
        )

        transport.send_request.assert_awaited_once_with("thread/unsubscribe", {"threadId": "thread-1"})
        transport.stop.assert_not_awaited()
        self.assertIs(codex_transports(agent)["/tmp/work"], transport)
        self.assertIsNone(agent.transport_for_session("session-1"))
        self.assertIs(agent.transport_for_session("session-2"), transport)
        self.assertEqual(self.invalidated, ["session-1"])
        self.assertEqual(self.cleared_sessions, ["session-1"])
        self.retire_scope.assert_not_called()

    async def test_resume_and_clear_wait_out_a_turn_admission_for_the_session(self):
        """Neither unloads a Session's thread while a turn for it is being admitted."""
        for operation in ("resume", "clear"):
            with self.subTest(operation=operation):
                agent = init_generation_state(object.__new__(CodexAgent))
                unsubscribed = asyncio.Event()
                unload = asyncio.Event()

                async def send_request(method, params):
                    unsubscribed.set()
                    await unload.wait()
                    return {"status": "notLoaded"}

                transport = SimpleNamespace(is_alive=True, _process=None, send_request=send_request, stop=AsyncMock())
                session_mgr = RealCodexSessionManager()
                session_mgr.set_session_key("session-1", "key-a")
                session_mgr.set_cwd("session-1", "/tmp/work")
                session_mgr.set_thread_id("session-1", "thread-1")
                agent._session_mgr = session_mgr
                agent.sessions = SimpleNamespace(clear_agent_sessions=Mock())
                agent._turn_registry = SimpleNamespace(get_active_turn=lambda _base: None, clear_session=Mock())
                install_codex_transport(
                    agent, "/tmp/work", transport, sessions={"session-1": "thread-1", "session-2": "thread-2"}
                )
                order = []

                async def admit_turn():
                    # Turn admission serializes on the Session's registered lock.
                    async with agent._session_locks.setdefault("session-1", asyncio.Lock()):
                        order.append("admitted")

                if operation == "resume":
                    pending = asyncio.create_task(
                        agent.prepare_resume_binding(
                            base_session_id="session-1", session_key="key-a", working_path="/tmp/work"
                        )
                    )
                else:
                    pending = asyncio.create_task(agent.clear_sessions("key-a"))
                await asyncio.wait_for(unsubscribed.wait(), 1)
                admission = asyncio.create_task(admit_turn())
                await asyncio.sleep(0.01)
                order.append("released")
                unload.set()
                await pending
                await admission

                self.assertEqual(order, ["released", "admitted"])

    async def test_prepare_resume_binding_aborts_the_resume_when_the_thread_stays_loaded(self):
        transport = SimpleNamespace(
            is_alive=True,
            stop=AsyncMock(),
            send_request=AsyncMock(
                side_effect=CodexRPCError({"code": -32603, "message": "unsubscribe failed"})
            ),
        )
        agent = self._resume_agent(transport, sessions={"session-1": "thread-1"})

        # The resume must fail before its mapping changes, or the next turn
        # would still reach the old thread.
        with self.assertRaises(_MODULE.CodexThreadReleaseUnavailableError):
            await agent.prepare_resume_binding(
                base_session_id="session-1",
                session_key="scope-1",
                working_path="/tmp/work",
            )

        self.assertIs(agent.transport_for_session("session-1"), transport)
        self.assertEqual(self.invalidated, [])
        self.assertEqual(self.cleared_sessions, [])
        transport.stop.assert_not_awaited()

    async def test_shutdown_runtime_retires_generation_before_transport_stop(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        transport = SimpleNamespace()
        agent._session_last_activity = {}
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.sessions = SimpleNamespace()
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = activation.attach("codex", "/tmp/work#1")
        install_codex_transport(agent, "/tmp/work", transport, activation=identity)
        late_commit = Mock(return_value="started")

        async def stop_transport():
            self.assertFalse(
                activation.commit_if_current(identity, late_commit).admitted
            )

        transport.stop = stop_transport

        await agent.shutdown_runtime()

        late_commit.assert_not_called()
        self.assertEqual(codex_transports(agent), {})
        self.assertIsNone(activation.current("codex", "/tmp/work#1"))

    async def test_shutdown_runtime_ends_busy_generations_without_the_update_notice(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        transport = SimpleNamespace(stop=AsyncMock())
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: "turn-1",
            clear_session=Mock(),
        )
        agent.sessions = SimpleNamespace()
        agent.controller = SimpleNamespace(
            agent_service=SimpleNamespace(force_end_runtime_work=AsyncMock())
        )
        install_codex_transport(agent, "/tmp/work", transport, sessions={"session-1": "thread-1"})

        await agent.shutdown_runtime()

        transport.stop.assert_awaited_once_with()
        agent.controller.agent_service.force_end_runtime_work.assert_not_awaited()

    async def test_refresh_keeps_sessions_bound_to_a_process_that_failed_to_stop(self):
        """A surviving process still holds its threads, so its Sessions stay bound to it."""
        agent = init_generation_state(object.__new__(CodexAgent))
        transport = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("stop failed")), _process=None)
        invalidated = []
        agent._session_mgr = SimpleNamespace(
            all_base_sessions=lambda: ["session-1", "session-2"],
            invalidate_thread=invalidated.append,
        )
        agent._turn_registry = SimpleNamespace(clear_session=Mock())
        agent.controller = SimpleNamespace()
        install_codex_transport(
            agent, "/tmp/work", transport, sessions={"session-1": "thread-1", "session-2": "thread-2"}
        )

        await agent.refresh_auth_state()

        self.assertIs(agent.transport_for_session("session-1"), transport)
        self.assertIs(agent.transport_for_session("session-2"), transport)
        self.assertEqual(invalidated, [])

    async def test_clearing_a_session_releases_its_thread_on_a_shared_app_server(self):
        """A cleared conversation must not leave its thread loaded in a shared process."""
        agent = init_generation_state(object.__new__(CodexAgent))
        sent = []

        async def send_request(method, params):
            sent.append((method, params))
            return {"status": "notLoaded"}

        transport = SimpleNamespace(is_alive=True, send_request=send_request, stop=AsyncMock())
        session_mgr = RealCodexSessionManager()
        for base, key in (("session-1", "key-a"), ("session-2", "key-b")):
            session_mgr.set_session_key(base, key)
            session_mgr.set_cwd(base, "/tmp/work")
            session_mgr.set_thread_id(base, f"thread-{base}")
        agent._session_mgr = session_mgr
        agent.sessions = SimpleNamespace(clear_agent_sessions=Mock())
        agent._turn_registry = SimpleNamespace(get_active_turn=lambda _base: None, clear_session=Mock())
        agent._session_locks = {}
        install_codex_transport(
            agent,
            "/tmp/work",
            transport,
            sessions={"session-1": "thread-session-1", "session-2": "thread-session-2"},
        )

        await agent.clear_sessions("key-a")

        self.assertEqual(sent, [("thread/unsubscribe", {"threadId": "thread-session-1"})])
        self.assertIsNone(agent.transport_for_session("session-1"))
        self.assertIs(agent.transport_for_session("session-2"), transport)
        transport.stop.assert_not_awaited()

    async def test_clearing_a_session_keeps_it_when_its_thread_stays_loaded(self):
        """A failed release fails the clear before anything is forgotten, so it can be retried."""
        agent = init_generation_state(object.__new__(CodexAgent))
        transport = SimpleNamespace(
            is_alive=True,
            send_request=AsyncMock(side_effect=CodexRPCError({"code": -32603, "message": "unsubscribe failed"})),
            stop=AsyncMock(),
        )
        session_mgr = RealCodexSessionManager()
        session_mgr.set_session_key("session-1", "key-a")
        session_mgr.set_cwd("session-1", "/tmp/work")
        session_mgr.set_thread_id("session-1", "thread-session-1")
        agent._session_mgr = session_mgr
        agent.sessions = SimpleNamespace(clear_agent_sessions=Mock())
        agent._turn_registry = SimpleNamespace(get_active_turn=lambda _base: None, clear_session=Mock())
        install_codex_transport(agent, "/tmp/work", transport, sessions={"session-1": "thread-session-1"})

        with self.assertRaises(_MODULE.CodexThreadReleaseUnavailableError):
            await agent.clear_sessions("key-a")

        agent.sessions.clear_agent_sessions.assert_not_called()
        self.assertEqual(session_mgr.get_thread_id("session-1"), "thread-session-1")
        self.assertEqual(session_mgr.get_sessions_by_session_key("key-a"), ["session-1"])
        self.assertIs(agent.transport_for_session("session-1"), transport)

    async def test_forced_stop_fences_owner_commits_before_settling(self):
        """No durable owner can commit to a generation while its forced stop settles work."""
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        identity = activation.attach("codex", "/tmp/work#1")
        transport = SimpleNamespace(stop=AsyncMock(), _process=None)
        agent._session_mgr = SimpleNamespace(invalidate_thread=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-1"), get_request_for_turn=Mock(return_value=None), clear_session=Mock()
        )
        commits = []

        async def settle(*_args, **_kwargs):
            # A queued turn tries to register its owner while the work settles.
            commits.append(activation.commit_if_current(identity, lambda: "owner").admitted)

        agent.controller = SimpleNamespace(
            runtime_activation=activation,
            agent_service=SimpleNamespace(force_end_runtime_work=AsyncMock(side_effect=settle)),
        )
        generation = install_codex_transport(
            agent, "/tmp/work", transport, sessions={"session-1": "thread-1"}, activation=identity
        )

        self.assertTrue(await agent._stop_generation(generation, True))

        self.assertEqual(commits, [False])
        transport.stop.assert_awaited_once_with()

    async def test_a_graceful_stop_waits_for_an_activity_its_sessions_no_longer_name(self):
        """The Session moved its thread to a newer process, but an Activity it
        started here still runs: the old process stays until that Activity ends."""
        from core.session_activities import SessionActivityRegistry
        from modules.agents.service import AgentService

        activation = RuntimeActivationRegistry()
        service = AgentService(
            controller=SimpleNamespace(),
            activities=SessionActivityRegistry(activation_registry=activation),
            activation_registry=activation,
        )
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(invalidate_thread=Mock())
        agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value=None), clear_session=Mock())
        agent.controller = SimpleNamespace(runtime_activation=activation, agent_service=service)
        identity = activation.attach("codex", "/tmp/work#1")
        transport = SimpleNamespace(stop=AsyncMock(), is_alive=True, _process=None)
        generation = install_codex_transport(agent, "/tmp/work", transport, activation=identity)
        service.activities.start(
            backend="codex",
            runtime_key="session-1:/tmp/work",
            session_id="ses-1",
            activity_id="task-1",
            kind="task",
            activation_identity=identity,
        )

        async def drained(_agent, generations):
            # No Session binding names the Activity any longer.
            return tuple(SimpleNamespace(blocks_transport_replacement=False) for _ in generations)

        with patch.object(CodexAgent, "_ownership_snapshots", new=drained):
            self.assertFalse(await agent._stop_generation(generation, False))
            transport.stop.assert_not_awaited()
            service.activities.complete(
                backend="codex",
                runtime_key="session-1:/tmp/work",
                activity_id="task-1",
                status="completed",
                activation_identity=identity,
            )
            self.assertTrue(await agent._stop_generation(generation, False))

        transport.stop.assert_awaited_once_with()

    async def test_graceful_stop_rechecks_ownership_inside_the_fence(self):
        """An owner that commits after the drained check keeps its process."""
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        identity = activation.attach("codex", "/tmp/work#1")
        transport = SimpleNamespace(stop=AsyncMock(), is_alive=True, _process=None)
        agent._session_mgr = SimpleNamespace(invalidate_thread=Mock())
        agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value=None), clear_session=Mock())
        agent.controller = SimpleNamespace(runtime_activation=activation)
        generation = install_codex_transport(
            agent, "/tmp/work", transport, sessions={"session-1": "thread-1"}, activation=identity
        )
        drained = SimpleNamespace(blocks_transport_replacement=False)
        # An Activity commits right after the first, drained snapshot.
        owned = SimpleNamespace(blocks_transport_replacement=True)
        snapshots = iter([drained, owned])

        async def snapshot(_agent, generations):
            return (next(snapshots),)

        with patch.object(CodexAgent, "_ownership_snapshots", new=snapshot):
            self.assertFalse(await agent._stop_generation(generation, False))

        transport.stop.assert_not_awaited()
        # The aborted reservation leaves the generation admitting owners.
        self.assertTrue(activation.commit_if_current(identity, lambda: "owner").admitted)

    async def test_last_session_end_settles_activity_owners_before_the_kill(self):
        """End kills the process only after the ending Session's Activities settle."""
        agent = init_generation_state(object.__new__(CodexAgent))
        events = []
        activation = object()

        async def stop():
            events.append("stop")

        async def end_work(backend, *, base_session_ids, activation_identities, reason, agent):
            events.append(("settle", backend, set(base_session_ids), activation_identities, reason))

        transport = SimpleNamespace(stop=stop, _process=None)
        agent._session_mgr = SimpleNamespace(invalidate_thread=Mock(), clear=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda _base: None,
            has_pending_turn_start=lambda _base: False,
            clear_session=Mock(),
        )
        agent.controller = SimpleNamespace(agent_service=SimpleNamespace(force_end_runtime_work=end_work))
        install_codex_transport(
            agent, "/tmp/work", transport, sessions={"session-1": "thread-1"}, activation=activation
        )

        self.assertTrue((await agent.end_session("session-1"))["process_killed"])

        self.assertEqual(
            events,
            [("settle", "codex", set(), {activation}, "stopped"), "stop"],
        )

    async def test_a_forced_stop_settles_activities_its_sessions_no_longer_name(self):
        """An Activity outlives its turn and its Session's thread move; a forced stop still ends it.

        The process's Activities are found by its activation, not by the
        Sessions still bound to it.
        """
        from core.session_activities import SessionActivityRegistry
        from modules.agents.service import AgentService

        activation = RuntimeActivationRegistry()
        service = AgentService(
            controller=SimpleNamespace(),
            activities=SessionActivityRegistry(activation_registry=activation),
            activation_registry=activation,
        )
        settled = []
        service.on_activity_terminal = settled.append
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(invalidate_thread=Mock())
        agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value=None), clear_session=Mock())
        agent.controller = SimpleNamespace(agent_service=service)
        identity = activation.attach("codex", "/tmp/work#1")
        transport = SimpleNamespace(stop=AsyncMock(), _process=None)
        # The Session's thread already moved to a newer process: none is bound here.
        generation = install_codex_transport(agent, "/tmp/work", transport, activation=identity)
        service.activities.start(
            backend="codex",
            runtime_key="session-1:/tmp/work",
            session_id="ses-1",
            activity_id="task-1",
            kind="task",
            activation_identity=identity,
        )

        self.assertTrue(await agent._stop_generation(generation, True))

        self.assertEqual(
            [(activity.id, activity.status, activity.metadata.get("interrupt_reason")) for activity in settled],
            [("task-1", "killed", "backend_refresh")],
        )
        transport.stop.assert_awaited_once_with()

    def test_request_activation_resolves_one_live_session_key_transport(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-dead", "base-live"],
            get_cwd=lambda base_session_id: {
                "base-dead": "/tmp/dead",
                "base-live": "/tmp/live",
            }[base_session_id],
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = activation.attach("codex", "/tmp/live#1")
        install_codex_transport(agent, "/tmp/live", SimpleNamespace(), activation=identity)

        resolved = agent.runtime_activation_identity_for_request(
            SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
        )

        self.assertEqual(resolved, identity)

    def test_request_activation_prefers_the_generation_holding_the_session(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        agent._session_mgr = SimpleNamespace(get_cwd=lambda base_session_id: "/tmp/work")
        running = activation.attach("codex", "/tmp/work#1")
        install_codex_transport(
            agent, "/tmp/work", SimpleNamespace(), activation=running, sessions={"base-1": "thread-1"}
        )
        newer = activation.attach("codex", "/tmp/work#2")
        install_codex_transport(agent, "/tmp/work", SimpleNamespace(), activation=newer, digest="newer")

        self.assertIs(
            agent.runtime_activation_identity_for_request(
                SimpleNamespace(base_session_id="base-1", working_path="/tmp/work")
            ),
            running,
        )
        self.assertIs(
            agent.runtime_activation_identity_for_request(
                SimpleNamespace(base_session_id="base-2", working_path="/tmp/work")
            ),
            newer,
        )
        self.assertIs(
            agent.runtime_activation_identity_for_session_binding(
                session_anchor="base-1", workdir="/tmp/work"
            ),
            running,
        )

    def test_request_activation_fails_closed_for_multiple_live_session_key_transports(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        activation = RuntimeActivationRegistry()
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-a", "base-b"],
            get_cwd=lambda base_session_id: {
                "base-a": "/tmp/a",
                "base-b": "/tmp/b",
            }[base_session_id],
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)
        install_codex_transport(agent, "/tmp/a", SimpleNamespace(), activation=activation.attach("codex", "/tmp/a#1"))
        install_codex_transport(agent, "/tmp/b", SimpleNamespace(), activation=activation.attach("codex", "/tmp/b#1"))

        with self.assertRaisesRegex(ValueError, "multiple live Codex runtime"):
            agent.runtime_activation_identity_for_request(
                SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
            )

    def test_request_activation_returns_none_when_session_key_has_no_live_transport(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-dead"],
            get_cwd=lambda base_session_id: "/tmp/dead",
        )

        resolved = agent.runtime_activation_identity_for_request(
            SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
        )

        self.assertIsNone(resolved)

    def _evict_agent(self, transport, *, active_turn=None, pending=False, last_activity=0.0):
        """Build a bare CodexAgent wired for evict_idle_transports tests."""
        agent = init_generation_state(object.__new__(CodexAgent))
        self.invalidated = []
        self.cleared_turns = []
        self.retire_scope = Mock()
        active_turns = {"session-1": active_turn}
        request = SimpleNamespace(context="ctx-1", base_session_id="session-1")

        def clear_session(base_session_id):
            self.cleared_turns.append(base_session_id)
            active_turns[base_session_id] = None

        agent._session_last_activity = {"session-1": last_activity}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: self.invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: active_turns.get(base_session_id),
            has_pending_turn_start=lambda base_session_id: pending,
            get_request_for_turn=lambda turn_id: request if turn_id == active_turn else None,
            get_latest_request=lambda base_session_id: request,
            clear_session=clear_session,
        )
        self.release_calls = []
        agent._event_handler = SimpleNamespace(
            _release_stream_turn=lambda context: self.release_calls.append(context),
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        self.activation = RuntimeActivationRegistry()
        self.identity = self.activation.attach("codex", "/tmp/work#1")
        agent.controller = SimpleNamespace(
            runtime_activation=self.activation,
            emit_agent_message=AsyncMock(),
            model_hub_runtime=SimpleNamespace(retire_process_scope=self.retire_scope),
        )
        self.generation = install_codex_transport(
            agent,
            "/tmp/work",
            transport,
            sessions={"session-1": "thread-1"},
            activation=self.identity,
            hub=True,
            last_activity=last_activity,
        )
        return agent

    def _stopping_transport(self):
        self.stop_calls = []

        async def stop_transport():
            self.stop_calls.append("stop")

        return SimpleNamespace(stop=stop_transport)

    async def test_evict_idle_transports_stops_idle_codex_runtime(self):
        agent = self._evict_agent(self._stopping_transport())

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertEqual(self.invalidated, ["session-1"])
        self.assertEqual(self.cleared_turns, ["session-1"])
        agent.sessions.clear_agent_session_mapping.assert_not_called()
        self.assertEqual(codex_transports(agent), {})
        self.assertIsNone(self.activation.current("codex", "/tmp/work#1"))
        self.assertEqual(
            self.activation.current("codex", "/tmp/work#1", include_retired=True),
            self.identity,
        )
        self.assertNotIn("session-1", agent._session_locks)
        self.retire_scope.assert_called_once_with("codex", agent._hub_process_scope("/tmp/work"))

    async def test_evict_idle_transports_keeps_active_codex_runtime(self):
        transport = SimpleNamespace(stop=AsyncMock(side_effect=AssertionError("active transport should not be stopped")))
        agent = self._evict_agent(transport, active_turn="turn-1", last_activity=500.0)

        with patch.object(_MODULE.time, "monotonic", return_value=1200.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIs(codex_transports(agent)["/tmp/work"], transport)
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_evict_idle_transports_keeps_pending_turn_start_runtime(self):
        transport = SimpleNamespace(stop=AsyncMock(side_effect=AssertionError("pending start should not be stopped")))
        agent = self._evict_agent(transport, pending=True)

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIs(codex_transports(agent)["/tmp/work"], transport)

    async def test_evict_idle_transports_keeps_a_generation_a_turn_is_binding(self):
        transport = self._stopping_transport()
        agent = self._evict_agent(transport)
        binding = bind_installed(agent, self.generation)

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        await binding.release()

    async def test_evict_idle_transports_retains_generation_when_stop_fails(self):
        transport = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("boom")))
        agent = self._evict_agent(transport)

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        # The generation stays for the next sweep and keeps its Session bound.
        self.assertIn(self.generation, agent._units["/tmp/work"].generations)
        self.assertIs(agent.transport_for_session("session-1"), transport)
        self.assertEqual(self.invalidated, [])
        self.assertEqual(self.cleared_turns, [])
        self.retire_scope.assert_not_called()
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_evict_idle_transports_revalidates_activity_before_stop(self):
        agent = self._evict_agent(self._stopping_transport())
        self.snapshot_gate = asyncio.Event()

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            self.generation.runtime.last_activity = 950.0
            self.snapshot_gate.set()
            evicted = await eviction_task

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        self.assertIs(codex_transports(agent)["/tmp/work"], self.generation.runtime.transport)
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_hfr_144_stuck_active_settles_only_exact_codex_owner(self):
        """HFR-144: the exact owner settles through the terminal chokepoint."""
        # active turn that has been idle WAY past the stuck-active cap
        # (max(600*3, 1800) = 1800s) must be force-evicted — the leak fix.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")
        agent.handle_message = AsyncMock()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertEqual(self.invalidated, ["session-1"])
        self.assertEqual(codex_transports(agent), {})
        # Force-reaped stuck turns must settle Workbench status + runtime gate
        # through the shared terminal-result chokepoint.
        agent.controller.emit_agent_message.assert_awaited_once_with(
            "ctx-1", "result", "", is_error=True, level="silent", output=ANY
        )
        agent.handle_message.assert_not_awaited()
        self.assertEqual(self.release_calls, [])

    async def test_evict_idle_transports_force_evict_release_falls_back_to_latest_request(self):
        # Defensive path: if the active turn has no per-turn request mapping,
        # the runtime gate is still settled via get_latest_request.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")
        fallback_request = SimpleNamespace(context="ctx-latest", base_session_id="session-1")
        agent._turn_registry.get_request_for_turn = lambda turn_id: None
        agent._turn_registry.get_latest_request = lambda base_session_id: fallback_request

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(self.stop_calls, ["stop"])
        agent.controller.emit_agent_message.assert_awaited_once_with(
            "ctx-latest", "result", "", is_error=True, level="silent", output=ANY
        )
        self.assertEqual(self.release_calls, [])

    async def test_evict_idle_transports_keeps_active_transport_under_stuck_cap(self):
        # active turn idle past idle_timeout (600) but under the cap (1800):
        # still vetoed, NOT force-evicted.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        self.assertIn("/tmp/work", codex_transports(agent))
        self.assertEqual(self.cleared_turns, [])

    async def test_evict_idle_transports_stuck_cap_floor_dominates_small_timeout(self):
        # With a tiny idle_timeout (100s) the multiplier window (300s) is below
        # the 1800s floor, so the floor governs: idle 1000s < 1800s stays vetoed.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(100)

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        self.assertIn("/tmp/work", codex_transports(agent))

    async def test_evict_idle_transports_stuck_backstop_disabled(self):
        # multiplier <= 0 disables the backstop: an active turn is an absolute
        # veto again, no matter how long it has been idle.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")

        with patch.object(_MODULE, "DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER", 0):
            with patch.object(_MODULE.time, "monotonic", return_value=1_000_000.0):
                evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        self.assertIn("/tmp/work", codex_transports(agent))
        self.assertEqual(self.cleared_turns, [])

    async def test_silent_owned_codex_turn_survives_both_reclamation_checks(self):
        for ownership_arrives_during_check in (False, True):
            with self.subTest(ownership_arrives_during_check=ownership_arrives_during_check):
                agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")

                def snapshot(disposition):
                    return RuntimeTargetOwnershipSnapshot(
                        backend="codex",
                        resource_key="/tmp/work",
                        activity_runtime_keys=(),
                        sessions=(),
                        sessionless_active_activity_ids=(),
                        sessionless_fallback_run_ids=(),
                        disposition=disposition,
                    )

                active = snapshot(SessionRuntimeDisposition.ACTIVE)
                snapshots = iter(
                    [snapshot(SessionRuntimeDisposition.RECLAIMABLE), active]
                    if ownership_arrives_during_check else [active]
                )

                async def ownership(generations, snapshots=snapshots, active=active):
                    return tuple(next(snapshots, active) for _ in generations)

                agent._ownership_snapshots = ownership
                with patch.object(_MODULE.time, "monotonic", return_value=1_000_000.0):
                    self.assertEqual(await agent.evict_idle_transports(600), 0)
                self.assertEqual(self.stop_calls, [])
                self.assertEqual(self.invalidated, [])
                self.assertEqual(self.cleared_turns, [])
                self.assertEqual(agent._turn_registry.get_active_turn("session-1"), "turn-1")
                agent.controller.emit_agent_message.assert_not_awaited()

    async def test_hfr_143_observable_session_progress_wins_locked_recheck(self):
        """HFR-143: attributable progress keeps a productive turn alive."""
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")
        self.snapshot_gate = asyncio.Event()
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
        )
        agent._find_request_for_notification = Mock(return_value=request)
        agent._event_handler.handle_notification = AsyncMock()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            # Fresh progress belongs to this exact Session, not merely its cwd.
            with patch.object(_MODULE.time, "monotonic", return_value=1900.0):
                await agent._on_notification(
                    "item/agentMessage/delta",
                    {"threadId": "thread-1", "turnId": "turn-1"},
                    runtime=self.generation.runtime,
                )
            self.snapshot_gate.set()
            evicted = await eviction_task

        self.assertEqual(evicted, 0)
        self.assertEqual(self.stop_calls, [])
        self.assertIn("/tmp/work", codex_transports(agent))
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_evict_idle_transports_reclassifies_when_turn_clears_between_passes(self):
        # Race: pass 1 sees a stuck-active candidate, but the turn completes
        # (active flag clears) before the recheck while activity stays stale.
        # The recheck reclassifies it as a NORMAL idle eviction.
        agent = self._evict_agent(self._stopping_transport(), active_turn="turn-1")
        self.snapshot_gate = asyncio.Event()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            # turn finished between the two passes; activity unchanged (stale)
            agent._turn_registry.get_active_turn = lambda base_session_id: None
            self.snapshot_gate.set()
            evicted = await eviction_task

        self.assertEqual(evicted, 1)
        self.assertEqual(self.stop_calls, ["stop"])
        self.assertEqual(self.invalidated, ["session-1"])
        # reclassified as normal idle: the prior turn already settled itself,
        # so no spurious terminal result or runtime-gate release fires here.
        agent.controller.emit_agent_message.assert_not_awaited()
        self.assertEqual(self.release_calls, [])

    async def test_evict_idle_transports_force_evict_preserves_state_when_stop_fails(self):
        # Stuck-active force-eviction path: if transport.stop() raises, the
        # generation and its Session binding stay for the next sweep.
        transport = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("boom")))
        agent = self._evict_agent(transport, active_turn="turn-1")
        agent._settle_stuck_active_request = AsyncMock()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIn(self.generation, agent._units["/tmp/work"].generations)
        self.assertIs(agent.transport_for_session("session-1"), transport)
        self.assertEqual(self.invalidated, [])
        self.assertEqual(self.cleared_turns, ["session-1"])


class _HandleMessageTurnRegistry:
    def __init__(self, active_turn: str | None):
        self.active_turn = active_turn
        self.remembered_requests = []
        self.cleared_sessions = []
        self.cleared_pending_starts = []

    def remember_request(self, request):
        self.remembered_requests.append(request)

    def get_active_turn(self, base_session_id: str):
        return self.active_turn

    def has_pending_turn_start(self, base_session_id: str):
        return False

    def clear_pending_turn_start(self, base_session_id: str, request=None):
        # Mirrors TurnRegistry.clear_pending_turn_start; the error path calls it.
        self.cleared_pending_starts.append((base_session_id, request))

    def clear_session(self, base_session_id: str):
        self.cleared_sessions.append(base_session_id)


class CodexAgentHandleMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_message_refreshes_cached_thread_instructions_before_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
            context=object(),
            session_key="settings-1",
            ack_message_id=None,
        )
        events = []
        transport = SimpleNamespace()
        agent._session_locks = {}
        agent._turn_registry = _HandleMessageTurnRegistry(active_turn=None)
        agent._event_handler = SimpleNamespace(clear_pending=Mock())
        agent._remove_ack_reaction = AsyncMock()
        agent._delete_ack = AsyncMock()
        agent.controller = SimpleNamespace(
            emit_agent_message=AsyncMock(),
            agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
        )
        agent._acquire_generation = acquire_returning(agent, transport)
        agent.ensure_agent_session_id = Mock(
            side_effect=lambda existing_request: events.append(
                ("ensure", existing_request)
            )
            or "ses-visible"
        )
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable prompt")
        agent._session_mgr = SimpleNamespace(
            set_session_key=Mock(),
            set_cwd=Mock(),
            get_thread_id=Mock(return_value="thread-cached"),
        )

        async def refresh(existing_transport, existing_request, thread_id):
            events.append(("refresh", existing_transport, existing_request, thread_id))

        async def start_turn(existing_transport, existing_request, thread_id, *, developer_instructions=None):
            self.assertEqual(developer_instructions, "stable prompt")
            events.append(("turn", existing_transport, existing_request, thread_id))
            return thread_id

        agent._refresh_thread_developer_instructions_if_needed = refresh
        agent._start_or_resume_thread = AsyncMock()
        agent._start_turn = start_turn

        await agent.handle_message(request)

        self.assertEqual(
            events,
            [
                ("ensure", request),
                ("refresh", transport, request, "thread-cached"),
                ("turn", transport, request, "thread-cached"),
            ],
        )
        agent._start_or_resume_thread.assert_not_awaited()

    async def test_prompt_refresh_failure_display_is_localized(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(language="zh"))

        display = agent._error_display_text(
            CodexPromptRefreshUnavailableError("internal diagnostic")
        )

        self.assertEqual(
            display,
            "❌ Codex 无法确认能否安全刷新此现有会话的 Avibe 指令。请检查或升级 Codex 后重试本回合。",
        )

    async def test_handle_message_localizes_fork_boundary_failure(self):
        messages = {
            "en": (
                "❌ Codex could not determine a safe history boundary for this fork. "
                "Wait for the source turn to finish, then retry. "
                "If the problem persists, check or update Codex."
            ),
            "zh": (
                "❌ Codex 无法确定此次分叉的安全历史边界。"
                "请等待源会话当前回合结束后重试；如果问题持续，请检查或升级 Codex。"
            ),
        }
        for language, expected in messages.items():
            with self.subTest(language=language):
                agent = init_generation_state(object.__new__(CodexAgent))
                context = SimpleNamespace(platform_specific={})
                request = SimpleNamespace(
                    base_session_id="session-1",
                    working_path="/tmp/work",
                    context=context,
                    session_key="settings-1",
                    ack_message_id=None,
                )
                transport = SimpleNamespace(stop=AsyncMock())
                agent.controller = SimpleNamespace(
                    config=SimpleNamespace(language=language),
                    emit_agent_message=AsyncMock(),
                )
                agent.sessions = SimpleNamespace()
                agent._session_locks = {}
                agent._turn_registry = _HandleMessageTurnRegistry(active_turn=None)
                agent._session_mgr = SimpleNamespace(
                    set_session_key=Mock(), set_cwd=Mock(), get_thread_id=Mock(return_value=None),
                )
                agent._acquire_generation = acquire_returning(agent, transport)
                agent._delete_ack = AsyncMock()
                agent._remove_ack_reaction = AsyncMock()
                agent._event_handler = SimpleNamespace(_release_stream_turn=Mock())
                agent._build_thread_developer_instructions = AsyncMock(return_value="prompt")
                diagnostic = "Cannot fork Codex thread while the source turn boundary is unknown"
                agent._start_or_resume_thread = AsyncMock(
                    side_effect=CodexForkBoundaryUnavailableError(diagnostic),
                )
                agent._start_turn = AsyncMock()
                agent._drop_generation_after_failure = AsyncMock()

                await agent.handle_message(request)

                calls = agent.controller.emit_agent_message.await_args_list
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[0].args, (context, "notify", expected))
                self.assertNotIn(diagnostic, calls[0].args[2])
                self.assertIn(diagnostic, calls[1].kwargs["terminal_error"])
                agent._start_or_resume_thread.assert_awaited_once()
                agent._start_turn.assert_not_awaited()
                agent._drop_generation_after_failure.assert_not_awaited()
                transport.stop.assert_not_awaited()
                agent._event_handler._release_stream_turn.assert_called_once_with(context)

    async def test_handle_message_does_not_hide_turn_before_interrupt_succeeds(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp",
            context=object(),
            session_key="settings-1",
            ack_message_id=None,
        )

        transport = SimpleNamespace(
            send_request=AsyncMock(side_effect=RuntimeError("interrupt failed")),
        )
        agent._session_locks = {}
        agent._turn_registry = _HandleMessageTurnRegistry(active_turn="turn-1")
        agent._event_handler = SimpleNamespace(
            clear_pending=Mock(return_value=SimpleNamespace()),
            _release_stream_turn=Mock(),
        )
        agent._remove_ack_reaction = AsyncMock()
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())
        agent._acquire_generation = acquire_returning(agent, transport)
        agent._session_mgr = SimpleNamespace(
            set_session_key=lambda base_session_id, session_key: None,
            set_cwd=lambda base_session_id, cwd: None,
            get_thread_id=lambda base_session_id: "thread-1",
        )

        await agent.handle_message(request)

        agent._event_handler.clear_pending.assert_not_called()
        agent._remove_ack_reaction.assert_awaited_once_with(request)
        self.assertEqual(agent.controller.emit_agent_message.await_count, 2)
        notify_call, terminal_call = agent.controller.emit_agent_message.await_args_list
        self.assertEqual(
            notify_call.args[:3],
            (
                request.context,
                "notify",
                "❌ Failed to interrupt previous Codex turn: interrupt failed",
            ),
        )
        self.assertEqual(terminal_call.args[:3], (request.context, "result", ""))
        self.assertTrue(terminal_call.kwargs["is_error"])
        self.assertEqual(terminal_call.kwargs["level"], "silent")
        self.assertEqual(terminal_call.kwargs["terminal_error"], "interrupt failed")

    async def test_handle_message_recovers_from_broken_transport_once(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
            context=SimpleNamespace(platform_specific={}),
            session_key="settings-1",
            ack_message_id=None,
        )
        set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)

        bad_transport = SimpleNamespace(
            stop=AsyncMock(),
            is_alive=False,
            _process=SimpleNamespace(returncode=137),
        )
        fresh_transport = SimpleNamespace()
        invalidated = []
        session_mgr = SimpleNamespace(
            set_session_key=Mock(),
            set_cwd=Mock(),
            get_thread_id=Mock(return_value=None),
            sessions_for_cwd=Mock(return_value=["session-1"]),
            invalidate_thread=Mock(side_effect=lambda base_session_id: invalidated.append(base_session_id)),
        )
        sessions = SimpleNamespace(clear_agent_session_mapping=Mock())

        agent._session_locks = {}
        agent._turn_registry = _HandleMessageTurnRegistry(active_turn=None)
        agent._event_handler = SimpleNamespace(clear_pending=Mock())
        agent._remove_ack_reaction = AsyncMock()
        agent._delete_ack = AsyncMock()
        agent.controller = SimpleNamespace(
            emit_agent_message=AsyncMock(),
            agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
        )
        agent._ownership_snapshots = AsyncMock(
            return_value=(SimpleNamespace(blocks_dead_transport_replacement=False),)
        )
        agent._session_mgr = session_mgr
        agent.sessions = sessions
        agent._acquire_generation = acquire_returning(agent, bad_transport, fresh_transport)
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable prompt")
        agent._start_or_resume_thread = AsyncMock(
            side_effect=[
                ConnectionError("Codex app-server stdout closed"),
                "thread-new",
            ]
        )
        agent._start_thread = AsyncMock(return_value="thread-new")
        agent._start_turn = AsyncMock(return_value="thread-new")

        with patch.object(_MODULE, "observe_agent_resource_pressure") as observe_pressure:
            await agent.handle_message(request)
        observe_pressure.assert_not_called()

        bad_transport.stop.assert_awaited_once()
        self.assertEqual(codex_transports(agent), {"/tmp/work": fresh_transport})
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(agent._turn_registry.cleared_sessions, ["session-1"])
        sessions.clear_agent_session_mapping.assert_not_called()
        self.assertEqual(agent._acquire_generation.await_count, 2)
        self.assertEqual(agent._start_or_resume_thread.await_args_list[-1].args, (fresh_transport, request))
        agent._start_thread.assert_not_awaited()
        agent._start_turn.assert_awaited_once_with(
            fresh_transport,
            request,
            "thread-new",
            developer_instructions="stable prompt",
        )
        agent.controller.emit_agent_message.assert_not_awaited()
        agent._remove_ack_reaction.assert_not_awaited()

    async def test_handle_message_reraises_recoverable_interrupt_error_for_retry(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
            context=SimpleNamespace(platform_specific={}),
            session_key="settings-1",
            ack_message_id=None,
        )
        set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)

        bad_transport = SimpleNamespace(
            send_request=AsyncMock(side_effect=ConnectionError("Codex app-server transport is not available")),
            stop=AsyncMock(),
            is_alive=False,
        )
        fresh_transport = SimpleNamespace()
        agent._session_locks = {}
        agent._turn_registry = _HandleMessageTurnRegistry(active_turn="turn-1")
        agent._event_handler = SimpleNamespace(clear_pending=Mock())
        agent._remove_ack_reaction = AsyncMock()
        agent._delete_ack = AsyncMock()
        agent.controller = SimpleNamespace(
            emit_agent_message=AsyncMock(),
            agent_auth_service=SimpleNamespace(maybe_emit_auth_recovery_message=AsyncMock(return_value=False)),
        )
        agent._ownership_snapshots = AsyncMock(
            return_value=(SimpleNamespace(blocks_dead_transport_replacement=False),)
        )
        agent._session_mgr = SimpleNamespace(
            set_session_key=Mock(),
            set_cwd=Mock(),
            get_thread_id=Mock(return_value="thread-old"),
            sessions_for_cwd=Mock(return_value=["session-1"]),
            invalidate_thread=Mock(),
        )
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        agent._acquire_generation = acquire_returning(agent, bad_transport, fresh_transport)
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable prompt")
        agent._start_or_resume_thread = AsyncMock(return_value="thread-new")
        agent._start_thread = AsyncMock(return_value="thread-new")
        agent._start_turn = AsyncMock(return_value="thread-new")

        await agent.handle_message(request)

        bad_transport.send_request.assert_awaited_once_with(
            "turn/interrupt",
            {"threadId": "thread-old", "turnId": "turn-1"},
        )
        agent._event_handler.clear_pending.assert_not_called()
        agent._start_or_resume_thread.assert_awaited_once_with(
            fresh_transport, request, developer_instructions="stable prompt"
        )
        agent._start_thread.assert_not_awaited()
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_cold_resume_failure_binds_session_before_ownership_checks(self):
        for incomplete_prior_binding in (False, True):
            with self.subTest(incomplete_prior_binding=incomplete_prior_binding):
                agent = init_generation_state(object.__new__(CodexAgent))
                request = SimpleNamespace(
                    base_session_id="session-1", working_path="/tmp/work",
                    context=SimpleNamespace(platform_specific={}),
                    session_key="settings-1", ack_message_id=None,
                )
                set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)
                bad = SimpleNamespace(stop=AsyncMock(), is_alive=False)
                fresh = SimpleNamespace(is_alive=True)
                agent._session_locks = {}
                agent._session_mgr = RealCodexSessionManager()
                if incomplete_prior_binding:
                    agent._session_mgr.set_session_key("session-1", "settings-1")
                    agent._session_mgr.set_cwd("session-1", request.working_path)
                agent.sessions = SimpleNamespace(
                    ensure_agent_session_id=Mock(return_value="ses-durable"),
                )
                agent._turn_registry = _HandleMessageTurnRegistry(active_turn=None)
                agent._event_handler = SimpleNamespace(_release_stream_turn=Mock())
                agent._delete_ack = AsyncMock()
                agent._remove_ack_reaction = AsyncMock()
                agent._build_thread_developer_instructions = AsyncMock(return_value="prompt")
                agent._start_or_resume_thread = AsyncMock(
                    side_effect=[ConnectionError("stdout closed"), "thread-restored"],
                )
                agent._start_turn = AsyncMock()

                def snapshot(target):
                    # The retired broken generation answers only for Sessions
                    # holding a thread there; the resume never loaded one.
                    self.assertEqual(target.bindings, ())
                    return SimpleNamespace(blocks_dead_transport_replacement=False)

                agent.controller = SimpleNamespace(
                    emit_agent_message=AsyncMock(),
                    runtime_ownership=SimpleNamespace(snapshot=Mock(side_effect=snapshot)),
                )

                served = iter([bad, fresh])

                async def acquire(cwd, launch=None, *, inputs=None):
                    generation = install_codex_transport(
                        agent, cwd, next(served), digest=f"spec-{agent._acquire_generation.await_count}"
                    )
                    # Both initial acquisition and failure recovery must see a
                    # complete producer-side binding, not a mocked predicate.
                    target = agent._runtime_ownership_target_for_generation(generation)
                    self.assertIsNotNone(target)
                    self.assertEqual([binding.session_id for binding in target.bindings], ["ses-durable"])
                    return bind_installed(agent, generation)

                # Acquisition is stubbed, so the launch load it alone consumes is too.
                agent._launch_inputs = lambda cwd, *, hub_config=None: SimpleNamespace(hub_config=hub_config)
                agent._acquire_generation = AsyncMock(side_effect=acquire)
                await agent.handle_message(request)
                bad.stop.assert_awaited_once()
                # Checked before the fence and again inside it.
                self.assertEqual(agent.controller.runtime_ownership.snapshot.call_count, 2)
                self.assertEqual(agent._acquire_generation.await_count, 2)
                agent._start_turn.assert_awaited_once()
                agent.controller.emit_agent_message.assert_not_awaited()

    async def test_drop_generation_after_failure_keeps_a_newer_generation_and_its_sessions(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(base_session_id="session-1")
        activation = RuntimeActivationRegistry()
        old_identity = activation.attach("codex", "/tmp/work#1")
        old_transport = SimpleNamespace(stop=AsyncMock(), is_alive=False)
        fresh_identity = activation.attach("codex", "/tmp/work#2")
        fresh_transport = SimpleNamespace(is_alive=True)
        invalidated = []
        cleared = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value=["session-1", "session-2"]),
            get_cwd=Mock(return_value="/tmp/work"),
            invalidate_thread=Mock(side_effect=lambda base_session_id: invalidated.append(base_session_id)),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value=None),
            clear_session=Mock(side_effect=lambda base_session_id: cleared.append(base_session_id)),
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)
        agent._ownership_snapshots = AsyncMock(
            return_value=(SimpleNamespace(blocks_dead_transport_replacement=False),)
        )
        old = install_codex_transport(
            agent, "/tmp/work", old_transport, activation=old_identity, sessions={"session-1": "thread-1"}
        )
        install_codex_transport(
            agent, "/tmp/work", fresh_transport, activation=fresh_identity, digest="fresh",
            sessions={"session-2": "thread-2"},
        )

        self.assertTrue(await agent._drop_generation_after_failure(old, request, bind_installed(agent, old)))

        old_transport.stop.assert_awaited_once()
        self.assertIs(codex_transports(agent)["/tmp/work"], fresh_transport)
        self.assertIs(agent.transport_for_session("session-2"), fresh_transport)
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared, ["session-1"])
        self.assertIsNone(activation.current("codex", "/tmp/work#1"))
        self.assertEqual(activation.current("codex", "/tmp/work#2"), fresh_identity)

    async def test_drop_generation_after_failure_retires_its_identity_before_stop(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(base_session_id="session-1")
        activation = RuntimeActivationRegistry()
        observed_current = []
        transport = SimpleNamespace(is_alive=False)
        identity = activation.attach("codex", "/tmp/work#1")

        async def stop_transport():
            observed_current.append(activation.is_current(identity))

        transport.stop = stop_transport
        agent._ownership_snapshots = AsyncMock(
            return_value=(SimpleNamespace(blocks_dead_transport_replacement=False),)
        )
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value=["session-1"]),
            invalidate_thread=Mock(),
        )
        agent._turn_registry = SimpleNamespace(clear_session=Mock())
        agent.controller = SimpleNamespace(runtime_activation=activation)
        generation = install_codex_transport(agent, "/tmp/work", transport, activation=identity)

        await agent._drop_generation_after_failure(generation, request, bind_installed(agent, generation))

        self.assertEqual(observed_current, [False])
        self.assertEqual(codex_transports(agent), {})
        self.assertIsNone(activation.current("codex", "/tmp/work#1"))

    async def test_start_or_resume_thread_reraises_recoverable_transport_error(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-old"))
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._start_thread = AsyncMock()
        request = SimpleNamespace(session_key="settings-1", base_session_id="session-1")
        transport = SimpleNamespace(send_request=AsyncMock(side_effect=ConnectionError("Codex app-server stdout closed")))

        with self.assertRaises(ConnectionError):
            await agent._start_or_resume_thread(transport, request)

        agent._start_thread.assert_not_awaited()

    async def test_unreleased_thread_holds_unwritten_input_without_hub_cooldown(self):
        """A thread its old generation cannot release settles as a visible, retryable prewrite failure."""
        agent = init_generation_state(object.__new__(CodexAgent))
        context = SimpleNamespace(platform_specific={})
        set_dispatch_phase(context, DISPATCH_PHASE_PREWRITE)
        request = SimpleNamespace(
            base_session_id="session-1", working_path="/tmp/work",
            context=context, session_key="settings-1", ack_message_id=None,
        )
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(language="en"), emit_agent_message=AsyncMock(),
        )
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(set_session_key=Mock(), set_cwd=Mock())
        agent.ensure_agent_session_id = Mock()
        agent._bind_runtime_agent_session_id = Mock()
        agent._acquire_generation = acquire_returning(agent, SimpleNamespace(), session=None)
        agent._move_session_to = AsyncMock(
            side_effect=_MODULE.CodexThreadReleaseUnavailableError("still loaded")
        )
        agent._record_model_hub_native_failure = AsyncMock()
        agent._remove_ack_reaction = AsyncMock()
        agent._event_handler = SimpleNamespace(_release_stream_turn=Mock())

        await agent.handle_message(request)

        self.assertEqual(
            prewrite_failure_evidence(context),
            {"reason": "codex_thread_release_unavailable", "requires_explicit_retry": True},
        )
        agent._record_model_hub_native_failure.assert_not_awaited()
        notify = agent.controller.emit_agent_message.await_args_list[0]
        self.assertEqual(notify.args[1], "notify")
        self.assertIn("could not move this conversation", notify.args[2])
        agent._event_handler._release_stream_turn.assert_called_once_with(context)
        # The admission binding is released, so the generation can retire.
        self.assertEqual(agent._units["/tmp/work"].current.bindings, 0)

    async def test_permanent_resume_failure_holds_only_proven_unwritten_input(self):
        for error in (
            CodexResponseTooLargeError(), _MODULE.CodexResumeUnavailableError("thread-old"),
            TimeoutError("no start acknowledgement"),
        ):
            for phase in (DISPATCH_PHASE_PREWRITE, DISPATCH_PHASE_ATTEMPTING, None):
                with self.subTest(error=type(error).__name__, phase=phase):
                    agent = init_generation_state(object.__new__(CodexAgent))
                    context = SimpleNamespace(platform_specific={})
                    if phase:
                        set_dispatch_phase(context, phase)
                    request = SimpleNamespace(
                        base_session_id="session-1", working_path="/tmp/work",
                        context=context, session_key="settings-1", ack_message_id=None,
                    )
                    transport = SimpleNamespace(stop=AsyncMock())
                    agent.controller = SimpleNamespace(
                        config=SimpleNamespace(language="en"), emit_agent_message=AsyncMock(),
                    )
                    agent.sessions = SimpleNamespace()
                    agent._session_locks = {}
                    agent._turn_registry = _HandleMessageTurnRegistry(active_turn=None)
                    agent._session_mgr = SimpleNamespace(
                        set_session_key=Mock(), set_cwd=Mock(), get_thread_id=Mock(return_value=None),
                    )
                    agent._acquire_generation = acquire_returning(agent, transport)
                    agent._delete_ack = AsyncMock()
                    agent._remove_ack_reaction = AsyncMock()
                    agent._event_handler = SimpleNamespace(_release_stream_turn=Mock())
                    agent._build_thread_developer_instructions = AsyncMock(return_value="prompt")
                    agent._start_or_resume_thread = AsyncMock(side_effect=error)
                    agent._start_turn = AsyncMock()
                    agent._drop_generation_after_failure = AsyncMock(return_value=False)

                    await agent.handle_message(request)

                    agent._acquire_generation.assert_awaited_once()
                    if isinstance(error, TimeoutError) and phase == DISPATCH_PHASE_PREWRITE:
                        agent._drop_generation_after_failure.assert_awaited_once()
                    else:
                        agent._drop_generation_after_failure.assert_not_awaited()
                    agent._start_turn.assert_not_awaited()
                    transport.stop.assert_not_awaited()
                    self.assertEqual(
                        prewrite_failure_evidence(context),
                        {"reason": "codex_resume_unavailable", "requires_explicit_retry": True}
                        if phase == DISPATCH_PHASE_PREWRITE and not isinstance(error, TimeoutError) else {},
                    )
                    self.assertTrue(agent.controller.emit_agent_message.await_args.kwargs["is_error"])

    async def test_failure_replacement_respects_live_durable_and_unknown_ownership(self):
        """MESSAGE-DELIVERY-033: one failed resume cannot kill a live neighbour."""
        for live, blocked, active, allowed in (
            (True, False, True, False),   # In-memory active/pending neighbour.
            (True, True, False, False),   # Durable owner without local registry.
            (True, None, False, False),   # Unreadable ownership fails closed.
            (False, True, False, False),  # Dead process still has protected work.
            (False, False, True, True),   # Dead process can be reclaimed.
            (True, False, False, True),   # Idle process can be reclaimed.
        ):
            with self.subTest(live=live, blocked=blocked, active=active):
                agent = init_generation_state(object.__new__(CodexAgent))
                activation = RuntimeActivationRegistry()
                transport = SimpleNamespace(stop=AsyncMock(), is_alive=live)
                agent.controller = SimpleNamespace(runtime_activation=activation)
                agent._session_mgr = SimpleNamespace(
                    sessions_for_cwd=Mock(return_value=["failed", "neighbour"]),
                    invalidate_thread=Mock(),
                )
                agent._turn_registry = SimpleNamespace(
                    clear_session=Mock(),
                    get_active_turn=lambda base_session_id, active=active: (
                        "turn-neighbour" if active and base_session_id == "neighbour" else None
                    ),
                )
                agent._ownership_snapshots = AsyncMock(
                    return_value=None if blocked is None else (SimpleNamespace(
                        blocks_transport_replacement=blocked,
                        blocks_dead_transport_replacement=blocked,
                    ),)
                )
                identity = activation.attach("codex", "/tmp/work#1")
                generation = install_codex_transport(
                    agent, "/tmp/work", transport, activation=identity,
                    sessions={"failed": "thread-failed", "neighbour": "thread-neighbour"},
                )
                result = await agent._drop_generation_after_failure(
                    generation,
                    SimpleNamespace(base_session_id="failed"),
                    bind_installed(agent, generation),
                )
                self.assertIs(result, allowed)
                if result:
                    transport.stop.assert_awaited_once()
                    self.assertEqual(agent._units["/tmp/work"].generations, ())
                else:
                    transport.stop.assert_not_awaited()
                    agent._session_mgr.invalidate_thread.assert_not_called()
                    agent._turn_registry.clear_session.assert_not_called()
                    # Retired, so no new turn binds to it, but the neighbour's
                    # process keeps running until its work drains.
                    self.assertIn(generation, agent._units["/tmp/work"].generations)
                    self.assertTrue(generation.closed)
                    self.assertFalse(generation.runtime.ended)
                    self.assertTrue(activation.is_current(identity))

    async def test_start_or_resume_preserves_oversized_response_identity(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-old"))
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._start_thread = AsyncMock()
        error = CodexResponseTooLargeError()
        transport = SimpleNamespace(send_request=AsyncMock(side_effect=error))
        request = SimpleNamespace(session_key="settings-1", base_session_id="session-1")
        with self.assertRaises(CodexResponseTooLargeError) as caught:
            await agent._start_or_resume_thread(transport, request)
        self.assertIs(caught.exception, error)
        self.assertFalse(agent._is_recoverable_transport_error(error))
        agent._start_thread.assert_not_awaited()

    def test_find_request_does_not_bootstrap_turn_completed_for_pending_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request
        agent._turn_registry._pending_requests["session-1"] = request

        resolved = agent._find_request_for_notification(
            "turn/completed", {"threadId": "thread-1", "turn": {"id": "turn-1"}}
        )

        self.assertIsNone(resolved)


class CodexAgentPayloadTests(unittest.IsolatedAsyncioTestCase):
    def test_inject_caller_env_config_preserves_private_codex_helpers_with_vendored_git(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.codex_config = SimpleNamespace(binary="/private/codex")
        params = {}
        request = SimpleNamespace(
            working_path="/tmp/workspace",
            context=SimpleNamespace(platform_specific={}),
        )
        managed_env = {"PATH": "/private/codex-path:/usr/bin"}

        def inject_git(env, *, base_env, working_dir):
            self.assertEqual(base_env, managed_env)
            self.assertEqual(working_dir, "/tmp/workspace")
            env["PATH"] = f"/managed/git/bin:{base_env['PATH']}"
            return True

        with patch("core.git_runtime.prepend_vendored_git_to_path", side_effect=inject_git):
            agent._inject_caller_env_config(params, request, managed_env)

        self.assertEqual(
            params["config"]["shell_environment_policy"]["set"]["PATH"],
            "/managed/git/bin:/private/codex-path:/usr/bin",
        )

    def test_inject_caller_env_config_adds_vendored_git_for_gitless_session(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        params = {"config": {"shell_environment_policy": {"set": {"PATH": ""}}}}
        request = SimpleNamespace(
            working_path="/tmp/workspace",
            context=SimpleNamespace(platform_specific={}),
        )

        def inject_git(env, *, base_env, working_dir):
            self.assertEqual(env["PATH"], "")
            self.assertIs(base_env, os.environ)
            self.assertEqual(working_dir, "/tmp/workspace")
            env["PATH"] = "/managed/git/bin"
            return True

        with patch("core.git_runtime.prepend_vendored_git_to_path", side_effect=inject_git):
            agent._inject_caller_env_config(params, request, os.environ)

        set_env = params["config"]["shell_environment_policy"]["set"]
        self.assertEqual(set_env["PATH"], "/managed/git/bin")
        self.assertEqual(set_env["AVIBE_SKILL_WORKING_DIR"], str(Path("/tmp/workspace").resolve()))
        self.assertTrue(set_env["BASH_ENV"].endswith("/codex-caller-env/session.sh"))
        self.assertFalse(params["config"]["skills.include_instructions"])

    def test_inject_caller_env_config_merges_shell_environment_policy(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        params = {"config": {"shell_environment_policy": {"set": {"KEEP": "1"}}}}
        request = SimpleNamespace(
            context=SimpleNamespace(
                platform_specific={
                    "task_execution_id": "run-parent",
                    "task_trigger_kind": "agent_run",
                    "agent_session_target": {
                        "id": "ses-parent",
                        "agent_backend": "codex",
                        "native_session_id": "thread-parent",
                    },
                }
            )
        )

        with patch("core.git_runtime.prepend_vendored_git_to_path", return_value=False):
            agent._inject_caller_env_config(params, request, os.environ)

        set_env = params["config"]["shell_environment_policy"]["set"]
        self.assertEqual(set_env["KEEP"], "1")
        self.assertEqual(set_env["AVIBE_SESSION_ID"], "ses-parent")
        self.assertEqual(set_env["AVIBE_RUN_ID"], "run-parent")
        self.assertEqual(set_env["AVIBE_CALLER_SOURCE"], "agent_run")
        self.assertEqual(set_env["AVIBE_CALLER_BACKEND"], "codex")
        self.assertEqual(set_env["AVIBE_NATIVE_SESSION_ID"], "thread-parent")
        self.assertTrue(set_env["BASH_ENV"].endswith("/codex-caller-env/ses-parent.sh"))
        self.assertNotIn("PATH", set_env)
        self.assertFalse(params["config"]["skills.include_instructions"])

    def test_write_caller_env_script_refreshes_reused_thread_run_id(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        request = SimpleNamespace(
            base_session_id="session-1",
            context=SimpleNamespace(
                platform_specific={
                    "task_execution_id": "run-one",
                    "task_trigger_kind": "agent_run",
                    "agent_session_target": {
                        "id": "ses-parent",
                        "agent_backend": "codex",
                        "native_session_id": "thread-parent",
                    },
                },
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.paths.get_runtime_dir", return_value=Path(tmpdir)):
                script_path = agent._write_caller_env_script(request)
                self.assertIsNotNone(script_path)
                first = script_path.read_text()
                request.context.platform_specific["task_execution_id"] = "run-two"
                second_path = agent._write_caller_env_script(request)
                second = second_path.read_text()

        self.assertIn("export AVIBE_RUN_ID=run-one", first)
        self.assertIn("export AVIBE_SESSION_ID=ses-parent", first)
        self.assertIn("export AVIBE_RUN_ID=run-two", second)
        self.assertNotIn("export AVIBE_RUN_ID=run-one", second)

    async def test_start_thread_requests_danger_full_access(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
            bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"agent_session_id": "sesk8m4q2p7x"},
                user_id="U1",
                channel_id="C1",
                thread_id=None,
            ),
            base_session_id="session-1",
            session_key="channel-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )

        transport = SimpleNamespace(
            send_request=AsyncMock(
                return_value={
                    "thread": {"id": "thread-1"},
                    "model": "gpt-5.4",
                    "reasoningEffort": "high",
                }
            )
        )

        thread_id = await agent._start_thread(transport, request)

        self.assertEqual(thread_id, "thread-1")
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["cwd"], "/tmp/work")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertNotIn("developerInstructions", params)
        self.assertEqual(
            agent._thread_model_settings["session-1"],
            ("thread-1", "gpt-5.4", "high"),
        )
        agent.sessions.ensure_agent_session_id.assert_called_once_with("channel-1", "codex", "session-1")
        agent.sessions.bind_agent_session.assert_called_once_with("channel-1", "codex", "session-1", "thread-1")

    async def test_start_thread_includes_codex_agent_developer_instructions(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
            bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"agent_session_id": "sesk8m4q2p7x"},
                user_id="U1",
                channel_id="C1",
                thread_id=None,
            ),
            base_session_id="session-1",
            session_key="channel-1",
            subagent_name="reviewer",
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-1"}}))

        with patch.object(
            _MODULE,
            "load_codex_subagent",
            return_value=SimpleNamespace(
                developer_instructions="Focus on regressions.",
                model="gpt-5.4-mini",
                reasoning_effort="high",
            ),
        ) as load_subagent:
            developer_instructions = await agent._build_thread_developer_instructions(request)

        load_subagent.assert_called_once_with("reviewer", project_root=Path("/tmp/work"))
        transport.send_request.assert_not_awaited()
        self.assertIn("Focus on regressions.", developer_instructions)
        self.assertTrue(developer_instructions.endswith("\n\nFocus on regressions."))
        self.assertEqual(developer_instructions.count("Focus on regressions."), 1)
        self.assertIn("# Avibe", developer_instructions)
        self.assertIn("Current session id: `sesk8m4q2p7x`", developer_instructions)
        self.assertNotIn("## Quick-reply buttons", developer_instructions)

    async def test_start_thread_adds_codex_generated_image_prompt_to_thread_instructions(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
            bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"agent_session_id": "sesk8m4q2p7x"},
                user_id="U1",
                channel_id="C1",
                thread_id=None,
            ),
            base_session_id="session-1",
            session_key="channel-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-1"}}))

        with patch.dict(os.environ, {"CODEX_HOME": "/Users/test/.codex"}):
            developer_instructions = await agent._build_thread_developer_instructions(request)

        transport.send_request.assert_not_awaited()
        self.assertIn("## Send files", developer_instructions)
        self.assertIn("## Codex-generated images", developer_instructions)
        self.assertIn("If you generate an image with Codex", developer_instructions)
        self.assertIn("Current session id: `sesk8m4q2p7x`", developer_instructions)
        self.assertIn(
            "file:///Users/test/.codex/generated_images/thread-id/image-file.png",
            developer_instructions,
        )

    async def test_start_thread_includes_show_pages_despite_legacy_opt_out(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(platform="slack", reply_enhancements=True, show_pages_prompt=False)
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
            bind_agent_session=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"agent_session_id": "sesk8m4q2p7x"},
                user_id="U1",
                channel_id="C1",
                thread_id=None,
            ),
            base_session_id="session-1",
            session_key="channel-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-1"}}))

        developer_instructions = await agent._build_thread_developer_instructions(request)

        transport.send_request.assert_not_awaited()
        self.assertIn("# Avibe", developer_instructions)
        self.assertIn("## Quick-reply buttons", developer_instructions)
        self.assertIn("Current session id: `sesk8m4q2p7x`", developer_instructions)
        self.assertIn("## Show Pages", developer_instructions)
        self.assertIn("load the `use-show-pages` Skill", developer_instructions)
        self.assertNotIn("vibe show path", developer_instructions)

    async def test_resume_thread_refreshes_developer_instructions_without_appending(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            get_agent_session_row_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name="reviewer",
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {"model_provider": "openai"}},
                    {"thread": {"id": "thread-existing", "modelProvider": "openai"}},
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        with patch.object(
            _MODULE,
            "load_codex_subagent",
            return_value=SimpleNamespace(
                developer_instructions="Focus on regressions.",
                model=None,
                reasoning_effort=None,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        self.assertEqual(transport.send_request.await_count, 3)
        method, params = transport.send_request.await_args_list[2].args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "thread-existing")
        self.assertIs(params["excludeTurns"], True)
        self.assertNotIn("developerInstructions", params)

    async def test_resume_thread_rebinds_managed_provider_when_thread_id_matches_config(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(
            default_model=None,
            auth_mode="api_key",
            base_url="https://relay.example/v1",
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {"model_provider": "openai-managed"}},
                    {"thread": {"id": "thread-existing", "modelProvider": "openai-managed"}},
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        self.assertEqual(transport.send_request.await_args_list[0].args[0], "config/read")
        self.assertEqual(transport.send_request.await_args_list[0].args[1]["cwd"], "/tmp/work")
        self.assertEqual(transport.send_request.await_args_list[1].args[0], "thread/read")
        method, params = transport.send_request.await_args_list[2].args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["modelProvider"], "openai-managed")

    async def test_resume_thread_overrides_stale_session_model_provider(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {"model_provider": "openai-managed"}},
                    {"thread": {"id": "thread-existing", "modelProvider": "openai"}},
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        self.assertEqual(transport.send_request.await_args_list[0].args[0], "config/read")
        self.assertEqual(transport.send_request.await_args_list[0].args[1]["cwd"], "/tmp/work")
        self.assertEqual(transport.send_request.await_args_list[1].args[0], "thread/read")
        method, params = transport.send_request.await_args_list[2].args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["modelProvider"], "openai-managed")

    async def test_resume_thread_prefers_reserved_native_for_main_turn(self):
        # avibe main turn: resume the native bound to the reserved row (by PK),
        # NOT the (session_key, anchor) projection — the restart-resume fix.
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-projection"))
        agent.bind_agent_session_id = Mock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-1",
                        "native_session_id": "native-reserved",
                        "session_anchor": "ses-1",
                    }
                },
            ),
            base_session_id="ses-1",
            session_key="avibe::ses-1",
            subagent_name=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"id": "native-reserved"}))

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "native-reserved")
        method, params = transport.send_request.await_args_list[0].args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "native-reserved")

    async def test_start_or_resume_thread_forks_pending_native_source(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model="gpt-5.2",
            vibe_agent_reasoning_effort="high",
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        method, params = transport.send_request.await_args_list[0].args
        self.assertEqual(method, "thread/fork")
        self.assertEqual(params["threadId"], "thread-source")
        self.assertTrue(params["excludeTurns"])
        self.assertEqual(params["cwd"], "/tmp/work")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertEqual(params["model"], "gpt-5.2")
        self.assertNotIn("effort", params)
        self.assertNotIn("developerInstructions", params)
        inject_method, inject_params = transport.send_request.await_args_list[1].args
        self.assertEqual(inject_method, "thread/inject_items")
        self.assertEqual(inject_params["threadId"], "thread-fork")
        self.assertEqual(inject_params["items"][0]["type"], "message")
        self.assertEqual(inject_params["items"][0]["role"], "developer")
        correction_text = inject_params["items"][0]["content"][0]["text"]
        self.assertIn("This Agent Session was forked from `ses-source`.", correction_text)
        self.assertIn(
            "The authoritative Avibe session id for this fork is `ses-target`.",
            correction_text,
        )
        agent.sessions.bind_agent_session.assert_called_once_with(
            "avibe::project::proj_1",
            "codex",
            "ses-target",
            "thread-fork",
        )

    async def test_fork_carries_persisted_fallback_prompt_strategy(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(platform="avibe", reply_enhancements=False)
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker_getter = Mock(
            return_value={
                "thread_id": "thread-source",
                "strategy": "fallback",
                "sha256": "a" * 64,
            }
        )
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
            get_agent_session_runtime_marker=marker_getter,
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model="gpt-5.2",
            vibe_agent_reasoning_effort="high",
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}})
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertNotIn(
            "ses-target",
            getattr(agent, "_thread_prompt_strategies", {}),
        )
        agent.sessions.set_agent_session_runtime_marker.assert_called_once_with(
            "ses-target",
            backend="codex",
            native_session_id="thread-fork",
            key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
            value={
                "thread_id": "thread-fork",
                "strategy": "fallback",
                "sha256": "a" * 64,
            },
        )
        marker_getter.assert_called_once_with(
            "ses-source",
            backend="codex",
            native_session_id="thread-source",
            key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
        )

    async def test_fork_finalizes_a_source_prompt_with_pending_marker_persistence(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.ensure_agent_session_id = Mock(return_value="ses-target")
        agent._fork_source_prompt_state = Mock(
            return_value=("injected_pending_persist", None, None)
        )
        agent._thread_unpersisted_prompts = {
            "ses-source": ("thread-source", "stable prompt", "fallback")
        }
        agent._resolve_codex_agent_settings = Mock(
            return_value=(None, "gpt-5.4", "high", None)
        )
        agent._inject_caller_env_config = Mock(return_value=("path-state", True))
        agent._mark_fork_correction_pending = Mock()
        agent._clear_fork_correction_pending = Mock()
        agent._should_trim_forked_running_turn = AsyncMock(return_value=False)
        agent._inject_forked_session_correction = AsyncMock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.bind_agent_session_id = Mock(return_value="ses-target")
        agent._persist_prompt_strategy = Mock(return_value=True)
        agent._remember_thread_model_settings_from_response = Mock()
        agent._remember_thread_caller_env_config = Mock()
        agent._remember_thread_git_path_config = Mock()
        agent._caller_env_for_request = Mock(return_value={})
        request = SimpleNamespace(
            working_path="/tmp/work",
            base_session_id="ses-target",
        )
        fork = {
            "source_session_id": "ses-source",
            "source_native_session_id": "thread-source",
        }
        transport = SimpleNamespace(
            send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}})
        )

        thread_id = await agent._fork_thread(transport, request, fork)

        self.assertEqual(thread_id, "thread-fork")
        agent._persist_prompt_strategy.assert_called_once_with(
            request,
            "thread-fork",
            "stable prompt",
            strategy="fallback",
            agent_session_id="ses-target",
        )
        self.assertEqual(
            agent._thread_developer_instructions["ses-target"],
            ("thread-fork", "stable prompt"),
        )
        self.assertEqual(
            agent._thread_prompt_strategies["ses-target"],
            ("thread-fork", "fallback"),
        )

    async def test_fork_persists_carried_collaboration_strategy_for_target(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.ensure_agent_session_id = Mock(return_value="ses-target")
        agent._fork_source_prompt_state = Mock(
            return_value=("collaboration", None, None)
        )
        agent._resolve_codex_agent_settings = Mock(
            return_value=(None, "gpt-5.4", "high", None)
        )
        agent._inject_caller_env_config = Mock(return_value=("path-state", True))
        agent._mark_fork_correction_pending = Mock()
        agent._clear_fork_correction_pending = Mock()
        agent._should_trim_forked_running_turn = AsyncMock(return_value=False)
        agent._inject_forked_session_correction = AsyncMock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.bind_agent_session_id = Mock(return_value="ses-target")
        agent._persist_prompt_strategy = Mock(return_value=True)
        agent._remember_thread_model_settings_from_response = Mock()
        agent._remember_thread_caller_env_config = Mock()
        agent._remember_thread_git_path_config = Mock()
        agent._caller_env_for_request = Mock(return_value={})
        request = SimpleNamespace(
            working_path="/tmp/work",
            base_session_id="ses-target",
        )
        fork = {"source_native_session_id": "thread-source"}
        transport = SimpleNamespace(
            send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}})
        )

        thread_id = await agent._fork_thread(transport, request, fork)

        self.assertEqual(thread_id, "thread-fork")
        agent._persist_prompt_strategy.assert_called_once_with(
            request,
            "thread-fork",
            None,
            strategy="collaboration",
            agent_session_id="ses-target",
        )
        self.assertEqual(
            agent._thread_prompt_strategies["ses-target"],
            ("thread-fork", "collaboration"),
        )

    async def test_fork_does_not_cache_unpersisted_collaboration_strategy(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.ensure_agent_session_id = Mock(return_value="ses-target")
        agent._fork_source_prompt_state = Mock(
            return_value=("collaboration", None, None)
        )
        agent._resolve_codex_agent_settings = Mock(
            return_value=(None, "gpt-5.4", "high", None)
        )
        agent._inject_caller_env_config = Mock(return_value=("path-state", True))
        agent._mark_fork_correction_pending = Mock()
        agent._clear_fork_correction_pending = Mock()
        agent._should_trim_forked_running_turn = AsyncMock(return_value=False)
        agent._inject_forked_session_correction = AsyncMock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.bind_agent_session_id = Mock(return_value="ses-target")
        agent._persist_prompt_strategy = Mock(return_value=False)
        request = SimpleNamespace(
            working_path="/tmp/work",
            base_session_id="ses-target",
        )
        fork = {"source_native_session_id": "thread-source"}
        transport = SimpleNamespace(
            send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}})
        )

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "Could not persist the forked Codex prompt strategy",
        ):
            await agent._fork_thread(transport, request, fork)

        self.assertNotIn(
            "ses-target",
            getattr(agent, "_thread_prompt_strategies", {}),
        )

    async def test_start_or_resume_thread_does_not_bind_failed_fork_correction(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"thread": {"id": "thread-fork"}},
                    RuntimeError("inject failed"),
                ]
            )
        )

        with self.assertRaisesRegex(RuntimeError, "inject failed"):
            await agent._start_or_resume_thread(transport, request)

        self.assertEqual(transport.send_request.await_count, 2)
        agent._session_mgr.set_thread_id.assert_not_called()
        agent.sessions.bind_agent_session.assert_not_called()
        self.assertFalse(agent.is_fork_correction_pending("ses-target"))

    async def test_fork_rejects_trim_without_native_turn_boundary(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(platform="avibe", reply_enhancements=False),
            session_turns=SimpleNamespace(
                native_turn_id_for_initial_message=Mock(return_value=None),
            ),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value=None),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        agent._fork_source_prompt_state = Mock(return_value=(None, None, None))
        agent._resolve_codex_agent_settings = Mock(
            return_value=(None, None, None, None),
        )
        agent._inject_caller_env_config = Mock(return_value=("", False))
        agent._should_trim_forked_running_turn = AsyncMock(return_value=True)
        agent._mark_fork_correction_pending = Mock()
        agent._clear_fork_correction_pending = Mock()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(platform_specific={}),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
        )
        transport = SimpleNamespace(send_request=AsyncMock())

        with patch(
            "vibe.internal_client.turn_state",
            new=AsyncMock(
                return_value={
                    "body": {
                        "in_flight": True,
                        "native_turn_started": True,
                    }
                }
            ),
        ):
            with self.assertRaisesRegex(
                CodexForkBoundaryUnavailableError, "source turn boundary is unknown"
            ):
                await agent._fork_thread(
                    transport,
                    request,
                    {
                        "source_session_id": "ses-source",
                        "source_native_session_id": "thread-source",
                        "source_message_id": "msg-source",
                        "trim_latest_running_turn": True,
                    },
                )

        transport.send_request.assert_not_awaited()
        agent._clear_fork_correction_pending.assert_called_once_with("ses-target")

    async def test_fork_boundary_requires_a_completed_predecessor(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source"),
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                return_value={
                    "data": [
                        {"id": "turn-source", "status": "inProgress"},
                    ],
                }
            )
        )

        boundary = await agent._fork_source_last_completed_turn_id(
            transport,
            {
                "source_session_id": "ses-source",
                "source_native_session_id": "thread-source",
            },
        )

        self.assertEqual(boundary, (True, None))
        transport.send_request.assert_awaited_once_with(
            "thread/turns/list",
            {
                "threadId": "thread-source",
                "limit": 2,
                "itemsView": "notLoaded",
                "sortDirection": "desc",
            },
        )

    async def test_fork_boundary_keeps_completed_reserved_turn_inclusive(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source"),
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                return_value={
                    "data": [
                        {"id": "turn-new", "status": "inProgress"},
                        {"id": "turn-source", "status": "completed"},
                    ],
                }
            )
        )

        boundary = await agent._fork_source_last_completed_turn_id(
            transport,
            {
                "source_session_id": "ses-source",
                "source_native_session_id": "thread-source",
            },
        )

        self.assertEqual(boundary, (False, "turn-source"))
        transport.send_request.assert_awaited_once_with(
            "thread/turns/list",
            {
                "threadId": "thread-source",
                "limit": 2,
                "itemsView": "notLoaded",
                "sortDirection": "desc",
            },
        )

    async def test_fork_keeps_boundary_when_source_completes_before_rpc_write(self):
        for status in ("completed", "interrupted", "failed"):
            with self.subTest(status=status):
                agent = init_generation_state(object.__new__(CodexAgent))
                agent.sessions = SimpleNamespace(
                    ensure_agent_session_id=Mock(return_value="ses-target"),
                    bind_agent_session=Mock(return_value="ses-target"),
                )
                agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
                agent._turn_registry = SimpleNamespace(
                    get_active_turn=Mock(return_value="turn-source"),
                )
                agent._fork_source_prompt_state = Mock(return_value=(None, None, None))
                agent._resolve_codex_agent_settings = Mock(return_value=(None, None, None, None))
                agent._inject_caller_env_config = Mock(return_value=("", False))
                agent._should_trim_forked_running_turn = AsyncMock(return_value=True)
                agent._inject_forked_session_correction = AsyncMock()
                agent._mark_fork_correction_pending = Mock()
                agent._clear_fork_correction_pending = Mock()
                request = SimpleNamespace(
                    working_path="/tmp/work",
                    context=SimpleNamespace(platform_specific={}),
                    base_session_id="ses-target",
                    session_key="avibe::project::proj_1",
                )

                async def send_request(method, params):
                    if method == "thread/turns/list":
                        self.assertEqual(params["itemsView"], "notLoaded")
                        self.assertEqual(params["limit"], 2)
                        return {"data": [{"id": "turn-source", "status": status}]}
                    self.assertEqual(method, "thread/fork")
                    # A queued turn starts during the RPC write, after the
                    # boundary read. The fork must retain the completed source
                    # turn but must not copy this new active turn.
                    await asyncio.sleep(0)
                    agent._turn_registry.get_active_turn.return_value = "turn-next"
                    self.assertEqual(params["lastTurnId"], "turn-source")
                    self.assertTrue(params["excludeTurns"])
                    return {"thread": {"id": "thread-fork"}}

                transport = SimpleNamespace(send_request=AsyncMock(side_effect=send_request))
                thread_id = await agent._fork_thread(
                    transport,
                    request,
                    {
                        "source_session_id": "ses-source",
                        "source_native_session_id": "thread-source",
                        "trim_latest_running_turn": True,
                    },
                )
                self.assertEqual(thread_id, "thread-fork")
                self.assertEqual(transport.send_request.await_count, 2)
                agent.sessions.bind_agent_session.assert_called_once()
                agent._inject_forked_session_correction.assert_awaited_once_with(
                    transport, request, "thread-fork"
                )

    async def test_fork_boundary_pages_to_find_terminal_predecessor(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source"),
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {
                        "data": [
                            {"id": "turn-source", "status": "inProgress"},
                        ],
                        "nextCursor": "older",
                    },
                    {
                        "data": [
                            {"id": "turn-before", "status": "completed"},
                        ],
                        "nextCursor": None,
                    },
                ]
            )
        )

        boundary = await agent._fork_source_last_completed_turn_id(
            transport,
            {
                "source_session_id": "ses-source",
                "source_native_session_id": "thread-source",
            },
        )

        self.assertEqual(boundary, (True, "turn-before"))
        self.assertEqual(
            transport.send_request.await_args_list,
            [
                call(
                    "thread/turns/list",
                    {
                        "threadId": "thread-source",
                        "limit": 2,
                        "itemsView": "notLoaded",
                        "sortDirection": "desc",
                    },
                ),
                call(
                    "thread/turns/list",
                    {
                        "threadId": "thread-source",
                        "limit": 2,
                        "itemsView": "notLoaded",
                        "sortDirection": "desc",
                        "cursor": "older",
                    },
                ),
            ],
        )

    async def test_fork_boundary_searches_older_pages_for_reserved_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value="turn-source"))
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {
                        "data": [
                            {"id": "turn-next", "status": "inProgress"},
                            {"id": "turn-later", "status": "completed"},
                        ],
                        "nextCursor": "older",
                    },
                    {"data": [{"id": "turn-source", "status": "completed"}]},
                ]
            )
        )
        boundary = await agent._fork_source_last_completed_turn_id(
            transport,
            {"source_session_id": "ses-source", "source_native_session_id": "thread-source"},
        )
        self.assertEqual(boundary, (False, "turn-source"))
        self.assertEqual(transport.send_request.await_count, 2)
        self.assertEqual(transport.send_request.await_args.args[1]["cursor"], "older")

    async def test_fork_boundary_rejects_unproven_history(self):
        responses = [
            None,
            {},
            {"data": {}},
            {"data": []},
            {"data": [{"id": "other", "status": "completed"}]},
            {"data": [{"id": "turn-source", "status": "unknown"}]},
            {
                "data": [
                    {"id": "turn-source", "status": "inProgress"},
                    {"id": "turn-before", "status": "unknown"},
                ],
            },
            {"data": [None]},
            CodexRPCError({"code": -32601, "message": "unsupported"}),
            CodexResponseTooLargeError(),
            ConnectionError("disconnected"),
            TimeoutError("timed out"),
        ]
        for response in responses:
            with self.subTest(response=response):
                agent = init_generation_state(object.__new__(CodexAgent))
                agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value="turn-source"))
                transport = SimpleNamespace(send_request=AsyncMock(side_effect=[response]))
                boundary = await agent._fork_source_last_completed_turn_id(
                    transport,
                    {"source_session_id": "ses-source", "source_native_session_id": "thread-source"},
                )
                self.assertEqual(boundary, (True, None))
                transport.send_request.assert_awaited_once()

    async def test_fork_boundary_rejects_repeated_pagination_cursor(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._turn_registry = SimpleNamespace(get_active_turn=Mock(return_value="turn-source"))
        transport = SimpleNamespace(
            send_request=AsyncMock(return_value={"data": [], "nextCursor": "same"}),
        )
        boundary = await agent._fork_source_last_completed_turn_id(
            transport,
            {"source_session_id": "ses-source", "source_native_session_id": "thread-source"},
        )
        self.assertEqual(boundary, (True, None))
        self.assertEqual(transport.send_request.await_count, 2)

    async def test_start_or_resume_thread_trims_running_fork_before_correction(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source")
        )
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "trim_latest_running_turn": True,
                            "native_turn_started": True,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/turns/list",
            "thread/fork",
            "thread/inject_items",
        ])
        self.assertEqual(
            transport.send_request.await_args_list[1].args[1]["lastTurnId"],
            "turn-before",
        )
        agent.sessions.bind_agent_session.assert_called_once_with(
            "avibe::project::proj_1",
            "codex",
            "ses-target",
            "thread-fork",
        )

    async def test_start_or_resume_thread_keeps_running_fork_before_native_start(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        with patch(
            "vibe.internal_client.turn_state",
            new=AsyncMock(return_value={"body": {"in_flight": False, "native_turn_started": False}}),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

    async def test_start_or_resume_thread_trims_pre_start_fork_after_source_started(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        turn_state_checked = False

        async def turn_state(_source_session_id):
            nonlocal turn_state_checked
            turn_state_checked = True
            return {
                "body": {
                    "in_flight": True,
                    "native_turn_started": True,
                    "native_turn_id": "turn-source",
                }
            }

        def send_request(method, _params):
            if method == "thread/turns/list":
                return {
                    "data": [
                        {"id": "turn-source", "status": "inProgress"},
                        {"id": "turn-before", "status": "completed"},
                    ],
                }
            if method == "thread/fork":
                self.assertTrue(turn_state_checked)
            return {"thread": {"id": "thread-fork"}}

        transport = SimpleNamespace(send_request=AsyncMock(side_effect=send_request))

        with patch(
            "vibe.internal_client.turn_state",
            new=AsyncMock(side_effect=turn_state),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/turns/list",
            "thread/fork",
            "thread/inject_items",
        ])
        fork_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(fork_params["lastTurnId"], "turn-before")

    async def test_start_or_resume_thread_trims_pre_start_fork_after_source_output(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source")
        )
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_is_terminal_agent_output=False,
                latest_after_anchor_author="agent",
                latest_after_anchor_type="assistant",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=False,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/turns/list",
            "thread/fork",
            "thread/inject_items",
        ])
        fork_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(fork_params["lastTurnId"], "turn-before")

    async def test_start_or_resume_thread_keeps_running_fork_when_anchor_completed(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source")
        )
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-result",
                            "trim_latest_running_turn": True,
                            "native_turn_started": True,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_is_terminal_agent_output=True,
                latest_after_anchor_author=None,
                latest_after_anchor_type=None,
                has_messages_after_anchor=False,
                has_terminal_agent_output_after_anchor=False,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

    async def test_start_or_resume_thread_keeps_running_fork_after_source_completed(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_is_terminal_agent_output=False,
                latest_after_anchor_author="agent",
                latest_after_anchor_type="result",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=True,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

    async def test_start_or_resume_thread_trims_reserved_user_anchor_after_source_completed(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source")
        )
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": True,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_author="user",
                anchor_type="user",
                anchor_is_terminal_agent_output=False,
                latest_after_anchor_author="agent",
                latest_after_anchor_type="result",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=True,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/turns/list",
            "thread/fork",
            "thread/inject_items",
        ])
        fork_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(fork_params["lastTurnId"], "turn-before")

    async def test_start_or_resume_thread_trims_user_anchor_completed_before_native_start_flag(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value="turn-source")
        )
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = _fork_transport()

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_author="user",
                anchor_type="user",
                anchor_is_terminal_agent_output=False,
                latest_after_anchor_author="agent",
                latest_after_anchor_type="result",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=True,
                has_input_turn_after_anchor=False,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/turns/list",
            "thread/fork",
            "thread/inject_items",
        ])
        fork_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(fork_params["lastTurnId"], "turn-before")

    async def test_start_or_resume_thread_does_not_trim_when_new_user_after_anchor(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user-a",
                            "trim_latest_running_turn": True,
                            "native_turn_started": True,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_author="user",
                anchor_type="user",
                anchor_is_terminal_agent_output=False,
                latest_after_anchor_author="user",
                latest_after_anchor_type="user",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=False,
                has_input_turn_after_anchor=True,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

    async def test_should_roll_back_forked_running_harness_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        fork = {
            "source_session_id": "ses-source",
            "source_message_id": "msg-harness",
            "trim_latest_running_turn": True,
            "native_turn_started": True,
        }

        with patch.object(
            _MODULE,
            "fork_source_state",
            return_value=SimpleNamespace(
                anchor_author="harness",
                anchor_type="harness",
                anchor_is_terminal_agent_output=False,
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=False,
                has_input_turn_after_anchor=False,
            ),
        ):
            should_trim = await agent._should_trim_forked_running_turn(fork)

        self.assertTrue(should_trim)

    async def test_start_or_resume_thread_does_not_trim_user_anchor_before_native_start(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="avibe", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value=None),
            ensure_agent_session_id=Mock(return_value="ses-target"),
            bind_agent_session=Mock(return_value="ses-target"),
        )
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._fork_correction_pending_base_sessions = set()
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-target",
                        "agent_backend": "codex",
                        "native_session_id": "",
                        "native_session_fork": {
                            "source_session_id": "ses-source",
                            "source_native_session_id": "thread-source",
                            "source_backend": "codex",
                            "source_message_id": "msg-user",
                            "trim_latest_running_turn": True,
                            "native_turn_started": False,
                        },
                    }
                },
                user_id="scheduled",
                channel_id="ses-target",
                thread_id=None,
            ),
            base_session_id="ses-target",
            session_key="avibe::project::proj_1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

        with (
            patch.object(
                _MODULE,
                "fork_source_state",
                return_value=SimpleNamespace(
                    anchor_author="user",
                    anchor_type="user",
                    anchor_is_terminal_agent_output=False,
                    latest_after_anchor_author=None,
                    latest_after_anchor_type=None,
                    has_messages_after_anchor=False,
                    has_terminal_agent_output_after_anchor=False,
                ),
            ),
            patch(
                "vibe.internal_client.turn_state",
                new=AsyncMock(return_value={"body": {"in_flight": False, "native_turn_started": False}}),
            ) as turn_state,
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        turn_state.assert_awaited_once_with("ses-source")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

    async def test_resume_thread_skips_reserved_native_for_explicit_subagent(self):
        # Explicit per-turn subagent: it has its own thread; must NOT resume the
        # reserved MAIN native.
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-subagent"))
        agent.bind_agent_session_id = Mock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={
                    "agent_session_target": {
                        "id": "ses-1",
                        "native_session_id": "native-reserved",
                        "session_anchor": "ses-1",
                    }
                },
            ),
            base_session_id="ses-1:reviewer",
            session_key="avibe::ses-1",
            subagent_name="reviewer",
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"id": "thread-subagent"}))

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-subagent")
        method, params = transport.send_request.await_args_list[0].args
        self.assertEqual(params["threadId"], "thread-subagent")

    async def test_resume_thread_fails_loud_on_non_transport_resume_error(self):
        # An associated thread that won't resume for a non-transport reason
        # (expired/gone) must RAISE, not silently start a fresh thread.
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-old"))
        agent.bind_agent_session_id = Mock()
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._start_thread = AsyncMock()
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(platform="slack", platform_specific={}),
            base_session_id="session-1",
            session_key="slack::channel::C1",
            subagent_name=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(side_effect=RuntimeError("thread is gone")))

        with self.assertRaises(CodexResumeUnavailableError):
            await agent._start_or_resume_thread(transport, request)
        agent._start_thread.assert_not_awaited()  # must NOT silently fork a fresh thread

    async def test_resume_thread_preserves_unmanaged_cross_provider_session(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {"model_provider": "openai-managed"}},
                    {"thread": {"id": "thread-existing", "modelProvider": "anthropic"}},
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        method, params = transport.send_request.await_args_list[2].args
        self.assertEqual(method, "thread/resume")
        self.assertNotIn("modelProvider", params)

    async def test_resume_thread_omits_model_provider_when_provider_read_fails(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {"model_provider": "openai-managed"}},
                    RuntimeError("thread/read unavailable"),
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        method, params = transport.send_request.await_args_list[2].args
        self.assertEqual(method, "thread/resume")
        self.assertNotIn("modelProvider", params)

    async def test_resume_thread_omits_model_provider_when_config_read_fails(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
        )
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    RuntimeError("config/read unavailable"),
                    {"thread": {"id": "thread-existing"}},
                ]
            )
        )

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        method, params = transport.send_request.await_args_list[1].args
        self.assertEqual(method, "thread/resume")
        self.assertNotIn("modelProvider", params)

    async def test_resume_thread_clears_legacy_thread_prompt_before_turn_strategy(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=False))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"),
            get_agent_session_runtime_marker=Mock(return_value=None),
        )
        agent._prompt_state_agent_session_id = Mock(return_value="sesk8m4q2p7x")
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            base_session_id="session-1",
            session_key="slack::channel::C1::thread::171717.123",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/resume")
        self.assertIsNone(params["developerInstructions"])

    async def test_resume_thread_routes_prompt_marker_read_failure_through_i18n(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(
            get_agent_session_id=Mock(return_value="thread-existing"),
            get_agent_session_runtime_marker=Mock(
                side_effect=OSError("database busy")
            ),
        )
        agent.bind_agent_session_id = Mock()
        agent._prompt_state_agent_session_id = Mock(return_value="ses-runtime")
        request = SimpleNamespace(
            working_path="/tmp/work",
            context=SimpleNamespace(platform_specific={}),
            base_session_id="session-1",
            session_key="channel-1",
            subagent_name=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock())

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "Could not resolve the Codex prompt strategy",
        ):
            await agent._start_or_resume_thread(transport, request)

        transport.send_request.assert_not_awaited()


    def test_build_input_does_not_add_codex_generated_image_prompt_to_each_turn(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(reply_enhancements=True))
        request = SimpleNamespace(message="hello", files=None)

        with patch.dict(os.environ, {"CODEX_HOME": "/Users/test/.codex"}):
            items = agent._build_input(request)

        self.assertEqual(items, [{"type": "text", "text": "hello"}])

    async def test_refresh_thread_developer_instructions_updates_cached_thread_once(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"))
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        agent._thread_developer_instructions = {}
        request = SimpleNamespace(
            working_path="/tmp/work",
            session_key="slack::channel::C1::thread::171717.123",
            base_session_id="session-1",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        await agent._refresh_thread_developer_instructions_if_needed(transport, request, "thread-existing")
        await agent._refresh_thread_developer_instructions_if_needed(transport, request, "thread-existing")

        transport.send_request.assert_awaited_once()
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "thread-existing")
        self.assertIs(params["excludeTurns"], True)
        self.assertNotIn("modelProvider", params)
        self.assertNotIn("developerInstructions", params)

    async def test_refresh_thread_developer_instructions_refreshes_caller_env_when_prompt_cached(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"))
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        agent._thread_developer_instructions = {}
        request = SimpleNamespace(
            working_path="/tmp/work",
            session_key="slack::channel::C1::thread::171717.123",
            base_session_id="session-1",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={
                    "task_execution_id": "run-one",
                    "task_trigger_kind": "agent_run",
                    "agent_session_target": {
                        "id": "sesk8m4q2p7x",
                        "agent_backend": "codex",
                        "native_session_id": "thread-existing",
                    },
                },
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        await agent._refresh_thread_developer_instructions_if_needed(transport, request, "thread-existing")
        request.context.platform_specific["task_execution_id"] = "run-two"
        await agent._refresh_thread_developer_instructions_if_needed(transport, request, "thread-existing")

        self.assertEqual(transport.send_request.await_count, 2)
        first_params = transport.send_request.await_args_list[0].args[1]
        second_params = transport.send_request.await_args_list[1].args[1]
        self.assertNotIn("developerInstructions", first_params)
        self.assertNotIn("developerInstructions", second_params)
        self.assertEqual(
            second_params["config"]["shell_environment_policy"]["set"]["AVIBE_RUN_ID"],
            "run-two",
        )
        self.assertEqual(second_params["threadId"], "thread-existing")

    async def test_refresh_thread_developer_instructions_refreshes_git_path_state(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"))
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable instructions")
        agent._thread_developer_instructions = {
            "session-1": ("thread-existing", "stable instructions")
        }
        agent._thread_caller_env_configs = {}
        agent._thread_git_path_configs = {
            "session-1": ("thread-existing", "/gitless/bin", False)
        }
        request = SimpleNamespace(
            working_path="/tmp/work",
            session_key="slack::channel::C1::thread::171717.123",
            base_session_id="session-1",
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        def inject_git(env, *, base_env, working_dir):
            env["PATH"] = "/managed/git/bin:/gitless/bin"
            return True

        with patch.dict(os.environ, {"PATH": "/gitless/bin"}), patch(
            "core.git_runtime.prepend_vendored_git_to_path",
            side_effect=inject_git,
        ):
            await agent._refresh_thread_developer_instructions_if_needed(
                transport,
                request,
                "thread-existing",
            )
            await agent._refresh_thread_developer_instructions_if_needed(
                transport,
                request,
                "thread-existing",
            )

        transport.send_request.assert_awaited_once()
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/resume")
        self.assertNotIn("developerInstructions", params)
        self.assertEqual(
            params["config"]["shell_environment_policy"]["set"]["PATH"],
            "/managed/git/bin:/gitless/bin",
        )
        self.assertEqual(
            agent._thread_git_path_configs["session-1"],
            ("thread-existing", "/managed/git/bin:/gitless/bin", True),
        )

    async def test_refresh_thread_developer_instructions_clears_stale_vendored_path(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"))
        agent._resolve_resume_model_provider_override = AsyncMock(return_value=None)
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable instructions")
        agent._thread_developer_instructions = {
            "session-1": ("thread-existing", "stable instructions")
        }
        agent._thread_caller_env_configs = {}
        agent._thread_git_path_configs = {
            "session-1": ("thread-existing", "/managed/git/bin:/usr/bin", True)
        }
        request = SimpleNamespace(
            working_path="/tmp/work",
            session_key="slack::channel::C1::thread::171717.123",
            base_session_id="session-1",
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        with patch.dict(os.environ, {"PATH": "/usr/bin"}), patch(
            "core.git_runtime.prepend_vendored_git_to_path",
            return_value=False,
        ):
            await agent._refresh_thread_developer_instructions_if_needed(
                transport,
                request,
                "thread-existing",
            )

        _, params = transport.send_request.await_args.args
        self.assertEqual(
            params["config"]["shell_environment_policy"]["set"]["PATH"],
            "/usr/bin",
        )
        self.assertEqual(
            agent._thread_git_path_configs["session-1"],
            ("thread-existing", "/usr/bin", True),
        )

    async def test_refresh_thread_developer_instructions_preserves_resume_model_provider_override(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=True))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(ensure_agent_session_id=Mock(return_value="sesk8m4q2p7x"))
        agent._resolve_resume_model_provider_override = AsyncMock(return_value="openai-managed")
        agent._thread_developer_instructions = {}
        request = SimpleNamespace(
            working_path="/tmp/work",
            session_key="slack::channel::C1::thread::171717.123",
            base_session_id="session-1",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={"is_dm": False},
                user_id="U1",
                channel_id="C1",
                thread_id="171717.123",
            ),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        await agent._refresh_thread_developer_instructions_if_needed(transport, request, "thread-existing")

        agent._resolve_resume_model_provider_override.assert_awaited_once_with(
            transport,
            request,
            "thread-existing",
        )
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/resume")
        self.assertEqual(params["threadId"], "thread-existing")
        self.assertEqual(params["modelProvider"], "openai-managed")
        self.assertNotIn("developerInstructions", params)

    async def test_start_turn_injects_stable_developer_instructions_once(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._thread_model_settings = {
            "session-1": ("thread-1", "gpt-5.4", "high"),
        }
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=None),
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )
        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        calls = transport.send_request.await_args_list
        self.assertEqual(
            [entry.args[0] for entry in calls],
            ["thread/inject_items", "turn/start", "turn/start"],
        )
        self.assertEqual(calls[0].args[1]["items"][0]["role"], "developer")
        self.assertEqual(
            calls[0].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("stable prompt"),
        )
        for entry in calls[1:]:
            self.assertNotIn("collaborationMode", entry.args[1])
            self.assertEqual(entry.args[1]["model"], "gpt-5.4")
            self.assertEqual(entry.args[1]["effort"], "high")
        self.assertEqual(agent.sessions.set_agent_session_runtime_marker.call_count, 2)
        agent.sessions.set_agent_session_runtime_marker.assert_called_with(
            "ses-runtime",
            backend="codex",
            native_session_id="thread-1",
            key="codex_prompt_strategy",
            value={
                "thread_id": "thread-1",
                "strategy": "fallback",
                "sha256": agent._prompt_fingerprint("stable prompt"),
            },
        )
        self.assertEqual(
            agent._thread_developer_instructions["session-1"],
            ("thread-1", "stable prompt"),
        )

    async def test_start_turn_persists_subagent_strategy_on_backend_session(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_row_id=Mock(return_value="ses-backend"),
            get_agent_session_runtime_marker=Mock(return_value=None),
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-visible")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="avibe::project::proj-1",
            base_session_id="session-1:subagent:reviewer",
            composite_session_id="avibe:session-1",
            subagent_name="reviewer",
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-subagent",
            developer_instructions="stable prompt",
        )

        agent.sessions.get_agent_session_row_id.assert_called_once_with(
            "avibe::project::proj-1",
            "session-1:subagent:reviewer",
            "codex",
        )
        self.assertEqual(agent.sessions.set_agent_session_runtime_marker.call_count, 2)
        agent.sessions.set_agent_session_runtime_marker.assert_called_with(
            "ses-backend",
            backend="codex",
            native_session_id="thread-subagent",
            key=CODEX_PROMPT_STRATEGY_METADATA_KEY,
            value={
                "thread_id": "thread-subagent",
                "strategy": "fallback",
                "sha256": agent._prompt_fingerprint("stable prompt"),
            },
        )

    async def test_start_turn_honors_explicit_null_model_instead_of_cached_route(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "routing-model", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace()
        agent._thread_model_settings = {
            "session-1": ("thread-1", "gpt-5.4", "high"),
        }
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
            vibe_agent_model_explicit=True,
            vibe_agent_reasoning_effort_explicit=True,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions=None,
        )

        params = transport.send_request.await_args.args[1]
        self.assertIsNone(params["model"])
        self.assertIsNone(params["effort"])
        self.assertNotIn("collaborationMode", params)
        self.assertNotIn("session-1", agent._thread_model_settings)

    async def test_start_turn_fails_when_fallback_strategy_cannot_persist(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=None),
            set_agent_session_runtime_marker=Mock(return_value=False),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-1"}}],
            ),
        )

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "Could not prepare the fallback prompt strategy",
        ):
            await agent._start_turn(
                transport,
                request,
                "thread-1",
                developer_instructions="stable prompt",
            )

        calls = transport.send_request.await_args_list
        self.assertEqual(
            [call.args[0] for call in calls],
            [],
        )
        self.assertNotIn("session-1", agent._thread_prompt_strategies)
        self.assertNotIn("session-1", getattr(agent, "_thread_developer_instructions", {}))

    async def test_start_turn_repairs_marker_without_reinjecting_known_prompt(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=None),
            set_agent_session_runtime_marker=Mock(
                side_effect=[True, False, False, True]
            ),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-1"}}],
            ),
        )

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "Could not persist the fallback prompt strategy",
        ):
            await agent._start_turn(
                transport,
                request,
                "thread-1",
                developer_instructions="stable prompt",
            )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        self.assertEqual(
            [rpc.args[0] for rpc in transport.send_request.await_args_list],
            ["thread/inject_items", "turn/start"],
        )
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )
        self.assertNotIn("session-1", agent._thread_unpersisted_prompts)

    async def test_start_turn_reuses_persisted_fallback_prompt_after_process_restart(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "fallback",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=marker),
            set_agent_session_runtime_marker=Mock(),
        )
        agent._thread_developer_instructions = {}
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        self.assertEqual(
            [call.args[0] for call in transport.send_request.await_args_list],
            ["turn/start"],
        )
        agent.sessions.get_agent_session_runtime_marker.assert_called_once_with(
            "ses-runtime",
            backend="codex",
            native_session_id="thread-1",
            key="codex_prompt_strategy",
        )
        agent.sessions.set_agent_session_runtime_marker.assert_not_called()
        self.assertEqual(
            agent._thread_developer_instructions["session-1"],
            ("thread-1", "stable prompt"),
        )
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )

    async def test_start_turn_disables_refresh_for_invalid_persisted_prompt_marker(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(
                return_value={
                    "thread_id": "thread-1",
                    "strategy": "future-strategy",
                }
            ),
            set_agent_session_runtime_marker=Mock(),
        )
        agent._thread_developer_instructions = {}
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="current prompt",
        )

        self.assertEqual(
            [call.args[0] for call in transport.send_request.await_args_list],
            ["turn/start"],
        )
        self.assertNotIn(
            "collaborationMode",
            transport.send_request.await_args_list[0].args[1],
        )
        agent.sessions.set_agent_session_runtime_marker.assert_not_called()
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "unavailable"),
        )

    def test_prompt_marker_read_failure_uses_localized_error_path(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(side_effect=OSError("database busy"))
        )

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "Could not resolve the Codex prompt strategy",
        ):
            agent._read_persisted_prompt_strategy_marker(
                "thread-1",
                agent_session_id="ses-runtime",
            )

    async def test_start_turn_migrates_persisted_collaboration_after_inconclusive_probe(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "collaboration",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=marker),
            set_agent_session_runtime_marker=Mock(),
        )
        agent._thread_developer_instructions = {}
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        self.assertEqual(
            [call.args[0] for call in transport.send_request.await_args_list],
            ["collaborationMode/list", "thread/inject_items", "turn/start"],
        )
        params = transport.send_request.await_args_list[2].args[1]
        self.assertIsNone(params["collaborationMode"])
        self.assertEqual(params["model"], "gpt-5.4")
        self.assertEqual(params["effort"], "high")
        self.assertEqual(
            transport.send_request.await_args_list[1].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("stable prompt"),
        )
        self.assertEqual(agent.sessions.set_agent_session_runtime_marker.call_count, 3)
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )

    async def test_start_turn_fails_closed_when_collaboration_reprobe_is_negative(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "collaboration",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=marker),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(side_effect=TimeoutError("probe unavailable")),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "did not confirm collaboration mode support",
        ):
            await agent._start_turn(
                transport,
                request,
                "thread-1",
                developer_instructions="stable prompt",
            )

        transport.send_request.assert_awaited_once_with("collaborationMode/list", {})

    async def test_start_turn_clears_sticky_collaboration_mode_with_explicit_null_model(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "routing-model", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent._thread_developer_instructions = {
            "session-1": ("thread-1", "stable prompt"),
        }
        agent._thread_prompt_strategies = {
            "session-1": ("thread-1", "collaboration"),
        }
        agent._thread_model_settings = {
            "session-1": ("thread-1", "gpt-5.4", "high"),
        }
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            vibe_agent_model=None,
            vibe_agent_reasoning_effort=None,
            vibe_agent_model_explicit=True,
            vibe_agent_reasoning_effort_explicit=True,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-1"}}],
            ),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        calls = transport.send_request.await_args_list
        self.assertEqual([call.args[0] for call in calls], ["thread/inject_items", "turn/start"])
        turn_params = calls[1].args[1]
        self.assertIsNone(turn_params["collaborationMode"])
        self.assertIsNone(turn_params["model"])
        self.assertIsNone(turn_params["effort"])
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )

    async def test_start_turn_clears_persisted_collaboration_before_model_less_fallback(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "collaboration",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(return_value=marker),
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(
                side_effect=[{}, {}, {"turn": {"id": "turn-1"}}],
            ),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        calls = transport.send_request.await_args_list
        self.assertEqual(
            [call.args[0] for call in calls],
            ["collaborationMode/list", "thread/inject_items", "turn/start"],
        )
        self.assertIsNone(calls[2].args[1]["collaborationMode"])
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )

    async def test_start_turn_retries_a_pending_collaboration_clear_after_restart(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "collaboration",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }

        def read_marker(*_args, **_kwargs):
            return dict(marker)

        def write_marker(*_args, **kwargs):
            marker.clear()
            marker.update(kwargs["value"])
            return True

        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(side_effect=read_marker),
            set_agent_session_runtime_marker=Mock(side_effect=write_marker),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        failed_transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(side_effect=[{}, TimeoutError("connection lost")]),
        )

        with self.assertRaisesRegex(
            CodexPromptRefreshUnavailableError,
            "cleared the previous collaboration prompt",
        ):
            await agent._start_turn(
                failed_transport,
                request,
                "thread-1",
                developer_instructions="stable prompt",
            )

        self.assertEqual(marker["strategy"], "fallback_pending_clear")
        self.assertEqual(
            marker["sha256"],
            agent._prompt_fingerprint("stable prompt"),
        )

        # Simulate a controller restart: only the durable transitional marker remains.
        agent._thread_prompt_strategies = {}
        agent._thread_developer_instructions = {}
        resumed_transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-2"}}],
            ),
        )

        await agent._start_turn(
            resumed_transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        resumed_calls = resumed_transport.send_request.await_args_list
        self.assertEqual(
            [call.args[0] for call in resumed_calls],
            ["collaborationMode/list", "turn/start"],
        )
        self.assertIsNone(resumed_calls[1].args[1]["collaborationMode"])
        self.assertEqual(marker["strategy"], "fallback")
        self.assertNotIn("pending_collaboration_clear", marker)
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback"),
        )

    async def test_start_turn_clears_collaboration_without_reinjecting_after_unknown_injection(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        marker = {
            "thread_id": "thread-1",
            "strategy": "collaboration",
            "sha256": agent._prompt_fingerprint("stable prompt"),
        }

        def read_marker(*_args, **_kwargs):
            return dict(marker)

        def write_marker(*_args, **kwargs):
            marker.clear()
            marker.update(kwargs["value"])
            return True

        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(side_effect=read_marker),
            set_agent_session_runtime_marker=Mock(side_effect=write_marker),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        failed_transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(
                side_effect=[{}, TimeoutError("injection outcome unknown")],
            ),
        )

        with self.assertRaisesRegex(TimeoutError, "injection outcome unknown"):
            await agent._start_turn(
                failed_transport,
                request,
                "thread-1",
                developer_instructions="stable prompt",
            )

        self.assertEqual(marker["strategy"], "fallback_pending_clear_injection")
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "fallback_pending_clear_injection"),
        )

        # Simulate a controller restart: recovery has only the write-ahead marker.
        agent._thread_prompt_strategies = {}
        agent._thread_developer_instructions = {}
        resumed_transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-2"}}],
            ),
        )

        await agent._start_turn(
            resumed_transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        resumed_calls = resumed_transport.send_request.await_args_list
        self.assertEqual(
            [call.args[0] for call in resumed_calls],
            ["collaborationMode/list", "turn/start"],
        )
        self.assertIsNone(resumed_calls[1].args[1]["collaborationMode"])
        self.assertEqual(marker["strategy"], "unavailable")
        self.assertNotIn("sha256", marker)
        self.assertEqual(
            agent._thread_prompt_strategies["session-1"],
            ("thread-1", "unavailable"),
        )

    def test_prompt_strategy_rebinds_a_stale_native_session_before_retry(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.sessions = SimpleNamespace(
            set_agent_session_runtime_marker=Mock(side_effect=[False, True]),
        )
        agent.bind_agent_session_id = Mock(return_value="ses-runtime")
        request = SimpleNamespace(
            base_session_id="session-1",
            context=SimpleNamespace(platform_specific={}),
        )

        persisted = agent._persist_prompt_strategy(
            request,
            "thread-1",
            "stable prompt",
            strategy="collaboration",
            agent_session_id="ses-runtime",
        )

        self.assertTrue(persisted)
        agent.bind_agent_session_id.assert_called_once_with(request, "thread-1")
        self.assertEqual(
            agent.sessions.set_agent_session_runtime_marker.call_count,
            2,
        )

    async def test_start_turn_persists_changed_fallback_prompt(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=Mock(
                return_value={
                    "thread_id": "thread-1",
                    "strategy": "fallback",
                    "sha256": agent._prompt_fingerprint("old prompt"),
                }
            ),
            set_agent_session_runtime_marker=Mock(return_value=True),
        )
        agent._thread_developer_instructions = {}
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="avibe:session-1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="changed prompt",
        )

        self.assertEqual(
            [call.args[0] for call in transport.send_request.await_args_list],
            ["thread/inject_items", "turn/start"],
        )
        prompt_sha = agent._prompt_fingerprint("changed prompt")
        agent.sessions.set_agent_session_runtime_marker.assert_has_calls(
            [
                call(
                    "ses-runtime",
                    backend="codex",
                    native_session_id="thread-1",
                    key="codex_prompt_strategy",
                    value={
                        "thread_id": "thread-1",
                        "strategy": "fallback_pending_injection",
                        "sha256": prompt_sha,
                    },
                ),
                call(
                    "ses-runtime",
                    backend="codex",
                    native_session_id="thread-1",
                    key="codex_prompt_strategy",
                    value={
                        "thread_id": "thread-1",
                        "strategy": "fallback",
                        "sha256": prompt_sha,
                    },
                ),
            ]
        )

    async def test_start_turn_does_not_reuse_cached_effort_for_an_explicit_model_change(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.5", None)),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._thread_model_settings = {
            "session-1": ("thread-1", "gpt-5.4", "high"),
        }
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        params = transport.send_request.await_args.args[1]
        self.assertEqual(params["model"], "gpt-5.5")
        self.assertNotIn("effort", params)
        self.assertNotIn("collaborationMode", params)

    async def test_start_turn_preserves_explicit_effort_while_restoring_cached_model(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, "xhigh")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent._thread_model_settings = {
            "session-1": ("thread-1", "gpt-5.4", "high"),
        }
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        params = transport.send_request.await_args.args[1]
        self.assertEqual(params["model"], "gpt-5.4")
        self.assertEqual(params["effort"], "xhigh")
        self.assertNotIn("collaborationMode", params)

    async def test_start_turn_injects_updated_instructions_when_prompt_changes(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )

        for prompt in ("prompt one", "prompt two"):
            await agent._start_turn(
                transport,
                request,
                "thread-1",
                developer_instructions=prompt,
            )

        calls = transport.send_request.await_args_list
        self.assertEqual(
            [entry.args[0] for entry in calls],
            ["thread/inject_items", "turn/start", "thread/inject_items", "turn/start"],
        )
        self.assertEqual(calls[1].args[1], calls[3].args[1])
        self.assertEqual(
            calls[0].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("prompt one"),
        )
        self.assertEqual(
            calls[2].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("prompt two"),
        )
        latest = calls[2].args[1]["items"][0]["content"][0]["text"]
        self.assertEqual(latest, "<avibe_runtime_instructions>\n\nprompt two\n</avibe_runtime_instructions>")

    async def test_start_turn_does_not_reinject_when_explicit_model_reset_is_unsupported(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(
                side_effect=[
                    {},
                    RuntimeError("unknown field collaborationMode: experimental API unsupported"),
                    {"turn": {"id": "turn-1"}},
                ]
            ),
        )
        request.vibe_agent_model_explicit = True
        request.vibe_agent_model = None

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        calls = transport.send_request.await_args_list
        self.assertEqual(calls[0].args[0], "thread/inject_items")
        self.assertEqual(
            calls[0].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("stable prompt"),
        )
        self.assertEqual(calls[1].args[0], "turn/start")
        self.assertIsNone(calls[1].args[1]["collaborationMode"])
        self.assertEqual(calls[2].args[0], "turn/start")
        self.assertNotIn("collaborationMode", calls[2].args[1])
        self.assertFalse(transport.supports_turn_collaboration_mode)

    async def test_start_turn_uses_injection_after_collaboration_probe_fails(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        transport = SimpleNamespace(
            supports_turn_collaboration_mode=False,
            send_request=AsyncMock(
                side_effect=[{}, {"turn": {"id": "turn-1"}}],
            ),
        )

        await agent._start_turn(
            transport,
            request,
            "thread-1",
            developer_instructions="stable prompt",
        )

        calls = transport.send_request.await_args_list
        self.assertEqual(calls[0].args[0], "thread/inject_items")
        self.assertEqual(
            calls[0].args[1]["items"][0]["content"][0]["text"],
            agent._render_developer_prompt_snapshot("stable prompt"),
        )
        self.assertNotIn("collaborationMode", calls[1].args[1])

    async def test_start_turn_uses_sandbox_policy_object(self):
        from core.native_dispatch_phase import (
            DISPATCH_PHASE_PREWRITE,
            backend_dispatch_attempted,
            set_dispatch_phase,
        )

        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(get_codex_overrides=Mock(return_value=(None, None, None)))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            context=SimpleNamespace(platform_specific={}),
        )
        set_dispatch_phase(request.context, DISPATCH_PHASE_PREWRITE)

        async def _send_at_native_boundary(*_args, **_kwargs):
            self.assertIs(backend_dispatch_attempted(request.context), True)
            return {"turn": {"id": "turn-1"}}

        transport = SimpleNamespace(send_request=AsyncMock(side_effect=_send_at_native_boundary))

        thread_id = await agent._start_turn(transport, request, "thread-1")

        self.assertEqual(thread_id, "thread-1")
        agent.ensure_agent_session_id.assert_called_once_with(request)
        transport.send_request.assert_awaited_once_with(
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        )
        agent.controller.get_codex_overrides.assert_called_once_with(request.context)

    async def test_start_turn_writes_current_caller_env_script_for_reused_threads(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(get_codex_overrides=Mock(return_value=(None, None, None)))
        agent.codex_config = SimpleNamespace(default_model=None)
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            context=SimpleNamespace(
                platform="slack",
                platform_specific={
                    "task_execution_id": "run-two",
                    "task_trigger_kind": "agent_run",
                    "agent_session_target": {
                        "id": "sesk8m4q2p7x",
                        "agent_backend": "codex",
                        "native_session_id": "thread-existing",
                    },
                },
            ),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "turn-2"}}))

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.paths.get_runtime_dir", return_value=Path(tmpdir)):
                await agent._start_turn(transport, request, "thread-existing")
                env_script = Path(tmpdir) / "codex-caller-env" / "session-1.sh"
                script_text = env_script.read_text()

        params = transport.send_request.await_args.args[1]
        self.assertNotIn("config", params)
        self.assertIn("export AVIBE_SESSION_ID=sesk8m4q2p7x", script_text)
        self.assertIn("export AVIBE_RUN_ID=run-two", script_text)
        self.assertIn("export AVIBE_CALLER_SOURCE=agent_run", script_text)
        self.assertIn("export AVIBE_CALLER_BACKEND=codex", script_text)
        self.assertIn("export AVIBE_NATIVE_SESSION_ID=thread-existing", script_text)

    async def test_start_turn_uses_controller_codex_overrides(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.settings_manager = SimpleNamespace(
            get_channel_settings=Mock(side_effect=AssertionError("Codex must use controller routing overrides"))
        )
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.4", "high")),
        )
        agent.codex_config = SimpleNamespace(default_model="fallback-model")
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="discord::D123",
            base_session_id="session-1",
            composite_session_id="discord:D1:T1",
            context=SimpleNamespace(platform="discord", platform_specific={"is_dm": True}),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}))

        await agent._start_turn(transport, request, "thread-1")

        agent.controller.get_codex_overrides.assert_called_once_with(request.context)
        transport.send_request.assert_awaited_once_with(
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "model": "gpt-5.4",
                "effort": "high",
            },
        )

    def test_model_hub_filters_every_codex_effort_source_at_the_adapter_boundary(self):
        from modules.agents.model_hub import ModelHubLaunch, bind_launch

        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(),
            model_hub_runtime=object(),
        )
        launch = ModelHubLaunch(
            backend="codex",
            channel="hub",
            requested_model="no-reasoning-model",
            target_model="upstream-model",
            runtime_model="no-reasoning-model",
            reasoning_efforts=(),
            supports_reasoning=False,
        )
        cases = (
            (
                SimpleNamespace(
                    context=SimpleNamespace(),
                    subagent_name=None,
                    subagent_model=None,
                    subagent_reasoning_effort="high",
                ),
                (None, None, None),
            ),
            (
                SimpleNamespace(
                    context=SimpleNamespace(),
                    subagent_name=None,
                    subagent_model=None,
                    subagent_reasoning_effort=None,
                    vibe_agent_reasoning_effort="high",
                ),
                (None, None, None),
            ),
            (
                SimpleNamespace(
                    context=SimpleNamespace(),
                    subagent_name=None,
                    subagent_model=None,
                    subagent_reasoning_effort=None,
                ),
                (None, None, "high"),
            ),
            (
                SimpleNamespace(
                    context=SimpleNamespace(),
                    subagent_name="reviewer",
                    subagent_model=None,
                    subagent_reasoning_effort=None,
                    working_path="/tmp/work",
                ),
                (None, None, None),
            ),
        )

        with patch.object(
            _MODULE,
            "load_codex_subagent",
            return_value=SimpleNamespace(
                model=None,
                reasoning_effort="high",
                developer_instructions=None,
            ),
        ):
            for request, overrides in cases:
                agent.controller.get_codex_overrides.return_value = overrides
                bind_launch(request.context, launch)
                self.assertIsNone(agent._resolve_codex_agent_settings(request)[2])

    def test_model_hub_keeps_supported_codex_effort_and_does_not_filter_direct(self):
        from modules.agents.model_hub import ModelHubLaunch, bind_launch

        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, None, "high")),
            model_hub_runtime=object(),
        )
        request = SimpleNamespace(
            context=SimpleNamespace(),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        common = {
            "backend": "codex",
            "requested_model": "gpt-5",
            "target_model": "gpt-5",
            "runtime_model": "gpt-5",
        }

        bind_launch(
            request.context,
            ModelHubLaunch(channel="hub", reasoning_efforts=("high",), **common),
        )
        self.assertEqual(agent._resolve_codex_agent_settings(request)[2], "high")

        bind_launch(
            request.context,
            ModelHubLaunch(channel="direct", reasoning_efforts=(), **common),
        )
        self.assertEqual(agent._resolve_codex_agent_settings(request)[2], "high")

    async def test_start_turn_uses_codex_dm_user_effort_from_shared_overrides(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.settings_manager = SimpleNamespace(
            get_channel_settings=Mock(side_effect=AssertionError("Codex must not read scope storage directly"))
        )
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=(None, "gpt-5.5", "xhigh")),
        )
        agent.codex_config = SimpleNamespace(default_model="fallback-model")
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="discord::D123",
            base_session_id="session-1",
            composite_session_id="discord:D1:T1",
            context=SimpleNamespace(platform="discord", platform_specific={"is_dm": True}),
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
            working_path="/tmp/work",
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}))

        await agent._start_turn(transport, request, "thread-1")

        agent.controller.get_codex_overrides.assert_called_once_with(request.context)
        transport.send_request.assert_awaited_once_with(
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "model": "gpt-5.5",
                "effort": "xhigh",
            },
        )

    async def test_start_turn_uses_codex_agent_defaults_when_routing_selects_agent(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent.controller = SimpleNamespace(
            get_codex_overrides=Mock(return_value=("reviewer", None, None)),
        )
        agent.codex_config = SimpleNamespace(default_model="fallback-model")
        agent.ensure_agent_session_id = Mock()
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(),
            get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        request = SimpleNamespace(
            session_key="channel-1",
            base_session_id="session-1",
            composite_session_id="slack:C1:T1",
            context=SimpleNamespace(platform="slack", platform_specific={"is_dm": False}),
            working_path="/tmp/work",
            subagent_name=None,
            subagent_model=None,
            subagent_reasoning_effort=None,
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}))

        with patch.object(
            _MODULE,
            "load_codex_subagent",
            return_value=SimpleNamespace(
                developer_instructions="Focus on regressions.",
                model="gpt-5.4",
                reasoning_effort="high",
            ),
        ) as load_subagent:
            await agent._start_turn(transport, request, "thread-1")

        load_subagent.assert_called_once_with("reviewer", project_root=Path("/tmp/work"))
        transport.send_request.assert_awaited_once_with(
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "hello"}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "model": "gpt-5.4",
                "effort": "high",
            },
        )

class CodexTransportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_always_starts_app_server_with_global_bypass_flag(self):
        import importlib.util
        from pathlib import Path

        transport_path = Path(__file__).resolve().parents[1] / "modules/agents/codex/transport.py"
        spec = importlib.util.spec_from_file_location("test_codex_transport_module", transport_path)
        assert spec is not None and spec.loader is not None
        transport_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(transport_module)
        Transport = transport_module.CodexTransport

        writes = []
        created_cmds = []

        class _FakeStdin:
            def __init__(self):
                self._closing = False
                self._request_events = {
                    1: asyncio.Event(),
                    2: asyncio.Event(),
                }

            def write(self, data):
                writes.append(data.decode())
                message = json.loads(data)
                request_id = message.get("id")
                if request_id in self._request_events:
                    self._request_events[request_id].set()

            async def drain(self):
                return None

            def is_closing(self):
                return self._closing

            def close(self):
                self._closing = True

        class _FakeStdout:
            def __init__(self, stdin):
                self._stdin = stdin
                self._next_response_id = 1

            async def readline(self):
                if self._next_response_id <= 2:
                    response_id = self._next_response_id
                    await self._stdin._request_events[response_id].wait()
                    self._next_response_id += 1
                    return json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": response_id,
                            "result": {"data": []} if response_id == 2 else {},
                        }
                    ).encode() + b"\n"
                await asyncio.Event().wait()
                return b""

        class _FakeStderr:
            async def readline(self):
                return b""

        class _FakeProcess:
            def __init__(self):
                self.stdin = _FakeStdin()
                self.stdout = _FakeStdout(self.stdin)
                self.stderr = _FakeStderr()
                self.pid = 123
                self.returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            created_cmds.append(list(cmd))
            return _FakeProcess()

        with patch.object(
            transport_module.asyncio,
            "create_subprocess_exec",
            new=fake_create_subprocess_exec,
        ):
            transport = Transport(binary="codex", cwd="/tmp/work")
            await transport.start()
            await transport.stop()
            initialize_request = json.loads(writes[0])
            self.assertEqual(initialize_request["method"], "initialize")
            self.assertEqual(
                initialize_request["params"],
                {
                    "clientInfo": {
                        "name": "avibe",
                        "title": "Avibe",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            transport = Transport(
                binary="codex",
                cwd="/tmp/work",
                runtime_args=["-c", 'model_provider="avibe_model_hub"'],
            )
            await transport.start()
            await transport.stop()

        forced_args = [
            arg
            for override in transport_module.AVIBE_APP_SERVER_CONFIG_OVERRIDES
            for arg in ("-c", override)
        ]
        self.assertEqual(
            created_cmds,
            [
                [
                    "codex",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "app-server",
                    *forced_args,
                ],
                [
                    "codex",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "app-server",
                    "-c",
                    'model_provider="avibe_model_hub"',
                    *forced_args,
                ],
            ],
        )


class CodexGenerationAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    """Turns bind to the generation their launch spec needs, started on demand.

    #561: an app-server whose spawn directory was deleted (and possibly
    re-created at the same path) sits in a dead inode and fails every
    thread/start with "failed to load configuration"; the inode is part of
    the launch spec, so such a directory gets a new generation.
    """

    def _agent(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(sessions_for_cwd=lambda cwd: [])
        agent._turn_registry = SimpleNamespace(
            get_active_turn=Mock(return_value=None),
            has_pending_turn_start=Mock(return_value=False),
            clear_session=Mock(),
        )
        agent.codex_config = SimpleNamespace(binary="codex-acquisition-fixture", extra_args=[])
        agent.controller = SimpleNamespace(config=SimpleNamespace(codex=agent.codex_config))
        self.ownership = SimpleNamespace(
            blocks_transport_replacement=False,
            blocks_dead_transport_replacement=False,
        )

        async def ownership(generations):
            return tuple(self.ownership for _ in generations)

        agent._ownership_snapshots = ownership
        return agent

    @staticmethod
    def _fresh(**extra):
        return SimpleNamespace(
            is_initialized=True,
            pid=2468,
            start=AsyncMock(),
            stop=AsyncMock(),
            on_notification=Mock(),
            on_server_request=Mock(),
            **extra,
        )

    async def test_hfr_142_server_request_does_not_refresh_progress_activity(self):
        """HFR-142: protocol approval frames are not real Session progress."""
        agent = self._agent()
        generation = install_codex_transport(agent, "/tmp/work", SimpleNamespace(), last_activity=0.0)
        agent._session_last_activity = {}

        with patch.object(_MODULE.time, "monotonic", return_value=1234.0):
            result = await agent._on_server_request(
                "/tmp/work",
                7,
                "item/commandExecution/requestApproval",
                {"itemId": "item-1"},
            )

        self.assertEqual(result, {"approved": True})
        self.assertEqual(generation.runtime.last_activity, 0.0)
        self.assertEqual(agent._session_last_activity, {})

    async def test_request_user_input_returns_valid_empty_answers(self):
        agent = self._agent()

        result = await agent._on_server_request(
            "/tmp/work",
            8,
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "isBlocking": True,
                "questions": [],
            },
        )

        self.assertEqual(result, {"answers": {}})

    async def test_current_time_request_returns_protocol_shape(self):
        agent = self._agent()

        with patch.object(_MODULE.time, "time", return_value=1234.9):
            result = await agent._on_server_request(
                "/tmp/work",
                9,
                "currentTime/read",
                {"threadId": "thread-1"},
            )

        self.assertEqual(result, {"currentTimeAt": 1234})

    async def test_unhandled_experimental_server_request_is_rejected(self):
        agent = self._agent()

        with self.assertRaisesRegex(
            NotImplementedError,
            "Unsupported Codex server request: item/permissions/requestApproval",
        ):
            await agent._on_server_request(
                "/tmp/work",
                10,
                "item/permissions/requestApproval",
                {"itemId": "item-1"},
            )

    def test_turn_start_refreshes_the_sessions_generation_clock(self):
        agent = self._agent()
        generation = install_codex_transport(
            agent, "/tmp/work", SimpleNamespace(), sessions={"session-1": "thread-1"}, last_activity=0.0
        )
        agent._session_last_activity = {"session-1": 0.0}
        request = SimpleNamespace(
            working_path="/tmp/work",
            base_session_id="session-1",
        )

        with patch.object(_MODULE.time, "monotonic", return_value=1234.0):
            agent.record_runtime_turn_start(
                runtime_key="session:/tmp/work",
                request=request,
            )

        self.assertEqual(generation.runtime.last_activity, 1234.0)
        self.assertEqual(agent._session_last_activity, {"session-1": 1234.0})

    async def test_hfr_142_server_request_callback_does_not_create_progress(self):
        """HFR-142: the generation-bound callback preserves the prior progress clock."""
        agent = self._agent()
        captured = {}

        with tempfile.TemporaryDirectory() as cwd:
            fresh = self._fresh()
            fresh.on_server_request = Mock(side_effect=lambda cb: captured.update(cb=cb))
            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                binding = await agent._acquire_generation(cwd)

            self.assertIn("cb", captured)
            before = binding.generation.runtime.last_activity
            with patch.object(_MODULE.time, "monotonic", return_value=999.0):
                result = await captured["cb"](1, "item/fileChange/requestApproval", {"itemId": "x"})

            self.assertEqual(result, {"approved": True})
            self.assertEqual(binding.generation.runtime.last_activity, before)

    async def test_acquire_moves_app_server_into_agent_cgroup(self):
        agent = self._agent()
        calls = []
        with tempfile.TemporaryDirectory() as cwd:
            fresh = self._fresh()
            with (
                patch.object(_MODULE, "CodexTransport", return_value=fresh),
                patch.object(
                    _MODULE,
                    "governor_from_controller",
                    return_value=SimpleNamespace(
                        apply_to_pid=lambda pid, label="agent": calls.append((pid, label)) or True
                    ),
                ),
            ):
                binding = await agent._acquire_generation(cwd)

            self.assertIs(binding.generation.runtime.transport, fresh)
            self.assertEqual(calls, [(2468, "codex app-server")])

    async def test_acquire_launches_with_private_codex_helpers(self):
        agent = self._agent()
        agent.codex_config.binary = "/private/release/vendor/target/bin/codex"
        managed_env = {"PATH": "/private/release/vendor/target/codex-path:/usr/bin"}
        with tempfile.TemporaryDirectory() as cwd:
            with (
                patch.object(_MODULE, "CodexTransport", return_value=self._fresh()) as constructor,
                patch.object(
                    _MODULE,
                    "desktop_backend_subprocess_environment",
                    return_value=managed_env,
                ),
            ):
                await agent._acquire_generation(cwd)

        self.assertEqual(constructor.call_args.kwargs["runtime_env"], managed_env)

    async def test_model_hub_transport_preserves_private_codex_helpers(self):
        agent = self._agent()
        agent.codex_config.binary = "/private/release/vendor/target/bin/codex"
        managed_env = {
            "PATH": "/private/release/vendor/target/codex-path:/usr/bin",
            "OPENAI_API_KEY": "native-token",
        }
        launch = SimpleNamespace(
            channel="hub",
            gateway_base_url="http://127.0.0.1:18443",
            gateway_token="gateway-token",
        )
        with tempfile.TemporaryDirectory() as cwd:
            with (
                patch.object(_MODULE, "CodexTransport", return_value=self._fresh()) as constructor,
                patch.object(
                    _MODULE,
                    "desktop_backend_subprocess_environment",
                    return_value=managed_env,
                ),
                patch(
                    "vibe.backend_model_catalog.prepare_codex_hub_catalog",
                    return_value=_catalog_reference(Path(cwd) / "codex-hub-catalog.json"),
                ) as prepare_catalog,
            ):
                await agent._acquire_generation(cwd, launch)

        runtime_env = constructor.call_args.kwargs["runtime_env"]
        self.assertEqual(runtime_env["PATH"], managed_env["PATH"])
        self.assertEqual(runtime_env["AVIBE_MODEL_HUB_TOKEN"], "gateway-token")
        self.assertNotIn("OPENAI_API_KEY", runtime_env)
        prepare_catalog.assert_called_once_with(agent.codex_config.binary, None, None)

    async def test_cached_transport_reused_while_launch_inputs_are_unchanged(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            cached = self._fresh()
            with patch.object(_MODULE, "CodexTransport", return_value=cached) as ctor:
                first = await agent._acquire_generation(cwd)
                await first.release()
                second = await agent._acquire_generation(cwd)

            self.assertIs(second.generation, first.generation)
            cached.stop.assert_not_awaited()
            ctor.assert_called_once()

    async def test_replaced_cwd_inode_starts_a_new_generation(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            stale = self._fresh()
            with patch.object(_MODULE, "CodexTransport", return_value=stale):
                await (await agent._acquire_generation(cwd)).release()
            inode = os.stat(cwd).st_ino
            fresh = self._fresh()
            with (
                patch.object(_MODULE, "CodexTransport", return_value=fresh),
                patch.object(CodexAgent, "_cwd_inode", staticmethod(lambda _cwd: inode + 1)),
            ):
                binding = await agent._acquire_generation(cwd)
                await agent._units[cwd].settled()

            self.assertIs(binding.generation.runtime.transport, fresh)
            fresh.start.assert_awaited_once()
            # The idle predecessor stops once the new one serves.
            stale.stop.assert_awaited_once()

    async def test_replaced_cwd_keeps_a_predecessor_with_a_durable_owner(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            stale = self._fresh()
            with patch.object(_MODULE, "CodexTransport", return_value=stale):
                previous = await agent._acquire_generation(cwd)
                await previous.release()
            self.ownership.blocks_transport_replacement = True
            fresh = self._fresh()
            inode = os.stat(cwd).st_ino
            with (
                patch.object(_MODULE, "CodexTransport", return_value=fresh),
                patch.object(CodexAgent, "_cwd_inode", staticmethod(lambda _cwd: inode + 1)),
            ):
                binding = await agent._acquire_generation(cwd)

            self.assertIs(binding.generation.runtime.transport, fresh)
            stale.stop.assert_not_awaited()
            self.assertIn(previous.generation, agent._units[cwd].generations)
            self.assertTrue(previous.generation.retiring)

    async def test_hfr_473_dead_transport_restarts_past_stale_turn_ownership(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            dead = SimpleNamespace(
                is_initialized=False,
                is_alive=False,
                has_pending_notifications=False,
                stop=AsyncMock(),
            )
            self.ownership.blocks_transport_replacement = True
            agent._session_mgr = SimpleNamespace(
                sessions_for_cwd=Mock(return_value=["session-1"]),
                invalidate_thread=Mock(),
            )
            agent._turn_registry.get_active_turn = Mock(return_value="turn-from-dead-generation")
            agent._clear_thread_developer_instructions = Mock()
            install_codex_transport(agent, cwd, dead, sessions={"session-1": "thread-1"})
            fresh = self._fresh()

            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                binding = await agent._acquire_generation(cwd)
                await agent._units[cwd].settled()

            self.assertIs(binding.generation.runtime.transport, fresh)
            dead.stop.assert_awaited_once()
            fresh.start.assert_awaited_once()
            agent._session_mgr.invalidate_thread.assert_called_once_with("session-1")
            agent._turn_registry.clear_session.assert_called_once_with("session-1")

    async def test_dead_transport_with_an_activity_owner_never_holds_new_turns(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            dead = SimpleNamespace(
                is_initialized=False,
                is_alive=False,
                has_pending_notifications=False,
                stop=AsyncMock(),
            )
            self.ownership.blocks_transport_replacement = True
            self.ownership.blocks_dead_transport_replacement = True
            dead_generation = install_codex_transport(agent, cwd, dead)
            fresh = self._fresh()

            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                binding = await agent._acquire_generation(cwd)

            self.assertIs(binding.generation.runtime.transport, fresh)
            dead.stop.assert_not_awaited()
            self.assertIn(dead_generation, agent._units[cwd].generations)

    async def test_runtime_change_keeps_a_predecessor_with_a_pid_run_owner(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            existing = self._fresh()
            with patch.object(_MODULE, "CodexTransport", return_value=existing):
                previous = await agent._acquire_generation(cwd)
                await previous.release()
            self.ownership.blocks_transport_replacement = True
            launch = SimpleNamespace(
                channel="hub",
                gateway_base_url="http://127.0.0.1:8317",
                gateway_token="ephemeral-token",
            )
            fresh = self._fresh()

            with (
                patch.object(_MODULE, "CodexTransport", return_value=fresh),
                patch(
                    "vibe.backend_model_catalog.prepare_codex_hub_catalog",
                    return_value=_catalog_reference(Path(cwd) / "codex-hub-catalog.json"),
                ),
            ):
                binding = await agent._acquire_generation(cwd, launch)

            self.assertIs(binding.generation.runtime.transport, fresh)
            existing.stop.assert_not_awaited()
            self.assertTrue(previous.generation.retiring)

    async def test_renewal_switches_binary_without_catalog_export(self):
        agent = self._agent()
        transport = SimpleNamespace(stop=AsyncMock())
        install_codex_transport(agent, "/repo", transport)
        next_config = SimpleNamespace(binary="/opt/codex-next", extra_args=[])

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=RuntimeError("catalog export must not run"),
        ) as prepare_catalog:
            await agent.renew_runtime(next_config)

        prepare_catalog.assert_not_called()
        transport.stop.assert_not_awaited()
        self.assertIs(agent.codex_config, next_config)
        self.assertIs(agent.controller.config.codex, next_config)
        self.assertEqual(agent._runtime_epoch, 1)

    async def test_plain_config_save_adopts_config_without_renewing(self):
        agent = self._agent()
        next_config = SimpleNamespace(binary=agent.codex_config.binary, extra_args=[], idle_timeout_seconds=60)

        await agent.renew_runtime(next_config, config_save=True)

        self.assertIs(agent.codex_config, next_config)
        self.assertEqual(agent._runtime_epoch, 0)

    async def test_runtime_config_refresh_stops_everything_without_catalog_export(self):
        agent = self._agent()
        previous_catalog = _catalog_reference("/runtime/codex-old.json")
        agent._model_hub_catalogs[("old", "models")] = previous_catalog
        agent.refresh_auth_state = AsyncMock()
        next_config = SimpleNamespace(binary=agent.codex_config.binary, extra_args=["--next"])

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=RuntimeError("catalog export must not run"),
        ) as prepare_catalog:
            await agent.refresh_runtime_config(next_config)

        prepare_catalog.assert_not_called()
        self.assertIs(agent.codex_config, next_config)
        self.assertEqual(dict(agent._model_hub_catalogs), {})
        agent.refresh_auth_state.assert_awaited_once_with()

    async def test_model_hub_catalog_adoption_preserves_running_transports(self):
        agent = self._agent()
        transport = SimpleNamespace(stop=AsyncMock())
        install_codex_transport(agent, "/repo", transport)
        agent._model_hub_catalogs[("old", "models")] = _catalog_reference("/runtime/codex-old.json")

        await agent.adopt_model_hub_catalog()

        self.assertEqual(dict(agent._model_hub_catalogs), {})
        transport.stop.assert_not_awaited()
        self.assertIs(codex_transports(agent)["/repo"], transport)

    async def test_catalog_prepared_for_a_previous_binary_stays_keyed_to_it(self):
        agent = self._agent()
        previous_config = agent.codex_config
        previous_catalog = _catalog_reference("/runtime/codex-old.json")
        next_catalog = _catalog_reference("/runtime/codex-new.json")
        next_config = SimpleNamespace(binary="/opt/codex-next", extra_args=[])
        previous_started = threading.Event()
        release_previous = threading.Event()
        calls = []

        def prepare(binary, base_env, configured_models):
            calls.append((binary, base_env, configured_models))
            if binary == previous_config.binary:
                previous_started.set()
                release_previous.wait(timeout=2)
                return previous_catalog
            return next_catalog

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=prepare,
        ):
            startup = asyncio.create_task(agent.prepare_model_hub_runtime())
            self.assertTrue(await asyncio.to_thread(previous_started.wait, 1))
            await agent.renew_runtime(next_config)
            release_previous.set()
            # The turn that asked first keeps a catalog for the binary it captured.
            self.assertIs(await startup, previous_catalog)
            recovered = await agent.prepare_model_hub_runtime()

        self.assertEqual(
            calls,
            [
                (previous_config.binary, None, None),
                (next_config.binary, None, None),
            ],
        )
        self.assertIs(recovered, next_catalog)

    async def test_model_hub_catalog_preparation_retries_after_transient_failure(self):
        agent = self._agent()
        catalog = _catalog_reference("/runtime/codex-recovered.json")

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=[RuntimeError("transient export failure"), catalog],
        ) as prepare_catalog:
            with self.assertRaises(_MODULE.CodexModelHubCatalogUnavailableError):
                await agent.prepare_model_hub_runtime()
            self.assertEqual(dict(agent._model_hub_catalogs), {})
            recovered = await agent.prepare_model_hub_runtime()

        self.assertEqual(recovered, catalog)
        self.assertEqual(list(agent._model_hub_catalogs.values()), [catalog])
        self.assertEqual(prepare_catalog.call_count, 2)

    async def test_missing_prepared_hub_catalog_preserves_existing_transport_and_threads(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            existing = SimpleNamespace(is_initialized=True, stop=AsyncMock())
            install_codex_transport(agent, cwd, existing, sessions={"session-1": "thread-1"})
            agent._session_mgr = SimpleNamespace(
                sessions_for_cwd=Mock(return_value=["session-1"]),
                invalidate_thread=Mock(),
            )
            agent._clear_thread_developer_instructions = Mock()
            launch = SimpleNamespace(
                channel="hub",
                gateway_base_url="http://127.0.0.1:8317",
                gateway_token="ephemeral-token",
            )

            with patch(
                "vibe.backend_model_catalog.prepare_codex_hub_catalog",
                side_effect=RuntimeError("transient export failure"),
            ) as prepare_catalog:
                with self.assertRaises(_MODULE.CodexModelHubCatalogUnavailableError):
                    await agent._acquire_generation(cwd, launch)

            prepare_catalog.assert_called_once_with(
                agent.codex_config.binary,
                None,
                None,
            )
            existing.stop.assert_not_awaited()
            self.assertIs(codex_transports(agent)[cwd], existing)
            self.assertIs(agent.transport_for_session("session-1"), existing)
            agent._session_mgr.invalidate_thread.assert_not_called()
            agent._turn_registry.clear_session.assert_not_called()

    async def test_predecessor_stop_failure_retains_its_exact_generation(self):
        agent = self._agent()
        activation = RuntimeActivationRegistry()
        agent.controller.runtime_activation = activation
        with tempfile.TemporaryDirectory() as cwd:
            observed_current = []
            stale = self._fresh()
            with patch.object(_MODULE, "CodexTransport", return_value=stale):
                previous = await agent._acquire_generation(cwd)
                await previous.release()
            identity = previous.generation.runtime.activation

            async def stop_stale():
                observed_current.append(activation.is_current(identity))
                raise RuntimeError("stop failed")

            stale.stop = stop_stale
            inode = os.stat(cwd).st_ino
            with (
                patch.object(_MODULE, "CodexTransport", return_value=self._fresh()),
                patch.object(CodexAgent, "_cwd_inode", staticmethod(lambda _cwd: inode + 1)),
            ):
                await agent._acquire_generation(cwd)
                await agent._units[cwd].settled()

            self.assertEqual(observed_current, [False])
            self.assertIn(previous.generation, agent._units[cwd].generations)
            self.assertTrue(previous.generation.closed)
            self.assertTrue(activation.is_current(identity))

    def test_config_load_failure_is_recoverable(self):
        agent = init_generation_state(object.__new__(CodexAgent))
        err = RuntimeError(
            "Codex RPC error: {'code': -32600, 'message': "
            "'failed to load configuration: No such file or directory (os error 2)'}"
        )
        self.assertTrue(agent._is_recoverable_transport_error(err))


class CodexPromptSnapshotRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def _setup(self, strategy=None, *, legacy=False):
        self.marker = {}
        if strategy:
            prompt = "stable prompt"
            fingerprint = (
                hashlib.sha256(prompt.encode()).hexdigest()
                if legacy else CodexAgent._prompt_fingerprint(prompt)
            )
            self.marker.update(thread_id="thread-1", strategy=strategy, sha256=fingerprint)
        self.original_marker = dict(self.marker)
        self.request = SimpleNamespace(
            session_key="channel-1", base_session_id="session-1",
            composite_session_id="avibe:session-1", subagent_name=None,
            context=SimpleNamespace(platform_specific={}),
        )
        self.transport = SimpleNamespace(
            supports_turn_collaboration_mode=True,
            send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}),
        )
        return self._agent()

    def _agent(self):
        agent = init_generation_state(object.__new__(CodexAgent))

        def persist(*_args, **kwargs):
            self.marker.clear()
            self.marker.update(kwargs["value"] or {})
            return True

        agent.sessions = SimpleNamespace(
            get_agent_session_runtime_marker=lambda *_args, **_kwargs: dict(self.marker) or None,
            set_agent_session_runtime_marker=Mock(side_effect=persist),
        )
        agent.ensure_agent_session_id = Mock(return_value="ses-runtime")
        agent._resolve_codex_agent_settings = Mock(return_value=(None, "gpt-5.4", "high", None))
        agent._build_input = Mock(return_value=[{"type": "text", "text": "hello"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(), finalize_turn_start_response=Mock(),
        )
        return agent

    async def test_legacy_fallback_snapshots_migrate_once_including_fork_and_restart(self):
        for strategy in ("fallback", "fallback_pending_clear"):
            for forked in (False, True):
                with self.subTest(strategy=strategy, forked=forked):
                    agent = self._setup(strategy, legacy=True)
                    thread_id = "thread-1"
                    if forked:
                        agent._inject_caller_env_config = Mock(return_value=("path", True))
                        agent._should_trim_forked_running_turn = AsyncMock(return_value=False)
                        agent._inject_forked_session_correction = AsyncMock()
                        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
                        agent.bind_agent_session_id = Mock(return_value="ses-runtime")
                        agent._caller_env_for_request = Mock(return_value={})
                        self.request.working_path = "/tmp/work"
                        self.transport.send_request.return_value = {"thread": {"id": "thread-fork"}}
                        thread_id = await agent._fork_thread(self.transport, self.request, {
                            "source_session_id": "source", "source_native_session_id": "thread-1",
                        })
                        self.assertEqual(self.marker["sha256"], self.original_marker["sha256"])
                        self.transport.send_request.reset_mock()
                        self.transport.send_request.return_value = {"turn": {"id": "turn-1"}}
                    for restart in (False, True):
                        if restart:
                            agent = self._agent()
                        await agent._start_turn(
                            self.transport, self.request, thread_id,
                            developer_instructions="stable prompt",
                        )
                    injections = [
                        call for call in self.transport.send_request.await_args_list
                        if call.args[0] == "thread/inject_items"
                    ]
                    self.assertEqual(len(injections), 1)
                    self.assertEqual(
                        injections[0].args[1]["items"][0]["content"][0]["text"],
                        CodexAgent._render_developer_prompt_snapshot("stable prompt"),
                    )
                    self.assertEqual(self.marker["strategy"], "fallback")
                    self.assertEqual(self.marker["sha256"], CodexAgent._prompt_fingerprint("stable prompt"))

    async def test_previous_envelope_migrates_once_without_changing_prompt_body(self):
        agent = self._setup("fallback")
        old_snapshot = (
            "<avibe_runtime_instructions>\n"
            "Previous snapshot replacement declaration.\n\n"
            "stable prompt\n</avibe_runtime_instructions>"
        )
        self.marker["sha256"] = hashlib.sha256(old_snapshot.encode()).hexdigest()
        for restart in (False, False, True):
            if restart:
                agent = self._agent()
            await agent._start_turn(
                self.transport, self.request, "thread-1",
                developer_instructions="stable prompt",
            )
        injections = [
            entry for entry in self.transport.send_request.await_args_list
            if entry.args[0] == "thread/inject_items"
        ]
        self.assertEqual(len(injections), 1)
        self.assertEqual(
            injections[0].args[1]["items"][0]["content"][0]["text"],
            "<avibe_runtime_instructions>\n\nstable prompt\n</avibe_runtime_instructions>",
        )
        self.assertEqual(self.marker["sha256"], CodexAgent._prompt_fingerprint("stable prompt"))

    async def test_rejected_injection_restores_marker_and_retries_before_dispatch(self):
        for strategy in (None, "fallback", "collaboration", "fallback_pending_clear"):
            for restart in (False, True):
                for code in (-32600, -32601, -32602):
                    with self.subTest(strategy=strategy, restart=restart, code=code):
                        agent = self._setup(strategy)
                        self.transport.send_request.side_effect = CodexRPCError({"code": code, "message": "rejected"})
                        with self.assertRaisesRegex(CodexPromptRefreshUnavailableError, "rejected"):
                            await agent._start_turn(
                                self.transport, self.request, "thread-1",
                                developer_instructions="changed prompt",
                            )
                        self.assertEqual(self.marker, self.original_marker)
                        agent._turn_registry.begin_turn_start.assert_not_called()
                        if restart:
                            agent = self._agent()
                        self.transport.send_request.side_effect = None
                        await agent._start_turn(
                            self.transport, self.request, "thread-1",
                            developer_instructions="changed prompt",
                        )
                        calls = self.transport.send_request.await_args_list
                        self.assertEqual([c.args[0] for c in calls], [
                            "thread/inject_items", "thread/inject_items", "turn/start",
                        ])
                        if strategy in {"collaboration", "fallback_pending_clear"}:
                            self.assertIsNone(calls[-1].args[1]["collaborationMode"])
                        self.assertEqual(self.marker["strategy"], "fallback")

    async def test_internal_rpc_error_keeps_ambiguous_injection_marker(self):
        agent = self._setup()
        self.transport.send_request.side_effect = CodexRPCError({"code": -32603, "message": "internal error"})
        with self.assertRaises(CodexRPCError):
            await agent._start_turn(
                self.transport, self.request, "thread-1", developer_instructions="stable prompt",
            )
        self.assertEqual(self.marker["strategy"], "fallback_pending_injection")
        agent._turn_registry.begin_turn_start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
