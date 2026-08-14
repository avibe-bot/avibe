from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
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
from core.agent_auth_service import BackendLoginInProgressError
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
from core.handlers.model_hub.oauth import (
    NativeOAuthSourceStatus,
    OAuthFlowRegistry,
)
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    CONTRACT_VERSION,
    ModelHubError,
    ModelHubService,
    create_default_service,
)
from tests.ui_server_test_helpers import csrf_headers, remote_peer, save_config
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
        self.recovery = False

    def load(self):
        return self.config

    def save(self, config):
        self.config = config

    def ensure_writable(self):
        if self.recovery:
            raise ModelHubError("config_recovery", status=409)

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
        self.observation: SourceObservation | None = None
        self.observed_protocol_orders: list[tuple[str, ...]] = []
        self.retargeted_credentials: list[tuple[str, str, str, str | None, str]] = []
        self.refreshable_credential_refs: set[str] = set()
        self.discovery_credential_refs: list[str] = []

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

    async def retarget_api_key_credential(
        self,
        credential_ref,
        vendor,
        protocol,
        base_url,
    ):
        self.credential_count += 1
        replacement_ref = f"cred_test{self.credential_count:03d}"
        self.retargeted_credentials.append(
            (credential_ref, vendor, protocol, base_url, replacement_ref)
        )
        return replacement_ref

    async def credential_supports_refresh(self, credential_ref):
        return credential_ref in self.refreshable_credential_refs

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
        self.discovery_credential_refs.append(credential_ref)
        return ("claude-opus-4-6", "claude-sonnet-4-6")

    async def observe_source(self, vendor, base_url, credential_ref, protocol_order):
        self.observed_protocol_orders.append(tuple(protocol_order))
        if self.observation is not None:
            return self.observation
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


def _service(tmp_path, adapter=None):
    store = MemoryStore()
    adapter = adapter or FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(
            tmp_path / "oauth_flows.json",
            now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
        ),
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


def _set_claude_route_fixture(
    store: MemoryStore,
    source_ids: tuple[str, ...],
    model_id: str,
) -> tuple[ModelHubRouteHopConfig, ...]:
    sources = [
        ModelHubSourceConfig(
            id=source_id,
            kind="api_key",
            vendor="anthropic",
            display_name=source_id,
            protocol="anthropic",
            supply_channel="hub",
            billing="metered",
            state=ModelHubSourceStateConfig(status="standby"),
            models=[ModelHubModelConfig(id=model_id, provenance="discovered")],
            credential_ref=f"cred_{source_id}",
        )
        for source_id in source_ids
    ]
    hops = tuple(ModelHubRouteHopConfig(source.id, model_id) for source in sources)
    store.config.sources = sources
    store.config.agents["claude"].sources.order = list(source_ids)
    store.config.agents["claude"].routes[model_id] = ModelHubRouteConfig(hops=hops)
    return hops


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


def test_api_key_create_route_persists_client_nonce_for_list_reconciliation(
    monkeypatch,
    tmp_path,
):
    service, store, adapter = _service(tmp_path)
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    request_body = {
        "kind": "api_key",
        "vendor": "custom",
        "display_name": "Relay",
        "base_url": "https://relay.example/v1",
        "key": "sk-test-source-create-nonce",
        "client_nonce": "scn_01j5w8z7p4n6q2rt",
    }

    response = client.post(
        "/api/models/sources",
        json=request_body,
        headers=csrf_headers(client, base_url),
        base_url=base_url,
    )

    assert response.status_code == 201
    body = response.get_json()
    _assert_envelope(body)
    assert body["source"]["client_nonce"] == request_body["client_nonce"]
    _assert_valid("source.schema.json", body["source"])
    assert store.config.sources[0].client_nonce == request_body["client_nonce"]
    assert adapter.secret_lengths == [
        len(request_body["key"]),
        len(request_body["key"]),
    ]
    assert request_body["key"] not in json.dumps(body)

    listed = client.get("/api/models/sources", base_url=base_url).get_json()
    assert [source["client_nonce"] for source in listed["sources"]] == [
        request_body["client_nonce"]
    ]


@pytest.mark.parametrize(
    "client_nonce",
    [None, "scn_short", "SCN_01j5w8z7p4n6q2rt", "scn_01j5w8z7p4n6q2r!"],
)
def test_source_create_rejects_invalid_client_nonce_before_upstream(
    tmp_path,
    client_nonce,
):
    service, store, adapter = _service(tmp_path)

    with pytest.raises(ModelHubError) as rejected:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "custom",
                    "key": "sk-test-invalid-source-create-nonce",
                    "client_nonce": client_nonce,
                }
            )
        )

    assert rejected.value.code == "discovery_failed"
    assert adapter.secret_lengths == []
    assert store.config.sources == []


