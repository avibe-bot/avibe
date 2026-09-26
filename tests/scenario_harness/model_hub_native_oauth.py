from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import AsyncMock

from config.v2_config import ModelHubAgentSupplyConfig, ModelHubConfig, V2Config
from core.agent_auth_service import AgentAuthService
from core.handlers.model_hub.adapter import (
    DiscoveredModel,
    ObservationDiscovery,
    ObservationOutcome,
    OAuthFlowState,
    SourceObservation,
    make_source_observation,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.native_oauth import (
    AgentAuthNativeOAuthAdapter,
    _account_label,
    _signed_in,
    _VENDOR_BACKENDS,
)
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubService,
    UnavailableEngineAdapter,
    V2ModelHubConfigStore,
    _NATIVE_VENDOR_BACKENDS,
)
from vibe.model_hub_runtime.adapter import _OAUTH_ENDPOINTS, hub_subscription_serving_protocol


def native_custody_vendors() -> frozenset[str]:
    """Every vendor id a sanctioned CLI can hold custody of.

    Two tables state this fact — the service's route table and the native
    bridge's — and they are expected to agree. Unioning them means a drift
    widens native custody rather than inventing a hub-only vendor on top of it.
    """

    return frozenset(_NATIVE_VENDOR_BACKENDS) | frozenset(_VENDOR_BACKENDS)


def hub_only_subscription_vendors() -> tuple[str, ...]:
    """The subscription vendors the hub channel owns outright.

    Derived, not listed: a vendor is hub-only when the engine can start an OAuth
    flow for it and no sanctioned CLI holds custody of that flow's grant.

    Custody is compared on the engine start ROW, not on the vendor id, because
    one vendor can answer to two names. ``codex`` and ``openai`` share an
    endpoint, a callback provider, and an auth provider, so they are the same
    subscription reached by two ids — and the natively-custodied one of the two
    claims the row for both. A vendor added to either product table lands here
    without anyone editing a scenario; a second name for an existing vendor does
    not.
    """

    custodied = native_custody_vendors()
    native_rows = {_OAUTH_ENDPOINTS[vendor] for vendor in custodied & set(_OAUTH_ENDPOINTS)}
    return tuple(
        sorted(
            vendor
            for vendor, row in _OAUTH_ENDPOINTS.items()
            if row not in native_rows
        )
    )


class MemoryStore:
    def __init__(self, *, experimental: bool = False):
        self.config = ModelHubConfig(
            agents={
                backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
                for backend in ("claude", "codex", "opencode")
            }
        )
        self.config.subscription_hub_experimental = experimental
        self.requested_models = {"claude": "claude-opus-4-6"}

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


