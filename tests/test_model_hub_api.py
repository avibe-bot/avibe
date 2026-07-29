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
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    OAuthFlowState,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.errors import ModelDiscoveryError
from core.handlers.model_hub.events import BoundedEventLog, ResolutionEvent
from core.handlers.model_hub.oauth import NativeOAuthSourceStatus, OAuthFlowRegistry
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    CONTRACT_VERSION,
    ModelHubError,
    ModelHubService,
    create_default_service,
)
from vibe.model_hub_client import ModelHubRemoteService, _decode
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.ui_server import app

CONTRACTS = Path("docs/plans/model-hub-contracts")


@pytest.fixture(autouse=True)
def _enable_model_hub_for_existing_contract_tests(monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")


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
        self.native_signed_in = True
        self.native_account_label = None
        self.oauth_start_calls = []

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
        self.oauth_start_calls.append((source_id, vendor))
        flow = self._flow(source_id, f"oaf_{len(self.flows) + 1:08d}")
        flow = OAuthFlowState(**{**flow.__dict__, "vendor": vendor})
        self.flows[flow.flow_id] = flow
        return flow

    async def start_reauth(self, source_id, vendor):
        return await self.start_oauth(source_id, vendor)

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

    def completed_source_status(self, flow_id):
        if flow_id not in self.flows:
            raise KeyError(flow_id)
        return NativeOAuthSourceStatus(
            signed_in=self.native_signed_in,
            account_label=self.native_account_label,
        )


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


async def _create_source(service: ModelHubService, payload: dict) -> dict:
    return (await service.create_source(payload))["source"]


def _assert_envelope(payload: dict, *, ok: bool = True):
    assert payload["ok"] is ok
    assert payload["contract_version"] == CONTRACT_VERSION


def test_default_service_uses_real_engine_adapter(monkeypatch, tmp_path):
    from vibe.model_hub_runtime import adapter as runtime_adapter
    from vibe.model_hub_runtime import supervisor as runtime_supervisor
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    monkeypatch.setattr(runtime_adapter, "_adapter", None)
    monkeypatch.setattr(runtime_supervisor, "_supervisor", None)

    service = create_default_service(native_oauth_adapter=FakeAdapter())

    assert isinstance(service.adapter, CLIProxyEngineAdapter)
    assert service.adapter.supervisor.state_store.root.is_relative_to(tmp_path)


def test_default_service_uses_real_native_oauth_adapter(monkeypatch, tmp_path):
    from core.handlers.model_hub.native_oauth import AgentAuthNativeOAuthAdapter
    from vibe import api

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    agent_auth_service = object()
    monkeypatch.setattr(api, "_get_oauth_service", lambda: agent_auth_service)

    service = create_default_service(adapter=FakeAdapter())

    assert isinstance(service.native_oauth_adapter, AgentAuthNativeOAuthAdapter)
    assert service.native_oauth_adapter._agent_auth_service is agent_auth_service


def test_runtime_status_observes_engine_without_starting_it(tmp_path):
    class LifecycleAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.start_calls = 0
            self.status_calls = 0

        async def start(self):
            self.start_calls += 1
            return await super().start()

        async def status(self):
            self.status_calls += 1
            return await super().status()

    adapter = LifecycleAdapter()
    service = ModelHubService(
        store=MemoryStore(),
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    runtime = asyncio.run(service.runtime_status())

    assert adapter.start_calls == 0
    assert adapter.status_calls == 1
    assert runtime["status"]["health"] == "ok"
    assert runtime["status"]["verified"] is True


def test_runtime_status_reports_packaged_engine_manifest(tmp_path):
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    service, _store, _adapter = _service(tmp_path)

    runtime = asyncio.run(service.runtime_status())

    assert runtime["manifest"] == EngineRuntimeManager(offline=True).contract_manifest()
    assert [asset["platform"] for asset in runtime["manifest"]["assets"]] == [
        "darwin-arm64",
        "darwin-x64",
        "linux-amd64",
        "linux-arm64",
    ]


def test_runtime_status_reports_observed_not_installed_state(tmp_path):
    class NotInstalledAdapter(FakeAdapter):
        async def start(self):
            raise AssertionError("runtime_status must not start the engine")

        async def status(self):
            return EngineStatus(
                health=EngineHealth.NOT_INSTALLED,
                installed_version=None,
                verified=False,
                listen_host="127.0.0.1",
                listen_port=None,
                last_check_iso=None,
            )

    service = ModelHubService(
        store=MemoryStore(),
        adapter=NotInstalledAdapter(),
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    runtime = asyncio.run(service.runtime_status())

    assert runtime["status"] == {
        "installed_version": None,
        "verified": False,
        "listening": None,
        "health": "not_installed",
        "last_check": None,
    }


def test_discovery_probe_failure_is_not_reported_as_engine_down(tmp_path):
    class DiscoveryFailureAdapter(FakeAdapter):
        async def discover_models(self, vendor, protocol, base_url, credential_ref):
            raise ModelDiscoveryError("upstream rejected the credential")

    store = MemoryStore()
    adapter = DiscoveryFailureAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    with pytest.raises(ModelHubError) as error:
        asyncio.run(
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "openai",
                    "key": "sk-test-invalid-but-syntactically-valid",
                },
            )
        )

    assert error.value.code == "discovery_failed"
    assert adapter.revoked == ["cred_test123"]
    assert store.config.sources == []


def test_agents_endpoint_projects_builtin_models_and_standard_vendors(tmp_path):
    """Integration (2026-07-24): list_agents() carries the read-only builtin_models /
    standard_vendors projections straight from the backend modules, so the UI never
    hand-mirrors a fixed menu or the OpenCode vendor list (agent-supply v1.2)."""
    from core.handlers.model_hub.identifiers import STANDARD_OPENCODE_VENDOR_IDS
    from vibe.backend_model_catalog import backend_model_entries, load_bundled_catalog

    service, _store, _adapter = _service(tmp_path)
    agents = {agent["backend"]: agent for agent in service.list_agents()}
    for agent in agents.values():
        _assert_valid("agent-supply.schema.json", agent)

    catalog = load_bundled_catalog()
    for backend in ("claude", "codex"):
        expected = [entry["id"] for entry in backend_model_entries(backend, catalog)]
        assert expected, f"bundled catalog must list built-in {backend} models"
        assert agents[backend]["builtin_models"] == expected
        assert agents[backend]["standard_vendors"] is None

    assert agents["opencode"]["builtin_models"] is None
    assert agents["opencode"]["standard_vendors"] == sorted(STANDARD_OPENCODE_VENDOR_IDS)


def test_agents_endpoint_projects_each_enabled_named_agent_live(tmp_path):
    service, store, _adapter = _service(tmp_path)
    store.requested_model = lambda backend: {
        "claude": "claude-sonnet-4-6",
        "codex": "gpt-5.3-codex",
    }.get(backend)
    service.named_agents_override = lambda backend: {
        "claude": [("pm", None), ("reviewer", "claude-opus-4-6")],
        "codex": [("codex", None)],
        "opencode": [],
    }[backend]

    agents = {agent["backend"]: agent for agent in service.list_agents()}

    assert agents["claude"]["named_agents"] == [
        {
            "name": "pm",
            "effective_model_id": "claude-sonnet-4-6",
            "supply_status": "interrupted",
        },
        {
            "name": "reviewer",
            "effective_model_id": "claude-opus-4-6",
            "supply_status": "interrupted",
        },
    ]
    assert agents["codex"]["named_agents"] == [
        {
            "name": "codex",
            "effective_model_id": "gpt-5.3-codex",
            "supply_status": "interrupted",
        }
    ]
    assert agents["opencode"]["named_agents"] == []

    store.config.agents["claude"].mode = "direct"
    direct = service.get_agent_sources("claude")
    assert direct["named_agents"][0] == {
        "name": "pm",
        "effective_model_id": "claude-sonnet-4-6",
        "supply_status": None,
    }


def test_ui_model_hub_default_is_controller_rpc_client(monkeypatch):
    monkeypatch.setattr(ui_server, "_MODEL_HUB_SERVICE", None)

    service = ui_server._model_hub_service()

    assert isinstance(service, ModelHubRemoteService)
    assert not hasattr(service, "adapter")


def test_ui_model_hub_rpc_preserves_controller_error_contract():
    import httpx

    response = httpx.Response(
        409,
        json={
            "ok": False,
            "error": "mode_switch_blocked",
            "detail": "modelHub.errors.mode_switch_blocked",
        },
    )

    with pytest.raises(ModelHubError) as exc_info:
        _decode(response)

    assert exc_info.value.code == "mode_switch_blocked"
    assert exc_info.value.status == 409
    assert exc_info.value.detail == "modelHub.errors.mode_switch_blocked"


def test_ui_model_hub_rpc_preserves_structured_guard_data():
    import httpx

    would_interrupt = [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": ["pm"],
        }
    ]
    response = httpx.Response(
        409,
        json={
            "ok": False,
            "error": "source_last_supplier",
            "detail": "modelHub.errors.source_last_supplier",
            "would_interrupt": would_interrupt,
        },
    )

    with pytest.raises(ModelHubError) as exc_info:
        _decode(response)

    assert exc_info.value.data == {"would_interrupt": would_interrupt}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/models/sources"),
        ("POST", "/api/models/sources"),
        ("PATCH", "/api/models/sources/src_test0001"),
        ("PUT", "/api/models/sources/src_test0001/credential"),
        ("POST", "/api/models/sources/src_test0001/reauth"),
        ("DELETE", "/api/models/sources/src_test0001"),
        ("POST", "/api/models/sources/src_test0001/test"),
        ("GET", "/api/models/agents"),
        ("GET", "/api/models/agents/claude/sources"),
        ("PUT", "/api/models/agents/claude/sources"),
        ("PATCH", "/api/models/agents/claude/mode"),
        ("PUT", "/api/models/agents/claude/mappings"),
        ("PUT", "/api/models/agents/opencode/menu"),
        ("POST", "/api/models/custom-models"),
        ("DELETE", "/api/models/custom-models"),
        ("GET", "/api/models/events?limit=invalid"),
        ("POST", "/api/models/oauth/start"),
        ("GET", "/api/models/oauth/status/oaf_test0001"),
        ("POST", "/api/models/oauth/submit"),
        ("POST", "/api/models/oauth/cancel"),
        ("POST", "/api/models/migration/scan"),
        ("POST", "/api/models/migration/apply"),
        ("GET", "/api/models/runtime/status"),
    ],
)
def test_disabled_model_hub_rest_surface_returns_feature_disabled_without_runtime_work(
    monkeypatch,
    method,
    path,
):
    from vibe.model_hub_runtime.installer import EngineRuntimeManager
    from vibe.model_hub_runtime.supervisor import EngineSupervisor

    remote_service_calls = []
    installer_calls = []
    supervisor_calls = []

    def unexpected_remote_access_read():
        raise AssertionError("disabled Model Hub must short-circuit before remote access config")

    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    monkeypatch.setattr(ui_server, "_load_remote_access_config", unexpected_remote_access_read)
    monkeypatch.setattr(
        "vibe.model_hub_client.ModelHubRemoteService",
        lambda: remote_service_calls.append(True),
    )
    monkeypatch.setattr(
        EngineRuntimeManager,
        "ensure",
        lambda *_args, **_kwargs: installer_calls.append(True),
    )
    monkeypatch.setattr(
        EngineSupervisor,
        "ensure_running",
        lambda *_args, **_kwargs: supervisor_calls.append(True),
    )
    client = app.test_client()

    response = getattr(client, method.lower())(path, json={})

    assert response.status_code == 404
    assert response.get_json() == {
        "ok": False,
        "contract_version": CONTRACT_VERSION,
        "error": "feature_disabled",
    }
    assert remote_service_calls == []
    assert installer_calls == []
    assert supervisor_calls == []