def test_source_create_nonce_covers_in_flight_committed_and_deleted_states(tmp_path):
    async def scenario():
        class BlockingObservationAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.observation_started = asyncio.Event()
                self.release_observation = asyncio.Event()
                self.block_once = True

            async def observe_source(
                self,
                vendor,
                base_url,
                credential_ref,
                protocol_order,
            ):
                if self.block_once:
                    self.block_once = False
                    self.observation_started.set()
                    await self.release_observation.wait()
                return await super().observe_source(
                    vendor,
                    base_url,
                    credential_ref,
                    protocol_order,
                )

        store = MemoryStore()
        adapter = BlockingObservationAdapter()
        service = ModelHubService(
            store=store,
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        payload = {
            "kind": "api_key",
            "vendor": "custom",
            "display_name": "Relay",
            "base_url": "https://relay.example/v1",
            "key": "sk-test-source-create-state-machine",
            "client_nonce": "scn_01j5w8z7p4n6q2rt",
        }

        first = asyncio.create_task(service.create_source(payload))
        await adapter.observation_started.wait()
        before_retry_work = list(adapter.secret_lengths)
        with pytest.raises(ModelHubError) as in_flight:
            await service.create_source(payload)
        assert in_flight.value.code == "source_create_in_progress"
        assert in_flight.value.status == 409
        assert adapter.secret_lengths == before_retry_work

        adapter.release_observation.set()
        committed = await first
        after_commit_work = list(adapter.secret_lengths)
        with pytest.raises(ModelHubError) as conflict:
            await service.create_source(payload)
        assert conflict.value.code == "source_nonce_conflict"
        assert conflict.value.status == 409
        assert adapter.secret_lengths == after_commit_work
        assert [
            source["client_nonce"]
            for source in service.list_sources()
            if source.get("client_nonce") == payload["client_nonce"]
        ] == [payload["client_nonce"]]

        await service.delete_source(committed["source"]["id"], force=True)
        recreated = await service.create_source(payload)
        assert recreated["source"]["id"] != committed["source"]["id"]
        assert recreated["source"]["client_nonce"] == payload["client_nonce"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "malformed_fields",
    [
        pytest.param({"protocol_order": ["anthropic"]}, id="protocol-order"),
        pytest.param({"billing": "credits"}, id="billing"),
        pytest.param(
            {
                "models": [
                    {
                        "id": "duplicate-model",
                        "origin": "manual",
                        "reasoning_efforts": [],
                    },
                    {
                        "id": "duplicate-model",
                        "origin": "manual",
                        "reasoning_efforts": [],
                    },
                ]
            },
            id="duplicate-models",
        ),
    ],
)
def test_source_create_validates_all_fields_before_every_nonce_state(
    tmp_path,
    malformed_fields,
):
    async def scenario():
        class BlockingObservationAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.observation_started = asyncio.Event()
                self.release_observation = asyncio.Event()
                self.block_once = True

            async def observe_source(
                self,
                vendor,
                base_url,
                credential_ref,
                protocol_order,
            ):
                if self.block_once:
                    self.block_once = False
                    self.observation_started.set()
                    await self.release_observation.wait()
                return await super().observe_source(
                    vendor,
                    base_url,
                    credential_ref,
                    protocol_order,
                )

        adapter = BlockingObservationAdapter()
        service = ModelHubService(
            store=MemoryStore(),
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth-flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        payload = {
            "kind": "api_key",
            "vendor": "custom",
            "key": "sk-test-source-create-probe-order",
            "client_nonce": "scn_01j5w8z7p4n6q2rt",
        }
        malformed = {**payload, **malformed_fields}

        unclaimed_work = list(adapter.secret_lengths)
        with pytest.raises(ModelHubError) as unclaimed:
            await service.create_source(
                {
                    **malformed,
                    "client_nonce": "scn_01j5w8z7p4n6q2ru",
                }
            )
        assert unclaimed.value.code == "discovery_failed"
        assert adapter.secret_lengths == unclaimed_work

        first = asyncio.create_task(service.create_source(payload))
        await adapter.observation_started.wait()
        in_flight_work = list(adapter.secret_lengths)
        with pytest.raises(ModelHubError) as in_flight:
            await service.create_source(malformed)
        assert in_flight.value.code == "discovery_failed"
        assert adapter.secret_lengths == in_flight_work

        adapter.release_observation.set()
        await first
        committed_work = list(adapter.secret_lengths)
        with pytest.raises(ModelHubError) as committed:
            await service.create_source(malformed)
        assert committed.value.code == "discovery_failed"
        assert adapter.secret_lengths == committed_work

        recreated = await service.create_source(
            {
                **payload,
                "client_nonce": "scn_01j5w8z7p4n6q2ru",
            }
        )
        assert recreated["source"]["client_nonce"] == "scn_01j5w8z7p4n6q2ru"

    asyncio.run(scenario())


def test_source_create_nonce_releases_only_after_credential_cleanup(tmp_path):
    async def scenario():
        adapter = FakeAdapter()
        adapter.observation = SourceObservation(
            outcome=ObservationOutcome.AUTHENTICATION_FAILED,
            reachable=True,
            authenticated=False,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            model_ids=(),
        )
        service = ModelHubService(
            store=MemoryStore(),
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        payload = {
            "kind": "api_key",
            "vendor": "custom",
            "key": "sk-test-source-create-retry",
            "client_nonce": "scn_01j5w8z7p4n6q2rt",
        }

        with pytest.raises(ModelHubError) as rejected:
            await service.create_source(payload)
        assert rejected.value.code == "discovery_failed"
        assert adapter.revoked == ["cred_test001"]

        adapter.observation = None
        created = await service.create_source(payload)
        assert created["source"]["client_nonce"] == payload["client_nonce"]

    asyncio.run(scenario())


def test_source_create_nonce_retains_unsettled_cleanup_until_restart(tmp_path):
    async def scenario():
        class CleanupFailingAdapter(FakeAdapter):
            async def revoke_credential(self, credential_ref):
                raise RuntimeError("cleanup unavailable")

        class UnwritableRevocations(CredentialRevocationJournal):
            def add(self, source_id, credential_ref, *, operation="revoke_credential"):
                raise OSError("journal unavailable")

        store = MemoryStore()
        adapter = CleanupFailingAdapter()
        adapter.observation = SourceObservation(
            outcome=ObservationOutcome.AUTHENTICATION_FAILED,
            reachable=True,
            authenticated=False,
            protocol=None,
            discovery=ObservationDiscovery.NOT_ATTEMPTED,
            model_ids=(),
        )
        service = ModelHubService(
            store=store,
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth_flows.json"),
            revocations=UnwritableRevocations(tmp_path / "revocations.json"),
        )
        payload = {
            "kind": "api_key",
            "vendor": "custom",
            "key": "sk-test-source-create-cleanup",
            "client_nonce": "scn_01j5w8z7p4n6q2rt",
        }

        with pytest.raises(ModelHubError) as cleanup_failed:
            await service.create_source(payload)
        assert cleanup_failed.value.code == "engine_down"
        upstream_work = list(adapter.secret_lengths)

        with pytest.raises(ModelHubError) as retained:
            await service.create_source(payload)
        assert retained.value.code == "source_create_in_progress"
        assert adapter.secret_lengths == upstream_work

        restarted_adapter = FakeAdapter()
        restarted = ModelHubService(
            store=store,
            adapter=restarted_adapter,
            events=BoundedEventLog(tmp_path / "restarted-events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "restarted-oauth-flows.json"),
            revocations=CredentialRevocationJournal(
                tmp_path / "restarted-revocations.json"
            ),
        )
        created = await restarted.create_source(payload)
        assert created["source"]["client_nonce"] == payload["client_nonce"]

    asyncio.run(scenario())


def test_source_create_nonce_reconciles_pending_revocations_before_retry(tmp_path):
    async def scenario():
        class OrderedAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.operations = []

            async def sync_sources(self, bindings):
                self.operations.append("sync")
                await super().sync_sources(bindings)

            async def revoke_credential(self, credential_ref):
                self.operations.append(f"revoke:{credential_ref}")
                await super().revoke_credential(credential_ref)

            async def provision_transient_credential(self, vendor, secret, base_url):
                self.operations.append("provision_transient")
                return await super().provision_transient_credential(
                    vendor,
                    secret,
                    base_url,
                )

        journal_path = tmp_path / "revocations.json"
        journal = CredentialRevocationJournal(journal_path)
        journal.add("src_failed01", "cred_orphaned")
        adapter = OrderedAdapter()
        service = ModelHubService(
            store=MemoryStore(),
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth-flows.json"),
            revocations=CredentialRevocationJournal(journal_path),
        )

        created = await service.create_source(
            {
                "kind": "api_key",
                "vendor": "custom",
                "key": "sk-test-source-create-reconcile",
                "client_nonce": "scn_01j5w8z7p4n6q2rt",
            }
        )

        assert created["source"]["client_nonce"] == "scn_01j5w8z7p4n6q2rt"
        assert adapter.operations[:3] == [
            "sync",
            "revoke:cred_orphaned",
            "provision_transient",
        ]
        assert service.revocations.list() == []

    asyncio.run(scenario())


def test_source_create_nonce_owns_permanent_credential_through_cancellation(tmp_path):
    async def scenario():
        class BlockingProvisionAdapter(FakeAdapter):
            def __init__(self):
                super().__init__()
                self.provision_started = asyncio.Event()
                self.release_provision = asyncio.Event()

            async def provision_credential(self, vendor, protocol, secret, base_url):
                self.provision_started.set()
                await self.release_provision.wait()
                return await super().provision_credential(
                    vendor,
                    protocol,
                    secret,
                    base_url,
                )

        adapter = BlockingProvisionAdapter()
        service = ModelHubService(
            store=MemoryStore(),
            adapter=adapter,
            events=BoundedEventLog(tmp_path / "events.json"),
            oauth_flows=OAuthFlowRegistry(tmp_path / "oauth-flows.json"),
            revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        )
        payload = {
            "kind": "api_key",
            "vendor": "custom",
            "key": "sk-test-source-create-cancel",
            "client_nonce": "scn_01j5w8z7p4n6q2rt",
        }

        create = asyncio.create_task(service.create_source(payload))
        await adapter.provision_started.wait()
        revoked_before_cancel = list(adapter.revoked)
        create.cancel()
        adapter.release_provision.set()
        with pytest.raises(asyncio.CancelledError):
            await create

        assert adapter.revoked == [*revoked_before_cancel, "cred_test002"]
        assert service.revocations.list() == []
        recreated = await service.create_source(payload)
        assert recreated["source"]["client_nonce"] == payload["client_nonce"]

    asyncio.run(scenario())


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


def test_agents_endpoint_projects_cli_presence_from_runtime(tmp_path):
    service, _store, _adapter = _service(tmp_path)
    service.cli_present_override = lambda backend: backend in {"claude", "codex"}

    agents = {agent["backend"]: agent for agent in service.list_agents()}

    assert {backend: agent["cli_present"] for backend, agent in agents.items()} == {
        "claude": True,
        "codex": True,
        "opencode": False,
    }


def test_agents_endpoint_cli_presence_probe_errors_fail_closed(tmp_path):
    service, _store, _adapter = _service(tmp_path)

    def broken_probe(_backend):
        raise OSError("probe failed")

    service.cli_present_override = broken_probe
    agents = {agent["backend"]: agent for agent in service.list_agents()}

    assert all(agent["cli_present"] is False for agent in agents.values())


def test_agent_supply_rpc_refreshes_cli_presence_before_first_response(tmp_path):
    from core.handlers.model_hub.rpc import dispatch_model_hub_rpc

    service, _store, _adapter = _service(tmp_path)
    calls = []
    service.cli_presence_refresh = lambda: calls.append("refresh")
    service.cli_present_override = lambda backend: backend == "claude"

    payload = asyncio.run(
        dispatch_model_hub_rpc(
            service,
            "get_agent_sources",
            {"backend": "claude"},
        )
    )

    assert payload["cli_present"] is True
    assert calls == ["refresh"]


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


def test_native_oauth_preserves_transient_login_conflict_identity(tmp_path):
    class LoginInProgressAdapter(FakeAdapter):
        async def start_oauth(self, source_id, vendor):
            raise BackendLoginInProgressError(vendor, "claude")

    service, _, _ = _service(tmp_path, LoginInProgressAdapter())

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))

    assert exc_info.value.status == 409
    assert exc_info.value.code == "native_login_in_progress"
    assert exc_info.value.detail == "modelHub.errors.native_login_in_progress"
    assert exc_info.value.data == {}


def test_nonce_oauth_start_replays_committed_flow_before_native_slot_conflict(tmp_path):
    service, store, adapter = _service(tmp_path)
    request = {
        "vendor": "anthropic",
        "channel": "native_cli",
        "client_nonce": "ofn_01j5w8z7p4n6q2rt",
    }
    first = asyncio.run(service.oauth_start(request))["flow"]
    existing = ModelHubSourceConfig(
        id="src_native0001",
        kind="subscription",
        vendor="anthropic",
        display_name="Claude subscription",
        protocol="anthropic",
        supply_channel="native_cli",
        billing="monthly",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="claude-opus-4-6", provenance="discovered")],
    )
    store.config.sources.append(existing)
    _refresh_fixture_routes(store.config)

    replayed = asyncio.run(service.oauth_start(request))["flow"]

    assert replayed == first
    assert adapter.oauth_start_calls == [(first["source_id"], "anthropic")]


