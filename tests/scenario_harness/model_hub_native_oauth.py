from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from config.v2_config import ModelHubConfig
from core.handlers.model_hub.adapter import OAuthFlowState
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.native_oauth import AgentAuthNativeOAuthAdapter
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubService, UnavailableEngineAdapter


class MemoryStore:
    def __init__(self, *, experimental: bool = False):
        self.config = ModelHubConfig.fresh()
        self.config.subscription_hub_experimental = experimental

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config


class FakeAgentAuthService:
    """AgentAuthService boundary fake; the native adapter remains real."""

    setup_timeout_seconds = 900.0

    def __init__(self):
        self.flows: dict[str, SimpleNamespace] = {}
        self.submissions: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def start_web_setup(self, backend: str, *, force_reset: bool = True):
        assert force_reset is False
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
    def __init__(self, state_dir: Path):
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
        self.store = MemoryStore()
        self.service = ModelHubService(
            store=self.store,
            adapter=UnavailableEngineAdapter(),
            events=BoundedEventLog(state_dir / "events.json"),
            native_oauth_adapter=self.adapter,
            oauth_flows=OAuthFlowRegistry(state_dir / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(state_dir / "revocations.json"),
            now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
        )


class FakeHubOAuthAdapter(UnavailableEngineAdapter):
    def __init__(self):
        self.flows: dict[str, OAuthFlowState] = {}
        self.synced = []

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        flow = OAuthFlowState(
            flow_id=f"hub_{uuid.uuid4().hex[:8]}",
            source_id=source_id,
            vendor=vendor,
            state="awaiting_action",
            auth_url="https://example.test/oauth",
            device_code=None,
            expects="paste_code",
            instructions_key="settings.models.oauth.pasteCode.hint",
            error_key=None,
            expires_at_iso="2026-07-25T00:15:00+00:00",
            credential_ref=None,
        )
        self.flows[flow.flow_id] = flow
        return flow

    async def oauth_status(self, flow_id: str) -> OAuthFlowState:
        return self.flows[flow_id]

    async def sync_sources(self, bindings) -> None:
        self.synced.append(tuple(bindings))

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        return ("claude-opus-4-6",)

    def complete(self, flow_id: str) -> None:
        flow = self.flows[flow_id]
        self.flows[flow_id] = OAuthFlowState(
            **{
                **flow.__dict__,
                "state": "success",
                "credential_ref": "cred_consent01",
            }
        )


class HubOAuthScenarioHarness:
    def __init__(self, state_dir: Path):
        self.adapter = FakeHubOAuthAdapter()
        self.store = MemoryStore(experimental=True)
        self.service = ModelHubService(
            store=self.store,
            adapter=self.adapter,
            events=BoundedEventLog(state_dir / "hub-events.json"),
            oauth_flows=OAuthFlowRegistry(state_dir / "hub-oauth-flows.json"),
            revocations=CredentialRevocationJournal(state_dir / "hub-revocations.json"),
            now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
        )
