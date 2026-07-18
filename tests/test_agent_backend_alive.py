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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.agents.base import AGENT_RUNTIME_TURN_KEY
from modules.agents.service import AgentService
from modules.agents.claude_agent import ClaudeAgent
from modules.agents.codex.agent import CodexAgent
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