def test_native_oauth_malformed_start_keeps_flow_id_when_cleanup_fails(tmp_path):
    """A malformed provider response remains reconcilable if cancel fails."""
    service, _store, adapter = _service(tmp_path)
    original_start = adapter.start_oauth

    async def malformed_start(source_id, vendor):
        flow = await original_start(source_id, vendor)
        return OAuthFlowState(
            **{**flow.__dict__, "source_id": "src_provider_mismatch"}
        )

    adapter.start_oauth = malformed_start
    adapter.fail_cancel = True
    with pytest.raises(ModelHubError) as failed:
        asyncio.run(
            service.oauth_start({"vendor": "anthropic", "channel": "native_cli"})
        )

    assert failed.value.code == "engine_down"
    # The flow id the provider was started under stays the cleanup handle even
    # though its own response disagreed about the source it belongs to.
    assert adapter.cancelled == ["oaf_00000001"]


def test_oauth_start_normalizes_vendor_before_singleton_and_adapter(tmp_path):
    service, _store, adapter = _service(tmp_path)

    flow = asyncio.run(
        service.oauth_start({"vendor": " Anthropic ", "channel": "hub"})
    )["flow"]

    assert flow["vendor"] == "anthropic"
    assert adapter.oauth_start_calls == [(flow["source_id"], "anthropic")]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"vendor": "anthropic", "channel": "hub", "client_nonce": None},
        {
            "vendor": "anthropic",
            "channel": "hub",
            "client_nonce": "invalid",
        },
        {
            "vendor": "anthropic",
            "channel": "hub",
            "client_nonce": "ofn_01j5w8z7p4n6q2rt",
            "unexpected": True,
        },
    ],
)
def test_oauth_start_rejects_invalid_nonce_payload_before_provider(tmp_path, payload):
    service, _, adapter = _service(tmp_path)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_start(payload))

    assert exc_info.value.code == "flow_not_found"
    assert adapter.oauth_start_calls == []


