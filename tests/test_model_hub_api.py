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
    OAuthFlowState,
    RawCallOutcome,
    RawOutcomeKind,
    RetainedMaterialDisposition,
    SOURCE_PROTOCOLS,
    SourceObservation,
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
from tests.test_ui_remote_access_auth import _remote_peer, _save_config
from tests.ui_server_test_helpers import csrf_headers
from vibe import ui_server
from vibe.model_hub_client import ModelHubRemoteService, _decode
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
        self.requested_models = {}

    def load(self):
        return self.config

    def save(self, config):
        self.config = config

    def requested_model(self, backend):
        return self.requested_models.get(backend, "")


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
        self.orphan_cleanup_calls = []
        self.orphan_cleanup_succeeds = False
        self.cancel_disposition = RetainedMaterialDisposition.NONE
        self.start_calls = 0
        self.credential_count = 0

    async def ensure_installed(self):
        return await self.status()

    async def start(self):
        self.start_calls += 1
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
        self.credential_count += 1
        return f"cred_test{self.credential_count:03d}"

    async def provision_transient_credential(self, vendor, secret, base_url):
        self.secret_lengths.append(len(secret))
        self.credential_count += 1
        return f"cred_test{self.credential_count:03d}"

    async def revoke_credential(self, credential_ref):
        self.revoked.append(credential_ref)

    async def cleanup_orphaned_oauth_material(self, credential_ref):
        self.orphan_cleanup_calls.append(credential_ref)
        return self.orphan_cleanup_succeeds

    async def sync_sources(self, bindings):
        self.synced.append(tuple(bindings))
        if self.fail_sync:
            raise RuntimeError("upstream failure with sk-secret-material")

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        return ("claude-opus-4-6", "claude-sonnet-4-6")

    async def observe_source(self, vendor, base_url, credential_ref, protocol_order):
        models = await self.discover_models(
            vendor,
            protocol_order[0],
            base_url,
            credential_ref,
        )
        return SourceObservation(
            outcome=ObservationOutcome.OBSERVED,
            reachable=True,
            authenticated=True,
            protocol=protocol_order[0],
            discovery=ObservationDiscovery.SUCCEEDED,
            model_ids=tuple(models),
        )

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

    async def start_reauth(
        self,
        source_id,
        vendor,
        *,
        on_irreversible_start=None,
    ):
        if on_irreversible_start is not None:
            on_irreversible_start()
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
        flow = self.flows[flow_id]
        if flow.state not in {"success", "failed", "cancelled"}:
            self.flows[flow_id] = OAuthFlowState(
                **{
                    **flow.__dict__,
                    "state": "cancelled",
                    "retained_material_disposition": self.cancel_disposition,
                    "retained_credential_ref": None,
                }
            )

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
        requested_model_override=lambda backend: store.requested_model(backend),
    )
    return service, store, adapter


def _refresh_fixture_routes(config: ModelHubConfig) -> None:
    by_id = {source.id: source for source in config.sources}
    for backend, agent in config.agents.items():
        agent.sources.order = config.recommended_source_order(backend)
        agent.routes = {
            model_id: ModelHubRouteConfig()
            for model_id in agent.routes
        }
        for source_id in agent.sources.order:
            source = by_id[source_id]
            for model in source.models:
                if agent.menu_kind == "fixed" and model.id not in agent.routes:
                    continue
                if agent.menu_kind == "open" and (
                    agent.menu is None or model.id not in agent.menu.checked
                ):
                    continue
                route = agent.routes.setdefault(model.id, ModelHubRouteConfig())
                route.hops = (*route.hops, ModelHubRouteHopConfig(source.id, model.id))


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


def test_runtime_start_is_explicit_and_returns_v4_status(tmp_path):
    service, _store, adapter = _service(tmp_path)

    runtime = asyncio.run(service.runtime_start())

    assert adapter.start_calls == 1
    assert runtime["contract_version"] == 5
    assert runtime["status"]["health"] == "ok"
    _assert_valid("runtime-dependency.schema.json", runtime)


def test_runtime_start_syncs_sources_before_starting_once(tmp_path):
    class OrderedAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def sync_sources(self, bindings):
            self.calls.append("sync")
            await super().sync_sources(bindings)

        async def start(self):
            self.calls.append("start")
            return await super().start()

    store = MemoryStore()
    store.config.sources.append(
        ModelHubSourceConfig(
            id="src_runtime01",
            kind="api_key",
            vendor="openai",
            display_name="Runtime source",
            protocol="openai_responses",
            supply_channel="hub",
            billing="metered",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[ModelHubModelConfig(id="gpt-5", provenance="manual")],
            credential_ref="cred_runtime01",
        )
    )
    adapter = OrderedAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    async def start_then_enter_normal_gateway_path():
        runtime = await service.runtime_start()
        await service._ensure_engine_synced()
        return runtime

    runtime = asyncio.run(start_then_enter_normal_gateway_path())

    assert runtime["status"]["health"] == "ok"
    assert adapter.calls == ["sync", "start"]
    assert [binding.source_id for binding in adapter.synced[0]] == ["src_runtime01"]


