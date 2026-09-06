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
from core.runtime_activation import RuntimeActivationRegistry
from modules.agents.base import BaseAgent as RealBaseAgent
from modules.agents.codex.transport import CodexRPCError

_AGENT_PATH = Path(__file__).resolve().parents[1] / "modules/agents/codex/agent.py"

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
        ensure = getattr(self.sessions, "ensure_agent_session_id", None)
        if callable(ensure):
            session_id = ensure(request.session_key, self.name, anchor)
        else:
            getter = getattr(self.sessions, "get_agent_session_row_id", None)
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
_SPEC.loader.exec_module(_MODULE)
CodexAgent = _MODULE.CodexAgent
CODEX_PROMPT_STRATEGY_METADATA_KEY = _MODULE.CODEX_PROMPT_STRATEGY_METADATA_KEY
CodexConnectionProbeRuntimeMismatchError = (
    _MODULE.CodexConnectionProbeRuntimeMismatchError
)
CodexPromptRefreshUnavailableError = _MODULE.CodexPromptRefreshUnavailableError
CodexResumeUnavailableError = _MODULE.CodexResumeUnavailableError

for name, module in _saved_modules.items():
    if module is None:
        sys.modules.pop(name, None)
    else:
        sys.modules[name] = module


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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request

        resolved = agent._find_request_for_notification("thread/started", {"threadId": "thread-1"})

        self.assertIs(resolved, request)

    def test_find_request_does_not_fall_back_to_thread_when_turn_is_unknown(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
        agent._session_mgr = _StubSessionManager()
        agent._turn_registry = _StubTurnRegistry()

        request = SimpleNamespace(base_session_id="session-1", context="current")
        agent._session_mgr._threads["session-1"] = "thread-1"
        agent._turn_registry._latest_requests["session-1"] = request
        agent._turn_registry._pending_requests["session-1"] = request

        resolved = agent._find_request_for_notification("item/completed", {"threadId": "thread-1", "turnId": "turn-1"})

        self.assertIsNone(resolved)


class CodexAgentConnectionProbeTests(unittest.IsolatedAsyncioTestCase):
    def _agent(self, cwd: str, transport):
        agent = object.__new__(CodexAgent)
        agent._transports = {cwd: transport}
        agent._transport_last_activity = {}
        agent._connection_probes = {}
        agent._connection_probe_turns = {}
        agent._connection_probe_cwds = {}
        agent.can_reuse_direct_connection_probe = Mock(
            return_value=getattr(transport, "runtime_fingerprint", "direct") == "direct"
        )
        agent._get_or_create_transport = AsyncMock(return_value=transport)
        return agent

    def test_live_probe_requires_a_cached_direct_transport_and_existing_cwd(self):
        agent = object.__new__(CodexAgent)
        agent._transports = {}
        with tempfile.TemporaryDirectory() as cwd:
            self.assertFalse(agent.can_reuse_direct_connection_probe(cwd))

            direct = SimpleNamespace(runtime_fingerprint="direct")
            agent._transports[cwd] = direct
            self.assertTrue(agent.can_reuse_direct_connection_probe(cwd))

            direct.runtime_fingerprint = "hub:openai/gpt-5.4-mini"
            self.assertFalse(agent.can_reuse_direct_connection_probe(cwd))

            direct.runtime_fingerprint = "direct"
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

        async def acquire_transport(_cwd, *, allow_runtime_replacement):
            self.assertEqual(agent._connection_probe_cwds, {})
            self.assertFalse(allow_runtime_replacement)
            return transport

        agent._get_or_create_transport = AsyncMock(side_effect=acquire_transport)

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
        agent._get_or_create_transport.assert_awaited_once_with(
            cwd,
            allow_runtime_replacement=False,
        )
        self.assertEqual(requests[0][0], "thread/start")
        self.assertEqual(
            requests[0][1],
            {
                "cwd": str(Path(runtime_dir.name) / "codex-connection-probe"),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
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
        self.assertEqual(agent._connection_probe_cwds, {})
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
            await agent.probe_connection(cwd)

        self.assertEqual(agent._connection_probes, {})
        self.assertEqual(agent._connection_probe_turns, {})
        self.assertEqual(agent._connection_probe_cwds, {})

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
            task = asyncio.create_task(agent.probe_connection(cwd))
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
        self.assertEqual(agent._connection_probe_cwds, {})

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
                    on_diagnostic=diagnostics.append,
                )

        self.assertEqual(diagnostics, ["401 Unauthorized"])

    async def test_rejects_initialized_model_hub_transport(self):
        cwd = "/tmp/user-project"
        transport = SimpleNamespace(
            is_initialized=True,
            runtime_fingerprint="hub:openai/gpt-5.4-mini",
        )
        agent = self._agent(cwd, transport)

        with self.assertRaises(CodexConnectionProbeRuntimeMismatchError):
            await agent.probe_connection(cwd)

        self.assertEqual(agent._connection_probe_cwds, {})

    async def test_runtime_fingerprint_race_does_not_replace_model_hub_transport(self):
        cwd = "/tmp/user-project"
        transport = SimpleNamespace(is_initialized=True, runtime_fingerprint="direct")
        agent = self._agent(cwd, transport)
        agent._get_or_create_transport = AsyncMock(
            side_effect=CodexConnectionProbeRuntimeMismatchError("runtime changed")
        )

        with self.assertRaises(CodexConnectionProbeRuntimeMismatchError):
            await agent.probe_connection(cwd)

        agent._get_or_create_transport.assert_awaited_once_with(
            cwd,
            allow_runtime_replacement=False,
        )
        self.assertEqual(agent._connection_probe_cwds, {})

    async def test_unregistered_probe_runtime_preserves_live_activation(self):
        cwd = "/tmp/user-project"
        activation = RuntimeActivationRegistry()
        live_identity = activation.attach("codex", cwd)
        retire_scope = Mock()
        transport = SimpleNamespace(stop=AsyncMock())
        agent = object.__new__(CodexAgent)
        agent._registered_runtime = False
        agent._transports = {cwd: transport}
        agent._transport_last_activity = {cwd: 1.0}
        agent._transport_locks = {cwd: asyncio.Lock()}
        agent._transport_cwd_inodes = {cwd: 1}
        agent._session_last_activity = {}
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.sessions = SimpleNamespace()
        agent.controller = SimpleNamespace(
            runtime_activation=activation,
            model_hub_runtime=SimpleNamespace(retire_process_scope=retire_scope),
        )

        self.assertIsNone(agent._attach_transport_activation(cwd, transport))
        await agent.shutdown_runtime()

        transport.stop.assert_awaited_once_with()
        self.assertIs(activation.current("codex", cwd), live_identity)
        retire_scope.assert_not_called()


class CodexAgentStopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ownership = SimpleNamespace(
            disposition="reclaimable",
            blocks_reclamation=False,
        )
        patcher = patch.object(
            CodexAgent,
            "_runtime_ownership_snapshot_for_cwd",
            new=lambda _agent, _cwd: ownership,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_handle_stop_does_not_hide_turn_before_interrupt_succeeds(self):
        agent = object.__new__(CodexAgent)
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()
        transport = SimpleNamespace(is_alive=True, send_request=AsyncMock(side_effect=RuntimeError("boom")))
        agent._transports = {"/tmp": transport}
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
        agent = object.__new__(CodexAgent)
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

        agent._transports = {"/tmp": SimpleNamespace(is_alive=True, send_request=send_request)}
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

    async def test_handle_stop_does_not_cancel_a_turn_that_completed_during_rpc(self):
        agent = object.__new__(CodexAgent)
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
        agent._user_stopped_turn_ids = set()
        agent._transports = {"/tmp": SimpleNamespace(is_alive=True, send_request=AsyncMock(return_value={}))}
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
        agent = object.__new__(CodexAgent)
        stop_calls = []
        retire_scope = Mock()

        async def stop_a():
            stop_calls.append("a")

        async def stop_b():
            stop_calls.append("b")

        invalidated = []
        cleared_sessions = []
        agent._transports = {
            "/tmp/a": SimpleNamespace(stop=stop_a),
            "/tmp/b": SimpleNamespace(stop=stop_b),
        }
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
        self.assertEqual(agent._transports, {})
        self.assertEqual(invalidated, ["session-1", "session-2"])
        self.assertEqual(cleared_sessions, ["session-1", "session-2"])
        self.assertEqual(
            retire_scope.call_args_list,
            [
                call("codex", "/tmp/a"),
                call("codex", "/tmp/b"),
            ],
        )

    async def test_refresh_auth_state_retires_generation_before_transport_stop(self):
        agent = object.__new__(CodexAgent)
        activation = RuntimeActivationRegistry()
        transport = SimpleNamespace()
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._session_last_activity = {}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_cwd_inodes = {"/tmp/work": 1}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = agent._attach_transport_activation("/tmp/work", transport)
        late_commit = Mock(return_value="started")

        async def stop_transport():
            result = activation.commit_if_current(identity, late_commit)
            self.assertFalse(result.admitted)

        transport.stop = stop_transport

        await agent.refresh_auth_state()

        late_commit.assert_not_called()
        self.assertNotIn("/tmp/work", agent._transports)
        self.assertIsNone(activation.current("codex", "/tmp/work"))

    async def test_prepare_resume_binding_restarts_unshared_transport(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []
        retire_scope = Mock()

        async def stop_transport():
            stop_calls.append("stop")

        transport = SimpleNamespace(stop=stop_transport)
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        invalidated = []
        cleared_sessions = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(clear_session=lambda base_session_id: cleared_sessions.append(base_session_id))
        agent.controller = SimpleNamespace(
            model_hub_runtime=SimpleNamespace(
                retire_process_scope=retire_scope,
            )
        )

        await agent.prepare_resume_binding(
            base_session_id="session-1",
            session_key="scope-1",
            working_path="/tmp/work",
        )

        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(agent._transports, {})
        self.assertEqual(agent._transport_last_activity, {})
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared_sessions, ["session-1"])
        retire_scope.assert_called_once_with("codex", "/tmp/work")

    async def test_prepare_resume_binding_skips_shared_transport(self):
        agent = object.__new__(CodexAgent)
        stop_transport = AsyncMock()
        transport = SimpleNamespace(stop=stop_transport)
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        invalidated = []
        cleared_sessions = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1", "session-2"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(clear_session=lambda base_session_id: cleared_sessions.append(base_session_id))

        await agent.prepare_resume_binding(
            base_session_id="session-1",
            session_key="scope-1",
            working_path="/tmp/work",
        )

        stop_transport.assert_not_awaited()
        self.assertIs(agent._transports["/tmp/work"], transport)
        self.assertEqual(agent._transport_last_activity, {"/tmp/work": 1.0})
        self.assertEqual(invalidated, [])
        self.assertEqual(cleared_sessions, [])

    async def test_prepare_resume_binding_retains_generation_when_stop_fails(self):
        agent = object.__new__(CodexAgent)
        activation = RuntimeActivationRegistry()
        transport = SimpleNamespace()
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._transport_cwd_inodes = {"/tmp/work": 1}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        invalidated = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(clear_session=Mock())
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = agent._attach_transport_activation("/tmp/work", transport)
        late_commit = Mock(return_value="started")

        async def stop_transport():
            self.assertFalse(
                activation.commit_if_current(identity, late_commit).admitted
            )
            raise RuntimeError("stop failed")

        transport.stop = stop_transport

        await agent.prepare_resume_binding(
            base_session_id="session-1",
            session_key="scope-1",
            working_path="/tmp/work",
        )

        late_commit.assert_not_called()
        self.assertIs(agent._transports["/tmp/work"], transport)
        self.assertEqual(invalidated, [])
        self.assertIs(activation.current("codex", "/tmp/work"), identity)
        self.assertTrue(activation.commit_if_current(identity, late_commit).admitted)
        late_commit.assert_called_once_with()

    async def test_shutdown_runtime_retires_generation_before_transport_stop(self):
        agent = object.__new__(CodexAgent)
        activation = RuntimeActivationRegistry()
        transport = SimpleNamespace()
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._session_last_activity = {}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_cwd_inodes = {"/tmp/work": 1}
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(all_base_sessions=lambda: [])
        agent._turn_registry = SimpleNamespace()
        agent.sessions = SimpleNamespace()
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = agent._attach_transport_activation("/tmp/work", transport)
        late_commit = Mock(return_value="started")

        async def stop_transport():
            self.assertFalse(
                activation.commit_if_current(identity, late_commit).admitted
            )

        transport.stop = stop_transport

        await agent.shutdown_runtime()

        late_commit.assert_not_called()
        self.assertNotIn("/tmp/work", agent._transports)
        self.assertIsNone(activation.current("codex", "/tmp/work"))

    def test_request_activation_resolves_one_live_session_key_transport(self):
        agent = object.__new__(CodexAgent)
        activation = RuntimeActivationRegistry()
        live = SimpleNamespace()
        agent._transports = {"/tmp/live": live}
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-dead", "base-live"],
            get_cwd=lambda base_session_id: {
                "base-dead": "/tmp/dead",
                "base-live": "/tmp/live",
            }[base_session_id],
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = agent._attach_transport_activation("/tmp/live", live)

        resolved = agent.runtime_activation_identity_for_request(
            SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
        )

        self.assertEqual(resolved, identity)

    def test_request_activation_fails_closed_for_multiple_live_session_key_transports(self):
        agent = object.__new__(CodexAgent)
        activation = RuntimeActivationRegistry()
        first = SimpleNamespace()
        second = SimpleNamespace()
        agent._transports = {"/tmp/a": first, "/tmp/b": second}
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-a", "base-b"],
            get_cwd=lambda base_session_id: {
                "base-a": "/tmp/a",
                "base-b": "/tmp/b",
            }[base_session_id],
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)
        agent._attach_transport_activation("/tmp/a", first)
        agent._attach_transport_activation("/tmp/b", second)

        with self.assertRaisesRegex(ValueError, "multiple live Codex runtime"):
            agent.runtime_activation_identity_for_request(
                SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
            )

    def test_request_activation_returns_none_when_session_key_has_no_live_transport(self):
        agent = object.__new__(CodexAgent)
        agent._transports = {}
        agent._session_mgr = SimpleNamespace(
            get_sessions_by_session_key=lambda session_key: ["base-dead"],
            get_cwd=lambda base_session_id: "/tmp/dead",
        )

        resolved = agent.runtime_activation_identity_for_request(
            SimpleNamespace(working_path=None, metadata={}, session_key="route:base")
        )

        self.assertIsNone(resolved)

    async def test_evict_idle_transports_stops_idle_codex_runtime(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []
        invalidated_sessions = []
        cleared_turns = []
        retire_scope = Mock()

        async def stop_transport():
            stop_calls.append("stop")

        transport = SimpleNamespace(stop=stop_transport)
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated_sessions.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: None,
            clear_session=lambda base_session_id: cleared_turns.append(base_session_id),
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        activation = RuntimeActivationRegistry()
        agent.controller = SimpleNamespace(
            runtime_activation=activation,
            model_hub_runtime=SimpleNamespace(
                retire_process_scope=retire_scope,
            )
        )
        identity = agent._attach_transport_activation("/tmp/work", transport)

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(invalidated_sessions, ["session-1"])
        self.assertEqual(cleared_turns, ["session-1"])
        agent.sessions.clear_agent_session_mapping.assert_not_called()
        self.assertEqual(agent._transports, {})
        self.assertIn("/tmp/work", agent._transport_locks)
        self.assertIsNone(activation.current("codex", "/tmp/work"))
        self.assertEqual(
            activation.current("codex", "/tmp/work", include_retired=True),
            identity,
        )
        self.assertEqual(agent._transport_last_activity, {})
        retire_scope.assert_called_once_with("codex", "/tmp/work")

    async def test_evict_idle_transports_keeps_active_codex_runtime(self):
        agent = object.__new__(CodexAgent)

        async def stop_transport():
            raise AssertionError("active transport should not be stopped")

        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: None,
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: "turn-1",
            clear_session=lambda base_session_id: None,
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIn("/tmp/work", agent._transports)
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_evict_idle_transports_keeps_pending_turn_start_runtime(self):
        agent = object.__new__(CodexAgent)

        async def stop_transport():
            raise AssertionError("pending turn-start transport should not be stopped")

        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: None,
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: None,
            has_pending_turn_start=lambda base_session_id: True,
            clear_session=lambda base_session_id: None,
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIn("/tmp/work", agent._transports)
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_evict_idle_transports_retains_generation_when_stop_fails(self):
        agent = object.__new__(CodexAgent)
        invalidated_sessions = []
        cleared_turns = []

        async def stop_transport():
            raise RuntimeError("boom")

        transport = SimpleNamespace(stop=stop_transport)
        lock = asyncio.Lock()
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._transport_locks = {"/tmp/work": lock}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated_sessions.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: None,
            has_pending_turn_start=lambda base_session_id: False,
            clear_session=lambda base_session_id: cleared_turns.append(base_session_id),
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIs(agent._transports["/tmp/work"], transport)
        self.assertIs(agent._transport_locks["/tmp/work"], lock)
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 0.0)
        self.assertEqual(invalidated_sessions, [])
        self.assertEqual(cleared_turns, [])
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    async def test_evict_idle_transports_revalidates_activity_before_stop(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []

        async def stop_transport():
            stop_calls.append("stop")

        lock = asyncio.Lock()
        await lock.acquire()
        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._transport_locks = {"/tmp/work": lock}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: None,
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: None,
            has_pending_turn_start=lambda base_session_id: False,
            clear_session=lambda base_session_id: None,
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            agent._transport_last_activity["/tmp/work"] = 950.0
            lock.release()
            evicted = await eviction_task

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 950.0)
        agent.sessions.clear_agent_session_mapping.assert_not_called()

    @staticmethod
    def _make_evict_agent(*, active_turn, last_activity=0.0):
        """Build a bare CodexAgent wired for evict_idle_transports tests."""
        agent = object.__new__(CodexAgent)
        stop_calls = []

        async def stop_transport():
            stop_calls.append("stop")

        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
        agent._transport_last_activity = {"/tmp/work": last_activity}
        agent._session_last_activity = {"session-1": last_activity}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        invalidated = []
        cleared_turns = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        request = SimpleNamespace(context="ctx-1", base_session_id="session-1")
        active_turns = {"session-1": active_turn}

        def clear_session(base_session_id):
            cleared_turns.append(base_session_id)
            active_turns[base_session_id] = None

        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: active_turns.get(base_session_id),
            has_pending_turn_start=lambda base_session_id: False,
            get_request_for_turn=lambda turn_id: request if turn_id == active_turn else None,
            get_latest_request=lambda base_session_id: request,
            clear_session=clear_session,
        )
        release_calls = []
        agent._event_handler = SimpleNamespace(
            _release_stream_turn=lambda context: release_calls.append(context),
            release_calls=release_calls,
        )
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        return agent, stop_calls, invalidated, cleared_turns

    async def test_hfr_144_stuck_active_settles_only_exact_codex_owner(self):
        """HFR-144: the exact owner settles through the terminal chokepoint."""
        # active turn that has been idle WAY past the stuck-active cap
        # (max(600*3, 1800) = 1800s) must be force-evicted — the leak fix.
        agent, stop_calls, invalidated, cleared_turns = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )
        agent.handle_message = AsyncMock()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared_turns, ["session-1"])
        self.assertEqual(agent._transports, {})
        self.assertEqual(agent._transport_last_activity, {})
        # Force-reaped stuck turns must settle Workbench status + runtime gate
        # through the shared terminal-result chokepoint.
        agent.controller.emit_agent_message.assert_awaited_once_with(
            "ctx-1", "result", "", is_error=True, level="silent", output=ANY
        )
        agent.handle_message.assert_not_awaited()
        self.assertEqual(agent._event_handler.release_calls, [])

    async def test_evict_idle_transports_force_evict_release_falls_back_to_latest_request(self):
        # Defensive path: if the active turn has no per-turn request mapping,
        # the runtime gate is still settled via get_latest_request.
        agent, stop_calls, _invalidated, _cleared = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )
        fallback_request = SimpleNamespace(context="ctx-latest", base_session_id="session-1")
        agent._turn_registry.get_request_for_turn = lambda turn_id: None
        agent._turn_registry.get_latest_request = lambda base_session_id: fallback_request

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(stop_calls, ["stop"])
        agent.controller.emit_agent_message.assert_awaited_once_with(
            "ctx-latest", "result", "", is_error=True, level="silent", output=ANY
        )
        self.assertEqual(agent._event_handler.release_calls, [])

    async def test_evict_idle_transports_keeps_active_transport_under_stuck_cap(self):
        # active turn idle past idle_timeout (600) but under the cap (1800):
        # still vetoed, NOT force-evicted.
        agent, stop_calls, _invalidated, cleared_turns = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)
        self.assertEqual(cleared_turns, [])

    async def test_evict_idle_transports_stuck_cap_floor_dominates_small_timeout(self):
        # With a tiny idle_timeout (100s) the multiplier window (300s) is below
        # the 1800s floor, so the floor governs: idle 1000s < 1800s stays vetoed.
        agent, stop_calls, _invalidated, _cleared = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(100)

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)

    async def test_evict_idle_transports_stuck_backstop_disabled(self):
        # multiplier <= 0 disables the backstop: an active turn is an absolute
        # veto again, no matter how long it has been idle.
        agent, stop_calls, _invalidated, cleared_turns = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )

        with patch.object(_MODULE, "DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER", 0):
            with patch.object(_MODULE.time, "monotonic", return_value=1_000_000.0):
                evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)
        self.assertEqual(cleared_turns, [])

    async def test_hfr_143_observable_session_progress_wins_locked_recheck(self):
        """HFR-143: attributable progress keeps a productive turn alive."""
        agent, stop_calls, _invalidated, _cleared = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )
        lock = asyncio.Lock()
        await lock.acquire()
        agent._transport_locks = {"/tmp/work": lock}
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
                )
            lock.release()
            evicted = await eviction_task

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_evict_idle_transports_reclassifies_when_turn_clears_between_passes(self):
        # Race: pass 1 sees a stuck-active candidate, but the turn completes
        # (active flag clears) before the locked recheck while activity stays
        # stale. The recheck reclassifies it as a NORMAL idle eviction.
        agent, stop_calls, invalidated, cleared_turns = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )
        lock = asyncio.Lock()
        await lock.acquire()
        agent._transport_locks = {"/tmp/work": lock}

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            # turn finished between the two passes; activity unchanged (stale)
            agent._turn_registry.get_active_turn = lambda base_session_id: None
            lock.release()
            evicted = await eviction_task

        self.assertEqual(evicted, 1)
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared_turns, ["session-1"])
        # reclassified as normal idle: the prior turn already settled itself,
        # so no spurious terminal result or runtime-gate release fires here.
        agent.controller.emit_agent_message.assert_not_awaited()
        self.assertEqual(agent._event_handler.release_calls, [])

    async def test_evict_idle_transports_force_evict_preserves_state_when_stop_fails(self):
        # Stuck-active force-eviction path: if transport.stop() raises, the
        # transport and its bookkeeping must be left intact (next sweep retries).
        agent = object.__new__(CodexAgent)
        invalidated = []
        cleared_turns = []

        async def stop_transport():
            raise RuntimeError("boom")

        transport = SimpleNamespace(stop=stop_transport)
        lock = asyncio.Lock()
        agent._transports = {"/tmp/work": transport}
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._session_last_activity = {"session-1": 0.0}
        agent._transport_locks = {"/tmp/work": lock}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: "turn-1",
            has_pending_turn_start=lambda base_session_id: False,
            clear_session=lambda base_session_id: cleared_turns.append(base_session_id),
        )
        agent._session_locks = {"session-1": asyncio.Lock()}
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        agent._settle_stuck_active_request = AsyncMock()

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIs(agent._transports["/tmp/work"], transport)
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 0.0)
        self.assertEqual(invalidated, [])
        self.assertEqual(cleared_turns, ["session-1"])

    async def test_get_or_create_transport_fast_path_waits_for_transport_lock(self):
        agent = object.__new__(CodexAgent)
        lock = asyncio.Lock()
        await lock.acquire()
        transport = SimpleNamespace(is_initialized=True)
        agent._transports = {"/tmp/work": transport}
        agent._transport_locks = {"/tmp/work": lock}
        agent._transport_last_activity = {}

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            transport_task = asyncio.create_task(agent._get_or_create_transport("/tmp/work"))
            await asyncio.sleep(0)
            self.assertFalse(transport_task.done())
            lock.release()
            resolved = await transport_task

        self.assertIs(resolved, transport)
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 1000.0)


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
        agent = object.__new__(CodexAgent)
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
        agent._get_or_create_transport = AsyncMock(return_value=transport)
        agent._touch_transport_activity = Mock()
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
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace(config=SimpleNamespace(language="zh"))

        display = agent._error_display_text(
            CodexPromptRefreshUnavailableError("internal diagnostic")
        )

        self.assertEqual(
            display,
            "❌ Codex 无法确认能否安全刷新此现有会话的 Avibe 指令。请检查或升级 Codex 后重试本回合。",
        )

    async def test_handle_message_does_not_hide_turn_before_interrupt_succeeds(self):
        agent = object.__new__(CodexAgent)
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
        agent._get_or_create_transport = AsyncMock(return_value=transport)
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
        agent = object.__new__(CodexAgent)
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
            context=object(),
            session_key="settings-1",
            ack_message_id=None,
        )

        bad_transport = SimpleNamespace(stop=AsyncMock())
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
        agent._transports = {"/tmp/work": bad_transport}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._session_mgr = session_mgr
        agent.sessions = sessions
        agent._get_or_create_transport = AsyncMock(side_effect=[bad_transport, fresh_transport])
        agent._touch_transport_activity = Mock()
        agent._build_thread_developer_instructions = AsyncMock(return_value="stable prompt")
        agent._start_or_resume_thread = AsyncMock(
            side_effect=[
                ConnectionError("Codex app-server stdout closed"),
                "thread-new",
            ]
        )
        agent._start_thread = AsyncMock(return_value="thread-new")
        agent._start_turn = AsyncMock(return_value="thread-new")

        await agent.handle_message(request)

        bad_transport.stop.assert_awaited_once()
        self.assertEqual(agent._transports, {})
        self.assertEqual(agent._transport_last_activity, {})
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(agent._turn_registry.cleared_sessions, ["session-1"])
        sessions.clear_agent_session_mapping.assert_not_called()
        agent._get_or_create_transport.assert_any_await("/tmp/work")
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
        agent = object.__new__(CodexAgent)
        request = SimpleNamespace(
            base_session_id="session-1",
            working_path="/tmp/work",
            context=object(),
            session_key="settings-1",
            ack_message_id=None,
        )

        bad_transport = SimpleNamespace(
            send_request=AsyncMock(side_effect=ConnectionError("Codex app-server transport is not available")),
            stop=AsyncMock(),
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
        agent._transports = {"/tmp/work": bad_transport}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._session_mgr = SimpleNamespace(
            set_session_key=Mock(),
            set_cwd=Mock(),
            get_thread_id=Mock(return_value="thread-old"),
            sessions_for_cwd=Mock(return_value=["session-1"]),
            invalidate_thread=Mock(),
        )
        agent.sessions = SimpleNamespace(clear_agent_session_mapping=Mock())
        agent._get_or_create_transport = AsyncMock(side_effect=[bad_transport, fresh_transport])
        agent._touch_transport_activity = Mock()
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
        agent._start_or_resume_thread.assert_awaited_once_with(fresh_transport, request)
        agent._start_thread.assert_not_awaited()
        agent.controller.emit_agent_message.assert_not_awaited()

    async def test_drop_transport_after_failure_keeps_other_sessions_when_transport_was_replaced(self):
        agent = object.__new__(CodexAgent)
        request = SimpleNamespace(base_session_id="session-1")
        activation = RuntimeActivationRegistry()
        old_identity = activation.attach("codex", "/tmp/work")
        old_transport = SimpleNamespace(
            stop=AsyncMock(),
            _vibe_runtime_activation_identity=old_identity,
        )
        fresh_identity = activation.attach("codex", "/tmp/work")
        fresh_transport = SimpleNamespace()
        fresh_transport._vibe_runtime_activation_identity = fresh_identity
        invalidated = []
        cleared = []
        agent._transports = {"/tmp/work": fresh_transport}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value=["session-1", "session-2"]),
            invalidate_thread=Mock(side_effect=lambda base_session_id: invalidated.append(base_session_id)),
        )
        agent._turn_registry = SimpleNamespace(
            clear_session=Mock(side_effect=lambda base_session_id: cleared.append(base_session_id))
        )
        agent.controller = SimpleNamespace(runtime_activation=activation)

        await agent._drop_transport_after_failure("/tmp/work", old_transport, request)

        old_transport.stop.assert_awaited_once()
        self.assertIs(agent._transports["/tmp/work"], fresh_transport)
        self.assertEqual(agent._transport_last_activity, {"/tmp/work": 1.0})
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared, ["session-1"])
        self.assertEqual(activation.current("codex", "/tmp/work"), fresh_identity)

    async def test_drop_transport_after_failure_retires_current_generation_before_stop(self):
        agent = object.__new__(CodexAgent)
        request = SimpleNamespace(base_session_id="session-1")
        activation = RuntimeActivationRegistry()
        observed_current = []
        transport = SimpleNamespace()

        async def stop_transport():
            observed_current.append(activation.is_current(identity))

        transport.stop = stop_transport
        agent._transports = {"/tmp/work": transport}
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        agent._transport_cwd_inodes = {"/tmp/work": 1}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value=["session-1"]),
            invalidate_thread=Mock(),
        )
        agent._turn_registry = SimpleNamespace(clear_session=Mock())
        agent.controller = SimpleNamespace(runtime_activation=activation)
        identity = agent._attach_transport_activation("/tmp/work", transport)

        await agent._drop_transport_after_failure("/tmp/work", transport, request)

        self.assertEqual(observed_current, [False])
        self.assertNotIn("/tmp/work", agent._transports)
        self.assertIsNone(activation.current("codex", "/tmp/work"))

    async def test_start_or_resume_thread_reraises_recoverable_transport_error(self):
        agent = object.__new__(CodexAgent)
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-old"))
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = AsyncMock(return_value=None)
        agent._start_thread = AsyncMock()
        request = SimpleNamespace(session_key="settings-1", base_session_id="session-1")
        transport = SimpleNamespace(send_request=AsyncMock(side_effect=ConnectionError("Codex app-server stdout closed")))

        with self.assertRaises(ConnectionError):
            await agent._start_or_resume_thread(transport, request)

        agent._start_thread.assert_not_awaited()

    def test_find_request_does_not_bootstrap_turn_completed_for_pending_turn(self):
        agent = object.__new__(CodexAgent)
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
    def test_inject_caller_env_config_adds_vendored_git_for_gitless_session(self):
        agent = object.__new__(CodexAgent)
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
            agent._inject_caller_env_config(params, request)

        set_env = params["config"]["shell_environment_policy"]["set"]
        self.assertEqual(set_env["PATH"], "/managed/git/bin")
        self.assertEqual(set_env["AVIBE_SKILL_WORKING_DIR"], str(Path("/tmp/workspace").resolve()))
        self.assertTrue(set_env["BASH_ENV"].endswith("/codex-caller-env/session.sh"))
        self.assertFalse(params["config"]["skills.include_instructions"])

    def test_inject_caller_env_config_merges_shell_environment_policy(self):
        agent = object.__new__(CodexAgent)
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
            agent._inject_caller_env_config(params, request)

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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        self.assertIn("# Avibe", developer_instructions)
        self.assertIn("Current session id: `sesk8m4q2p7x`", developer_instructions)
        self.assertNotIn("## Quick-reply buttons", developer_instructions)

    async def test_start_thread_adds_codex_generated_image_prompt_to_thread_instructions(self):
        agent = object.__new__(CodexAgent)
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

    async def test_start_thread_omits_show_pages_prompt_when_disabled(self):
        agent = object.__new__(CodexAgent)
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
        self.assertNotIn("## Show Pages", developer_instructions)
        self.assertNotIn("vibe show path", developer_instructions)

    async def test_resume_thread_refreshes_developer_instructions_without_appending(self):
        agent = object.__new__(CodexAgent)
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
        self.assertNotIn("developerInstructions", params)

    async def test_resume_thread_rebinds_managed_provider_when_thread_id_matches_config(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent._should_rollback_forked_running_turn = AsyncMock(return_value=False)
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
        agent = object.__new__(CodexAgent)
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
        agent._should_rollback_forked_running_turn = AsyncMock(return_value=False)
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
        agent = object.__new__(CodexAgent)
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
        agent._should_rollback_forked_running_turn = AsyncMock(return_value=False)
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
        agent = object.__new__(CodexAgent)
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

    async def test_start_or_resume_thread_rolls_back_running_fork_before_correction(self):
        agent = object.__new__(CodexAgent)
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

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/rollback",
            "thread/inject_items",
        ])
        rollback_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(rollback_params, {"threadId": "thread-fork", "numTurns": 1})
        agent.sessions.bind_agent_session.assert_called_once_with(
            "avibe::project::proj_1",
            "codex",
            "ses-target",
            "thread-fork",
        )

    async def test_start_or_resume_thread_skips_running_fork_rollback_before_native_start(self):
        agent = object.__new__(CodexAgent)
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
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

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

    async def test_start_or_resume_thread_rolls_back_pre_start_fork_after_source_started(self):
        agent = object.__new__(CodexAgent)
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
            return {"body": {"in_flight": True, "native_turn_started": True}}

        async def send_request(method, params):
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
            "thread/fork",
            "thread/rollback",
            "thread/inject_items",
        ])

    async def test_start_or_resume_thread_rolls_back_pre_start_fork_after_source_output(self):
        agent = object.__new__(CodexAgent)
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
                latest_after_anchor_type="assistant",
                has_messages_after_anchor=True,
                has_terminal_agent_output_after_anchor=False,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/rollback",
            "thread/inject_items",
        ])
        rollback_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(rollback_params, {"threadId": "thread-fork", "numTurns": 1})

    async def test_start_or_resume_thread_skips_running_fork_rollback_when_anchor_completed(self):
        agent = object.__new__(CodexAgent)
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
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-fork"}}))

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

    async def test_start_or_resume_thread_skips_running_fork_rollback_after_source_completed(self):
        agent = object.__new__(CodexAgent)
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

    async def test_start_or_resume_thread_rolls_back_reserved_user_anchor_after_source_completed(self):
        agent = object.__new__(CodexAgent)
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
            "thread/rollback",
            "thread/inject_items",
        ])
        rollback_params = transport.send_request.await_args_list[1].args[1]
        self.assertEqual(rollback_params, {"threadId": "thread-fork", "numTurns": 1})

    async def test_start_or_resume_thread_rolls_back_user_anchor_completed_before_native_start_flag(self):
        agent = object.__new__(CodexAgent)
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
            "thread/fork",
            "thread/rollback",
            "thread/inject_items",
        ])

    async def test_start_or_resume_thread_does_not_roll_back_when_new_user_after_anchor(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
            should_rollback = await agent._should_rollback_forked_running_turn(fork)

        self.assertTrue(should_rollback)

    async def test_start_or_resume_thread_does_not_roll_back_user_anchor_before_native_start(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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

    def test_thread_developer_instructions_follow_live_memory_enabled_state(self):
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace(
            config=SimpleNamespace(
                platform="avibe",
                reply_enhancements=True,
                memory=SimpleNamespace(enabled=False),
            )
        )
        agent.codex_config = SimpleNamespace(default_model=None)
        request = SimpleNamespace(
            context=SimpleNamespace(
                platform="avibe",
                platform_specific={"agent_session_id": "sesk8m4q2p7x", "memory_cli_admitted": True},
                user_id="U1",
                channel_id="C1",
            ),
            subagent_name=None,
        )

        disabled_instructions = asyncio.run(
            agent._build_thread_developer_instructions(request)
        )
        agent.controller.config.memory.enabled = True
        enabled_instructions = asyncio.run(
            agent._build_thread_developer_instructions(request)
        )

        self.assertNotIn("## Personal Memory", disabled_instructions)
        self.assertIn("## Personal Memory", enabled_instructions)
        self.assertIn('vibe memory search "<query>" --json', enabled_instructions)

    def test_build_input_does_not_add_codex_generated_image_prompt_to_each_turn(self):
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace(config=SimpleNamespace(reply_enhancements=True))
        request = SimpleNamespace(message="hello", files=None)

        with patch.dict(os.environ, {"CODEX_HOME": "/Users/test/.codex"}):
            items = agent._build_input(request)

        self.assertEqual(items, [{"type": "text", "text": "hello"}])

    async def test_refresh_thread_developer_instructions_updates_cached_thread_once(self):
        agent = object.__new__(CodexAgent)
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
        self.assertNotIn("modelProvider", params)
        self.assertNotIn("developerInstructions", params)

    async def test_refresh_thread_developer_instructions_refreshes_caller_env_when_prompt_cached(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        self.assertIn("Only the most recent snapshot is active", latest)
        self.assertIn("including untagged versions", latest)
        self.assertIn("omitted from this snapshot no longer apply", latest)
        self.assertNotIn("prompt one", latest)

    async def test_start_turn_does_not_reinject_when_explicit_model_reset_is_unsupported(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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

        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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

        agent = object.__new__(CodexAgent)
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

        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)
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


class CodexTransportCwdStalenessTests(unittest.IsolatedAsyncioTestCase):
    """#561: a cached app-server whose spawn directory was deleted (and possibly
    re-created at the same path) sits in a dead inode and fails every
    thread/start with "failed to load configuration"."""

    def _agent(self):
        agent = object.__new__(CodexAgent)
        agent._transports = {}
        agent._transport_locks = {}
        agent._transport_last_activity = {}
        agent._transport_cwd_inodes = {}
        agent._session_locks = {}
        agent._session_mgr = SimpleNamespace(sessions_for_cwd=lambda cwd: [])
        agent.codex_config = SimpleNamespace(binary="codex", extra_args=[])
        agent._model_hub_catalog_path = None
        agent._model_hub_catalog_lock = asyncio.Lock()
        agent._model_hub_catalog_generation = 0
        agent.controller = SimpleNamespace(config=SimpleNamespace(codex=agent.codex_config))
        agent._runtime_ownership_snapshot_for_cwd = Mock(
            return_value=SimpleNamespace(blocks_transport_replacement=False)
        )
        return agent

    async def test_hfr_142_server_request_does_not_refresh_progress_activity(self):
        """HFR-142: protocol approval frames are not real Session progress."""
        agent = self._agent()
        agent._transport_last_activity = {"/tmp/work": 0.0}
        agent._session_last_activity = {}

        with patch.object(_MODULE.time, "monotonic", return_value=1234.0):
            result = await agent._on_server_request(
                "/tmp/work",
                7,
                "item/commandExecution/requestApproval",
                {"itemId": "item-1"},
            )

        self.assertEqual(result, {"approved": True})
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 0.0)
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

    def test_turn_start_refreshes_the_cwd_transport_clock(self):
        agent = self._agent()
        agent._transport_last_activity = {"/tmp/work": 0.0}
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

        self.assertEqual(agent._transport_last_activity, {"/tmp/work": 1234.0})
        self.assertEqual(agent._session_last_activity, {"session-1": 1234.0})

    async def test_hfr_142_server_request_callback_does_not_create_progress(self):
        """HFR-142: the cwd-bound callback preserves the prior progress clock."""
        import tempfile

        agent = self._agent()
        captured = {}

        with tempfile.TemporaryDirectory() as cwd:
            fresh = SimpleNamespace(
                is_initialized=True,
                start=AsyncMock(),
                on_notification=Mock(),
                on_server_request=Mock(side_effect=lambda cb: captured.update(cb=cb)),
            )
            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                await agent._get_or_create_transport(cwd)

            self.assertIn("cb", captured)
            before = agent._transport_last_activity[cwd]
            with patch.object(_MODULE.time, "monotonic", return_value=999.0):
                result = await captured["cb"](1, "item/fileChange/requestApproval", {"itemId": "x"})

            self.assertEqual(result, {"approved": True})
            self.assertEqual(agent._transport_last_activity[cwd], before)

    async def test_get_or_create_transport_moves_app_server_into_agent_cgroup(self):
        import tempfile

        agent = self._agent()
        calls = []
        agent.controller._agent_resource_governor = SimpleNamespace(
            apply_to_pid=lambda pid, label="agent": calls.append((pid, label)) or True
        )
        with tempfile.TemporaryDirectory() as cwd:
            fresh = SimpleNamespace(
                is_initialized=True,
                pid=2468,
                start=AsyncMock(),
                on_notification=Mock(),
                on_server_request=Mock(),
            )
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
                result = await agent._get_or_create_transport(cwd)

            self.assertIs(result, fresh)
            self.assertEqual(calls, [(2468, "codex app-server")])

    async def test_cached_transport_evicted_when_cwd_inode_changes(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            stale = SimpleNamespace(is_initialized=True, stop=AsyncMock())
            agent._transports[cwd] = stale
            # Simulate "spawned in a directory that was since replaced": the
            # recorded spawn-time inode differs from the current one.
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino + 1

            fresh = SimpleNamespace(
                is_initialized=True,
                start=AsyncMock(),
                on_notification=Mock(),
                on_server_request=Mock(),
            )
            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                result = await agent._get_or_create_transport(cwd)

            stale.stop.assert_awaited_once()
            self.assertIs(result, fresh)
            fresh.start.assert_awaited_once()
            # The new spawn re-records the CURRENT inode.
            self.assertEqual(agent._transport_cwd_inodes[cwd], os.stat(cwd).st_ino)

    async def test_stale_cwd_preserves_transport_with_durable_native_owner(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            stale = SimpleNamespace(is_initialized=True, stop=AsyncMock())
            agent._transports[cwd] = stale
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino + 1
            agent._runtime_ownership_snapshot_for_cwd = Mock(
                return_value=SimpleNamespace(blocks_transport_replacement=True)
            )

            with self.assertRaisesRegex(RuntimeError, "durable owner"):
                await agent._get_or_create_transport(cwd)

            stale.stop.assert_not_awaited()
            self.assertIs(agent._transports[cwd], stale)

    async def test_hfr_473_dead_transport_restarts_past_stale_turn_ownership(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            dead = SimpleNamespace(
                is_initialized=False,
                is_alive=False,
                has_pending_notifications=False,
                runtime_fingerprint="direct",
                stop=AsyncMock(),
            )
            agent._transports[cwd] = dead
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino
            agent._runtime_ownership_snapshot_for_cwd = Mock(
                return_value=SimpleNamespace(
                    blocks_transport_replacement=True,
                    blocks_dead_transport_replacement=False,
                )
            )
            agent._session_mgr = SimpleNamespace(
                sessions_for_cwd=Mock(return_value=["session-1"]),
                invalidate_thread=Mock(),
            )
            agent._turn_registry = SimpleNamespace(
                get_active_turn=Mock(return_value="turn-from-dead-generation"),
                has_pending_turn_start=Mock(return_value=False),
                clear_session=Mock(),
            )
            agent._clear_thread_developer_instructions = Mock()
            fresh = SimpleNamespace(
                is_initialized=True,
                pid=2468,
                start=AsyncMock(),
                on_notification=Mock(),
                on_server_request=Mock(),
            )

            with patch.object(_MODULE, "CodexTransport", return_value=fresh):
                result = await agent._get_or_create_transport(cwd)

            self.assertIs(result, fresh)
            dead.stop.assert_awaited_once()
            fresh.start.assert_awaited_once()
            agent._session_mgr.invalidate_thread.assert_called_once_with("session-1")
            agent._turn_registry.clear_session.assert_called_once_with("session-1")

    async def test_dead_transport_preserves_generation_with_active_activity_owner(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            dead = SimpleNamespace(
                is_initialized=False,
                is_alive=False,
                has_pending_notifications=False,
                runtime_fingerprint="direct",
                stop=AsyncMock(),
            )
            agent._transports[cwd] = dead
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino
            agent._runtime_ownership_snapshot_for_cwd = Mock(
                return_value=SimpleNamespace(
                    blocks_transport_replacement=True,
                    blocks_dead_transport_replacement=True,
                )
            )

            with self.assertRaisesRegex(RuntimeError, "durable owner"):
                await agent._get_or_create_transport(cwd)

            dead.stop.assert_not_awaited()
            self.assertIs(agent._transports[cwd], dead)

    async def test_runtime_change_preserves_transport_with_pid_run_owner(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            existing = SimpleNamespace(
                is_initialized=True,
                runtime_fingerprint="direct",
                stop=AsyncMock(),
            )
            agent._transports[cwd] = existing
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino
            agent._runtime_ownership_snapshot_for_cwd = Mock(
                return_value=SimpleNamespace(blocks_transport_replacement=True)
            )
            launch = SimpleNamespace(
                channel="hub",
                fingerprint="hub:replacement",
                gateway_base_url="http://127.0.0.1:8317",
                gateway_token="ephemeral-token",
            )
            agent._model_hub_catalog_path = Path(cwd) / "codex-hub-catalog.json"

            with patch(
                "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            ) as prepare_catalog:
                with self.assertRaisesRegex(RuntimeError, "durable owner"):
                    await agent._get_or_create_transport(cwd, launch)

            prepare_catalog.assert_not_called()
            existing.stop.assert_not_awaited()
            self.assertIs(agent._transports[cwd], existing)

    async def test_runtime_config_switches_binary_without_catalog_export(self):
        agent = self._agent()
        previous_catalog = Path("/runtime/codex-old.json")
        agent._model_hub_catalog_path = previous_catalog
        next_config = SimpleNamespace(binary="/opt/codex-next", extra_args=[])
        agent.refresh_auth_state = AsyncMock()

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=RuntimeError("catalog export must not run"),
        ) as prepare_catalog:
            await agent.refresh_runtime_config(next_config)

        prepare_catalog.assert_not_called()
        self.assertIs(agent.codex_config, next_config)
        self.assertIs(agent.controller.config.codex, next_config)
        self.assertIsNone(agent._model_hub_catalog_path)
        self.assertEqual(agent._model_hub_catalog_generation, 1)
        agent.refresh_auth_state.assert_awaited_once_with()

    async def test_runtime_config_same_binary_invalidates_prepared_catalog(self):
        agent = self._agent()
        previous_catalog = Path("/runtime/codex-old.json")
        agent._model_hub_catalog_path = previous_catalog
        agent.refresh_auth_state = AsyncMock()
        next_config = SimpleNamespace(binary=agent.codex_config.binary, extra_args=["--next"])

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=RuntimeError("catalog export must not run"),
        ) as prepare_catalog:
            await agent.refresh_runtime_config(next_config)

        prepare_catalog.assert_not_called()
        self.assertIs(agent.codex_config, next_config)
        self.assertIsNone(agent._model_hub_catalog_path)
        self.assertEqual(agent._model_hub_catalog_generation, 1)
        agent.refresh_auth_state.assert_awaited_once_with()

    async def test_model_hub_catalog_invalidation_preserves_direct_transports(self):
        agent = self._agent()
        transport = SimpleNamespace(stop=AsyncMock())
        agent._transports["/repo"] = transport
        agent._model_hub_catalog_path = Path("/runtime/codex-old.json")

        await agent.invalidate_model_hub_runtime()

        self.assertIsNone(agent._model_hub_catalog_path)
        self.assertEqual(agent._model_hub_catalog_generation, 1)
        transport.stop.assert_not_awaited()

    async def test_startup_catalog_preparation_cannot_overwrite_new_runtime_generation(self):
        agent = self._agent()
        previous_config = agent.codex_config
        previous_catalog = Path("/runtime/codex-old.json")
        next_catalog = Path("/runtime/codex-new.json")
        next_config = SimpleNamespace(binary="/opt/codex-next", extra_args=[])
        agent.refresh_auth_state = AsyncMock()
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
            await agent.refresh_runtime_config(next_config)
            self.assertIs(agent.codex_config, next_config)
            self.assertIsNone(agent._model_hub_catalog_path)
            release_previous.set()
            with self.assertRaises(_MODULE.CodexModelHubCatalogUnavailableError):
                await startup
            recovered = await agent.prepare_model_hub_runtime()

        self.assertEqual(
            calls,
            [
                (previous_config.binary, None, None),
                (next_config.binary, None, None),
            ],
        )
        self.assertIs(agent.codex_config, next_config)
        self.assertEqual(agent._model_hub_catalog_path, next_catalog)
        self.assertEqual(recovered, next_catalog)

    async def test_model_hub_catalog_preparation_retries_after_transient_failure(self):
        agent = self._agent()
        catalog = Path("/runtime/codex-recovered.json")

        with patch(
            "vibe.backend_model_catalog.prepare_codex_hub_catalog",
            side_effect=[RuntimeError("transient export failure"), catalog],
        ) as prepare_catalog:
            with self.assertRaises(_MODULE.CodexModelHubCatalogUnavailableError):
                await agent.prepare_model_hub_runtime()
            self.assertIsNone(agent._model_hub_catalog_path)
            recovered = await agent.prepare_model_hub_runtime()

        self.assertEqual(recovered, catalog)
        self.assertEqual(agent._model_hub_catalog_path, catalog)
        self.assertEqual(prepare_catalog.call_count, 2)

    async def test_missing_prepared_hub_catalog_preserves_existing_transport_and_threads(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            existing = SimpleNamespace(
                is_initialized=True,
                runtime_fingerprint="direct",
                stop=AsyncMock(),
            )
            agent._transports[cwd] = existing
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino
            agent._session_mgr = SimpleNamespace(
                sessions_for_cwd=Mock(return_value=["session-1"]),
                invalidate_thread=Mock(),
            )
            agent._turn_registry = SimpleNamespace(
                clear_session=Mock(),
                get_active_turn=Mock(return_value=None),
            )
            agent._clear_thread_developer_instructions = Mock()
            launch = SimpleNamespace(
                channel="hub",
                fingerprint="hub:replacement",
                gateway_base_url="http://127.0.0.1:8317",
                gateway_token="ephemeral-token",
            )

            with patch(
                "vibe.backend_model_catalog.prepare_codex_hub_catalog",
                side_effect=RuntimeError("transient export failure"),
            ) as prepare_catalog:
                with self.assertRaises(_MODULE.CodexModelHubCatalogUnavailableError):
                    await agent._get_or_create_transport(cwd, launch)

            prepare_catalog.assert_called_once_with(
                agent.codex_config.binary,
                None,
                None,
            )
            existing.stop.assert_not_awaited()
            self.assertIs(agent._transports[cwd], existing)
            agent._session_mgr.invalidate_thread.assert_not_called()
            agent._turn_registry.clear_session.assert_not_called()

    async def test_stale_transport_stop_failure_retains_exact_generation(self):
        import tempfile

        agent = self._agent()
        activation = RuntimeActivationRegistry()
        agent.controller.runtime_activation = activation
        with tempfile.TemporaryDirectory() as cwd:
            observed_current = []
            stale = SimpleNamespace(is_initialized=True)
            agent._transports[cwd] = stale
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino + 1
            identity = agent._attach_transport_activation(cwd, stale)

            async def stop_stale():
                observed_current.append(activation.is_current(identity))
                raise RuntimeError("stop failed")

            stale.stop = stop_stale

            with self.assertRaisesRegex(RuntimeError, "stop failed"):
                await agent._get_or_create_transport(cwd)

            self.assertEqual(observed_current, [False])
            self.assertIs(agent._transports[cwd], stale)
            self.assertIs(activation.current("codex", cwd), identity)

    async def test_cached_transport_reused_while_cwd_unchanged(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            cached = SimpleNamespace(is_initialized=True, stop=AsyncMock())
            agent._transports[cwd] = cached
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino

            with patch.object(_MODULE, "CodexTransport") as ctor:
                result = await agent._get_or_create_transport(cwd)

            self.assertIs(result, cached)
            cached.stop.assert_not_awaited()
            ctor.assert_not_called()

    async def test_probe_validation_does_not_replace_incompatible_runtime(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            cached = SimpleNamespace(
                is_initialized=True,
                runtime_fingerprint="hub:openai/gpt-5.4-mini",
                stop=AsyncMock(),
            )
            agent._transports[cwd] = cached
            agent._transport_cwd_inodes[cwd] = os.stat(cwd).st_ino

            with self.assertRaises(CodexConnectionProbeRuntimeMismatchError):
                await agent._get_or_create_transport(
                    cwd,
                    allow_runtime_replacement=False,
                )

            cached.stop.assert_not_awaited()
            self.assertIs(agent._transports[cwd], cached)

    async def test_untracked_legacy_entry_reuses_without_inode(self):
        import tempfile

        agent = self._agent()
        with tempfile.TemporaryDirectory() as cwd:
            cached = SimpleNamespace(is_initialized=True, stop=AsyncMock())
            agent._transports[cwd] = cached
            # No recorded inode (legacy entry) -> reuse as before, no eviction.
            with patch.object(_MODULE, "CodexTransport") as ctor:
                result = await agent._get_or_create_transport(cwd)
            self.assertIs(result, cached)
            ctor.assert_not_called()

    def test_config_load_failure_is_recoverable(self):
        agent = object.__new__(CodexAgent)
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
        agent = object.__new__(CodexAgent)

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
                        agent._should_rollback_forked_running_turn = AsyncMock(return_value=False)
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