def test_model_hub_oauth_blocks_recovery_before_external_auth(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(
        service.oauth_start({"vendor": "anthropic", "channel": "native_cli"})
    )["flow"]
    store.recovery = True

    with pytest.raises(ModelHubError) as start_error:
        asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))
    assert start_error.value.code == "config_recovery"
    assert len(adapter.oauth_start_calls) == 1

    with pytest.raises(ModelHubError) as submit_error:
        asyncio.run(
            service.oauth_submit({"flow_id": flow["flow_id"], "value": "auth-code"})
        )
    assert submit_error.value.code == "config_recovery"
    assert adapter.secret_lengths == []


def test_chain_route_preserves_submitted_hops_against_opposite_source_order(
    monkeypatch, tmp_path
):
    service, store, _ = _service(tmp_path)
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
    store.config.agents["claude"].sources.order = [first["id"], second["id"]]
    hops = [
        {"source_id": second["id"], "model_id": model_id},
        {"source_id": first["id"], "model_id": model_id},
    ]
    assert store.config.agents["claude"].sources.order == [
        hop["source_id"] for hop in reversed(hops)
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
    assert [
        (hop.source_id, hop.model_id)
        for hop in store.config.agents["claude"].routes[model_id].hops
    ] == [(hop["source_id"], hop["model_id"]) for hop in hops]


def test_chain_route_guard_requires_and_replays_the_exact_current_plan(
    monkeypatch, tmp_path
):
    service, store, _ = _service(tmp_path)
    model_id = "claude-opus-4-6"
    first_id, second_id = "src_chain0101", "src_chain0102"
    old_hops = _set_claude_route_fixture(
        store,
        (first_id, second_id),
        model_id,
    )
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: service)
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)
    endpoint = f"/api/models/agents/claude/chain?model={model_id}"

    refused = client.put(
        endpoint,
        json={"hops": []},
        headers=headers,
        base_url=base_url,
    )

    assert refused.status_code == 409
    refusal = refused.get_json()
    _assert_envelope(refusal, ok=False)
    assert refusal["error"] == "source_last_supplier"
    assert refusal["would_remove_hops"] == [
        {
            "backend": "claude",
            "menu_model": model_id,
            "source_id": first_id,
            "model_id": model_id,
            "position": 1,
        },
        {
            "backend": "claude",
            "menu_model": model_id,
            "source_id": second_id,
            "model_id": model_id,
            "position": 2,
        },
    ]
    assert refusal["would_interrupt"] == [
        {"backend": "claude", "model_id": model_id, "agents": []}
    ]

    unconfirmed = client.put(
        endpoint,
        json={
            "hops": [],
            "force": True,
            "would_remove_hops": [
                {**refusal["would_remove_hops"][0], "position": True},
                refusal["would_remove_hops"][1],
            ],
            "would_interrupt": refusal["would_interrupt"],
        },
        headers=headers,
        base_url=base_url,
    )

    assert unconfirmed.status_code == 409
    assert unconfirmed.get_json()["would_remove_hops"] == refusal["would_remove_hops"]
    assert [
        (hop.source_id, hop.model_id)
        for hop in store.config.agents["claude"].routes[model_id].hops
    ] == [(hop.source_id, hop.model_id) for hop in old_hops]

    committed = client.put(
        endpoint,
        json={
            "hops": [],
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
        headers=headers,
        base_url=base_url,
    )

    assert committed.status_code == 200
    success = committed.get_json()
    assert json.dumps(success["removed_hops"], separators=(",", ":")) == json.dumps(
        refusal["would_remove_hops"], separators=(",", ":")
    )
    assert json.dumps(success["interrupted"], separators=(",", ":")) == json.dumps(
        refusal["would_interrupt"], separators=(",", ":")
    )
    assert success["chain"]["chain"] == []
    assert store.config.agents["claude"].routes[model_id].hops == ()


def test_chain_route_noninterrupting_success_is_force_invariant(monkeypatch, tmp_path):
    unforced_service, unforced_store, _ = _service(tmp_path / "unforced")
    model_id = "claude-opus-4-6"
    first_id, second_id = "src_chain0201", "src_chain0202"
    _set_claude_route_fixture(
        unforced_store,
        (first_id, second_id),
        model_id,
    )
    forced_service, forced_store, _ = _service(tmp_path / "forced")
    forced_store.config = ModelHubConfig.from_payload(
        unforced_store.config.to_payload()
    )
    client = app.test_client()
    base_url = "http://127.0.0.1:15131"
    headers = csrf_headers(client, base_url)
    endpoint = f"/api/models/agents/claude/chain?model={model_id}"
    hops = [{"source_id": first_id, "model_id": model_id}]

    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: unforced_service)
    unforced = client.put(
        endpoint,
        json={"hops": hops},
        headers=headers,
        base_url=base_url,
    )
    monkeypatch.setattr(ui_server, "_model_hub_service", lambda: forced_service)
    forced = client.put(
        endpoint,
        json={"hops": hops, "force": True},
        headers=headers,
        base_url=base_url,
    )

    assert unforced.status_code == forced.status_code == 200
    assert unforced.content == forced.content
    assert unforced.get_json()["removed_hops"] == [
        {
            "backend": "claude",
            "menu_model": model_id,
            "source_id": second_id,
            "model_id": model_id,
            "position": 2,
        }
    ]


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