@pytest.mark.parametrize(
    ("idle_health", "installed_version", "verified", "recovered_health"),
    [
        (EngineHealth.NOT_STARTED, "v7.2.95", True, "not_started"),
        (EngineHealth.NOT_INSTALLED, None, False, "not_installed"),
    ],
)
def test_runtime_start_sync_failure_is_reported_as_down(
    tmp_path,
    idle_health,
    installed_version,
    verified,
    recovered_health,
):
    class IdleAdapter(FakeAdapter):
        async def status(self):
            return EngineStatus(
                health=idle_health,
                installed_version=installed_version,
                verified=verified,
                listen_host="127.0.0.1",
                listen_port=None,
                last_check_iso=None,
            )

    store = MemoryStore()
    store.config.sources.append(
        ModelHubSourceConfig(
            id="src_runtime01",
            kind="api_key",
            vendor="openai",
            display_name="Runtime source",
            protocol="openai_responses",
            supply_channel="hub",
            billing="metered",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[ModelHubModelConfig(id="gpt-5", provenance="manual")],
            credential_ref="cred_runtime01",
        )
    )
    adapter = IdleAdapter()
    adapter.fail_sync = True
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    with pytest.raises(ModelHubError, match="engine_down"):
        asyncio.run(service.runtime_start())
    runtime = asyncio.run(service.runtime_status())

    assert adapter.start_calls == 0
    assert runtime["status"]["health"] == "down"

    adapter.fail_sync = False
    config = store.load()
    asyncio.run(service._commit_synced(config, service._clone_config(config)))

    recovered = asyncio.run(service.runtime_status())
    assert recovered["status"]["health"] == recovered_health


def test_runtime_start_in_progress_remains_not_started(tmp_path):
    class IdleAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.started = False

        async def start(self):
            self.started = True
            return await super().start()

        async def status(self):
            return EngineStatus(
                health=(EngineHealth.OK if self.started else EngineHealth.NOT_STARTED),
                installed_version="v7.2.95",
                verified=True,
                listen_host="127.0.0.1",
                listen_port=15220 if self.started else None,
                last_check_iso=None,
            )

    service = ModelHubService(
        store=MemoryStore(),
        adapter=IdleAdapter(),
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
    )

    async def status_while_start_waits_for_sync():
        await service._mutation_lock.acquire()
        start = asyncio.create_task(service.runtime_start())
        await asyncio.sleep(0)
        assert not start.done()
        pending = await service.runtime_status()
        service._mutation_lock.release()
        started = await start
        return pending, started

    pending, started = asyncio.run(status_while_start_waits_for_sync())

    assert pending["status"]["health"] == "not_started"
    assert started["status"]["health"] == "ok"


def test_runtime_start_crosses_the_controller_rpc_boundary(monkeypatch):
    from vibe import model_hub_client

    calls = []

    async def rpc(operation, payload=None):
        calls.append((operation, payload))
        return {"contract_version": 5, "status": {"health": "ok"}}

    monkeypatch.setattr(model_hub_client, "_rpc", rpc)

    runtime = asyncio.run(ModelHubRemoteService().runtime_start())

    assert runtime["status"]["health"] == "ok"
    assert calls == [("runtime_start", None)]


def test_runtime_start_is_allowlisted_by_controller_rpc(tmp_path):
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc

    service, _store, adapter = _service(tmp_path)

    runtime = asyncio.run(dispatch_model_hub_rpc(service, "runtime_start", {}))

    assert runtime["status"]["health"] == "ok"
    assert adapter.start_calls == 1


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
    assert adapter.revoked == ["cred_test001"]
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
        "claude": [("pm", "claude-sonnet-4-6"), ("reviewer", "claude-opus-4-6")],
        "codex": [("codex", "gpt-5.3-codex")],
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
        ("POST", "/api/models/sources/observe"),
        ("POST", "/api/models/sources"),
        ("PATCH", "/api/models/sources/src_test0001"),
        ("PUT", "/api/models/sources/src_test0001/credential"),
        ("POST", "/api/models/sources/src_test0001/reauth"),
        ("DELETE", "/api/models/sources/src_test0001"),
        ("POST", "/api/models/sources/src_test0001/refresh"),
        ("GET", "/api/models/agents"),
        ("GET", "/api/models/agents/claude/chain?model=claude-opus-4-6"),
        ("PUT", "/api/models/agents/claude/chain?model=claude-opus-4-6"),
        ("POST", "/api/models/agents/claude/probe"),
        ("GET", "/api/models/agents/claude/sources"),
        ("PUT", "/api/models/agents/claude/sources"),
        ("PATCH", "/api/models/agents/claude/mode"),
        ("PUT", "/api/models/agents/opencode/menu"),
        ("POST", "/api/models/sources/src_test0001/models"),
        ("PATCH", "/api/models/sources/src_test0001/models/custom-model"),
        ("DELETE", "/api/models/sources/src_test0001/models/custom-model"),
        ("GET", "/api/models/events?limit=invalid"),
        ("POST", "/api/models/oauth/start"),
        ("GET", "/api/models/oauth/status/oaf_test0001"),
        ("POST", "/api/models/oauth/submit"),
        ("POST", "/api/models/oauth/cancel"),
        ("POST", "/api/models/migration/scan"),
        ("POST", "/api/models/migration/apply"),
        ("GET", "/api/models/runtime/status"),
        ("POST", "/api/models/runtime/start"),
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


def test_final_model_hub_route_surface_has_observe_refresh_and_no_saved_test_route():
    routes = {route.path: set(route.methods or ()) for route in app.routes if route.path.startswith("/api/models/")}

    assert "POST" in routes["/api/models/sources/observe"]
    assert "POST" in routes["/api/models/sources/{source_id}/refresh"]
    assert all(not path.endswith("/test") for path in routes)
    assert all("/mappings" not in path for path in routes)


