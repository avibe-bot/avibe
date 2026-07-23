from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from config.v2_config import (
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.classification import classify_outcome
from core.handlers.model_hub.events import BoundedEventLog, build_resolution_event
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService, _mask_credential
from vibe.i18n import t as i18n_t


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.fail_save = False

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        if self.fail_save:
            raise OSError("save failed")
        self.config = config


class FakeAdapter:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.invocations = []
        self.synced = []
        self.revoked = []
        self.provisioned = []
        self.fail_sync = False
        self.fail_revoke = False

    async def ensure_installed(self):
        return await self.status()

    async def start(self):
        return await self.status()

    async def stop(self):
        return None

    async def status(self):
        return EngineStatus(EngineHealth.OK, "v7.2.95", True, "127.0.0.1", 15220, None)

    async def gateway_token(self):
        return "local-test-token"

    async def provision_credential(self, vendor, protocol, secret, base_url):
        self.provisioned.append((vendor, protocol, base_url))
        return "cred_test"

    async def revoke_credential(self, credential_ref):
        self.revoked.append(credential_ref)
        if self.fail_revoke:
            raise RuntimeError("revoke failed")
        return None

    async def sync_sources(self, bindings):
        self.synced.append(tuple(bindings))
        if self.fail_sync:
            raise RuntimeError("sync failed")

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        return ("claude-opus-4-6",)

    async def invoke(self, source_id, model_id, request, stream, origin):
        self.invocations.append((source_id, model_id, origin))
        result = self.outcomes.popleft()
        return result if isinstance(result, FakeInvokeHandle) else FakeInvokeHandle(result)

    async def start_oauth(self, source_id, vendor):
        raise AssertionError

    async def oauth_status(self, flow_id):
        raise AssertionError

    async def submit_oauth(self, flow_id, value):
        raise AssertionError

    async def cancel_oauth(self, flow_id):
        raise AssertionError


class FakeInvokeHandle:
    def __init__(self, outcome, stream=None):
        self._outcome = outcome
        self._stream = stream

    @property
    def stream(self):
        return self._stream

    async def outcome(self):
        return self._outcome


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        ("sk-test-never-persist-this", "sk-test…this"),
        ("abcde", "…bcde"),
        ("abcd", "…••••"),
    ],
)
def test_credential_display_mask_never_exposes_the_whole_secret(secret, expected):
    assert _mask_credential(secret) == expected
    assert secret != expected


def _outcome(kind, *, status=None, code=None, message=None, stream_started=False):
    return RawCallOutcome(
        kind=kind,
        http_status=status,
        error_code=code,
        redacted_message=message,
        stream_started=stream_started,
        model_id="claude-opus-4-6",
        source_id="src_primary01",
    )


def _source(source_id: str, display_name: str, *, billing: str = "metered") -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind="api_key",
        vendor="anthropic",
        display_name=display_name,
        protocol="anthropic",
        supply_channel="hub",
        billing=billing,
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="claude-opus-4-6", provenance="discovered")],
        credential_ref=f"cred_{source_id}",
    )


def _service(tmp_path, adapter, *, agents=None, now=None):
    sources = [
        _source("src_primary01", "Primary", billing="monthly"),
        _source("src_backup001", "Backup"),
    ]
    config = ModelHubConfig(
        sources=sources,
        priority_order=[source.id for source in sources],
        agents=agents
        or {
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
            for backend in ("claude", "codex", "opencode")
        },
    )
    return ModelHubService(
        store=MemoryStore(config),
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json", max_entries=5),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now or (lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)),
    )


@pytest.mark.parametrize(
    ("outcome", "refresh_attempted", "action", "reason"),
    [
        (_outcome(RawOutcomeKind.SUCCESS, status=200), False, "return", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_parameter"), False, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=422, code="tool_schema_error"), False, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401), False, "refresh", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401), True, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401, code="invalid_request"), False, "refresh", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401, code="invalid_request"), True, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=429), False, "fallback", "rate_limited"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=403, code="quota_exhausted"), False, "fallback", "quota_exhausted"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=503), False, "fallback", "server_error"),
        (_outcome(RawOutcomeKind.NETWORK_ERROR), False, "fallback", "network"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=429, stream_started=True), False, "surface", None),
    ],
)
def test_error_classification_table(outcome, refresh_attempted, action, reason):
    decision = classify_outcome(outcome, refresh_attempted=refresh_attempted)
    assert decision.action == action
    assert decision.reason == reason