def test_hub_reauth_requires_acknowledgement_then_materializes(tmp_path):
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

    for acknowledgement in ({}, {"acknowledge_irreversible": False}):
        with pytest.raises(ModelHubError) as exc_info:
            asyncio.run(service.reauth_source(source.id, acknowledgement))
        assert exc_info.value.code == "reauth_confirmation_required"
        assert adapter.oauth_start_calls == []

    flow = asyncio.run(
        service.reauth_source(
            source.id,
            {"acknowledge_irreversible": True},
        )
    )["flow"]
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
    first = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]

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
    second = asyncio.run(restarted.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]

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

    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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


def test_completed_hub_reauth_blocks_recovery_before_discovery(tmp_path):
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
        models=[ModelHubModelConfig(id="stale-entitlement", provenance="discovered")],
        credential_ref="cred_hub_reused",
    )
    store.config.sources.append(source)
    _refresh_fixture_routes(store.config)
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_hub_reused",
        }
    )
    store.recovery = True

    with pytest.raises(ModelHubError) as error:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert error.value.code == "config_recovery"
    assert adapter.secret_lengths == []
    assert store.config.sources[0].models[0].id == "stale-entitlement"


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

        flow = (await service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    first = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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

    second = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]

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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
    with pytest.raises(ModelHubError) as delete_refusal:
        asyncio.run(service.delete_source(source.id))
    asyncio.run(
        service.delete_source(
            source.id,
            force=True,
            confirmed_remove_hops=delete_refusal.value.data["would_remove_hops"],
            confirmed_interruptions=delete_refusal.value.data["would_interrupt"],
        )
    )
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    route_hop_schema = _schema("guard-refusal.schema.json")["definitions"][
        "RouteHopRef"
    ]
    for hop in refusal["would_remove_hops"]:
        Draft7Validator(route_hop_schema).validate(hop)
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
    unconfirmed = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json={**request_body, "force": True},
        headers=headers,
        base_url=base_url,
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.get_json()["would_remove_hops"] == refusal["would_remove_hops"]
    assert unconfirmed.get_json()["would_interrupt"] == refusal["would_interrupt"]

    stale_confirmation = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json={
            **request_body,
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"][:-1],
        },
        headers=headers,
        base_url=base_url,
    )
    assert stale_confirmation.status_code == 409
    assert stale_confirmation.get_json()["error"] == refusal["error"]
    assert stale_confirmation.get_json()["would_remove_hops"] == refusal[
        "would_remove_hops"
    ]
    assert stale_confirmation.get_json()["would_interrupt"] == refusal[
        "would_interrupt"
    ]

    committed = client.put(
        f"/api/models/sources/{created['id']}/credential",
        json={
            **request_body,
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
        headers=headers,
        base_url=base_url,
    ).get_json()

    assert committed["removed_hops"] == refusal["would_remove_hops"]
    assert committed["interrupted"] == refusal["would_interrupt"]
    assert committed["source"]["credential_ref"] == "cred_route_5"
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
    assert adapter.revoked == [
        "cred_test001",
        "cred_route_2",
        "cred_route_3",
        "cred_route_4",
        "cred_route_1",
    ]


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


def test_completed_hub_oauth_persists_only_a_response_proven_protocol(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "openai", "channel": "hub"}))[
        "flow"
    ]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_oauth_proven",
        }
    )
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.OBSERVED,
        reachable=True,
        authenticated=True,
        protocol="openai_chat",
        discovery=ObservationDiscovery.SUCCEEDED,
        model_ids=("gpt-5.6",),
    )

    result = asyncio.run(service.oauth_status(flow["flow_id"]))

    assert result["source"]["protocol"] == "openai_chat"
    assert store.config.sources[0].protocol == "openai_chat"
    assert store.config.sources[0].credential_ref == "cred_oauth_proven"
    assert adapter.observed_protocol_orders == [SOURCE_PROTOCOLS]
    assert adapter.revoked == []