def test_unsaved_observation_route_returns_valid_result_and_revokes_transient_ref(
    monkeypatch,
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    response = client.post(
        "/api/models/sources/observe",
        json={
            "vendor": "anthropic",
            "key": "sk-test-observation-only",
        },
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert response.status_code == 200
    body = response.get_json()
    _assert_envelope(body)
    _assert_valid("observation-result.schema.json", body["observation"])
    assert body["observation"]["protocol"] == SOURCE_PROTOCOLS[0]
    assert store.config.sources == []
    assert adapter.revoked == ["cred_test001"]
    assert "credential_ref" not in json.dumps(body)


def test_source_order_route_rejects_retired_policy_payload(monkeypatch, tmp_path):
    service, _, _ = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    retired_field = "pol" + "icy"
    retired_value = "fol" + "low"

    response = client.put(
        "/api/models/agents/claude/sources",
        json={retired_field: retired_value},
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_source_order"


def test_native_oauth_rejects_duplicate_source_before_adapter(tmp_path):
    service, store, adapter = _service(tmp_path)
    existing = ModelHubSourceConfig(
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
    store.config.sources.append(existing)
    _refresh_fixture_routes(store.config)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))

    assert exc_info.value.code == "native_source_already_exists"
    assert exc_info.value.data == {"existing_source_id": existing.id}
    assert adapter.oauth_start_calls == []


def test_oauth_start_normalizes_vendor_before_singleton_and_adapter(tmp_path):
    service, _store, adapter = _service(tmp_path)

    flow = asyncio.run(
        service.oauth_start({"vendor": " Anthropic ", "channel": "hub"})
    )["flow"]

    assert flow["vendor"] == "anthropic"
    assert adapter.oauth_start_calls == [(flow["source_id"], "anthropic")]


def test_chain_route_reorders_exact_persisted_hops(monkeypatch, tmp_path):
    service, _, _ = _service(tmp_path)
    first = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "First",
                "key": "sk-test-chain-first",
            },
        )
    )
    second = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Second",
                "key": "sk-test-chain-second",
            },
        )
    )
    model_id = "claude-opus-4-6"
    hops = [
        {"source_id": second["id"], "model_id": model_id},
        {"source_id": first["id"], "model_id": model_id},
    ]
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    response = client.put(
        f"/api/models/agents/claude/chain?model={model_id}",
        json={"hops": hops},
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert response.status_code == 200
    body = response.get_json()
    _assert_envelope(body)
    _assert_valid("agent-chain.schema.json", body["chain"])
    assert [(hop["source_id"], hop["model_id"]) for hop in body["chain"]["chain"]] == [
        (hop["source_id"], hop["model_id"]) for hop in hops
    ]
    assert body["chain"]["current"] == {"source_id": second["id"], "model_id": model_id}


def test_delete_guard_reports_only_routes_emptied_by_this_mutation(tmp_path):
    service, store, _ = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_guard0001",
        kind="api_key",
        vendor="anthropic",
        display_name="Guarded",
        protocol="anthropic",
        supply_channel="hub",
        billing="metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_guard0001",
    )
    store.config.sources.append(source)
    claude = store.config.agents["claude"]
    claude.sources.order = [source.id]
    claude.routes = {
        **{
            model_id: ModelHubRouteConfig()
            for model_id in claude.routes
        },
        "claude-opus-4-6": ModelHubRouteConfig(hops=(ModelHubRouteHopConfig(source.id, "claude-opus-4-6"),)),
        "claude-sonnet-4-6": ModelHubRouteConfig(),
    }

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source(source.id))

    assert exc_info.value.code == "source_in_route_chain"
    assert exc_info.value.data["would_interrupt"] == [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": [],
        }
    ]


def test_disabled_gate_preserves_existing_model_hub_config_bytes(monkeypatch):
    from config import paths
    from core.services.settings import default_config

    monkeypatch.delenv("VIBE_MODEL_HUB_ENABLED", raising=False)
    config = default_config()
    config.save()
    config_path = paths.get_config_path()
    before = config_path.read_bytes()
    client = app.test_client()

    assert client.get("/api/config").status_code == 200
    assert client.get("/api/models/sources").status_code == 404

    assert config_path.read_bytes() == before


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
        ("/api/models/agents/opencode/menu", "put", "mapping_target_unavailable"),
        (
            "/api/models/sources/src_test0001/models",
            "post",
            "mapping_target_unavailable",
        ),
        (
            "/api/models/sources/src_test0001/models/custom-model",
            "patch",
            "mapping_target_unavailable",
        ),
        (
            "/api/models/sources/src_test0001/models/custom-model",
            "delete",
            "mapping_target_unavailable",
        ),
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


