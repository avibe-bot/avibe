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
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    ObservationDiscovery,
    ObservationOutcome,
    SourceObservation,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.rpc import dispatch_model_hub_rpc
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import CONTRACT_VERSION, ModelHubError, ModelHubService
from vibe import ui_server
from vibe.ui_server import app
from tests.ui_server_test_helpers import csrf_headers


CONTRACTS = Path("docs/plans/model-hub-contracts")


@pytest.fixture(autouse=True)
def enable_model_hub(monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


class Store:
    def __init__(self, config: ModelHubConfig):
        self.config = config

    def load(self):
        return self.config

    def save(self, config):
        self.config = config

    def requested_model(self, backend):
        return ""


class Adapter:
    async def status(self):
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 15220, None)

    async def sync_sources(self, bindings):
        return None

    async def revoke_credential(self, ref):
        return None

    async def provision_transient_credential(self, vendor, secret, base_url):
        return "cred_observe01"

    async def observe_source(self, vendor, base_url, credential_ref, protocol_order):
        return SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol=protocol_order[0],
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=("claude-opus-4-6",),
        )


def source(source_id="src_api00001", model_id="claude-opus-4-6"):
    return ModelHubSourceConfig(
        id=source_id,
        kind="api_key",
        vendor="anthropic",
        display_name="API source",
        protocol="anthropic",
        supply_channel="hub",
        billing="metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id=model_id, provenance="discovered")],
        credential_ref=f"cred_{source_id}",
    )


def config_with_route():
    src = source()
    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
        for backend in ("claude", "codex", "opencode")
    }
    agents["claude"].sources.order = [src.id]
    agents["claude"].routes[src.models[0].id] = ModelHubRouteConfig(
        hops=(ModelHubRouteHopConfig(src.id, src.models[0].id),)
    )
    return ModelHubConfig(sources=[src], agents=agents)


def service(tmp_path: Path) -> ModelHubService:
    return ModelHubService(
        store=Store(config_with_route()),
        adapter=Adapter(),
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc),
    )