def test_completed_hub_oauth_rejects_unproven_protocol_before_persistence(tmp_path):
    service, store, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "openai", "channel": "hub"}))[
        "flow"
    ]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "state": "success",
            "credential_ref": "cred_oauth_unproven",
        }
    )
    adapter.observation = SourceObservation(
        outcome=ObservationOutcome.AMBIGUOUS,
        reachable=True,
        authenticated=True,
        protocol=None,
        discovery=ObservationDiscovery.NOT_ATTEMPTED,
        model_ids=(),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.oauth_status(flow["flow_id"]))

    assert exc_info.value.code == "discovery_failed"
    assert exc_info.value.status == 422
    assert store.config.sources == []
    assert adapter.revoked == ["cred_oauth_unproven"]
    assert service.oauth_flows.binding(flow["flow_id"]) is None


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


def test_nonce_oauth_start_replays_cancelled_flow_until_expiry(tmp_path):
    current = [datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)]
    store = MemoryStore()
    adapter = FakeAdapter()
    service = ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        native_oauth_adapter=adapter,
        oauth_flows=OAuthFlowRegistry(
            tmp_path / "oauth_flows.json",
            now=lambda: current[0],
        ),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: current[0],
        requested_model_override=store.requested_model,
    )
    request = {
        "vendor": "anthropic",
        "channel": "native_cli",
        "client_nonce": "ofn_01j5w8z7p4n6q2rt",
    }

    first = asyncio.run(service.oauth_start(request))["flow"]
    asyncio.run(service.oauth_cancel(first["flow_id"]))
    retained = asyncio.run(service.oauth_start(request))["flow"]

    assert retained == {
        **first,
        "state": "cancelled",
        "presentation": {
            "auth_url": None,
            "device_code": None,
            "expects": "none",
            "instructions_key": None,
        },
    }
    assert retained["expires_at"] == first["expires_at"]
    assert adapter.oauth_start_calls == [
        (first["source_id"], "anthropic")
    ]

    current[0] = datetime(2026, 7, 23, 4, 15, tzinfo=timezone.utc)
    fresh = asyncio.run(service.oauth_start(request))["flow"]

    assert fresh["flow_id"] != first["flow_id"]
    assert len(adapter.oauth_start_calls) == 2