@pytest.mark.parametrize(("env_value", "expected"), [(None, False), ("0", False), ("1", True)])
def test_config_capability_exactly_projects_backend_model_hub_gate(monkeypatch, env_value, expected):
    from config.v2_config import is_model_hub_enabled

    if env_value is None:
        monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    else:
        monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", env_value)

    response = app.test_client().get("/api/config")

    assert response.status_code == 200
    enabled = response.get_json()["capabilities"]["model_hub"]["enabled"]
    assert enabled is expected
    assert enabled is is_model_hub_enabled()


def test_disabled_gate_preserves_existing_model_hub_config_bytes(monkeypatch):
    from config import paths
    from core.services.settings import default_config

    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    config = default_config()
    config.model_hub.subscription_hub_experimental = True
    config.save()
    config_path = paths.get_config_path()
    before = config_path.read_bytes()
    client = app.test_client()

    assert client.get("/api/config").status_code == 200
    assert client.get("/api/models/sources").status_code == 404

    assert config_path.read_bytes() == before


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
    assert set(body) == {"ok", "contract_version", "source", "adopted_by"}
    assert {item["backend"] for item in body["adopted_by"]} == {
        "claude",
        "codex",
        "opencode",
    }
    source = body["source"]
    _assert_valid("source.schema.json", source)
    source_id = source["id"]
    assert source["masked_credential"] == "sk-test…this"
    assert fake_key not in json.dumps(store.config.to_payload())
    assert adapter.secret_lengths[0] == len(fake_key)

    response = client.put(
        "/api/models/agents/claude/sources",
        json={"policy": "custom", "order": ["src_missing01"]},
        headers=headers,
        base_url=base_url,
    )
    error = response.get_json()
    _assert_envelope(error, ok=False)
    assert error["error"] == "invalid_source_order"

    response = client.put(
        "/api/models/agents/claude/sources",
        json={
            "policy": "follow",
            "unexpected": True,
            "another_rejected_key": True,
        },
        headers=headers,
        base_url=base_url,
    )
    error = response.get_json()
    assert error["error"] == "invalid_source_order"
    assert error["detail"] == "modelHub.errors.invalid_source_order"
    assert error["rejected_keys"] == ["another_rejected_key", "unexpected"]

    credential_shaped_key = "sk-secret-material-that-must-not-reflect"
    response = client.put(
        "/api/models/agents/claude/sources",
        json={"policy": "follow", credential_shaped_key: True},
        headers=headers,
        base_url=base_url,
    )
    error = response.get_json()
    assert credential_shaped_key not in json.dumps(error)
    assert error["rejected_keys"] == ["[redacted]"]

    response = client.put(
        "/api/models/agents/claude/sources",
        json={"policy": "custom", "order": []},
        headers=headers,
        base_url=base_url,
    )
    custom = response.get_json()
    _assert_envelope(custom)
    assert custom["agent"]["sources"]["policy"] == "custom"
    assert custom["agent"]["sources"]["order"] == []

    codex_sources = client.get(
        "/api/models/agents/codex/sources",
        base_url=base_url,
    ).get_json()["agent"]["sources"]
    assert codex_sources["policy"] == "follow"
    assert codex_sources["order"] == [source_id]

    restored = client.put(
        "/api/models/agents/claude/sources",
        json={"policy": "follow"},
        headers=headers,
        base_url=base_url,
    ).get_json()
    assert restored["agent"]["sources"]["policy"] == "follow"
    assert restored["agent"]["sources"]["order"] == [source_id]

    response = client.patch(
        f"/api/models/sources/{source_id}",
        json={"display_name": "Primary Anthropic"},
        headers=headers,
        base_url=base_url,
    )
    _assert_valid("source.schema.json", response.get_json()["source"])

    persisted_source = store.config.sources[0]
    immutable_identity = (
        persisted_source.id,
        persisted_source.created_at,
        persisted_source.credential_ref,
    )
    persisted_source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.balance_exhausted",
    )
    response = client.post(
        f"/api/models/sources/{source_id}/test",
        headers=headers,
        base_url=base_url,
    )
    body = response.get_json()
    _assert_envelope(body)
    assert body["discovered"] == 2
    assert store.config.sources[0].state.status == "standby"
    assert (
        store.config.sources[0].id,
        store.config.sources[0].created_at,
        store.config.sources[0].credential_ref,
    ) == immutable_identity

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
        json={"mappings": [{"builtin_id": "claude-native", "target_model_id": "custom-model", "enabled": True}]},
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
        assert {
            "selected_by_agent",
            "selected_model_id",
            "current",
            "sources",
            "supply_status",
            "model_supply",
            "named_agents",
        } <= set(agent)
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
    assert asyncio.run(restarted.oauth_status(flow["flow_id"]))["flow"]["channel"] == "native_cli"

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
    oauth_creation = response.get_json()
    assert set(oauth_creation) == {
        "ok",
        "contract_version",
        "source",
        "adopted_by",
    }
    consented_source = oauth_creation["source"]
    _assert_valid("source.schema.json", consented_source)
    assert consented_source["experimental_consent_at"] == "2026-07-23T03:00:00+00:00"
    completed_binding = service.oauth_flows.binding(hub_flow["flow_id"])
    assert completed_binding is not None
    assert completed_binding.completed is True

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
        (
            "/api/models/sources/src_test0001/credential",
            "put",
            "discovery_failed",
        ),
        (
            "/api/models/sources/src_test0001/reauth",
            "post",
            "discovery_failed",
        ),
        ("/api/models/agents/claude/sources", "put", "invalid_source_order"),
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