def test_discovered_source_model_delete_is_rejected_as_upstream_managed(
    monkeypatch,
    tmp_path,
):
    service, store, _ = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_discovered01",
        kind="api_key",
        vendor="openai",
        display_name="Observed inventory",
        protocol="openai_responses",
        supply_channel="hub",
        billing="metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="gpt-5", provenance="discovered")],
        credential_ref="cred_discovered01",
    )
    store.config.sources.append(source)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    updated = client.patch(
        f"/api/models/sources/{source.id}/models/gpt-5",
        json={"reasoning_efforts": ["high"]},
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert updated.status_code == 200
    updated_model = updated.get_json()["source"]["models"][0]
    assert updated_model["origin"] == "discovered"
    assert updated_model["reasoning_efforts"] == ["high"]

    refused = client.delete(
        f"/api/models/sources/{source.id}/models/gpt-5",
        json={},
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert refused.status_code == 409
    assert refused.get_json()["error"] == "source_model_managed_upstream"
    assert [model.id for model in store.config.sources[0].models] == ["gpt-5"]
    assert store.config.sources[0].models[0].reasoning_efforts == ["high"]


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
    _refresh_fixture_routes(store.config)
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

    async def assert_source_unavailable_before_logout(
        source_id,
        vendor,
        *,
        on_irreversible_start=None,
    ):
        persisted = store.config.sources[0]
        assert persisted.state.status == "standby"
        assert [model.id for model in persisted.models] == ["claude-opus-4-6"]
        assert on_irreversible_start is not None
        on_irreversible_start()
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
    _refresh_fixture_routes(store.config)
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
    _refresh_fixture_routes(store.config)
    store.requested_model = lambda backend: ("claude-opus-4-6" if backend == "claude" else "")
    service.named_agents_override = lambda backend: ([("claude", "claude-opus-4-6")] if backend == "claude" else [])
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
            "agents": ["claude"],
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
    _refresh_fixture_routes(store.config)

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
    assert adapter.cancelled == [next(iter(adapter.flows))]


def test_native_reauth_pre_boundary_failure_preserves_prior_supply(tmp_path):
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
    _refresh_fixture_routes(store.config)
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    async def fail_before_logout(
        source_id,
        vendor,
        *,
        on_irreversible_start=None,
    ):
        flow = OAuthFlowState(
            **{
                **adapter._flow(source_id, "oaf_failed01").__dict__,
                "vendor": vendor,
                "state": "failed",
                "error_key": "settings.models.oauth.error.generic",
            }
        )
        adapter.flows[flow.flow_id] = flow
        return flow

    adapter.start_reauth = fail_before_logout
    result = asyncio.run(
        service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
    )

    assert result["flow"]["state"] == "failed"
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )


def test_native_reauth_logout_spawn_failure_restores_prior_supply(tmp_path):
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
    _refresh_fixture_routes(store.config)
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    async def fail_logout_spawn(
        source_id,
        vendor,
        *,
        on_irreversible_start=None,
    ):
        assert on_irreversible_start is not None
        restore = on_irreversible_start()
        assert store.config.sources[0].state.status == "needs_action"
        assert restore is not None
        restore()
        flow = OAuthFlowState(
            **{
                **adapter._flow(source_id, "oaf_failed01").__dict__,
                "vendor": vendor,
                "state": "failed",
                "error_key": "settings.models.oauth.error.generic",
            }
        )
        adapter.flows[flow.flow_id] = flow
        return flow

    adapter.start_reauth = fail_logout_spawn
    result = asyncio.run(
        service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
    )

    assert result["flow"]["state"] == "failed"
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )


def test_hub_reauth_is_transactional_without_native_acknowledgement(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
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
    _refresh_fixture_routes(store.config)

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


def test_hub_reauth_replaces_pending_flow_unknown_to_restarted_adapter(
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
    _refresh_fixture_routes(store.config)
    first = asyncio.run(service.reauth_source(source.id, {}))["flow"]

    async def restarted_adapter_does_not_know_flow(_flow_id):
        raise RuntimeError("OAuth flow is unknown after restart")

    restarted_adapter = FakeAdapter()
    restarted_adapter.oauth_status = restarted_adapter_does_not_know_flow
    restarted = ModelHubService(
        store=store,
        adapter=restarted_adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        native_oauth_adapter=restarted_adapter,
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )
    second = asyncio.run(restarted.reauth_source(source.id, {}))["flow"]

    assert first["flow_id"] in adapter.flows
    assert second["flow_id"] in restarted_adapter.flows
    replacement_binding = restarted.oauth_flows.binding(second["flow_id"])
    assert replacement_binding is not None
    assert replacement_binding.source_id == source.id
    assert adapter.oauth_start_calls == [(source.id, "anthropic")]
    assert restarted_adapter.oauth_start_calls == [(source.id, "anthropic")]


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
    _refresh_fixture_routes(store.config)

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


def test_concurrent_completed_hub_reauth_materializes_once(tmp_path):
    async def run_race():
        class BlockingDiscoveryAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.discovery_calls = 0
                self.discovery_started = asyncio.Event()
                self.release_discovery = asyncio.Event()

            async def discover_models(
                self,
                vendor,
                protocol,
                base_url,
                credential_ref,
            ):
                self.discovery_calls += 1
                if self.discovery_calls > 1:
                    raise ModelDiscoveryError("duplicate materialization")
                self.discovery_started.set()
                await self.release_discovery.wait()
                return ("claude-opus-4-6",)

        store = MemoryStore()
        adapter = BlockingDiscoveryAdapter()
        service = ModelHubService(
            store=store,
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            native_oauth_adapter=adapter,
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
            now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
        )
        source = ModelHubSourceConfig(
            id="src_huboauth01",
            kind="subscription",
            vendor="anthropic",
            display_name="Hub subscription",
            protocol="anthropic",
            supply_channel="hub",
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
        _refresh_fixture_routes(store.config)

        flow = (await service.reauth_source(source.id, {}))["flow"]
        adapter.flows[flow["flow_id"]] = OAuthFlowState(
            **{
                **adapter.flows[flow["flow_id"]].__dict__,
                "state": "success",
                "credential_ref": "cred_hub_reused",
            }
        )

        first = asyncio.create_task(service.oauth_status(flow["flow_id"]))
        await adapter.discovery_started.wait()
        second = asyncio.create_task(service.oauth_status(flow["flow_id"]))
        await asyncio.sleep(0)
        adapter.release_discovery.set()
        results = await asyncio.gather(first, second)
        return results, service, store, adapter, flow["flow_id"]

    results, service, store, adapter, flow_id = asyncio.run(run_race())

    assert adapter.discovery_calls == 1
    assert results[0] == results[1]
    assert store.config.sources[0].state.status == "standby"
    binding = service.oauth_flows.binding(flow_id)
    assert binding is not None
    assert binding.completed is True


@pytest.mark.parametrize(
    ("terminal_state", "disposition", "retained_credential_ref"),
    [
        (
            "failed",
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            "cred_hub_existing",
        ),
        ("failed", RetainedMaterialDisposition.UNKNOWN, None),
        ("cancelled", RetainedMaterialDisposition.UNKNOWN, None),
    ],
)
@pytest.mark.parametrize("entrypoint", ["status", "submit"])
def test_hub_reauth_irreversible_dispositions_fail_closed(
    tmp_path,
    terminal_state,
    disposition,
    retained_credential_ref,
    entrypoint,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            ),
            ModelHubModelConfig(
                id="manual-model",
                provenance="manual",
            ),
        ],
        credential_ref="cred_hub_existing",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": terminal_state,
            "error_key": ("models.oauth.binding_failed" if terminal_state == "failed" else None),
            "channel": "hub",
            "retained_material_disposition": disposition,
            "retained_credential_ref": retained_credential_ref,
        }
    )

    if entrypoint == "status":
        result = asyncio.run(service.oauth_status(flow["flow_id"]))
    else:

        async def return_terminal(_flow_id, _value):
            return adapter.flows[flow["flow_id"]]

        adapter.submit_oauth = return_terminal
        result = asyncio.run(
            service.oauth_submit(
                {
                    "flow_id": flow["flow_id"],
                    "value": "oauth-code",
                }
            )
        )

    assert result["flow"]["state"] == terminal_state
    persisted = store.config.sources[0]
    assert persisted.credential_ref == "cred_hub_existing"
    assert [model.id for model in persisted.models] == ["manual-model"]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.oauth_expired"
    assert adapter.revoked == []
    assert adapter.orphan_cleanup_calls == []
    assert service.revocations.list() == []


