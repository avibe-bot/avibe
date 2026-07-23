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


def test_opencode_unknown_vendor_uses_custom_provider_identifier(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].vendor = "relaycorp"
    config.agents["opencode"].menu.checked = ["custom/claude-opus-4-6"]

    menu = service.set_opencode_menu(config.agents["opencode"].menu.to_payload())
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


def test_agent_current_skips_cooldown_and_error_sources(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T03:05:00+00:00",
    )

    claude = next(agent for agent in service.list_agents() if agent["backend"] == "claude")
    assert claude["current"]["source_id"] == "src_backup001"

    config.sources[1].state = ModelHubSourceStateConfig(status="error")
    claude = next(agent for agent in service.list_agents() if agent["backend"] == "claude")
    assert claude["current"] is None


def test_direct_mode_never_enters_hub_resolution(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    service.store.load().agents["claude"].mode = "direct"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert exc_info.value.code == "mode_switch_blocked"
    assert adapter.invocations == []


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
    assert [source.id for source in service.store.load().sources] == original_ids
    assert adapter.revoked == ["cred_test"]


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
        service.set_mappings(
            "codex",
            [{"builtin_id": "gpt-5", "target_model_id": "claude-opus-4-6", "enabled": True}],
        )

    config.agents["claude"].mappings = [
        ModelHubMappingConfig("claude-native", "claude-opus-4-6", True)
    ]
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01"))
    assert exc_info.value.code == "mode_switch_blocked"


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
