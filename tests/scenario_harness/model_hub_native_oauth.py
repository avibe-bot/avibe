from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.handlers.model_hub.native_oauth import AgentAuthNativeOAuthAdapter


class FakeAgentAuthService:
    """AgentAuthService boundary fake; the native adapter remains real."""

    setup_timeout_seconds = 900.0

    def __init__(self):
        self.flows: dict[str, SimpleNamespace] = {}
        self.submissions: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def start_web_setup(self, backend: str, *, force_reset: bool = True):
        assert force_reset is True
        flow_id = f"web_{uuid.uuid4().hex[:8]}"
        flow = SimpleNamespace(
            flow_id=flow_id,
            backend=backend,
            state="awaiting_code" if backend == "claude" else "starting",
            url=("https://claude.ai/oauth/authorize?test=true" if backend == "claude" else None),
            device_code=None,
            error=None,
        )
        self.flows[flow_id] = flow
        return flow

    def get_web_flow_status(self, flow_id: str) -> dict[str, Any]:
        flow = self.flows.get(flow_id)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        return {
            "ok": True,
            "flow_id": flow.flow_id,
            "backend": flow.backend,
            "state": flow.state,
            "url": flow.url,
            "device_code": flow.device_code,
            "error": flow.error,
        }

    async def submit_web_code(self, flow_id: str, code: str) -> dict[str, Any]:
        flow = self.flows.get(flow_id)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        self.submissions.append((flow_id, code))
        flow.state = "verifying"
        return {"ok": True}

    async def cancel_web_flow(self, flow_id: str) -> dict[str, Any]:
        flow = self.flows.pop(flow_id, None)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        self.cancelled.append(flow_id)
        return {"ok": True}

    def expose_codex_device_flow(self, flow_id: str) -> None:
        flow = self.flows[flow_id]
        flow.url = "https://auth.openai.com/codex/device"
        flow.device_code = "T74L-XU61D"
        flow.state = "awaiting_code"

    def complete(self, flow_id: str) -> None:
        self.flows[flow_id].state = "success"

    def timeout(self, flow_id: str) -> None:
        flow = self.flows[flow_id]
        flow.state = "failed"
        flow.error = "timed_out"


class NativeOAuthScenarioHarness:
    def __init__(self):
        self.agent_auth = FakeAgentAuthService()
        self.auth_status = {
            "claude": {"active_auth_mode": "oauth"},
            "codex": {"active_auth_mode": "oauth"},
        }
        self.adapter = AgentAuthNativeOAuthAdapter(
            self.agent_auth,
            auth_status_reader=lambda backend: self.auth_status[backend],
            now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
        )