def test_quota_failure_cools_source_switches_and_emits_redacted_events(tmp_path):
    """Scenario: MH-RES-001."""

    fake_key = "sk-live-super-secret-material"
    clock = [datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)]
    adapter = FakeAdapter(
        [
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=429,
                code="quota_exhausted",
                message=f'upstream redaction failure included {fake_key}',
            ),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter, now=lambda: clock[0])

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={"messages": []},
        )
    )

    assert resolved.source_id == "src_backup001"
    assert [call[0] for call in adapter.invocations] == ["src_primary01", "src_backup001"]
    assert service.store.load().sources[0].state.status == "cooldown"
    persisted = (tmp_path / "events.json").read_text(encoding="utf-8")
    assert fake_key not in persisted
    events = service.list_events(limit=10)
    assert [event["kind"] for event in events] == ["switch", "cooldown"]

    clock[0] += timedelta(minutes=6)
    recovered = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={"messages": []},
        )
    )
    assert recovered.source_id == "src_primary01"
    assert service.store.load().sources[0].state.status == "standby"
    assert service.list_events(limit=10)[0]["kind"] == "recover"


def test_event_log_failure_does_not_abort_failover(tmp_path):
    class UnwritableEventLog:
        def append(self, event):
            raise OSError("read-only state")

        def list(self, *, limit=20, before=None):
            return []

    adapter = FakeAdapter(
        [
            _outcome(RawOutcomeKind.HTTP_ERROR, status=429),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter)
    service.events = UnwritableEventLog()

    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_backup001"
    assert service.store.load().sources[0].state.status == "cooldown"
    assert [call[0] for call in adapter.invocations] == ["src_primary01", "src_backup001"]


def test_401_refreshes_exactly_once_before_returning(tmp_path):
    adapter = FakeAdapter(
        [
            _outcome(RawOutcomeKind.HTTP_ERROR, status=401),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter)

    result = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert result.source_id == "src_primary01"
    assert len(adapter.invocations) == 2


def test_refreshed_fallback_stream_emits_switch_event(tmp_path):
    async def stream_bytes():
        yield b"ok"

    adapter = FakeAdapter(
        [
            _outcome(RawOutcomeKind.HTTP_ERROR, status=429),
            _outcome(RawOutcomeKind.HTTP_ERROR, status=401),
            FakeInvokeHandle(
                _outcome(RawOutcomeKind.SUCCESS, status=200, stream_started=True),
                stream=stream_bytes(),
            ),
        ]
    )
    service = _service(tmp_path, adapter)

    result = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
            stream=True,
        )
    )

    assert result.source_id == "src_backup001"
    assert [event["kind"] for event in service.list_events(limit=10)] == ["switch", "cooldown"]


def test_parameter_error_and_started_stream_never_fallback(tmp_path):
    for outcome in (
        _outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_parameter"),
        _outcome(RawOutcomeKind.HTTP_ERROR, status=429, stream_started=True),
    ):
        adapter = FakeAdapter([outcome])
        service = _service(tmp_path, adapter)
        with pytest.raises(ModelHubError):
            asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}, stream=True))
        assert len(adapter.invocations) == 1


def test_mapping_is_scoped_to_the_requesting_backend(tmp_path):
    """Scenario: MH-MAP-001."""

    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
        for backend in ("claude", "codex", "opencode")
    }
    agents["claude"].mappings = [
        ModelHubMappingConfig(builtin_id="claude-native", target_model_id="claude-opus-4-6", enabled=True)
    ]
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter, agents=agents)

    result = asyncio.run(service.resolve(backend="claude", model_id="claude-native", request={}))

    assert result.model_id == "claude-opus-4-6"
    assert agents["codex"].mappings == []


def test_opencode_provider_prefix_selects_matching_source_and_current_payload(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].vendor = "custom"
    config.sources[0].state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T02:59:00Z",
    )
    config.sources[1].vendor = "anthropic"
    config.agents["opencode"].menu.checked = ["anthropic/claude-opus-4-6"]

    current = next(agent for agent in service.list_agents() if agent["backend"] == "opencode")["current"]
    resolved = asyncio.run(
        service.resolve(
            backend="opencode",
            model_id="anthropic/claude-opus-4-6",
            request={},
        )
    )

    assert current["source_id"] == "src_backup001"
    assert resolved.source_id == "src_backup001"
    assert adapter.invocations == [("src_backup001", "claude-opus-4-6", "opencode")]
    assert service.list_events(limit=10) == []
    assert config.sources[0].state.status == "cooldown"