def test_nonce_oauth_start_coalesces_provider_failure_then_retries(tmp_path):
    async def run_failure_and_retry():
        service, _, adapter = _service(tmp_path)
        request = {
            "vendor": "anthropic",
            "channel": "native_cli",
            "client_nonce": "ofn_01j5w8z7p4n6q2rt",
        }
        provider_started = asyncio.Event()
        release_provider = asyncio.Event()
        original_start = adapter.start_oauth
        provider_calls = 0

        async def failing_start(source_id, vendor):
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            await release_provider.wait()
            raise RuntimeError("provider start failed")

        adapter.start_oauth = failing_start
        first = asyncio.create_task(service.oauth_start(request))
        await provider_started.wait()
        second = asyncio.create_task(service.oauth_start(dict(request)))
        await asyncio.sleep(0)
        assert second.done() is False
        release_provider.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert all(isinstance(result, ModelHubError) for result in results)
        assert [result.code for result in results] == [
            "engine_down",
            "engine_down",
        ]
        assert provider_calls == 1

        adapter.start_oauth = original_start
        fresh = await service.oauth_start(request)
        return fresh, provider_calls, adapter

    fresh, failed_calls, adapter = asyncio.run(run_failure_and_retry())

    assert fresh["flow"]["client_nonce"] == "ofn_01j5w8z7p4n6q2rt"
    assert failed_calls == 1
    assert len(adapter.oauth_start_calls) == 1


def test_nonce_oauth_start_owner_cancellation_releases_waiter_and_tuple(tmp_path):
    async def run_cancellation_and_retry():
        service, _, adapter = _service(tmp_path)
        request = {
            "vendor": "anthropic",
            "channel": "native_cli",
            "client_nonce": "ofn_01j5w8z7p4n6q2rt",
        }
        provider_started = asyncio.Event()
        original_start = adapter.start_oauth
        provider_calls = 0

        async def blocked_start(source_id, vendor):
            nonlocal provider_calls
            provider_calls += 1
            provider_started.set()
            await asyncio.Event().wait()

        adapter.start_oauth = blocked_start
        owner = asyncio.create_task(service.oauth_start(request))
        await provider_started.wait()
        waiter = asyncio.create_task(service.oauth_start(dict(request)))
        await asyncio.sleep(0)
        owner.cancel()
        results = await asyncio.gather(owner, waiter, return_exceptions=True)

        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert provider_calls == 1

        adapter.start_oauth = original_start
        fresh = await service.oauth_start(request)
        return fresh, adapter

    fresh, adapter = asyncio.run(run_cancellation_and_retry())

    assert fresh["flow"]["client_nonce"] == "ofn_01j5w8z7p4n6q2rt"
    assert len(adapter.oauth_start_calls) == 1