def test_native_reauth_route_requires_ack_before_oauth_and_returns_reauth_tail(
    monkeypatch,
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    native = ModelHubSourceConfig(
        id="src_native0001",
        kind="subscription",
        vendor="anthropic",
        display_name="Claude subscription",
        protocol="anthropic",
        supply_channel="native_cli",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
    )
    store.config.sources.append(native)
    store.config.refresh_follow_orders()
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)

    refused = client.post(
        f"/api/models/sources/{native.id}/reauth",
        json={},
        headers=headers,
        base_url=base_url,
    )

    assert refused.status_code == 409
    assert refused.get_json()["error"] == "reauth_confirmation_required"
    assert adapter.oauth_start_calls == []
    assert store.config.sources[0].state.status == "standby"
    assert [model.id for model in store.config.sources[0].models] == ["claude-opus-4-6"]

    original_start_reauth = adapter.start_reauth

    async def assert_source_unavailable_before_logout(source_id, vendor):
        persisted = store.config.sources[0]
        assert persisted.state.status == "needs_action"
        assert persisted.models == []
        return await original_start_reauth(source_id, vendor)

    adapter.start_reauth = assert_source_unavailable_before_logout
    started = client.post(
        f"/api/models/sources/{native.id}/reauth",
        json={"acknowledge_irreversible": True},
        headers=headers,
        base_url=base_url,
    ).get_json()
    flow = started["flow"]

    assert flow["intent"] == "reauth"
    assert adapter.oauth_start_calls == [(native.id, "anthropic")]
    assert store.config.sources[0].state.status == "needs_action"
    assert store.config.sources[0].models == []

    adapter.native_account_label = "claude@example.test"
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
        }
    )
    completed = client.get(
        f"/api/models/oauth/status/{flow['flow_id']}",
        base_url=base_url,
    ).get_json()

    assert completed["flow"]["intent"] == "reauth"
    assert set(completed) == {
        "ok",
        "contract_version",
        "flow",
        "source",
        "recovered",
        "interrupted_pairs",
    }
    assert completed["recovered"] is False
    assert completed["source"]["account_label"] == "claude@example.test"
    assert completed["source"]["state"]["status"] == "standby"