@pytest.mark.parametrize(
    "disposition",
    [
        RetainedMaterialDisposition.NONE,
        RetainedMaterialDisposition.FOREIGN_SOURCE_REF,
    ],
)
def test_failed_hub_reauth_non_owned_material_preserves_prior_state(
    tmp_path,
    disposition,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            ),
            ModelHubModelConfig(
                id="manual-model",
                provenance="manual",
            ),
        ],
        credential_ref="cred_hub_existing",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": disposition,
            "retained_credential_ref": None,
        }
    )
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["flow"]["state"] == "failed"
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )
    assert adapter.revoked == []
    assert adapter.orphan_cleanup_calls == []
    assert service.revocations.list() == []


def test_failed_hub_reauth_orphan_is_journaled_and_retried_by_ref(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_existing",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": RetainedMaterialDisposition.ORPHAN_REF,
            "retained_credential_ref": "cred_hub_orphan",
        }
    )
    before = json.dumps(
        store.config.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
    )

    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["flow"]["state"] == "failed"
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )
    assert adapter.revoked == []
    assert adapter.orphan_cleanup_calls == ["cred_hub_orphan"]
    pending = service.revocations.list()
    assert [(entry.source_id, entry.credential_ref, entry.operation) for entry in pending] == [
        (
            source.id,
            "cred_hub_orphan",
            "cleanup_orphaned_oauth_material",
        )
    ]

    restarted_adapter = FakeAdapter()
    restarted_adapter.orphan_cleanup_succeeds = True
    restarted = ModelHubService(
        store=store,
        adapter=restarted_adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "restarted-oauth-flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    asyncio.run(restarted._ensure_engine_synced())

    assert restarted_adapter.orphan_cleanup_calls == ["cred_hub_orphan"]
    assert restarted.revocations.list() == []


def test_failed_hub_create_orphan_is_journaled_before_flow_can_be_forgotten(
    tmp_path,
):
    service, _, adapter = _service(tmp_path)
    flow = asyncio.run(
        service.oauth_start(
            {
                "vendor": "anthropic",
                "channel": "hub",
            }
        )
    )["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": RetainedMaterialDisposition.ORPHAN_REF,
            "retained_credential_ref": "cred_create_orphan",
        }
    )

    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["flow"]["state"] == "failed"
    assert adapter.orphan_cleanup_calls == ["cred_create_orphan"]
    assert [(entry.source_id, entry.credential_ref, entry.operation) for entry in service.revocations.list()] == [
        (
            flow["source_id"],
            "cred_create_orphan",
            "cleanup_orphaned_oauth_material",
        )
    ]

    asyncio.run(service.oauth_cancel(flow["flow_id"]))

    assert service.oauth_flows.binding(flow["flow_id"]) is None
    assert service.revocations.list()


