from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft7Validator, FormatChecker

from config.v2_config import ModelHubAgentSupplyConfig, ModelHubConfig
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    OAuthFlowState,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.events import BoundedEventLog, ResolutionEvent
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.service import ModelHubService, UnavailableEngineAdapter
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.ui_server import app

CONTRACTS = Path("docs/plans/model-hub-contracts")


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_valid(name: str, payload: dict) -> None:
    errors = sorted(
        Draft7Validator(_schema(name), format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    assert not errors, [error.message for error in errors]


class MemoryStore:
    def __init__(self):
        self.config = ModelHubConfig(
            agents={
                backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
                for backend in ("claude", "codex", "opencode")
            }
        )

    def load(self):
        return self.config

    def save(self, config):
        self.config = config


class FakeInvokeHandle:
    def __init__(self, outcome):
        self._outcome = outcome

    @property
    def stream(self):
        return None

    async def outcome(self):
        return self._outcome


class FakeAdapter:
    def __init__(self):
        self.secret_lengths = []
        self.revoked = []
        self.cancelled = []
        self.synced = []
        self.flows = {}

    async def ensure_installed(self):
        return await self.status()

    async def start(self):
        return await self.status()

    async def stop(self):
        return None

    async def status(self):
        return EngineStatus(
            health=EngineHealth.OK,
            installed_version="v7.2.95",
            verified=True,
            listen_host="127.0.0.1",
            listen_port=15220,
            last_check_iso="2026-07-23T03:40:00+00:00",
        )

    async def gateway_token(self):
        return "local-gateway-test-token"

    async def provision_credential(self, vendor, protocol, secret, base_url):
        self.secret_lengths.append(len(secret))
        return "cred_test123"

    async def revoke_credential(self, credential_ref):
        self.revoked.append(credential_ref)

    async def sync_sources(self, bindings):
        self.synced.append(tuple(bindings))

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        return ("claude-opus-4-6", "claude-sonnet-4-6")

    async def invoke(self, source_id, model_id, request, stream, origin):
        return FakeInvokeHandle(
            RawCallOutcome(
                kind=RawOutcomeKind.SUCCESS,
                http_status=200,
                error_code=None,
                redacted_message=None,
                stream_started=False,
                model_id=model_id,
                source_id=source_id,
            )
        )

    def _flow(self, source_id, flow_id):
        return OAuthFlowState(
            flow_id=flow_id,
            source_id=source_id,
            vendor="anthropic",
            state="awaiting_action",
            auth_url="https://claude.ai/oauth/authorize?test=true",
            device_code=None,
            expects="paste_code",
            instructions_key="models.oauth.claude.paste_code",
            error_key=None,
            expires_at_iso="2026-07-23T04:15:00+00:00",
            credential_ref=None,
        )

    async def start_oauth(self, source_id, vendor):
        flow = self._flow(source_id, f"oaf_{len(self.flows) + 1:08d}")
        flow = OAuthFlowState(**{**flow.__dict__, "vendor": vendor})
        self.flows[flow.flow_id] = flow
        return flow

    async def oauth_status(self, flow_id):
        return self.flows[flow_id]

    async def submit_oauth(self, flow_id, value):
        self.secret_lengths.append(len(value))
        flow = OAuthFlowState(**{**self.flows[flow_id].__dict__, "state": "verifying"})
        self.flows[flow_id] = flow
        return flow

    async def cancel_oauth(self, flow_id):
        self.cancelled.append(flow_id)


def _service(tmp_path):
    store = MemoryStore()
    adapter = FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )
    return service, store, adapter


def _assert_envelope(payload: dict, *, ok: bool = True):
    assert payload["ok"] is ok
    assert payload["contract_version"] == 1


def test_model_hub_rest_api_contract(monkeypatch, tmp_path):
    service, store, adapter = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)

    response = client.get("/api/models/sources", base_url=base_url)
    body = response.get_json()
    _assert_envelope(body)
    assert body["sources"] == []

    response = client.post(
        "/api/models/sources",
        json={
            "kind": "subscription",
            "vendor": "anthropic",
            "display_name": "Experimental subscription",
            "supply_channel": "hub",
        },
        headers=headers,
        base_url=base_url,
    )
    error = response.get_json()
    assert response.status_code == 409
    _assert_envelope(error, ok=False)
    assert error["error"] == "consent_required"

    fake_key = "sk-test-never-persist-this"
    response = client.post(
        "/api/models/sources",
        json={
            "kind": "api_key",
            "vendor": "anthropic",
            "display_name": "Anthropic API Key",
            "protocol": "anthropic",
            "key": fake_key,
        },
        headers=headers,
        base_url=base_url,
    )
    assert response.status_code == 201
    body = response.get_json()
    _assert_envelope(body)
    source = body["source"]
    _assert_valid("source.schema.json", source)
    source_id = source["id"]
    assert fake_key not in json.dumps(store.config.to_payload())
    assert adapter.secret_lengths[0] == len(fake_key)

    response = client.put(
        "/api/models/priority",
        json={"order": []},
        headers=headers,
        base_url=base_url,
    )
    error = response.get_json()
    _assert_envelope(error, ok=False)
    assert error["error"] == "invalid_priority_order"

    response = client.put(
        "/api/models/priority",
        json={"order": [source_id]},
        headers=headers,
        base_url=base_url,
    )
    priority = response.get_json()
    _assert_envelope(priority)
    _assert_valid("priority.schema.json", {"contract_version": 1, "order": priority["order"]})

    response = client.patch(
        f"/api/models/sources/{source_id}",
        json={"display_name": "Primary Anthropic"},
        headers=headers,
        base_url=base_url,
    )
    _assert_valid("source.schema.json", response.get_json()["source"])

    response = client.post(
        f"/api/models/sources/{source_id}/test",
        headers=headers,
        base_url=base_url,
    )
    body = response.get_json()
    _assert_envelope(body)
    assert body["discovered"] == 2

    response = client.post(
        "/api/models/custom-models",
        json={"source_id": source_id, "model_id": "custom-model", "display_name": "Custom Model"},
        headers=headers,
        base_url=base_url,
    )
    assert response.status_code == 201
    _assert_valid("source.schema.json", response.get_json()["source"])

    response = client.put(
        "/api/models/agents/claude/mappings",
        json={
            "mappings": [
                {"builtin_id": "claude-native", "target_model_id": "custom-model", "enabled": True}
            ]
        },
        headers=headers,
        base_url=base_url,
    )
    _assert_valid("agent-supply.schema.json", response.get_json()["agent"])

    response = client.put(
        "/api/models/agents/opencode/menu",
        json={"menu": {"view": "featured", "checked": ["anthropic/custom-model"]}},
        headers=headers,
        base_url=base_url,
    )
    _assert_valid("agent-supply.schema.json", response.get_json()["agent"])

    response = client.patch(
        "/api/models/agents/codex/mode",
        json={"mode": "direct"},
        headers=headers,
        base_url=base_url,
    )
    assert response.get_json()["agent"]["current"] is None

    agents = client.get("/api/models/agents", base_url=base_url).get_json()["agents"]
    assert len(agents) == 3
    for agent in agents:
        _assert_valid("agent-supply.schema.json", agent)

    event_example = _schema("resolution-event.schema.json")["examples"][0]
    service.events.append(ResolutionEvent(**event_example))
    events = client.get("/api/models/events?limit=1", base_url=base_url).get_json()["events"]
    assert events == [event_example]
    _assert_valid("resolution-event.schema.json", events[0])

    response = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "native_cli"},
        headers=headers,
        base_url=base_url,
    )
    flow = response.get_json()["flow"]
    _assert_valid("oauth-flow.schema.json", flow)

    flow = client.get(f"/api/models/oauth/status/{flow['flow_id']}", base_url=base_url).get_json()["flow"]
    _assert_valid("oauth-flow.schema.json", flow)

    restarted = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )
    assert asyncio.run(restarted.oauth_status(flow["flow_id"]))["channel"] == "native_cli"

    flow = client.post(
        "/api/models/oauth/submit",
        json={"flow_id": flow["flow_id"], "value": "secret-auth-code"},
        headers=headers,
        base_url=base_url,
    ).get_json()["flow"]
    _assert_valid("oauth-flow.schema.json", flow)
    assert adapter.secret_lengths[-1] == len("secret-auth-code")
    assert "secret-auth-code" not in (tmp_path / "events.json").read_text(encoding="utf-8")

    response = client.post(
        "/api/models/oauth/cancel",
        json={"flow_id": flow["flow_id"]},
        headers=headers,
        base_url=base_url,
    )
    _assert_envelope(response.get_json())
    assert adapter.cancelled == [flow["flow_id"]]
    response = client.get(f"/api/models/oauth/status/{flow['flow_id']}", base_url=base_url)
    assert response.status_code == 404
    assert response.get_json()["error"] == "flow_not_found"

    expired = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "native_cli"},
        headers=headers,
        base_url=base_url,
    ).get_json()["flow"]
    adapter.flows[expired["flow_id"]] = OAuthFlowState(
        **{**adapter.flows[expired["flow_id"]].__dict__, "expires_at_iso": "2026-07-23T02:59:00+00:00"}
    )
    response = client.get(f"/api/models/oauth/status/{expired['flow_id']}", base_url=base_url)
    assert response.status_code == 410
    assert response.get_json()["error"] == "flow_expired"

    response = client.post(
        "/api/models/oauth/start",
        json={"vendor": "anthropic", "channel": "hub", "experimental_consent": True},
        headers=headers,
        base_url=base_url,
    )
    hub_flow = response.get_json()["flow"]
    _assert_valid("oauth-flow.schema.json", hub_flow)
    assert store.config.subscription_hub_experimental is True
    adapter.flows[hub_flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[hub_flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_oauth_test",
        }
    )
    response = client.post(
        "/api/models/sources",
        json={
            "kind": "subscription",
            "vendor": "anthropic",
            "display_name": "Experimental subscription",
            "supply_channel": "hub",
            "oauth_flow_ref": hub_flow["flow_id"],
            "experimental_consent": True,
        },
        headers=headers,
        base_url=base_url,
    )
    assert response.status_code == 201
    consented_source = response.get_json()["source"]
    _assert_valid("source.schema.json", consented_source)
    assert consented_source["experimental_consent_at"] == "2026-07-23T03:00:00+00:00"

    scan = client.post("/api/models/migration/scan", headers=headers, base_url=base_url).get_json()
    _assert_valid("migration-scan.schema.json", {"items": scan["items"]})
    applied = client.post(
        "/api/models/migration/apply",
        json={"item_ids": []},
        headers=headers,
        base_url=base_url,
    ).get_json()
    _assert_envelope(applied)
    assert applied["applied"] == 0

    runtime = client.get("/api/models/runtime/status", base_url=base_url).get_json()["runtime"]
    _assert_valid("runtime-dependency.schema.json", runtime)

    response = client.delete(
        "/api/models/custom-models",
        json={"source_id": source_id, "model_id": "custom-model"},
        headers=headers,
        base_url=base_url,
    )
    _assert_valid("source.schema.json", response.get_json()["source"])

    response = client.delete(
        f"/api/models/sources/{source_id}?force=true",
        headers=headers,
        base_url=base_url,
    )
    _assert_envelope(response.get_json())
    assert adapter.revoked == ["cred_test123"]


def test_model_hub_mutations_use_existing_origin_and_csrf_guards(monkeypatch, tmp_path):
    service, _, _ = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    model_response = client.post("/api/models/migration/scan", base_url=base_url)
    config_response = client.post("/api/config", json={}, base_url=base_url)

    assert model_response.status_code == config_response.status_code == 403
    assert model_response.get_json() == config_response.get_json()


def test_native_source_configuration_does_not_require_l1_engine(tmp_path):
    store = MemoryStore()
    native = FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=UnavailableEngineAdapter(),
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=native,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))
    native.flows[flow["flow_id"]] = OAuthFlowState(
        **{**native.flows[flow["flow_id"]].__dict__, "state": "success"}
    )
    source = asyncio.run(
        service.create_source(
            {
                "kind": "subscription",
                "vendor": "anthropic",
                "display_name": "Claude native",
                "supply_channel": "native_cli",
                "oauth_flow_ref": flow["flow_id"],
                "models": [{"id": "claude-opus-4-6", "provenance": "manual"}],
            }
        )
    )

    _assert_valid("source.schema.json", source)
    assert source["supply_channel"] == "native_cli"
    assert store.config.priority_order == [source["id"]]