def test_native_reauth_reuses_pending_flow_per_source(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_native0001",
        kind="subscription",
        vendor="anthropic",
        display_name="Claude subscription",
        protocol="anthropic",
        supply_channel="native_cli",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
    )
    store.config.sources.append(source)
    store.config.refresh_follow_orders()
    payload = {"acknowledge_irreversible": True}

    first = asyncio.run(service.reauth_source(source.id, payload))["flow"]
    second = asyncio.run(service.reauth_source(source.id, payload))["flow"]

    assert second["flow_id"] == first["flow_id"]
    assert adapter.oauth_start_calls == [(source.id, "anthropic")]


def test_native_reauth_post_login_discovery_failure_reports_honest_gaps(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_native0001",
        kind="subscription",
        vendor="anthropic",
        display_name="Claude subscription",
        protocol="anthropic",
        supply_channel="native_cli",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
    )
    store.config.sources.append(source)
    store.config.refresh_follow_orders()
    store.requested_model = lambda backend: ("claude-opus-4-6" if backend == "claude" else "")
    flow = asyncio.run(
        service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
    )["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
        }
    )

    def fail_completed_status(_flow_id):
        raise RuntimeError("new login cannot be inspected")

    adapter.completed_source_status = fail_completed_status
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "discovery_failed"
    assert exc_info.value.data["interrupted_pairs"] == [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": [],
        }
    ]
    assert store.config.sources[0].models == []
    assert store.config.sources[0].state.status == "error"