def assert_valid(schema_name: str, payload: dict):
    schema = json.loads((CONTRACTS / schema_name).read_text(encoding="utf-8"))
    errors = list(Draft7Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert not errors, [error.message for error in errors]


def test_contract_version_and_route_api_are_final():
    assert CONTRACT_VERSION == 5
    routes = [getattr(rule, "path", "") for rule in app.router.routes]
    assert "/api/models/sources/observe" in routes
    assert "/api/models/sources/{source_id}/refresh" in routes
    model_routes = [rule for rule in routes if rule.startswith("/api/models/")]
    assert not any("/test" in rule for rule in model_routes)
    assert not any("mappings" in rule for rule in model_routes)


def test_unsaved_observation_route_returns_response_backed_shape_without_ref(tmp_path):
    svc = service(tmp_path)
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(ui_server, "_MODEL_HUB_SERVICE", svc)
        client = app.test_client()
        response = client.post(
            "/api/models/sources/observe",
            json={
                "vendor": "anthropic",
                "base_url": None,
                "key": "sk-observation-test",
            },
            headers=csrf_headers(client),
        )
    finally:
        monkeypatch.undo()
    assert response.status_code == 200
    body = response.get_json()
    assert body["contract_version"] == 5
    observation = body["observation"]
    assert_valid("observation-result.schema.json", observation)
    assert observation["protocol"] == "anthropic"
    assert observation["models"] == ["claude-opus-4-6"]
    assert "credential_ref" not in json.dumps(body)


def test_native_oauth_rejects_duplicate_source_before_adapter(tmp_path):
    source_config = ModelHubConfig().to_payload()
    source_config["sources"] = [
        {
            **json.loads((CONTRACTS / "source.schema.json").read_text())["examples"][0],
            "models": [],
        }
    ]
    config = ModelHubConfig.from_payload(source_config)

    class NativeOAuthSpy:
        called = False

        async def start_oauth(self, source_id, vendor):
            self.called = True
            raise AssertionError("duplicate native guard must run before adapter")

    native = NativeOAuthSpy()
    svc = ModelHubService(
        store=Store(config),
        adapter=Adapter(),
        native_oauth_adapter=native,
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(svc.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))
    assert exc.value.code == "native_source_already_exists"
    assert exc.value.data == {"existing_source_id": "src_claudepro1"}
    assert native.called is False


def test_chain_schema_requires_exact_hops_and_no_mapping_fields():
    payload = {
        "contract_version": 5,
        "backend": "claude",
        "model_id": "claude-opus-4-6",
        "chain": [
            {
                "source_id": "src_api00001",
                "model_id": "claude-opus-4-6",
                "channel": "hub",
                "health": "healthy",
                "runnable": True,
                "reason": None,
                "retry_at": None,
            }
        ],
        "supply_state": "ok",
    }
    assert_valid("agent-chain.schema.json", payload)
    invalid = {**payload, "chain": [{**payload["chain"][0], "via_mapping": False}]}
    assert list(Draft7Validator(json.loads((CONTRACTS / "agent-chain.schema.json").read_text())).iter_errors(invalid))


def test_probe_schema_rejects_native_unreachable_without_reason():
    valid = {
        "contract_version": 5,
        "backend": "claude",
        "channel": "native_cli",
        "reachable": False,
        "source_id": "src_api00001",
        "model_id": "claude-opus-4-6",
        "latency_ms": None,
        "error": "models.probe.native_cli_unavailable",
    }
    assert_valid("probe-result.schema.json", valid)
    invalid = {**valid, "error": None}
    assert list(Draft7Validator(json.loads((CONTRACTS / "probe-result.schema.json").read_text())).iter_errors(invalid))


def test_rpc_route_replace_returns_guarded_success(tmp_path):
    svc = service(tmp_path)
    result = asyncio.run(
        dispatch_model_hub_rpc(
            svc,
            "set_agent_chain",
            {
                "backend": "claude",
                "model_id": "claude-opus-4-6",
                "chain": {
                    "hops": [
                        {"source_id": "src_api00001", "model_id": "claude-opus-4-6"}
                    ],
                    "force": True,
                },
            },
        )
    )
    assert set(result) == {"chain", "removed_hops", "interrupted"}
    assert result["chain"]["chain"][0]["model_id"] == "claude-opus-4-6"


def test_rpc_rejects_retired_source_order_modes(tmp_path):
    svc = service(tmp_path)
    for payload in ({"policy": "retired"}, {"mode": "retired"}):
        with pytest.raises(ModelHubError) as exc:
            asyncio.run(dispatch_model_hub_rpc(svc, "set_agent_sources", {"backend": "claude", "sources": payload}))
        assert exc.value.code == "invalid_source_order"


def test_discovered_source_model_delete_is_rejected_as_upstream_managed(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(ModelHubError) as exc:
        asyncio.run(svc.delete_custom_model("src_api00001", "claude-opus-4-6"))
    assert exc.value.code == "source_model_managed_upstream"


class FakeRemote:
    def __init__(self):
        self.refresh_args = None
        self.chain_args = None

    async def refresh_source(self, source_id, *, force=False):
        self.refresh_args = (source_id, force)
        return {"source": {"id": source_id}, "removed_hops": [], "interrupted": []}

    async def set_agent_chain(self, backend, model_id, payload):
        self.chain_args = (backend, model_id, payload)
        return {"chain": {"chain": []}, "removed_hops": [], "interrupted": []}


def test_ui_routes_use_one_refresh_endpoint_and_guarded_envelopes(monkeypatch):
    remote = FakeRemote()
    monkeypatch.setattr(ui_server, "_MODEL_HUB_SERVICE", remote)
    client = app.test_client()
    headers = csrf_headers(client)
    response = client.post("/api/models/sources/src_api00001/refresh", json={"force": True}, headers=headers)
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["source"]["id"] == "src_api00001"
    assert remote.refresh_args == ("src_api00001", True)
    response = client.put(
        "/api/models/agents/claude/chain?model=claude-opus-4-6",
        json={"hops": [], "force": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert set(response.get_json()) >= {"ok", "contract_version", "chain", "removed_hops", "interrupted"}
    assert remote.chain_args == ("claude", "claude-opus-4-6", {"hops": [], "force": True})


def test_ui_does_not_expose_mapping_route(monkeypatch):
    monkeypatch.setattr(ui_server, "_MODEL_HUB_SERVICE", FakeRemote())
    client = app.test_client()
    response = client.put("/api/models/agents/claude/mappings", json={})
    assert response.status_code in {404, 405}
