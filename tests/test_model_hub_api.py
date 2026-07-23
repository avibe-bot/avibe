from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, FormatChecker

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    OAuthFlowState,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.events import BoundedEventLog, ResolutionEvent
from core.handlers.model_hub.oauth import OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService
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
        self.fail_sync = False
        self.fail_cancel = False

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
        if self.fail_sync:
            raise RuntimeError("upstream failure with sk-secret-material")

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
        if self.fail_cancel:
            raise RuntimeError("temporary engine failure")


def _service(tmp_path):
    store = MemoryStore()
    adapter = FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )
    return service, store, adapter


def _assert_envelope(payload: dict, *, ok: bool = True):
    assert payload["ok"] is ok
    assert payload["contract_version"] == 1


def test_model_hub_rest_api_contract(monkeypatch, tmp_path):
    """Scenarios: MH-PRI-001, MH-OAUTH-A-001, MH-OAUTH-ERR-001."""

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
    assert error["detail"] == "modelHub.errors.consent_required"

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
    assert source["masked_credential"] == "sk-test…this"
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
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
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
    assert service.oauth_flows.channel(hub_flow["flow_id"]) is None

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
    assert all("<" not in asset["url"] for asset in runtime["manifest"]["assets"])

    response = client.delete(
        "/api/models/custom-models",
        json={"source_id": source_id, "model_id": "custom-model"},
        headers=headers,
        base_url=base_url,
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "mode_switch_blocked"

    response = client.put(
        "/api/models/agents/claude/mappings",
        json={"mappings": []},
        headers=headers,
        base_url=base_url,
    )
    _assert_envelope(response.get_json())
    response = client.put(
        "/api/models/agents/opencode/menu",
        json={"menu": {"view": "featured", "checked": []}},
        headers=headers,
        base_url=base_url,
    )
    _assert_envelope(response.get_json())
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


@pytest.mark.parametrize(
    ("path", "method", "error"),
    [
        ("/api/models/sources/src_test0001", "patch", "discovery_failed"),
        ("/api/models/priority", "put", "invalid_priority_order"),
        ("/api/models/agents/claude/mode", "patch", "mode_switch_blocked"),
        ("/api/models/agents/claude/mappings", "put", "mapping_target_unavailable"),
        ("/api/models/agents/opencode/menu", "put", "mapping_target_unavailable"),
    ],
)
def test_model_hub_routes_reject_non_object_json_with_error_envelope(
    monkeypatch,
    tmp_path,
    path,
    method,
    error,
):
    service, _, _ = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    for payload in ([], None):
        response = getattr(client, method)(
            path,
            json=payload,
            headers=csrf_headers(client, base_url),
            base_url=base_url,
        )

        assert response.status_code == 400
        body = response.get_json()
        _assert_envelope(body, ok=False)
        assert body["error"] == error


def test_failed_hub_oauth_source_creation_revokes_credential(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(
        service.oauth_start(
            {"vendor": "anthropic", "channel": "hub", "experimental_consent": True}
        )
    )
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_oauth_rollback",
        }
    )
    adapter.fail_sync = True

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "subscription",
                    "vendor": "anthropic",
                    "display_name": "Rollback subscription",
                    "supply_channel": "hub",
                    "oauth_flow_ref": flow["flow_id"],
                    "experimental_consent": True,
                }
            )
        )

    assert exc_info.value.code == "engine_down"
    assert exc_info.value.__cause__ is None
    assert adapter.revoked == ["cred_oauth_rollback"]
    assert store.config.sources == []
    assert service.oauth_flows.channel(flow["flow_id"]) is None