def test_oauth_start_keeps_every_owner_await_inside_the_installed_task(tmp_path):
    """A claimed nonce stays awaitable and releasable from the claim onward.

    The claim is what a concurrent same-tuple retry looks for, and the only
    release is ``start_and_remember``'s own ``finally``. An owner await placed
    before the task is installed therefore strands the tuple until restart, so
    this asserts the property rather than the one pre-check that broke it: on the
    owner path from claim to install there is nothing to wait on at all.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(ModelHubService.oauth_start)))
    body = tree.body[0].body
    claim = next(i for i, stmt in enumerate(body) if "claim_nonce" in ast.unparse(stmt))
    install = next(
        i for i, stmt in enumerate(body)
        if "_oauth_start_tasks[nonce_key] = task" in ast.unparse(stmt)
    )

    for stmt in body[claim + 1:install]:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # The task's own awaits are the safe ones: it is installed before it
            # is awaited, and it owns the release.
            continue
        if isinstance(stmt, ast.If) and ast.unparse(stmt.test) == "nonce_key is None":
            # No nonce, no claim — nothing for a retry to await or for a
            # cancellation to leak.
            continue
        waits = [
            node for node in ast.walk(stmt)
            if isinstance(node, (ast.Await, ast.AsyncWith, ast.AsyncFor))
        ]
        assert not waits, f"owner awaits before its task is installed: {ast.unparse(stmt)}"


def test_nonce_oauth_start_coalesces_a_retry_while_the_native_slot_read_waits(tmp_path):
    """The member the structure guard above exists for, end to end."""

    async def run_concurrent_start():
        service, _, adapter = _service(tmp_path)
        request = {
            "vendor": "anthropic",
            "channel": "native_cli",
            "client_nonce": "ofn_01j5w8z7p4n6q2rt",
        }
        # Migration holds the same lock the native-slot read needs, which is the
        # wait this test is about.
        await service._mutation_lock.acquire()
        owner = asyncio.create_task(service.oauth_start(request))
        await asyncio.sleep(0)
        retry = asyncio.create_task(service.oauth_start(dict(request)))
        await asyncio.sleep(0)

        assert owner.done() is False
        assert retry.done() is False

        service._mutation_lock.release()
        first, second = await asyncio.gather(owner, retry)
        return first, second, adapter

    first, second, adapter = asyncio.run(run_concurrent_start())

    assert first == second
    assert len(adapter.oauth_start_calls) == 1


def test_native_oauth_start_recovers_the_vendor_after_failure_and_cancel(tmp_path):
    """A login that never reached the CLI leaves nothing behind to unblock."""

    async def run_recovery():
        service, _, adapter = _service(tmp_path)
        original_start = adapter.start_oauth

        async def failing_start(source_id, vendor):
            raise RuntimeError("provider start failed")

        adapter.start_oauth = failing_start
        with pytest.raises(ModelHubError) as failure:
            await service.oauth_start({"vendor": "anthropic", "channel": "native_cli"})
        assert failure.value.code == "engine_down"

        provider_started = asyncio.Event()
        release_provider = asyncio.Event()

        async def blocked_start(source_id, vendor):
            provider_started.set()
            await release_provider.wait()
            return await original_start(source_id, vendor)

        adapter.start_oauth = blocked_start
        pending = asyncio.create_task(
            service.oauth_start({"vendor": "anthropic", "channel": "native_cli"})
        )
        await provider_started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        release_provider.set()
        await asyncio.sleep(0)
        adapter.start_oauth = original_start
        recovered = await service.oauth_start(
            {"vendor": "anthropic", "channel": "native_cli"}
        )
        return recovered, adapter

    recovered, adapter = asyncio.run(run_recovery())

    assert recovered["flow"]["vendor"] == "anthropic"
    assert len(adapter.oauth_start_calls) == 1


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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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
    flow = asyncio.run(service.reauth_source(source.id, {"acknowledge_irreversible": True}))["flow"]
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


@pytest.mark.parametrize("suffix", ["+00:00", "Z", ""])
@pytest.mark.parametrize("iso,expired", [("2026-07-23T02:59:00", True), ("2026-07-23T03:01:00", False)])
def test_a_flow_deadline_decides_the_same_way_however_it_was_written(tmp_path, iso, suffix, expired):
    """Seed every shape a provider can write the deadline in.

    The service clock is UTC-aware, so a naive timestamp compared against it
    raises ``TypeError`` instead of answering — turning "is this flow still
    open?" into a 500 on a path whose whole job is to answer it.
    """

    service, _, adapter = _service(tmp_path)
    flow = asyncio.run(service.oauth_start({"vendor": "anthropic", "channel": "native_cli"}))["flow"]
    adapter.flows[flow["flow_id"]] = OAuthFlowState(
        **{
            **adapter.flows[flow["flow_id"]].__dict__,
            "expires_at_iso": f"{iso}{suffix}",
        }
    )

    submit = lambda: asyncio.run(  # noqa: E731
        service.oauth_submit({"flow_id": flow["flow_id"], "value": "code"})
    )
    if expired:
        with pytest.raises(ModelHubError) as exc_info:
            submit()
        assert exc_info.value.code == "flow_expired"
    else:
        submit()
        assert adapter.secret_lengths == [4]


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
    save_config(tmp_path)

    response = app.test_client().post(
        "/api/models/runtime/start",
        base_url="https://alex.avibe.bot",
        environ_base=remote_peer(),
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
    old_credential_ref = source["credential_ref"]
    discovery_refs: list[str] = []

    async def discover_narrower(vendor, protocol, base_url, credential_ref):
        discovery_refs.append(credential_ref)
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
    refused_retarget = adapter.retargeted_credentials[0]
    assert refused_retarget[:4] == (
        old_credential_ref,
        "anthropic",
        "anthropic",
        replacement_url,
    )
    assert discovery_refs == [refused_retarget[4]]
    assert refused_retarget[4] in adapter.revoked

    committed = client.patch(
        f"/api/models/sources/{source['id']}",
        json={
            "base_url": replacement_url,
            "force": True,
            "would_remove_hops": refusal["would_remove_hops"],
            "would_interrupt": refusal["would_interrupt"],
        },
        headers=headers,
        base_url=origin,
    ).get_json()

    assert committed["source"]["base_url"] == replacement_url
    committed_retarget = adapter.retargeted_credentials[1]
    assert committed_retarget[:4] == refused_retarget[:4]
    assert committed["source"]["credential_ref"] == committed_retarget[4]
    assert discovery_refs == [refused_retarget[4], committed_retarget[4]]
    assert old_credential_ref in adapter.revoked
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


def test_base_url_retarget_cancellation_revokes_only_the_uncommitted_ref(
    tmp_path,
):
    class BlockingRetargetAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.retarget_started = asyncio.Event()
            self.release_retarget = asyncio.Event()

        async def retarget_api_key_credential(
            self,
            credential_ref,
            vendor,
            protocol,
            base_url,
        ):
            self.retarget_started.set()
            await self.release_retarget.wait()
            return await super().retarget_api_key_credential(
                credential_ref,
                vendor,
                protocol,
                base_url,
            )

    async def scenario():
        adapter = BlockingRetargetAdapter()
        service, store, _ = _service(tmp_path, adapter=adapter)
        source = await _create_source(
            service,
            {
                "kind": "api_key",
                "vendor": "custom",
                "display_name": "Retarget cancellation",
                "base_url": "https://old-relay.example/v1",
                "key": "sk-test-transient-only",
            },
        )
        old_ref = source["credential_ref"]
        revoked_before = list(adapter.revoked)
        task = asyncio.create_task(
            service.patch_source(
                source["id"],
                {"base_url": "https://new-relay.example/v1"},
            )
        )
        await adapter.retarget_started.wait()
        task.cancel()
        adapter.release_retarget.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        [persisted] = store.config.sources
        replacement_ref = adapter.retargeted_credentials[0][4]
        assert persisted.credential_ref == old_ref
        assert persisted.base_url == "https://old-relay.example/v1"
        assert adapter.revoked == [*revoked_before, replacement_ref]

    asyncio.run(scenario())


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