class FakeAgentAuthService:
    """AgentAuthService boundary fake; the native adapter remains real."""

    setup_timeout_seconds = 900.0

    def __init__(self):
        self.flows: dict[str, SimpleNamespace] = {}
        self.auth_status: dict[str, Mapping[str, Any]] = {}
        self.submissions: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.start_calls: list[tuple[str, bool]] = []

    async def start_web_setup(
        self,
        backend: str,
        *,
        force_reset: bool = True,
        owner_ref: str | None = None,
        new_source: bool = False,
        on_irreversible_start=None,
    ):
        if force_reset and on_irreversible_start is not None:
            on_irreversible_start()
        self.start_calls.append((backend, force_reset))
        flow_id = f"web_{uuid.uuid4().hex[:8]}"
        flow = SimpleNamespace(
            flow_id=flow_id,
            backend=backend,
            state="awaiting_code" if backend == "claude" else "starting",
            url=("https://claude.ai/oauth/authorize?test=true" if backend == "claude" else None),
            device_code=None,
            error=None,
            source_id=owner_ref,
            vendor={"claude": "anthropic", "codex": "openai"}.get(backend, "opencode"),
            expires_at_iso="2026-07-25T00:15:00+00:00",
            source_status={"signed_in": None, "account_label": None},
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
            "source_id": flow.source_id,
            "vendor": flow.vendor,
            "expires_at_iso": flow.expires_at_iso,
            "source_status": flow.source_status,
        }

    async def submit_web_code(self, flow_id: str, code: str) -> dict[str, Any]:
        flow = self.flows.get(flow_id)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        if "#" not in code:
            return {"ok": False, "error": "invalid_format"}
        self.submissions.append((flow_id, code))
        flow.state = "verifying"
        return {"ok": True}

    async def cancel_web_flow(self, flow_id: str) -> dict[str, Any]:
        flow = self.flows.pop(flow_id, None)
        if flow is None:
            return {"ok": False, "error": "flow_not_found"}
        self.cancelled.append(flow_id)
        return {"ok": True}

    def set_flow_source_status(
        self,
        flow_id: str,
        *,
        signed_in: bool,
        account_label: str | None,
    ) -> None:
        self.flows[flow_id].source_status = {
            "signed_in": signed_in,
            "account_label": account_label,
        }

    def expose_codex_device_flow(self, flow_id: str) -> None:
        flow = self.flows[flow_id]
        flow.url = "https://auth.openai.com/codex/device"
        flow.device_code = "T74L-XU61D"
        flow.state = "awaiting_code"

    def complete(self, flow_id: str) -> None:
        flow = self.flows[flow_id]
        flow.state = "success"
        status = self.auth_status.get(flow.backend, {})
        flow.source_status = {
            "signed_in": _signed_in(flow.backend, status),
            "account_label": _account_label(status),
        }

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
        self.agent_auth.auth_status = self.auth_status
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
            oauth_flows=OAuthFlowRegistry(
                state_dir / "oauth_flows.json",
                now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
            ),
            revocations=CredentialRevocationJournal(state_dir / "revocations.json"),
            now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
            requested_model_override=self.store.requested_model,
        )