def test_completed_orphan_cleanup_replay_clears_surviving_service_journal(
    tmp_path,
):
    from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
    from vibe.model_hub_runtime.state import EngineStateStore

    class Client:
        def management_request(
            self,
            method,
            path,
            *,
            query=None,
            payload=None,
            timeout=None,
        ):
            assert (method, path) == ("DELETE", "/auth-files")
            return {"status": "ok"}

    class Supervisor:
        def __init__(self, client):
            self._client = client

        def client_if_running(self):
            return self._client

    class RuntimeCleanupAdapter(FakeAdapter):
        def __init__(self, runtime_adapter):
            super().__init__()
            self.runtime_adapter = runtime_adapter

        async def cleanup_orphaned_oauth_material(self, credential_ref):
            self.orphan_cleanup_calls.append(credential_ref)
            return await self.runtime_adapter.cleanup_orphaned_oauth_material(credential_ref)

    state = EngineStateStore(tmp_path / "runtime-state")
    state.prepare_instance("install-1")
    auth_file = state.auth_dir / "claude-account.json"
    auth_file.write_text("{}", encoding="utf-8")
    auth_file.chmod(0o600)
    credential_ref = state.bind_oauth_credential(
        "src_pending01",
        "anthropic",
        auth_file.name,
    )
    runtime_adapter = CLIProxyEngineAdapter(
        supervisor=Supervisor(Client()),  # type: ignore[arg-type]
        state_store=state,
    )
    journal = CredentialRevocationJournal(tmp_path / "revocations.json")
    journal.add(
        "src_pending01",
        credential_ref,
        operation="cleanup_orphaned_oauth_material",
    )

    assert asyncio.run(runtime_adapter.cleanup_orphaned_oauth_material(credential_ref)) is True
    assert journal.list()

    adapter = RuntimeCleanupAdapter(runtime_adapter)
    service = ModelHubService(
        store=MemoryStore(),
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "oauth-flows.json"),
        revocations=journal,
    )

    asyncio.run(service._ensure_engine_synced())

    assert adapter.orphan_cleanup_calls == [credential_ref]
    assert journal.list() == []


@pytest.mark.parametrize(
    ("disposition", "retained_credential_ref", "expected_revoked"),
    [
        (
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            "cred_create_flow",
            ["cred_create_flow"],
        ),
        (RetainedMaterialDisposition.NONE, None, []),
        (RetainedMaterialDisposition.FOREIGN_SOURCE_REF, None, []),
        (RetainedMaterialDisposition.UNKNOWN, None, []),
    ],
)
def test_failed_hub_create_consumes_known_ref_without_fabricating_source_state(
    tmp_path,
    disposition,
    retained_credential_ref,
    expected_revoked,
):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(
        service.oauth_start(
            {
                "vendor": "anthropic",
                "channel": "hub",
            }
        )
    )["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": disposition,
            "retained_credential_ref": retained_credential_ref,
        }
    )

    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["flow"]["state"] == "failed"
    assert store.config.sources == []
    assert adapter.revoked == expected_revoked
    assert adapter.orphan_cleanup_calls == []
    assert service.revocations.list() == []


def test_failed_hub_create_keeps_flow_when_ref_cleanup_is_not_durable(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(
        service.oauth_start(
            {
                "vendor": "anthropic",
                "channel": "hub",
            }
        )
    )["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": RetainedMaterialDisposition.FLOW_SOURCE_REF,
            "retained_credential_ref": "cred_create_flow",
        }
    )

    def fail_journal_write(*_args, **_kwargs):
        raise OSError("journal is unavailable")

    async def fail_revocation(credential_ref):
        adapter.revoked.append(credential_ref)
        raise RuntimeError("engine is unavailable")

    service.revocations.add = fail_journal_write
    adapter.revoke_credential = fail_revocation

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "engine_down"
    assert store.config.sources == []
    assert adapter.revoked == ["cred_create_flow"]
    assert service.revocations.list() == []
    assert service.oauth_flows.binding(flow["flow_id"]) is not None


@pytest.mark.parametrize(
    ("terminal_state", "disposition", "retained_credential_ref"),
    [
        (
            "failed",
            RetainedMaterialDisposition.FLOW_SOURCE_REF,
            "cred_hub_existing",
        ),
        ("cancelled", RetainedMaterialDisposition.UNKNOWN, None),
    ],
)
def test_hub_reauth_retry_materializes_pending_flow(
    tmp_path,
    terminal_state,
    disposition,
    retained_credential_ref,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            ),
            ModelHubModelConfig(
                id="manual-model",
                provenance="manual",
            ),
        ],
        credential_ref="cred_hub_existing",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    first = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[first["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[first["flow_id"]].__dict__,
            "state": terminal_state,
            "error_key": ("models.oauth.binding_failed" if terminal_state == "failed" else None),
            "channel": "hub",
            "retained_material_disposition": disposition,
            "retained_credential_ref": retained_credential_ref,
        }
    )

    second = asyncio.run(service.reauth_source(source.id, {}))["flow"]

    persisted = store.config.sources[0]
    assert [model.id for model in persisted.models] == ["manual-model"]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.oauth_expired"
    assert service.oauth_flows.binding(first["flow_id"]) is None
    replacement = service.oauth_flows.binding(second["flow_id"])
    assert replacement is not None
    assert replacement.recovered is True


@pytest.mark.parametrize("registry_failure", ["missing", "unwritable"])
def test_hub_reauth_returns_terminal_tail_when_registry_completion_fails(
    tmp_path,
    registry_failure,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
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
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_reused",
        }
    )
    original_complete = service.oauth_flows.complete
    completion_calls = 0

    def fail_registry_complete(*args, **kwargs):
        nonlocal completion_calls
        completion_calls += 1
        if registry_failure == "unwritable":
            raise OSError("registry unavailable")
        if completion_calls == 1:
            service.oauth_flows.path.unlink()
        return original_complete(*args, **kwargs)

    service.oauth_flows.complete = fail_registry_complete
    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["flow"]["state"] == "success"
    assert result["source"]["state"]["status"] == "standby"
    assert result["recovered"] is False
    assert result["interrupted_pairs"] == []
    assert completion_calls == (2 if registry_failure == "missing" else 1)