def test_native_reauth_registry_failure_keeps_irreversible_state_honest(
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_native0001",
        kind="subscription",
        vendor="anthropic",
        display_name="Claude subscription",
        protocol="anthropic",
        supply_channel="native_cli",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
    )
    store.config.sources.append(source)
    store.config.refresh_follow_orders()

    def fail_remember(*args, **kwargs):
        raise OSError("registry unavailable")

    service.oauth_flows.remember = fail_remember
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.reauth_source(
                source.id,
                {"acknowledge_irreversible": True},
            )
        )

    assert exc_info.value.code == "engine_down"
    assert adapter.oauth_start_calls == [(source.id, "anthropic")]
    assert store.config.sources[0].models == []
    assert store.config.sources[0].state.status == "needs_action"


def test_hub_reauth_is_transactional_without_native_acknowledgement(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        experimental_consent_at="2026-07-23T02:00:00+00:00",
        billing="monthly",
        state=ModelHubSourceStateConfig(
            status="error",
            detail_key="models.source.error.unclassified",
        ),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_old",
    )
    store.config.sources.append(source)
    store.config.subscription_hub_experimental = True
    store.config.refresh_follow_orders()

    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    assert flow["intent"] == "reauth"
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_new",
        }
    )
    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["recovered"] is True
    assert result["source"]["credential_ref"] == "cred_hub_new"
    assert result["source"]["state"]["status"] == "standby"
    assert "adopted_by" not in result
    assert adapter.revoked == ["cred_hub_old"]
    assert service.revocations.list() == []