class CustodiedNativeOAuthScenarioHarness:
    """Native Model Hub OAuth through the real AgentAuthService and CLI custody.

    Only the Claude CLI transport and its login probe are faked. The saved Hub
    ownership, the native credential lease, the shared flow lifecycle and the
    Model Hub config all run as shipped against the isolated test home.
    """

    auth_url = "https://claude.ai/oauth/authorize?fixture=1"
    account = {"active_auth_mode": "oauth", "email": "owner@example.com"}

    def __init__(self, state_dir: Path):
        config = V2Config.default()
        config.model_hub.enabled = True
        for supply in config.model_hub.agents.values():
            supply.mode = "hub"
        config.save()
        self.agent_auth = AgentAuthService(SimpleNamespace(config=config))
        self.callbacks: list[tuple[str, str]] = []
        consent = asyncio.Event()

        async def wait_for_consent(_client, request, timeout=900.0):
            if request["subtype"] != "claude_oauth_wait_for_completion":
                raise AssertionError(f"unexpected control request: {request}")
            await consent.wait()
            return {}

        async def send_callback(_client, authorization_code, state):
            self.callbacks.append((authorization_code, state))
            consent.set()

        self.agent_auth._start_claude_control_flow = AsyncMock(
            return_value=(SimpleNamespace(), self.auth_url, None)
        )
        self.agent_auth._send_claude_control_request = wait_for_consent
        self.agent_auth._send_claude_callback = send_callback
        self.agent_auth._verify_login = AsyncMock(return_value=(True, '{"loggedIn": true}'))
        self.agent_auth._refresh_backend_runtime = AsyncMock()
        self.agent_auth._disconnect_claude_client = AsyncMock()
        self.service = ModelHubService(
            store=V2ModelHubConfigStore(),
            adapter=UnavailableEngineAdapter(),
            events=BoundedEventLog(state_dir / "events.json"),
            native_oauth_adapter=AgentAuthNativeOAuthAdapter(
                self.agent_auth,
                auth_status_reader=lambda _backend: self.account,
            ),
            oauth_flows=OAuthFlowRegistry(state_dir / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(state_dir / "revocations.json"),
        )

    async def login_settled(self, flow_id: str) -> None:
        """Wait until the CLI login has finished and released its lease."""

        await self.agent_auth._web_flows[flow_id].waiter_task


@dataclass(frozen=True)
class HubOAuthStartForm:
    """What one start asks of the user, as the engine declares it.

    The real adapter reads this from the start response rather than from the
    vendor, so a case that wants a device flow sets the form and every vendor
    answers with it. There is deliberately no per-vendor table here: which form
    a given vendor was observed to issue is a fact about the pinned engine, and
    freezing a second copy of it in a harness would outlive the pin it came from.
    """

    expects: str = "paste_code"
    auth_url: str | None = "https://example.test/oauth"
    device_code: str | None = None
    instructions_key: str | None = "settings.models.oauth.pasteCode.hint"


#: The engine refusal the real adapter ends on when a finished grant has no way
#: to establish a protocol: every probe candidate raised, nothing was received,
#: and so there is no reachability evidence at all. This is what a vendor with
#: neither a response probe nor an engine-declared serving pin reaches, which is
#: why it stays the default: it is the shape a *new* start row would produce
#: before it is given a binding route (AUTH-SETUP-117).
#: A case whose grant is supposed to bind sets `engine_served_observation`.
UNPROVEN_OAUTH_OBSERVATION = make_source_observation(
    outcome=ObservationOutcome.ADAPTER_ERROR,
    reachable=None,
    authenticated=None,
    protocol=None,
    discovery=ObservationDiscovery.NOT_ATTEMPTED,
    models=(),
)


def engine_served_observation(vendor: str, *, models: tuple[str, ...]) -> SourceObservation:
    """What the real adapter reports for a subscription bound by its serving pin.

    The protocol is read from the pin rather than restated, so a pin the engine
    changes at the next bump flows into the scenario instead of being asserted
    against a frozen copy of it. Reachability and authentication come from the
    engine-managed flow that just completed: it holds the credential, and
    completing the grant is it accepting the credential.
    """

    protocol = hub_subscription_serving_protocol(vendor)
    if protocol is None:
        raise AssertionError(f"{vendor} has no engine-declared serving protocol")
    return make_source_observation(
        outcome=ObservationOutcome.OBSERVED,
        reachable=True,
        authenticated=True,
        protocol=protocol,
        discovery=ObservationDiscovery.SUCCEEDED,
        models=tuple(DiscoveredModel(id=model) for model in models),
    )


class FakeHubOAuthAdapter(UnavailableEngineAdapter):
    def __init__(self):
        self.flows: dict[str, OAuthFlowState] = {}
        self.synced = []
        self.start_form = HubOAuthStartForm()
        self.start_calls: list[str] = []
        self.observation = UNPROVEN_OAUTH_OBSERVATION
        self.observation_calls: list[tuple[str, str | None, tuple[str, ...]]] = []
        self.revoked: list[str] = []

    async def start_oauth(self, source_id: str, vendor: str) -> OAuthFlowState:
        self.start_calls.append(vendor)
        flow = OAuthFlowState(
            flow_id=f"hub_{uuid.uuid4().hex[:8]}",
            source_id=source_id,
            vendor=vendor,
            state="awaiting_action",
            auth_url=self.start_form.auth_url,
            device_code=self.start_form.device_code,
            expects=self.start_form.expects,
            instructions_key=self.start_form.instructions_key,
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
        return (DiscoveredModel(id="claude-opus-4-6"),)

    async def observe_source(
        self,
        vendor: str,
        base_url: str | None,
        credential_ref: str,
        protocol_order,
    ) -> SourceObservation:
        self.observation_calls.append((vendor, base_url, tuple(protocol_order)))
        return self.observation

    async def revoke_credential(self, credential_ref: str) -> None:
        self.revoked.append(credential_ref)

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
            oauth_flows=OAuthFlowRegistry(
                state_dir / "hub-oauth-flows.json",
                now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
            ),
            revocations=CredentialRevocationJournal(state_dir / "hub-revocations.json"),
            now=lambda: datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc),
            requested_model_override=self.store.requested_model,
        )