@pytest.mark.parametrize("failure", ["discovery", "sync"])
def test_failed_same_handle_hub_reauth_requires_user_action(
    tmp_path,
    failure,
):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
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
    _refresh_fixture_routes(store.config)
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

    if failure == "discovery":
        adapter.discover_models = fail_discovery
    else:
        adapter.fail_sync = True
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == ("discovery_failed" if failure == "discovery" else "engine_down")
    persisted = store.config.sources[0]
    assert persisted.credential_ref == "cred_hub_reused"
    assert persisted.models == []
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.oauth_expired"
    assert adapter.revoked == []
    assert service.revocations.list() == []
    assert service.oauth_flows.binding(flow["flow_id"]) is None


def test_failed_zero_model_hub_source_is_omitted_from_restart_sync(tmp_path):
    class StrictProjectionAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            assert all(binding.model_ids for binding in bindings)
            await super().sync_sources(bindings)

    service, store, adapter = _service(tmp_path)
    failed = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Failed Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(
            status="needs_action",
            detail_key="models.source.needs_action.oauth_expired",
        ),
        models=[],
        credential_ref="cred_hub_reused",
    )
    healthy = ModelHubSourceConfig(
        id="src_hubhealthy01",
        kind="api_key",
        vendor="anthropic",
        display_name="Healthy Hub source",
        protocol="anthropic",
        supply_channel="hub",
        billing="metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            )
        ],
        credential_ref="cred_hub_healthy",
    )
    store.config.sources = [failed, healthy]
    _refresh_fixture_routes(store.config)
    restarted_adapter = StrictProjectionAdapter()
    restarted = ModelHubService(
        store=store,
        adapter=restarted_adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "restarted-oauth-flows.json"),
        revocations=CredentialRevocationJournal(tmp_path / "restarted-revocations.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    resolved = asyncio.run(
        restarted.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert resolved.source_id == healthy.id
    assert [tuple(binding.source_id for binding in batch) for batch in restarted_adapter.synced] == [(healthy.id,)]
    assert adapter.synced == []


def test_restart_reconciles_revocation_without_runnable_supply(tmp_path):
    store = MemoryStore()
    journal_path = tmp_path / "revocations.json"
    journal = CredentialRevocationJournal(journal_path)
    journal.add("src_deleted0001", "cred_pending_old")
    adapter = FakeAdapter()
    restarted = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        oauth_flows=OAuthFlowRegistry(tmp_path / "restarted-oauth-flows.json"),
        revocations=CredentialRevocationJournal(journal_path),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            restarted.resolve(
                backend="claude",
                model_id="claude-opus-4-6",
                request={},
            )
        )

    assert exc_info.value.code == "mapping_target_unavailable"
    assert adapter.synced == [()]
    assert adapter.revoked == ["cred_pending_old"]
    assert restarted.revocations.list() == []


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
    _refresh_fixture_routes(store.config)
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
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )
    assert adapter.revoked == ["cred_hub_new"]
    assert service.oauth_flows.binding(flow["flow_id"]) is None


def test_completed_hub_reauth_revokes_replacement_after_source_disappears(
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
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    asyncio.run(service.delete_source(source.id, force=True))
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_orphan",
        }
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "source_not_found"
    assert adapter.revoked == ["cred_hub_old", "cred_hub_orphan"]
    assert service.revocations.list() == []
    assert service.oauth_flows.binding(flow["flow_id"]) is None
    with pytest.raises(ModelHubError) as repeated:
        asyncio.run(service.oauth_status(flow["flow_id"]))
    assert repeated.value.code == "flow_not_found"


def test_failed_hub_reauth_rolls_back_when_old_journal_cleanup_fails(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
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
    _refresh_fixture_routes(store.config)
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
    assert (
        json.dumps(
            store.config.to_payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        == before
    )
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
                "key": "sk-original-route-key",
            }
        )
    )["source"]
    store.requested_model = lambda backend: ("claude-opus-4-6" if backend == "claude" else "")
    service.named_agents_override = lambda backend: ([("claude", "claude-opus-4-6")] if backend == "claude" else [])

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
    assert refusal["error"] == "source_model_in_route_chain"
    assert refusal["would_remove_hops"]
    assert all(
        hop["source_id"] == created["id"]
        for hop in refusal["would_remove_hops"]
    )
    assert refusal["would_interrupt"] == [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": ["claude"],
        },
        {
            "backend": "claude",
            "model_id": "claude-sonnet-4-6",
            "agents": [],
        },
    ]
    committed = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json={**request_body, "force": True},
        headers=headers,
        base_url=base_url,
    ).get_json()

    assert committed["removed_hops"] == refusal["would_remove_hops"]
    assert committed["interrupted"] == refusal["would_interrupt"]
    assert committed["source"]["credential_ref"] == "cred_route_3"
    removed_identities = {
        (hop["backend"], hop["menu_model"], hop["source_id"], hop["model_id"])
        for hop in committed["removed_hops"]
    }
    assert all(
        (backend, menu_model, hop.source_id, hop.model_id) not in removed_identities
        for backend, agent in store.config.agents.items()
        for menu_model, route in agent.routes.items()
        for hop in route.hops
    )
    assert adapter.revoked == ["cred_test001", "cred_route_2", "cred_route_1"]


def test_failed_hub_oauth_source_creation_revokes_credential(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "hub"}))["flow"]
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
        flow = (await service.oauth_start({"vendor": "anthropic", "channel": "hub"}))["flow"]
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