def test_opencode_unknown_vendor_uses_custom_provider_identifier(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].vendor = "relaycorp"
    config.agents["opencode"].menu.checked = ["custom/claude-opus-4-6"]

    menu = asyncio.run(service.set_opencode_menu(config.agents["opencode"].menu.to_payload()))
    current = next(agent for agent in service.list_agents() if agent["backend"] == "opencode")["current"]
    resolved = asyncio.run(
        service.resolve(
            backend="opencode",
            model_id="custom/claude-opus-4-6",
            request={},
        )
    )

    assert menu["menu"]["checked"] == ["custom/claude-opus-4-6"]
    assert current["source_id"] == "src_primary01"
    assert resolved.source_id == "src_primary01"


def test_opencode_resolution_rejects_models_outside_checked_menu(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    service.store.load().agents["opencode"].menu.checked = []

    current = next(agent for agent in service.list_agents() if agent["backend"] == "opencode")["current"]

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.resolve(
                backend="opencode",
                model_id="anthropic/claude-opus-4-6",
                request={},
            )
        )

    assert exc_info.value.code == "mapping_target_unavailable"
    assert current is None
    assert adapter.invocations == []


def test_persisted_hub_sources_sync_before_first_resolution(tmp_path):
    class RegistrationRequiredAdapter(FakeAdapter):
        def __init__(self):
            super().__init__([_outcome(RawOutcomeKind.SUCCESS, status=200)])
            self.registered = set()

        async def sync_sources(self, bindings):
            await super().sync_sources(bindings)
            self.registered = {binding.source_id for binding in bindings}

        async def invoke(self, source_id, model_id, request, stream, origin):
            assert source_id in self.registered
            return await super().invoke(source_id, model_id, request, stream, origin)

    adapter = RegistrationRequiredAdapter()
    service = _service(tmp_path, adapter)

    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_primary01"
    assert [binding.source_id for binding in adapter.synced[0]] == [
        "src_primary01",
        "src_backup001",
    ]


def test_agent_current_skips_cooldown_and_error_sources(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T03:05:00Z",
    )

    claude = next(agent for agent in service.list_agents() if agent["backend"] == "claude")
    assert claude["current"]["source_id"] == "src_backup001"

    config.sources[1].state = ModelHubSourceStateConfig(status="error")
    claude = next(agent for agent in service.list_agents() if agent["backend"] == "claude")
    assert claude["current"] is None


def test_native_source_is_dispatched_before_hub_and_cooldown_falls_through(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    native = service.store.load().sources[0]
    native.kind = "subscription"
    native.supply_channel = "native_cli"

    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_primary01"
    assert resolved.supply_channel == "native_cli"
    assert resolved.handle is None
    assert adapter.invocations == []

    native.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T03:05:00Z",
    )
    fallback = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert fallback.source_id == "src_backup001"
    assert fallback.supply_channel == "hub"
    assert adapter.invocations == [("src_backup001", "claude-opus-4-6", "claude")]


def test_native_dispatch_attempts_pending_credential_revoke(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    native = service.store.load().sources[0]
    native.kind = "subscription"
    native.supply_channel = "native_cli"
    native.credential_ref = None
    service.revocations.add("src_deleted", "cred_deleted")

    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_primary01"
    assert resolved.supply_channel == "native_cli"
    assert adapter.revoked == ["cred_deleted"]
    assert service.revocations.list() == []
    assert adapter.invocations == []


def test_direct_mode_never_enters_hub_resolution(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    service.store.load().agents["claude"].mode = "direct"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert exc_info.value.code == "mode_switch_blocked"
    assert adapter.invocations == []


def test_source_creation_persists_before_engine_sync(tmp_path):
    order = []

    class OrderingAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            order.append("sync")
            assert len(service.store.load().sources) == 3
            await super().sync_sources(bindings)

    adapter = OrderingAdapter([])
    service = _service(tmp_path, adapter)
    save = service.store.save

    def record_save(config):
        save(config)
        order.append("persist")

    service.store.save = record_save
    created = asyncio.run(
        service.create_source(
            {
                "kind": "api_key",
                "vendor": "anthropic",
                "display_name": "Ordered source",
                "key": "sk-test-transient-only",
            }
        )
    )

    assert order == ["persist", "sync"]
    assert service.store.load().sources[-1].id == created["id"]


def test_source_creation_revokes_credential_when_persist_fails(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    original_ids = [source.id for source in service.store.load().sources]
    service.store.fail_save = True

    with pytest.raises(OSError, match="save failed"):
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Unpersisted source",
                    "key": "sk-test-transient-only",
                }
            )
        )

    assert [source.id for source in service.store.load().sources] == original_ids
    assert adapter.synced == []
    assert adapter.revoked == ["cred_test"]
    assert service.revocations.list() == []