def test_concurrent_completed_hub_oauth_flow_has_single_credential_owner(tmp_path):
    async def run_race():
        class BlockingSyncAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.sync_started = asyncio.Event()
                self.release_sync = asyncio.Event()
                self.block_next_sync = False

            async def sync_sources(self, bindings):
                self.synced.append(tuple(bindings))
                if self.block_next_sync:
                    self.block_next_sync = False
                    self.sync_started.set()
                    await self.release_sync.wait()

        store = MemoryStore()
        adapter = BlockingSyncAdapter()
        service = ModelHubService(
            store=store,
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        flow = await service.oauth_start(
            {"vendor": "anthropic", "channel": "hub", "experimental_consent": True}
        )
        adapter.flows[flow["flow_id"]] = OAuthFlowState(
            **{
                **adapter.flows[flow["flow_id"]].__dict__,
                "state": "success",
                "credential_ref": "cred_oauth_single_owner",
            }
        )
        payload = {
            "kind": "subscription",
            "vendor": "anthropic",
            "display_name": "Single owner",
            "supply_channel": "hub",
            "oauth_flow_ref": flow["flow_id"],
            "experimental_consent": True,
        }

        adapter.block_next_sync = True
        first = asyncio.create_task(service.create_source(payload))
        await adapter.sync_started.wait()
        second = asyncio.create_task(service.create_source(payload))
        await asyncio.sleep(0)
        adapter.release_sync.set()
        return await asyncio.gather(first, second, return_exceptions=True), store, adapter

    results, store, adapter = asyncio.run(run_race())

    assert sum(isinstance(result, dict) for result in results) == 1
    failures = [result for result in results if isinstance(result, ModelHubError)]
    assert len(failures) == 1
    assert failures[0].code == "flow_not_found"
    assert len(store.config.sources) == 1
    assert store.config.sources[0].credential_ref == "cred_oauth_single_owner"
    assert adapter.revoked == []


def test_failed_oauth_cancel_keeps_flow_retryable(tmp_path):
    async def run_cancel_retry():
        service, _, adapter = _service(tmp_path)
        flow = await service.oauth_start({"vendor": "anthropic", "channel": "native_cli"})
        adapter.fail_cancel = True
        with pytest.raises(ModelHubError) as exc_info:
            await service.oauth_cancel(flow["flow_id"])
        assert exc_info.value.code == "engine_down"
        assert service.oauth_flows.channel(flow["flow_id"]) == "native_cli"

        adapter.fail_cancel = False
        await service.oauth_cancel(flow["flow_id"])
        return service, adapter, flow["flow_id"]

    service, adapter, flow_id = asyncio.run(run_cancel_retry())

    assert service.oauth_flows.channel(flow_id) is None
    assert adapter.cancelled == [flow_id, flow_id]


def test_oauth_completion_requires_the_persisted_pending_source_identity(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))
    binding = service.oauth_flows.binding(flow["flow_id"])

    assert binding is not None
    assert binding.source_id == flow["source_id"]
    assert binding.vendor == "anthropic"

    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "source_id": "src_wrong0001",
            "state": "success",
        }
    )
    restarted = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            restarted.create_source(
                {
                    "kind": "subscription",
                    "vendor": "anthropic",
                    "display_name": "Wrong pending source",
                    "supply_channel": "native_cli",
                    "oauth_flow_ref": flow["flow_id"],
                }
            )
        )

    assert exc_info.value.code == "flow_not_found"
    assert store.config.sources == []


def test_native_oauth_cannot_record_experimental_hub_consent(tmp_path):
    service, store, adapter = _service(tmp_path)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.oauth_start(
                {
                    "vendor": "anthropic",
                    "channel": "native_cli",
                    "experimental_consent": True,
                }
            )
        )

    assert exc_info.value.code == "consent_required"
    assert store.config.subscription_hub_experimental is False
    assert adapter.flows == {}


def test_expired_oauth_flow_is_rejected_before_submit(tmp_path):
    service, _, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "expires_at_iso": "2026-07-23T02:59:00+00:00",
        }
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_submit({"flow_id": flow["flow_id"], "value": "stale-code"}))

    assert exc_info.value.code == "flow_expired"
    assert adapter.secret_lengths == []
    assert service.oauth_flows.channel(flow["flow_id"]) is None


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
    native.fail_sync = True
    service = ModelHubService(
        store=store,
        adapter=native,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=native,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
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
            }
        )
    )

    _assert_valid("source.schema.json", source)
    assert source["supply_channel"] == "native_cli"
    assert any(model["id"] == "claude-opus-4-6" for model in source["models"])
    assert {model["provenance"] for model in source["models"]} == {"discovered"}
    assert store.config.priority_order == [source["id"]]
    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )
    assert resolved.source_id == source["id"]
    assert resolved.supply_channel == "native_cli"
    assert native.synced == []

    store.config.sources[0].state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T03:05:00Z",
    )
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.test_source(source["id"]))

    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources[0].state.status == "cooldown"
    assert store.config.sources[0].state.retry_at == "2026-07-23T03:05:00Z"
    assert native.synced == []


def test_concurrent_source_creates_preserve_both_aggregate_updates(tmp_path):
    async def run_creates():
        class ConcurrentAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.discover_started = 0
                self.all_discovering = asyncio.Event()

            async def provision_credential(self, vendor, protocol, secret, base_url):
                credential_ref = f"cred_concurrent_{len(self.secret_lengths)}"
                self.secret_lengths.append(len(secret))
                return credential_ref

            async def discover_models(self, vendor, protocol, base_url, credential_ref):
                self.discover_started += 1
                if self.discover_started == 2:
                    self.all_discovering.set()
                await self.all_discovering.wait()
                return ("claude-opus-4-6",)

        store = MemoryStore()
        adapter = ConcurrentAdapter()
        service = ModelHubService(
            store=store,
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        created = await asyncio.gather(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Concurrent A",
                    "key": "sk-test-concurrent-a",
                }
            ),
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Concurrent B",
                    "key": "sk-test-concurrent-b",
                }
            ),
        )
        return created, store.config

    created, config = asyncio.run(run_creates())

    assert {source["display_name"] for source in created} == {"Concurrent A", "Concurrent B"}
    assert {source.display_name for source in config.sources} == {"Concurrent A", "Concurrent B"}
    assert set(config.priority_order) == {source.id for source in config.sources}


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:password@relay.example/v1",
        "https://relay.example/v1?api_key=sk-test-never-persist-this",
        "https://relay.example/v1?X-Amz-Signature=abcdef123456",
        "https://relay.example/v1?oauth_signature=abcdef123456",
        "https://relay.example/v1?x-authorization=opaque-value",
        "https://relay.example/v1?target=sk-test-never-persist-this",
    ],
)
def test_source_base_url_rejects_embedded_credentials(tmp_path, base_url):
    service, store, adapter = _service(tmp_path)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "Unsafe relay",
                    "base_url": base_url,
                    "key": "sk-test-transient-only",
                }
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources == []
    assert adapter.secret_lengths == []