def test_hub_reauth_refreshes_discovery_when_engine_reuses_credential_ref(
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        experimental_consent_at="2026-07-23T02:00:00+00:00",
        billing="monthly",
        state=ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        ),
        models=[
            ModelHubModelConfig(
                id="stale-entitlement",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_reused",
    )
    store.config.sources.append(source)
    store.config.subscription_hub_experimental = True
    store.config.refresh_follow_orders()

    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_reused",
        }
    )
    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["recovered"] is True
    assert result["source"]["state"]["status"] == "standby"
    assert {model["id"] for model in result["source"]["models"]} == {
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
    assert adapter.revoked == []
    assert service.revocations.list() == []


def test_failed_same_handle_hub_reauth_does_not_revoke_active_credential(
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        experimental_consent_at="2026-07-23T02:00:00+00:00",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_reused",
    )
    store.config.sources.append(source)
    store.config.subscription_hub_experimental = True
    store.config.refresh_follow_orders()
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_reused",
        }
    )

    async def fail_discovery(vendor, protocol, base_url, credential_ref):
        raise ModelDiscoveryError("safe discovery failure")

    adapter.discover_models = fail_discovery
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "discovery_failed"
    assert json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    ) == before
    assert adapter.revoked == []
    assert service.revocations.list() == []


def test_failed_hub_reauth_preserves_prior_source_and_revokes_replacement(
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        experimental_consent_at="2026-07-23T02:00:00+00:00",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_old",
    )
    store.config.sources.append(source)
    store.config.subscription_hub_experimental = True
    store.config.refresh_follow_orders()
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_new",
        }
    )
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    async def fail_discovery(vendor, protocol, base_url, credential_ref):
        raise ModelDiscoveryError("safe discovery failure")

    adapter.discover_models = fail_discovery
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "discovery_failed"
    assert json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    ) == before
    assert adapter.revoked == ["cred_hub_new"]
    assert service.oauth_flows.binding(flow["flow_id"]) is None