def test_cancel_materializes_terminal_successful_hub_reauth(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
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
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_reused",
        }
    )

    asyncio.run(service.oauth_cancel(flow["flow_id"]))

    persisted = store.config.sources[0]
    assert persisted.state.status == "standby"
    assert {model.id for model in persisted.models} == {
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    }
    binding = service.oauth_flows.binding(flow["flow_id"])
    assert binding is not None
    assert binding.completed is True
    assert adapter.cancelled == []


def test_cancel_materializes_terminal_failed_hub_reauth(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            ),
            ModelHubModelConfig(
                id="manual-model",
                provenance="manual",
            ),
        ],
        credential_ref="cred_hub_reused",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "failed",
            "error_key": "models.oauth.binding_failed",
            "channel": "hub",
            "retained_material_disposition": RetainedMaterialDisposition.UNKNOWN,
            "retained_credential_ref": None,
        }
    )

    asyncio.run(service.oauth_cancel(flow["flow_id"]))

    persisted = store.config.sources[0]
    assert [model.id for model in persisted.models] == ["manual-model"]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.oauth_expired"
    assert service.oauth_flows.binding(flow["flow_id"]) is None
    assert adapter.cancelled == []


def test_cancel_materializes_post_cancel_unknown_hub_reauth(tmp_path):
    service, store, adapter = _service(tmp_path)
    source = ModelHubSourceConfig(
        id="src_huboauth01",
        kind="subscription",
        vendor="anthropic",
        display_name="Hub subscription",
        protocol="anthropic",
        supply_channel="hub",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[
            ModelHubModelConfig(
                id="claude-opus-4-6",
                provenance="discovered",
            ),
            ModelHubModelConfig(
                id="manual-model",
                provenance="manual",
            ),
        ],
        credential_ref="cred_hub_reused",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "verifying",
            "channel": "hub",
        }
    )
    adapter.cancel_disposition = RetainedMaterialDisposition.UNKNOWN

    asyncio.run(service.oauth_cancel(flow["flow_id"]))

    persisted = store.config.sources[0]
    assert [model.id for model in persisted.models] == ["manual-model"]
    assert persisted.state.status == "needs_action"
    assert persisted.state.detail_key == "models.source.needs_action.oauth_expired"
    assert service.oauth_flows.binding(flow["flow_id"]) is None
    assert adapter.cancelled == [flow["flow_id"]]


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


def test_runtime_start_route_requires_csrf_before_starting_engine(monkeypatch, tmp_path):
    service, _, adapter = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"

    rejected = client.post("/api/models/runtime/start", base_url=base_url)

    assert rejected.status_code == 403
    assert adapter.start_calls == 0

    accepted = client.post(
        "/api/models/runtime/start",
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert accepted.status_code == 200
    runtime = accepted.get_json()["runtime"]
    assert adapter.start_calls == 1
    assert runtime["contract_version"] == 5
    _assert_valid("runtime-dependency.schema.json", runtime)


def test_runtime_start_route_requires_remote_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_config(tmp_path)

    response = app.test_client().post(
        "/api/models/runtime/start",
        base_url="https://alex.avibe.bot",
        environ_base=_remote_peer(),
    )

    assert response.status_code == 401
    assert response.get_json()["error"] == "remote_access_login_required"


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
    assert {model["origin"] for model in source["models"]} == {"discovered"}
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
        asyncio.run(service.refresh_source(source["id"]))

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

    assert adapter.secret_lengths == [len(normalized), len(normalized)]
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
    assert adapter.secret_lengths == [len(normalized), len(normalized)]
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
            "models": [{"id": pasted_key, "origin": "manual"}],
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
                    "origin": "manual",
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
        asyncio.run(
            service.add_custom_model(
                source["id"],
                {"model_id": pasted_key, "reasoning_efforts": []},
            )
        )

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


def test_base_url_change_guards_and_prunes_invalid_exact_hops(
    monkeypatch,
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    source = asyncio.run(
        _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Guarded endpoint",
                "key": "sk-test-transient-only",
            },
        )
    )

    async def discover_narrower(vendor, protocol, base_url, credential_ref):
        return ("replacement-only-model",)

    adapter.discover_models = discover_narrower
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    origin = "http://127.0.0.1:15131"
    headers = csrf_headers(client, origin)
    replacement_url = "https://other-relay.example/v1"

    refused = client.patch(
        f"/api/models/sources/{source['id']}",
        json={"base_url": replacement_url},
        headers=headers,
        base_url=origin,
    )

    assert refused.status_code == 409
    refusal = refused.get_json()
    assert refusal["error"] == "source_model_in_route_chain"
    assert refusal["would_remove_hops"]
    assert store.config.sources[0].base_url is None

    committed = client.patch(
        f"/api/models/sources/{source['id']}",
        json={"base_url": replacement_url, "force": True},
        headers=headers,
        base_url=origin,
    ).get_json()

    assert committed["source"]["base_url"] == replacement_url
    assert committed["removed_hops"] == refusal["would_remove_hops"]
    assert committed["interrupted"] == refusal["would_interrupt"]
    assert store.config.sources[0].base_url == replacement_url
    removed_identities = {
        (hop["backend"], hop["menu_model"], hop["source_id"], hop["model_id"])
        for hop in committed["removed_hops"]
    }
    assert all(
        (backend, menu_model, hop.source_id, hop.model_id) not in removed_identities
        for backend, agent in store.config.agents.items()
        for menu_model, route in agent.routes.items()
        for hop in route.hops
    )


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

    assert updated["source"]["display_name"] == "After rename"
    assert updated["removed_hops"] == []
    assert updated["interrupted"] == []
    assert store.config.sources[0].display_name == "After rename"
    assert len(adapter.synced) == sync_count
