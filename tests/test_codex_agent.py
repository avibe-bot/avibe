import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_AGENT_PATH = Path(__file__).resolve().parents[1] / "modules/agents/codex/agent.py"

_modules_pkg = types.ModuleType("modules")
_agents_pkg = types.ModuleType("modules.agents")
_codex_pkg = types.ModuleType("modules.agents.codex")

_base_module = types.ModuleType("modules.agents.base")
setattr(_base_module, "AgentRequest", object)


class _BaseAgent:
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


class CodexAgentStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_stop_does_not_hide_turn_before_interrupt_succeeds(self):
        agent = object.__new__(CodexAgent)
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"
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

    async def test_handle_stop_hides_turn_after_interrupt_succeeds(self):
        agent = object.__new__(CodexAgent)
        agent._session_mgr = SimpleNamespace(get_thread_id=lambda base_session_id: "thread-1")
        agent._turn_registry = _StubTurnRegistry()
        agent._turn_registry._active_turns["session-1"] = "turn-1"

        events = []

        async def send_request(method, payload):
            events.append(("send", method, payload))
            return {}

        def clear_pending(turn_id):
            events.append(("clear", turn_id))
            return SimpleNamespace()

        agent._transports = {"/tmp": SimpleNamespace(is_alive=True, send_request=send_request)}
        agent._event_handler = SimpleNamespace(clear_pending=clear_pending)
        agent._remove_ack_reaction = AsyncMock(side_effect=lambda request: events.append(("ack", None)))
        agent.controller = SimpleNamespace(emit_agent_message=AsyncMock())

        request = SimpleNamespace(base_session_id="session-1", working_path="/tmp", context=object())

        result = await agent.handle_stop(request)

        self.assertTrue(result)
        self.assertEqual(events[0][0], "send")
        self.assertEqual(events[1][0], "clear")

    async def test_refresh_auth_state_stops_transports_and_invalidates_threads(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []

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
            session_turns=SimpleNamespace(release_for_backend_refresh=release_for_backend_refresh)
        )

        await agent.refresh_auth_state()

        self.assertEqual(release_calls, [("codex", {"session-1", "session-2"})])
        self.assertEqual(stop_calls, ["a", "b"])
        self.assertEqual(agent._transports, {})
        self.assertEqual(invalidated, ["session-1", "session-2"])
        self.assertEqual(cleared_sessions, ["session-1", "session-2"])

    async def test_prepare_resume_binding_restarts_unshared_transport(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []

        async def stop_transport():
            stop_calls.append("stop")

        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
        agent._transport_last_activity = {"/tmp/work": 1.0}
        invalidated = []
        cleared_sessions = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        agent._turn_registry = SimpleNamespace(clear_session=lambda base_session_id: cleared_sessions.append(base_session_id))

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

    async def test_evict_idle_transports_stops_idle_codex_runtime(self):
        agent = object.__new__(CodexAgent)
        stop_calls = []
        invalidated_sessions = []
        cleared_turns = []

        async def stop_transport():
            stop_calls.append("stop")

        agent._transports = {"/tmp/work": SimpleNamespace(stop=stop_transport)}
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

        with patch.object(_MODULE.time, "monotonic", return_value=1000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 1)
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(invalidated_sessions, ["session-1"])
        self.assertEqual(cleared_turns, ["session-1"])
        agent.sessions.clear_agent_session_mapping.assert_not_called()
        self.assertEqual(agent._transports, {})
        self.assertIn("/tmp/work", agent._transport_locks)
        self.assertEqual(agent._transport_last_activity, {})

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

    async def test_evict_idle_transports_preserves_state_when_stop_fails(self):
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
        agent._transport_locks = {"/tmp/work": asyncio.Lock()}
        invalidated = []
        cleared_turns = []
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=lambda cwd: ["session-1"] if cwd == "/tmp/work" else [],
            invalidate_thread=lambda base_session_id: invalidated.append(base_session_id),
        )
        request = SimpleNamespace(context="ctx-1", base_session_id="session-1")
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda base_session_id: active_turn,
            has_pending_turn_start=lambda base_session_id: False,
            get_request_for_turn=lambda turn_id: request if turn_id == active_turn else None,
            get_latest_request=lambda base_session_id: request,
            clear_session=lambda base_session_id: cleared_turns.append(base_session_id),
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

    async def test_evict_idle_transports_force_evicts_stuck_active_transport(self):
        # active turn that has been idle WAY past the stuck-active cap
        # (max(600*3, 1800) = 1800s) must be force-evicted — the leak fix.
        agent, stop_calls, invalidated, cleared_turns = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )

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
            "ctx-1", "result", "", is_error=True, level="silent"
        )
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
            "ctx-latest", "result", "", is_error=True, level="silent"
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

    async def test_evict_idle_transports_force_evict_skips_when_activity_refreshed(self):
        # Race: pass 1 sees a stuck-active candidate (idle past the 1800s cap),
        # but a fresh notification updates last_activity before the locked
        # recheck. The recheck recomputes idle from current state and bails.
        agent, stop_calls, _invalidated, _cleared = self._make_evict_agent(
            active_turn="turn-1", last_activity=0.0
        )
        lock = asyncio.Lock()
        await lock.acquire()
        agent._transport_locks = {"/tmp/work": lock}

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            eviction_task = asyncio.create_task(agent.evict_idle_transports(600))
            await asyncio.sleep(0)
            # fresh activity: idle recomputed as 2000-1900=100s, well under cap
            agent._transport_last_activity["/tmp/work"] = 1900.0
            lock.release()
            evicted = await eviction_task

        self.assertEqual(evicted, 0)
        self.assertEqual(stop_calls, [])
        self.assertIn("/tmp/work", agent._transports)

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

        with patch.object(_MODULE.time, "monotonic", return_value=2000.0):
            evicted = await agent.evict_idle_transports(600)

        self.assertEqual(evicted, 0)
        self.assertIs(agent._transports["/tmp/work"], transport)
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 0.0)
        self.assertEqual(invalidated, [])
        self.assertEqual(cleared_turns, [])

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
        agent._session_mgr = SimpleNamespace(
            set_session_key=Mock(),
            set_cwd=Mock(),
            get_thread_id=Mock(return_value="thread-cached"),
        )

        async def refresh(existing_transport, existing_request, thread_id):
            events.append(("refresh", existing_transport, existing_request, thread_id))

        async def start_turn(existing_transport, existing_request, thread_id):
            events.append(("turn", existing_transport, existing_request, thread_id))
            return thread_id

        agent._refresh_thread_developer_instructions_if_needed = refresh
        agent._start_or_resume_thread = AsyncMock()
        agent._start_turn = start_turn

        await agent.handle_message(request)

        self.assertEqual(
            events,
            [
                ("refresh", transport, request, "thread-cached"),
                ("turn", transport, request, "thread-cached"),
            ],
        )
        agent._start_or_resume_thread.assert_not_awaited()

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
        # The failed interrupt is a terminal failure → emitted as an ERROR result
        # (the outbound status chokepoint turns the dot red), not a bare notify.
        agent.controller.emit_agent_message.assert_awaited_once_with(
            request.context,
            "result",
            "❌ Failed to interrupt previous Codex turn: interrupt failed",
            is_error=True,
        )

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
        agent._start_turn.assert_awaited_once_with(fresh_transport, request, "thread-new")
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
        old_transport = SimpleNamespace(stop=AsyncMock())
        fresh_transport = SimpleNamespace()
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

        await agent._drop_transport_after_failure("/tmp/work", old_transport, request)

        old_transport.stop.assert_awaited_once()
        self.assertIs(agent._transports["/tmp/work"], fresh_transport)
        self.assertEqual(agent._transport_last_activity, {"/tmp/work": 1.0})
        self.assertEqual(invalidated, ["session-1"])
        self.assertEqual(cleared, ["session-1"])

    async def test_start_or_resume_thread_reraises_recoverable_transport_error(self):
        agent = object.__new__(CodexAgent)
        agent.sessions = SimpleNamespace(get_agent_session_id=Mock(return_value="thread-old"))
        agent._session_mgr = SimpleNamespace(set_thread_id=Mock())
        agent._build_thread_developer_instructions = Mock(return_value=None)
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
                platform_specific={},
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

        thread_id = await agent._start_thread(transport, request)

        self.assertEqual(thread_id, "thread-1")
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["cwd"], "/tmp/work")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertIn("# Avibe", params["developerInstructions"])
        self.assertNotIn("## Quick-reply buttons", params["developerInstructions"])
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])
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
                platform_specific={},
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
            await agent._start_thread(transport, request)

        load_subagent.assert_called_once_with("reviewer", project_root=Path("/tmp/work"))
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["cwd"], "/tmp/work")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandbox"], "danger-full-access")
        self.assertIn("Focus on regressions.", params["developerInstructions"])
        self.assertIn("# Avibe", params["developerInstructions"])
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])
        self.assertNotIn("## Quick-reply buttons", params["developerInstructions"])

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
                platform_specific={},
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
            await agent._start_thread(transport, request)

        params = transport.send_request.await_args.args[1]
        self.assertIn("## Send files", params["developerInstructions"])
        self.assertIn("### Codex-generated images", params["developerInstructions"])
        self.assertIn("If you generate an image with Codex", params["developerInstructions"])
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])
        self.assertIn(
            "file:///Users/test/.codex/generated_images/thread-id/image-file.png",
            params["developerInstructions"],
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
                platform_specific={},
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

        await agent._start_thread(transport, request)

        params = transport.send_request.await_args.args[1]
        self.assertIn("# Avibe", params["developerInstructions"])
        self.assertIn("## Quick-reply buttons", params["developerInstructions"])
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])
        self.assertNotIn("## Show Pages", params["developerInstructions"])
        self.assertNotIn("vibe show path", params["developerInstructions"])

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
                platform_specific={"is_dm": False},
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
        developer_instructions = params["developerInstructions"]
        self.assertEqual(developer_instructions.count("Focus on regressions."), 1)
        self.assertEqual(developer_instructions.count("Current session id:"), 3)
        self.assertNotIn("Legacy session key:", developer_instructions)
        self.assertNotIn("--session-key", developer_instructions)
        self.assertEqual(developer_instructions.count("If you generate an image with Codex"), 1)
        self.assertIn("Current session id: `sesk8m4q2p7x`", developer_instructions)
        self.assertNotIn("Channel-level session key:", developer_instructions)

    async def test_resume_thread_does_not_force_model_provider_when_thread_matches_config(self):
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
        self.assertNotIn("modelProvider", params)

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
        agent._build_thread_developer_instructions = Mock(return_value=None)
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
        developer_instructions = params["developerInstructions"]
        self.assertIn("Current session id: `ses-target`", developer_instructions)
        self.assertIn("This Agent Session was forked from `ses-source`.", developer_instructions)
        self.assertIn(
            "The authoritative Avibe session id for this fork is `ses-target`.",
            developer_instructions,
        )
        self.assertIn("use `ses-target` for Show Pages", developer_instructions)
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
                has_user_turn_after_anchor=False,
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
                has_user_turn_after_anchor=True,
            ),
        ):
            thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-fork")
        self.assertEqual([call.args[0] for call in transport.send_request.await_args_list], [
            "thread/fork",
            "thread/inject_items",
        ])

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
        agent._build_thread_developer_instructions = Mock(return_value=None)
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
        agent._build_thread_developer_instructions = Mock(return_value=None)
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

    async def test_resume_thread_keeps_system_prompt_injection_when_quick_replies_are_disabled(self):
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace(config=SimpleNamespace(platform="slack", reply_enhancements=False))
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
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"thread": {"id": "thread-existing"}}))

        thread_id = await agent._start_or_resume_thread(transport, request)

        self.assertEqual(thread_id, "thread-existing")
        method, params = transport.send_request.await_args.args
        self.assertEqual(method, "thread/resume")
        self.assertIn("developerInstructions", params)
        self.assertIn("# Avibe", params["developerInstructions"])
        self.assertIn("If you generate an image with Codex", params["developerInstructions"])
        self.assertNotIn("## Quick-reply buttons", params["developerInstructions"])

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
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])
        self.assertIn("# Avibe", params["developerInstructions"])

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
        self.assertIn("Current session id: `sesk8m4q2p7x`", params["developerInstructions"])

    async def test_start_turn_uses_sandbox_policy_object(self):
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
        )
        transport = SimpleNamespace(send_request=AsyncMock(return_value={"turn": {"id": "turn-1"}}))

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
        agent.controller.get_codex_overrides.assert_not_called()

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
        created_cmd = {}

        class _FakeStdin:
            def __init__(self):
                self._closing = False

            def write(self, data):
                writes.append(data.decode())

            async def drain(self):
                return None

            def is_closing(self):
                return self._closing

            def close(self):
                self._closing = True

        class _FakeStdout:
            def __init__(self):
                self._lines = [b'{"jsonrpc":"2.0","id":1,"result":{}}\n']

            async def readline(self):
                if self._lines:
                    return self._lines.pop(0)
                await asyncio.Event().wait()
                return b""

        class _FakeStderr:
            async def readline(self):
                return b""

        class _FakeProcess:
            def __init__(self):
                self.stdin = _FakeStdin()
                self.stdout = _FakeStdout()
                self.stderr = _FakeStderr()
                self.pid = 123
                self.returncode = None

            async def wait(self):
                self.returncode = 0
                return 0

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            created_cmd["cmd"] = list(cmd)
            return _FakeProcess()

        with patch.object(
            transport_module.asyncio,
            "create_subprocess_exec",
            new=fake_create_subprocess_exec,
        ):
            transport = Transport(binary="codex", cwd="/tmp/work")
            await transport.start()
            await transport.stop()

        self.assertEqual(
            created_cmd["cmd"],
            ["codex", "--dangerously-bypass-approvals-and-sandbox", "app-server"],
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
        return agent

    async def test_server_request_refreshes_transport_activity(self):
        # An auto-approved server request must refresh the bound cwd's activity
        # so the stuck-active idle backstop doesn't force-evict a live turn that
        # recently asked for approval.
        agent = self._agent()
        agent._transport_last_activity = {"/tmp/work": 0.0}

        with patch.object(_MODULE.time, "monotonic", return_value=1234.0):
            result = await agent._on_server_request(
                "/tmp/work",
                7,
                "item/commandExecution/requestApproval",
                {"itemId": "item-1"},
            )

        self.assertEqual(result, {"approved": True})
        self.assertEqual(agent._transport_last_activity["/tmp/work"], 1234.0)

    async def test_get_or_create_transport_binds_cwd_into_server_request_cb(self):
        # The server-request callback registered on the transport must carry the
        # cwd, so invoking it (as the transport would) refreshes that cwd.
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
            with patch.object(_MODULE.time, "monotonic", return_value=999.0):
                result = await captured["cb"](1, "item/fileChange/requestApproval", {"itemId": "x"})

            self.assertEqual(result, {"approved": True})
            self.assertEqual(agent._transport_last_activity[cwd], 999.0)

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



if __name__ == "__main__":
    unittest.main()