def test_failed_hub_reauth_rolls_back_when_old_journal_cleanup_fails(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        experimental_consent_at="2026-07-23T02:00:00+00:00",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_old",
    )
    store.config.sources.append(source)
    store.config.subscription_hub_experimental = True
    store.config.refresh_follow_orders()
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_new",
        }
    )
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )
    original_remove = service.revocations.remove

    def fail_old_journal_cleanup(source_id, credential_ref):
        if credential_ref == "cred_hub_old":
            raise OSError("journal cleanup failed")
        return original_remove(source_id, credential_ref)

    service.revocations.remove = fail_old_journal_cleanup
    adapter.fail_sync = True

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "engine_down"
    assert json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    ) == before
    assert adapter.revoked == ["cred_hub_new"]
    assert service.oauth_flows.binding(flow["flow_id"]) is None


def test_credential_route_carries_body_force_override_and_structured_guard(
    monkeypatch,
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    provision_count = 0

    async def provision(vendor, protocol, secret, base_url):
        nonlocal provision_count
        provision_count += 1
        adapter.secret_lengths.append(len(secret))
        return f"cred_route_{provision_count}"

    adapter.provision_credential = provision
    created = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Guarded source",
                "protocol": "anthropic",
                "key": "sk-original-route-key",
            }
        )
    )["source"]
    store.requested_model = lambda backend: (
        "claude-opus-4-6" if backend == "claude" else ""
    )

    async def discover_narrower(vendor, protocol, base_url, credential_ref):
        return ("replacement-only-model",)

    adapter.discover_models = discover_narrower
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)
    request_body = {"key": "sk-narrower-route-key"}

    refused = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json=request_body,
        headers=headers,
        base_url=base_url,
    )

    assert refused.status_code == 409
    refusal = refused.get_json()
    assert refusal["error"] == "source_last_supplier"
    assert refusal["would_interrupt"] == [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": [],
        }
    ]
    committed = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json={**request_body, "force": True},
        headers=headers,
        base_url=base_url,
    ).get_json()

    assert committed["recovered"] is False
    assert committed["interrupted_pairs"] == refusal["would_interrupt"]
    assert committed["source"]["credential_ref"] == "cred_route_3"
    assert adapter.revoked == ["cred_route_2", "cred_route_1"]


def test_failed_hub_oauth_source_creation_revokes_credential(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "hub", "experimental_consent": True}))[
        "flow"
    ]
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
            _create_source(
                service,
                {
                    "kind": "subscription",
                    "vendor": "anthropic",
                    "display_name": "Rollback subscription",
                    "supply_channel": "hub",
                    "oauth_flow_ref": flow["flow_id"],
                    "experimental_consent": True,
                },
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
        flow = (await service.oauth_start({"vendor": "anthropic", "channel": "hub", "experimental_consent": True}))[
            "flow"
        ]
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
        first = asyncio.create_task(_create_source(service, payload))
        await adapter.sync_started.wait()
        second = asyncio.create_task(_create_source(service, payload))
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
        flow = (await service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))["flow"]
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
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))["flow"]
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
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))["flow"]
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

    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))["flow"]
    native.flows[flow["flow_id"]] = OAuthFlowState(**{**native.flows[flow["flow_id"]].__dict__, "state": "success"})
    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "subscription",
                "vendor": "anthropic",
                "display_name": "Claude native",
                "supply_channel": "native_cli",
                "oauth_flow_ref": flow["flow_id"],
            },
        )
    )

    _assert_valid("source.schema.json", source)
    assert source["supply_channel"] == "native_cli"
    assert any(model["id"] == "claude-opus-4-6" for model in source["models"])
    assert {model["provenance"] for model in source["models"]} == {"discovered"}
    assert store.config.effective_source_order("claude") == [source["id"]]
    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))
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
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Concurrent A",
                    "key": "sk-test-concurrent-a",
                },
            ),
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Concurrent B",
                    "key": "sk-test-concurrent-b",
                },
            ),
        )
        return created, store.config

    created, config = asyncio.run(run_creates())

    assert {source["display_name"] for source in created} == {"Concurrent A", "Concurrent B"}
    assert {source.display_name for source in config.sources} == {"Concurrent A", "Concurrent B"}
    assert all(
        set(config.effective_source_order(backend)) == {source.id for source in config.sources}
        for backend in ("claude", "codex", "opencode")
    )


