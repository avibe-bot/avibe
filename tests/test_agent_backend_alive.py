"""Backend liveness probe (B1) for the concise status bubble.

AgentService.backend_alive resolves the backend via the turn gate and delegates
to the per-backend probe; ClaudeAgent.backend_alive reads receiver_tasks.
Unknown states return None so the caller never false-alarms.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.base import AGENT_RUNTIME_TURN_KEY
from modules.agents.service import AgentService
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.agent import OpenCodeAgent
from modules.agents.opencode.server import OpenCodeServerManager
from core.resource_governance import AgentResourceFailure
from modules.im import MessageContext


class _Task:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def _ctx(runtime_key: str | None):
    ps = {AGENT_RUNTIME_TURN_KEY: runtime_key} if runtime_key is not None else {}
    return MessageContext(user_id="U1", channel_id="C1", platform="slack", platform_specific=ps)


class ClaudeBackendAliveTests(unittest.TestCase):
    def _agent(self, receiver_tasks):
        agent = ClaudeAgent.__new__(ClaudeAgent)  # bypass heavy __init__
        agent.receiver_tasks = receiver_tasks
        return agent

    def test_running_task_is_alive(self):
        agent = self._agent({"k1": _Task(done=False)})
        self.assertIs(agent.backend_alive(_ctx("k1")), True)

    def test_done_task_is_dead(self):
        agent = self._agent({"k1": _Task(done=True)})
        self.assertIs(agent.backend_alive(_ctx("k1")), False)

    def test_missing_key_is_unknown(self):
        agent = self._agent({"k1": _Task(done=False)})
        self.assertIsNone(agent.backend_alive(_ctx("other")))

    def test_no_runtime_key_is_unknown(self):
        agent = self._agent({"k1": _Task(done=False)})
        self.assertIsNone(agent.backend_alive(_ctx(None)))

    def test_captured_probe_stays_bound_to_receiver_generation(self):
        accepted = _Task(done=False)
        agent = self._agent({"k1": accepted})
        probe = agent.capture_backend_liveness(_ctx("k1"))

        accepted._done = True
        agent.receiver_tasks["k1"] = _Task(done=False)

        self.assertIs(agent.backend_alive(_ctx("k1")), True)
        self.assertIs(probe(), False)


class CodexBackendAliveTests(unittest.TestCase):
    def _agent(self, *, cwd_for_session, transports):
        agent = CodexAgent.__new__(CodexAgent)  # bypass heavy __init__
        agent._session_mgr = types.SimpleNamespace(get_cwd=lambda bid: cwd_for_session.get(bid))
        agent._transports = transports
        return agent

    def _ctx_base(self, base_session_id):
        return MessageContext(
            user_id="U1", channel_id="C1", platform="slack",
            platform_specific={"turn_base_session_id": base_session_id} if base_session_id else {},
        )

    def test_alive_transport(self):
        agent = self._agent(
            cwd_for_session={"b1": "/repo"},
            transports={"/repo": types.SimpleNamespace(is_alive=True)},
        )
        self.assertIs(agent.backend_alive(self._ctx_base("b1")), True)

    def test_dead_transport(self):
        agent = self._agent(
            cwd_for_session={"b1": "/repo"},
            transports={"/repo": types.SimpleNamespace(is_alive=False)},
        )
        self.assertIs(agent.backend_alive(self._ctx_base("b1")), False)

    def test_no_transport_is_unknown(self):
        agent = self._agent(cwd_for_session={"b1": "/repo"}, transports={})
        self.assertIsNone(agent.backend_alive(self._ctx_base("b1")))

    def test_no_cwd_is_unknown(self):
        agent = self._agent(cwd_for_session={}, transports={})
        self.assertIsNone(agent.backend_alive(self._ctx_base("b1")))

    def test_no_base_session_is_unknown(self):
        agent = self._agent(cwd_for_session={"b1": "/repo"}, transports={})
        self.assertIsNone(agent.backend_alive(self._ctx_base(None)))

    def test_captured_probe_stays_bound_to_transport_generation(self):
        accepted = types.SimpleNamespace(
            is_alive=True,
            has_pending_notifications=False,
        )
        agent = self._agent(
            cwd_for_session={"b1": "/repo"},
            transports={"/repo": accepted},
        )
        context = self._ctx_base("b1")
        probe = agent.capture_backend_liveness(context)

        accepted.is_alive = False
        agent._transports["/repo"] = types.SimpleNamespace(
            is_alive=True,
            has_pending_notifications=False,
        )

        self.assertIs(agent.backend_alive(context), True)
        self.assertIs(probe(), False)

    def test_captured_exit_failure_uses_accepted_transport_after_replacement(self):
        process = types.SimpleNamespace(returncode=None)
        accepted = types.SimpleNamespace(is_alive=True, _process=process)
        agent = self._agent(
            cwd_for_session={"b1": "/repo"},
            transports={"/repo": accepted},
        )
        agent.controller = types.SimpleNamespace(
            config=types.SimpleNamespace(language="en")
        )
        diagnose = agent.capture_backend_exit_failure(self._ctx_base("b1"))
        self.assertIsNotNone(diagnose)
        process.returncode = 137
        agent._transports["/repo"] = types.SimpleNamespace(
            is_alive=True,
            _process=types.SimpleNamespace(returncode=None),
        )
        pressure = AgentResourceFailure(
            kind="pids",
            message="shared cgroup limit event",
            pids_current=0,
            pids_max=4096,
        )

        with patch(
            "modules.agents.codex.agent.observe_agent_resource_pressure",
            side_effect=[pressure, None],
        ) as observe:
            diagnostic, visible = diagnose()
            self.assertEqual(diagnose(), (diagnostic, visible))

        observe.assert_called_once_with(agent.controller)
        self.assertIn("Resource diagnosis: shared cgroup limit event", diagnostic)
        self.assertIn("(0/4096)", visible)

    def test_captured_exit_failure_caches_negative_pressure_check(self):
        process = types.SimpleNamespace(returncode=None)
        accepted = types.SimpleNamespace(is_alive=False, _process=process)
        agent = self._agent(
            cwd_for_session={"b1": "/repo"},
            transports={"/repo": accepted},
        )
        agent.controller = types.SimpleNamespace(
            config=types.SimpleNamespace(language="en")
        )
        diagnose = agent.capture_backend_exit_failure(self._ctx_base("b1"))
        self.assertIsNotNone(diagnose)
        later_pressure = AgentResourceFailure(
            kind="memory",
            message="pressure from a later process",
        )

        with patch(
            "modules.agents.codex.agent.observe_agent_resource_pressure",
            side_effect=[None, later_pressure],
        ) as observe:
            # The first liveness failure is not yet a confirmed process exit.
            self.assertIsNone(diagnose())
            observe.assert_not_called()
            process.returncode = 137
            self.assertIsNone(diagnose())
            self.assertIsNone(diagnose())

        observe.assert_called_once_with(agent.controller)


class OpenCodeResourceExitTests(unittest.TestCase):
    def test_adopted_exit_consumes_pressure_only_after_original_generation_exits(self):
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.controller = types.SimpleNamespace()
        server = OpenCodeServerManager(binary="opencode", port=4096)
        pressure = AgentResourceFailure(kind="pids", message="shared cgroup limit event")

        with patch(
            "modules.agents.opencode.server.runtime.process_create_time",
            side_effect=[1000.0, 1000.0, 2000.0],
        ), patch(
            "modules.agents.opencode.agent.observe_agent_resource_pressure",
            return_value=pressure,
        ) as observe:
            server._observe_runtime_generation({"pid": 654, "started_at": 1.0})
            self.assertIsNone(agent._resource_failure_for_server(server))
            observe.assert_not_called()
            self.assertIs(agent._resource_failure_for_server(server), pressure)
            observe.assert_called_once_with(agent.controller)

    def test_pressure_checks_are_cached_per_observed_server_generation(self):
        agent = OpenCodeAgent.__new__(OpenCodeAgent)
        agent.controller = types.SimpleNamespace()
        server = OpenCodeServerManager(binary="opencode", port=4096)
        server.observed_runtime_exit_pid = (
            lambda: server._runtime_generation_token[0]
        )
        pressure = AgentResourceFailure(kind="pids", message="first exit")
        later_pressure = AgentResourceFailure(kind="memory", message="third exit")

        with patch(
            "modules.agents.opencode.server.runtime.process_create_time",
            return_value=1000.0,
        ), patch(
            "modules.agents.opencode.agent.observe_agent_resource_pressure",
            side_effect=[pressure, None, later_pressure],
        ) as observe:
            server._observe_runtime_generation({"pid": 654, "started_at": 1.0})
            self.assertIs(agent._resource_failure_for_server(server), pressure)
            self.assertIs(agent._resource_failure_for_server(server), pressure)
            server._observe_runtime_generation({"pid": 655, "started_at": 2.0})
            self.assertIsNone(agent._resource_failure_for_server(server))
            self.assertIsNone(agent._resource_failure_for_server(server))
            server._observe_runtime_generation({"pid": 656, "started_at": 3.0})
            self.assertIs(agent._resource_failure_for_server(server), later_pressure)

        self.assertEqual(observe.call_count, 3)


class AgentServiceBackendAliveTests(unittest.TestCase):
    def _service(self, *, backend_name, gate_backend, probe_return):
        svc = AgentService.__new__(AgentService)  # bypass __init__
        fake_agent = types.SimpleNamespace(backend_alive=lambda context: probe_return)
        svc.agents = {backend_name: fake_agent}
        svc._turn_gates = {"rk": types.SimpleNamespace(backend=gate_backend, token="t")}
        return svc

    def test_resolves_backend_via_gate_and_delegates(self):
        svc = self._service(backend_name="claude", gate_backend="claude", probe_return=True)
        self.assertIs(svc.backend_alive(_ctx("rk")), True)

    def test_dead_propagates(self):
        svc = self._service(backend_name="claude", gate_backend="claude", probe_return=False)
        self.assertIs(svc.backend_alive(_ctx("rk")), False)

    def test_unknown_backend_returns_none(self):
        svc = self._service(backend_name="claude", gate_backend="codex", probe_return=True)
        self.assertIsNone(svc.backend_alive(_ctx("rk")))

    def test_no_gate_returns_none(self):
        svc = self._service(backend_name="claude", gate_backend="claude", probe_return=True)
        self.assertIsNone(svc.backend_alive(_ctx("missing")))


if __name__ == "__main__":
    unittest.main()