def test_source_patch_rejects_credential_bearing_base_url(tmp_path):
    service, store, _ = _service(tmp_path)
    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "custom",
                "display_name": "Safe relay",
                "base_url": "https://relay.example/v1?api-version=2026-07-23",
                "key": "sk-test-transient-only",
            }
        )
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.patch_source(
                source["id"],
                {"base_url": "https://relay.example/v1?access_token=do-not-store"},
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources[0].base_url == "https://relay.example/v1?api-version=2026-07-23"


def test_source_display_names_reject_credential_material(tmp_path):
    service, store, adapter = _service(tmp_path)
    pasted_key = "sk-test-never-persist-this"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": pasted_key,
                    "key": "sk-test-transient-only",
                }
            )
        )
    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources == []
    assert adapter.secret_lengths == []

    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Safe source",
                "key": "sk-test-transient-only",
            }
        )
    )
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.patch_source(source["id"], {"display_name": pasted_key}))
    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources[0].display_name == "Safe source"
    assert pasted_key not in json.dumps(store.config.to_payload())


def test_api_key_is_trimmed_once_and_empty_normalized_values_are_rejected(tmp_path):
    service, store, adapter = _service(tmp_path)
    normalized = "sk-test-trim-me"

    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Trimmed source",
                "key": f" \n{normalized}\t ",
            }
        )
    )

    assert adapter.secret_lengths == [len(normalized)]
    assert source["masked_credential"] == "sk-test…m-me"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Empty source",
                    "key": " \n\t ",
                }
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert adapter.secret_lengths == [len(normalized)]
    assert [source.display_name for source in store.config.sources] == ["Trimmed source"]


def test_source_vendor_and_custom_model_ids_reject_credential_material(tmp_path):
    service, store, adapter = _service(tmp_path)
    pasted_key = "sk-model-never-persist-this"

    for payload in (
        {
            "kind": "api_key",
            "vendor": pasted_key,
            "display_name": "Safe source",
            "key": "sk-test-transient-only",
        },
        {
            "kind": "api_key",
            "vendor": "anthropic",
            "display_name": "Safe source",
            "key": "sk-test-transient-only",
            "models": [{"id": pasted_key, "provenance": "manual"}],
        },
        {
            "kind": "api_key",
            "vendor": "anthropic",
            "display_name": "Safe source",
            "key": "sk-test-transient-only",
            "models": [
                {
                    "id": "safe-model-id",
                    "display_name": pasted_key,
                    "provenance": "manual",
                }
            ],
        },
    ):
        with pytest.raises(ModelHubError) as exc_info:
            asyncio.run(service.create_source(payload))
        assert exc_info.value.code == "discovery_failed"

    assert store.config.sources == []
    assert adapter.secret_lengths == []

    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Safe source",
                "key": "sk-test-transient-only",
            }
        )
    )
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.add_custom_model({"source_id": source["id"], "model_id": pasted_key}))

    assert exc_info.value.code == "mapping_target_unavailable"
    assert pasted_key not in json.dumps(store.config.to_payload())


def test_source_patch_rejects_credential_bearing_discovered_model_id(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "custom",
                "display_name": "Safe relay",
                "base_url": "https://relay.example/v1",
                "key": "sk-test-transient-only",
            }
        )
    )

    async def credential_bearing_models(vendor, protocol, base_url, credential_ref):
        return ("sk-model-never-persist-this",)

    adapter.discover_models = credential_bearing_models
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.patch_source(
                source["id"],
                {"base_url": "https://other-relay.example/v1"},
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources[0].base_url == "https://relay.example/v1"
    assert "sk-model-never-persist-this" not in json.dumps(store.config.to_payload())


def test_metadata_only_source_patch_does_not_require_engine_sync(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Before rename",
                "key": "sk-test-transient-only",
            }
        )
    )
    sync_count = len(adapter.synced)
    adapter.fail_sync = True

    updated = asyncio.run(service.patch_source(source["id"], {"display_name": "After rename"}))

    assert updated["display_name"] == "After rename"
    assert store.config.sources[0].display_name == "After rename"
    assert len(adapter.synced) == sync_count