def test_follow_auto_adopts_new_sources_while_custom_stays_frozen(tmp_path):
    service, store, _ = _service(tmp_path)
    first = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "First",
                "key": "sk-test-first-source",
            }
        )
    )
    first_id = first["source"]["id"]
    assert {item["backend"] for item in first["adopted_by"]} == {
        "claude",
        "codex",
        "opencode",
    }

    asyncio.run(
        service.set_agent_sources(
            "claude",
            {"policy": "custom", "order": [first_id]},
        )
    )
    second = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "openai",
                "display_name": "Second",
                "key": "sk-test-second-source",
            }
        )
    )
    second_id = second["source"]["id"]

    assert store.config.agents["claude"].sources.order == [first_id]
    assert "claude" not in {item["backend"] for item in second["adopted_by"]}
    assert set(store.config.effective_source_order("codex")) == {
        first_id,
        second_id,
    }

    restored = asyncio.run(service.set_agent_sources("claude", {"policy": "follow"}))
    assert restored["sources"]["policy"] == "follow"
    assert restored["sources"]["order"] == store.config.recommended_source_order("claude")


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
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "display_name": "Unsafe relay",
                    "base_url": base_url,
                    "key": "sk-test-transient-only",
                },
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources == []
    assert adapter.secret_lengths == []


def test_source_patch_rejects_credential_bearing_base_url(tmp_path):
    service, store, _ = _service(tmp_path)
    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "custom",
                "display_name": "Safe relay",
                "base_url": "https://relay.example/v1?api-version=2026-07-23",
                "key": "sk-test-transient-only",
            },
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
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": pasted_key,
                    "key": "sk-test-transient-only",
                },
            )
        )
    assert exc_info.value.code == "discovery_failed"
    assert store.config.sources == []
    assert adapter.secret_lengths == []

    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Safe source",
                "key": "sk-test-transient-only",
            },
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
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Trimmed source",
                "key": f" \n{normalized}\t ",
            },
        )
    )

    assert adapter.secret_lengths == [len(normalized)]
    assert source["masked_credential"] == "sk-test…m-me"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            _create_source(
                service,
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Empty source",
                    "key": " \n\t ",
                },
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
            asyncio.run(_create_source(service, payload))
        assert exc_info.value.code == "discovery_failed"

    assert store.config.sources == []
    assert adapter.secret_lengths == []

    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Safe source",
                "key": "sk-test-transient-only",
            },
        )
    )
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.add_custom_model({"source_id": source["id"], "model_id": pasted_key}))

    assert exc_info.value.code == "mapping_target_unavailable"
    assert pasted_key not in json.dumps(store.config.to_payload())


def test_source_patch_rejects_credential_bearing_discovered_model_id(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "custom",
                "display_name": "Safe relay",
                "base_url": "https://relay.example/v1",
                "key": "sk-test-transient-only",
            },
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
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Before rename",
                "key": "sk-test-transient-only",
            },
        )
    )
    sync_count = len(adapter.synced)
    adapter.fail_sync = True

    updated = asyncio.run(service.patch_source(source["id"], {"display_name": "After rename"}))

    assert updated["display_name"] == "After rename"
    assert store.config.sources[0].display_name == "After rename"
    assert len(adapter.synced) == sync_count