def test_source_creation_is_not_persisted_when_engine_sync_fails(tmp_path):
    adapter = FakeAdapter([])
    adapter.fail_sync = True
    service = _service(tmp_path, adapter)
    for source in service.store.load().sources:
        source.credential_ref = f"cred_{source.id}"
    original_ids = [source.id for source in service.store.load().sources]

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Uncommitted",
                    "key": "sk-test-transaction-only",
                }
            )
        )

    assert exc_info.value.code == "engine_down"
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    assert [source.id for source in service.store.load().sources] == original_ids
    assert adapter.revoked == ["cred_test"]


def test_failed_create_rollback_is_journaled_until_revoke_recovers(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    adapter.fail_sync = True
    adapter.fail_revoke = True
    service = _service(tmp_path, adapter)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Rollback source",
                    "key": "sk-test-transaction-only",
                }
            )
        )

    assert exc_info.value.code == "engine_down"
    pending = service.revocations.list()
    assert len(pending) == 1
    assert pending[0].credential_ref == "cred_test"

    adapter.fail_sync = False
    adapter.fail_revoke = False
    resolved = asyncio.run(
        service.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_primary01"
    assert adapter.revoked == ["cred_test", "cred_test"]
    assert service.revocations.list() == []


def test_subscription_source_rejects_api_key_credentials(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "subscription",
                    "vendor": "anthropic",
                    "display_name": "Invalid subscription",
                    "key": "sk-test-must-not-be-provisioned",
                }
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert adapter.provisioned == []


def test_source_delete_does_not_revoke_when_config_save_fails(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    service.store.load().sources[0].credential_ref = "cred_primary"
    service.store.load().sources[1].credential_ref = "cred_backup"
    service.store.fail_save = True

    with pytest.raises(OSError, match="save failed"):
        asyncio.run(service.delete_source("src_primary01", force=True))

    assert adapter.revoked == []
    assert [source.id for source in service.store.load().sources] == ["src_primary01", "src_backup001"]


def test_deleting_last_hub_source_syncs_empty_binding_set(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources = [config.sources[0]]
    config.priority_order = [config.sources[0].id]

    asyncio.run(service.delete_source("src_primary01", force=True))

    assert adapter.synced == [()]
    assert adapter.revoked == ["cred_src_primary01"]
    assert service.store.load().sources == []


def test_source_reference_survives_failed_credential_revoke(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    service.store.load().sources[0].credential_ref = "cred_primary"
    service.store.load().sources[1].credential_ref = "cred_backup"
    adapter.fail_revoke = True

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01", force=True))

    assert exc_info.value.code == "engine_down"
    assert [source.id for source in service.store.load().sources] == ["src_primary01", "src_backup001"]
    assert [tuple(binding.source_id for binding in batch) for batch in adapter.synced] == [
        ("src_backup001",),
        ("src_primary01", "src_backup001"),
    ]


def test_restart_replays_credential_revoke_after_delete_commit(tmp_path):
    class SimulatedProcessExit(BaseException):
        pass

    class CrashingAdapter(FakeAdapter):
        async def revoke_credential(self, credential_ref):
            raise SimulatedProcessExit

    journal = CredentialRevocationJournal(tmp_path / "revocations.json")
    crashing = CrashingAdapter([])
    service = _service(tmp_path, crashing)
    service.revocations = journal
    native = service.store.load().sources[1]
    native.kind = "subscription"
    native.supply_channel = "native_cli"
    native.credential_ref = None

    with pytest.raises(SimulatedProcessExit):
        asyncio.run(service.delete_source("src_primary01", force=True))

    assert [source.id for source in service.store.load().sources] == ["src_backup001"]
    assert journal.list()[0].credential_ref == "cred_src_primary01"

    recovered = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    restarted = ModelHubService(
        store=service.store,
        adapter=recovered,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        revocations=journal,
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )
    resolved = asyncio.run(
        restarted.resolve(backend="claude", model_id="claude-opus-4-6", request={})
    )

    assert resolved.source_id == "src_backup001"
    assert resolved.supply_channel == "native_cli"
    assert recovered.synced == [()]
    assert recovered.revoked == ["cred_src_primary01"]
    assert journal.list() == []


def test_selected_custom_model_cannot_be_deleted(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models.append(ModelHubModelConfig(id="manual-model", provenance="manual"))
    config.agents["opencode"].menu.checked = ["anthropic/manual-model"]

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_custom_model("src_primary01", "manual-model"))

    assert exc_info.value.code == "mode_switch_blocked"
    assert any(model.id == "manual-model" for model in service.store.load().sources[0].models)


def test_custom_model_preserves_slash_qualified_upstream_id(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    source = service.store.load().sources[0]
    source.vendor = "openrouter"
    for configured_source in service.store.load().sources:
        configured_source.credential_ref = f"cred_{configured_source.id}"

    updated = asyncio.run(
        service.add_custom_model(
            {
                "source_id": source.id,
                "model_id": "anthropic/claude-sonnet-4",
                "display_name": "Claude Sonnet 4",
            }
        )
    )
    menu = asyncio.run(
        service.set_opencode_menu(
            {"view": "featured", "checked": ["openrouter/anthropic/claude-sonnet-4"]}
        )
    )

    assert updated["models"][-1]["id"] == "anthropic/claude-sonnet-4"
    assert menu["menu"]["checked"] == ["openrouter/anthropic/claude-sonnet-4"]


def test_resolution_event_copy_comes_from_backend_i18n(tmp_path):
    event = build_resolution_event(
        agent="system",
        kind="cooldown",
        model_id="test-model",
        reason="network",
        from_label="Primary",
    )

    assert event.human_en == i18n_t(
        "modelHub.events.cooldown",
        "en",
        from_source="Primary",
        to_source=i18n_t("modelHub.events.sourceFallback", "en"),
        reason=i18n_t("modelHub.events.reason.network", "en"),
    )
    assert event.human_zh == i18n_t(
        "modelHub.events.cooldown",
        "zh",
        from_source="Primary",
        to_source=i18n_t("modelHub.events.sourceFallback", "zh"),
        reason=i18n_t("modelHub.events.reason.network", "zh"),
    )


def test_mapping_and_delete_guards_use_backend_eligible_sources(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].kind = "subscription"
    config.sources[0].supply_channel = "native_cli"
    config.sources[0].vendor = "anthropic"
    config.sources[1].kind = "subscription"
    config.sources[1].supply_channel = "native_cli"
    config.sources[1].vendor = "openai"
    config.sources[1].models = [ModelHubModelConfig(id="gpt-5", provenance="discovered")]

    with pytest.raises(ModelHubError, match="mapping_target_unavailable"):
        asyncio.run(
            service.set_mappings(
                "codex",
                [
                    {
                        "builtin_id": "gpt-5",
                        "target_model_id": "claude-opus-4-6",
                        "enabled": True,
                    }
                ],
            )
        )

    config.agents["claude"].mappings = [
        ModelHubMappingConfig("claude-native", "claude-opus-4-6", True)
    ]
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01"))
    assert exc_info.value.code == "mode_switch_blocked"

    config.agents["claude"].mode = "direct"
    asyncio.run(service.delete_source("src_primary01"))
    assert [source.id for source in service.store.load().sources] == ["src_backup001"]


def test_event_log_is_bounded_and_sanitizes_labels(tmp_path):
    log = BoundedEventLog(tmp_path / "events.json", max_entries=2)
    for index in range(3):
        log.append(
            build_resolution_event(
                agent="system",
                kind="cooldown",
                model_id=f"model-{index}",
                reason="network",
                from_source=f"src_source0{index}",
                from_label=(
                    "Bearer abcdefghijklmnop"
                    if index == 2
                    else "Anthropic API Key"
                    if index == 1
                    else f"Source {index}"
                ),
            )
        )

    events = json.loads((tmp_path / "events.json").read_text(encoding="utf-8"))
    assert len(events) == 2
    assert "abcdefghijklmnop" not in json.dumps(events)
