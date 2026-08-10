from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    V2Config,
)
from core.handlers.model_hub.adapter import (
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.classification import classify_outcome
from core.handlers.model_hub.errors import ModelDiscoveryError
from core.handlers.model_hub.events import BoundedEventLog, build_resolution_event
from core.handlers.model_hub.resolver import resolve_model_hub_turn
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    ModelHubError,
    ModelHubService,
    V2ModelHubConfigStore,
    _mask_credential,
)
from core.services.settings import default_config
from vibe.i18n import t as i18n_t
from vibe.model_hub_runtime.client import _SAFE_ERROR_CODES
from vibe.model_hub_runtime.state import EngineStateError


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config
        self.fail_save = False
        self.requested_models = {"claude": "claude-opus-4-6"}

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        if self.fail_save:
            raise OSError("save failed")
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


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
    agents = agents or {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub") for backend in ("claude", "codex", "opencode")
    }
    config = ModelHubConfig(sources=sources, agents=agents)
    for agent in agents.values():
        agent.sources = ModelHubAgentSourcesConfig(
            policy="custom",
            order=[source.id for source in sources],
        )
    store = MemoryStore(config)
    return ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json", max_entries=5),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now or (lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc)),
        requested_model_override=lambda backend: store.requested_model(backend),
    )


def _configure_referenced_manual_model(service, *, model_id: str = "retired-model"):
    config = service.store.load()
    config.sources[0].models.append(ModelHubModelConfig(id=model_id, provenance="manual"))
    config.agents["claude"].mappings = [
        ModelHubMappingConfig(
            builtin_id="claude-opus-4-6",
            target_model_id=model_id,
            enabled=True,
        )
    ]
    config.agents["opencode"].menu.checked = [
        f"anthropic/{model_id}",
        "anthropic/claude-opus-4-6",
    ]
    return config


def _serialized_config(service) -> str:
    return json.dumps(
        service.store.load().to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _assert_no_references_to(service, model_id: str) -> None:
    persisted = service.store.load()
    mapping_targets = {mapping.target_model_id for agent in persisted.agents.values() for mapping in agent.mappings}
    menu_target_models = {
        identifier.partition("/")[2]
        for agent in persisted.agents.values()
        if agent.menu is not None
        for identifier in agent.menu.checked
    }
    available_models = {model.id for source in persisted.sources for model in source.models}

    assert mapping_targets <= available_models
    assert menu_target_models <= available_models
    assert (mapping_targets | menu_target_models).isdisjoint({model_id})


@pytest.mark.parametrize(
    ("outcome", "refresh_attempted", "action", "reason"),
    [
        (_outcome(RawOutcomeKind.SUCCESS, status=200), False, "return", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=400, code="invalid_parameter"), False, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=422, code="tool_schema_error"), False, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=404, code="model_not_found"), False, "surface", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=413, code="request_too_large"), False, "surface", None),
        (
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=403,
                message="The requested model is not accessible",
            ),
            False,
            "surface",
            None,
        ),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401), False, "refresh", None),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401), True, "fallback", "credential_expired"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=401, code="invalid_request"), False, "refresh", None),
        (
            _outcome(RawOutcomeKind.HTTP_ERROR, status=401, code="invalid_request"),
            True,
            "fallback",
            "credential_expired",
        ),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=402), False, "fallback", "balance_exhausted"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=429), False, "fallback", "rate_limited"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=403, code="quota_exhausted"), False, "fallback", "quota_exhausted"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=403), False, "fallback", "credential_revoked"),
        (
            _outcome(RawOutcomeKind.HTTP_ERROR, status=403, code="account_suspended"),
            False,
            "fallback",
            "account_banned",
        ),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=418), False, "fallback", "unclassified_error"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=503), False, "fallback", "server_error"),
        (_outcome(RawOutcomeKind.NETWORK_ERROR), False, "fallback", "network"),
        (_outcome(RawOutcomeKind.HTTP_ERROR, status=429, stream_started=True), False, "surface", None),
    ],
)
def test_error_classification_table(outcome, refresh_attempted, action, reason):
    decision = classify_outcome(outcome, refresh_attempted=refresh_attempted)
    assert decision.action == action
    assert decision.reason == reason


def test_safe_error_code_family_is_exhaustively_classified_without_overclaim():
    dispositions = {
        "api_error": (500, False, "fallback", "server_error", None),
        "account_banned": (403, False, "fallback", "account_banned", None),
        "account_disabled": (403, False, "fallback", "account_banned", None),
        "account_suspended": (403, False, "fallback", "account_banned", None),
        "authentication_error": (401, False, "refresh", None, None),
        "billing_error": (402, False, "fallback", "balance_exhausted", None),
        "context_length_exceeded": (
            400,
            False,
            "surface",
            None,
            "upstream_request_invalid",
        ),
        "insufficient_quota": (
            429,
            False,
            "fallback",
            "quota_exhausted",
            None,
        ),
        "invalid_api_key": (401, False, "refresh", None, None),
        "invalid_request_error": (
            400,
            False,
            "surface",
            None,
            "upstream_request_invalid",
        ),
        "model_not_found": (
            404,
            False,
            "surface",
            None,
            "upstream_request_invalid",
        ),
        "not_found_error": (
            404,
            False,
            "surface",
            None,
            "upstream_request_invalid",
        ),
        "overloaded_error": (529, False, "fallback", "server_error", None),
        "permission_error": (
            403,
            False,
            "fallback",
            "permission_denied",
            None,
        ),
        "quota_exceeded": (
            429,
            False,
            "fallback",
            "quota_exhausted",
            None,
        ),
        "rate_limit_error": (429, False, "fallback", "rate_limited", None),
        "rate_limit_exceeded": (429, False, "fallback", "rate_limited", None),
        "request_too_large": (
            413,
            False,
            "surface",
            None,
            "upstream_request_invalid",
        ),
        "server_error": (500, False, "fallback", "server_error", None),
    }

    assert set(dispositions) == set(_SAFE_ERROR_CODES)
    for code, (
        status,
        refresh_attempted,
        action,
        reason,
        error_code,
    ) in dispositions.items():
        decision = classify_outcome(
            _outcome(RawOutcomeKind.HTTP_ERROR, status=status, code=code),
            refresh_attempted=refresh_attempted,
        )
        assert (
            decision.action,
            decision.reason,
            decision.error_code,
        ) == (action, reason, error_code), code


def test_permission_denial_falls_back_without_mutating_source_health(tmp_path):
    adapter = FakeAdapter(
        [
            _outcome(
                RawOutcomeKind.HTTP_ERROR,
                status=403,
                code="permission_error",
            ),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter)

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert resolved.source_id == "src_backup001"
    assert [source.state.status for source in service.store.load().sources] == [
        "standby",
        "standby",
    ]
    events = service.list_events(limit=10)
    assert len(events) == 1
    assert events[0]["kind"] == "switch"
    assert events[0]["reason"] == "permission_denied"
    assert events[0]["from_source"] == "src_primary01"
    assert events[0]["to_source"] == "src_backup001"


def test_permission_denial_probe_does_not_block_the_source(tmp_path):
    service = _service(
        tmp_path,
        FakeAdapter(
            [
                _outcome(
                    RawOutcomeKind.HTTP_ERROR,
                    status=403,
                    code="permission_error",
                )
            ]
        ),
    )

    result = asyncio.run(
        service.probe_agent("claude", "claude-opus-4-6")
    )

    assert result["reachable"] is False
    assert result["error"] == "models.source.error.unclassified"
    assert service.store.load().sources[0].state.status == "standby"
    assert service.list_events(limit=10) == []


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
                message=f"upstream redaction failure included {fake_key}",
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

    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

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


@pytest.mark.parametrize(
    ("outcomes", "reason", "detail_key"),
    [
        (
            [
                _outcome(RawOutcomeKind.HTTP_ERROR, status=401),
                _outcome(RawOutcomeKind.HTTP_ERROR, status=401),
                _outcome(RawOutcomeKind.SUCCESS, status=200),
            ],
            "credential_expired",
            "models.source.needs_action.oauth_expired",
        ),
        (
            [
                _outcome(RawOutcomeKind.HTTP_ERROR, status=402),
                _outcome(RawOutcomeKind.SUCCESS, status=200),
            ],
            "balance_exhausted",
            "models.source.needs_action.balance_exhausted",
        ),
        (
            [
                _outcome(RawOutcomeKind.HTTP_ERROR, status=418),
                _outcome(RawOutcomeKind.SUCCESS, status=200),
            ],
            "unclassified_error",
            "models.source.error.unclassified",
        ),
    ],
)
def test_non_self_healing_failure_blocks_source_then_falls_back(
    tmp_path,
    outcomes,
    reason,
    detail_key,
):
    service = _service(tmp_path, FakeAdapter(outcomes))

    result = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert result.source_id == "src_backup001"
    primary = service.store.load().sources[0]
    assert primary.state.status == (
        "error" if reason == "unclassified_error" else "needs_action"
    )
    assert primary.state.detail_key == detail_key
    events = service.list_events(limit=10)
    assert [event["kind"] for event in events] == ["switch", "needs_action"]
    assert events[0]["reason"] == events[1]["reason"] == reason
    assert events[0]["severity"] == "info"
    assert events[1]["severity"] == "action_required"


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
        _outcome(RawOutcomeKind.HTTP_ERROR, status=404, code="model_not_found"),
        _outcome(RawOutcomeKind.HTTP_ERROR, status=413, code="request_too_large"),
        _outcome(RawOutcomeKind.HTTP_ERROR, status=429, stream_started=True),
    ):
        adapter = FakeAdapter([outcome])
        service = _service(tmp_path, adapter)
        with pytest.raises(ModelHubError):
            asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}, stream=True))
        assert len(adapter.invocations) == 1
        assert service.store.load().sources[0].state.status == "standby"


def test_mapping_is_scoped_to_the_requesting_backend(tmp_path):
    """Scenario: MH-MAP-001."""

    agents = {
        backend: ModelHubAgentSupplyConfig.default(backend, mode="hub") for backend in ("claude", "codex", "opencode")
    }
    agents["claude"].mappings = [
        ModelHubMappingConfig(builtin_id="claude-native", target_model_id="claude-opus-4-6", enabled=True)
    ]
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter, agents=agents)

    result = asyncio.run(service.resolve(backend="claude", model_id="claude-native", request={}))

    assert result.model_id == "claude-opus-4-6"
    assert agents["codex"].mappings == []


@pytest.mark.parametrize(
    ("requested_model", "discovered_models", "expected_model"),
    [
        (
            "claude-opus-4-5",
            [
                "claude-opus-4-5",
                "claude-opus-4-5-20250929",
                "claude-opus-4-5-20251101",
            ],
            "claude-opus-4-5-20251101",
        ),
        (
            "opus",
            ["opus", "claude-opus-4-8-20260601", "claude-opus-5-20260724"],
            "claude-opus-5-20260724",
        ),
        (
            "opus[1m]",
            ["claude-opus-4-8-20260601", "claude-opus-5-20260724"],
            "claude-opus-5-20260724",
        ),
        (
            "sonnet",
            ["claude-sonnet-4-6-20260301", "claude-sonnet-5-20260720"],
            "claude-sonnet-5-20260720",
        ),
        (
            "sonnet[1m]",
            ["claude-sonnet-4-6-20260301", "claude-sonnet-5-20260720"],
            "claude-sonnet-5-20260720",
        ),
        (
            "haiku",
            ["claude-haiku-4-20250101", "claude-haiku-4-5-20251001"],
            "claude-haiku-4-5-20251001",
        ),
        (
            "claude-opus-4-5-20250929",
            ["claude-opus-4-5-20250929", "claude-opus-4-5-20251101"],
            "claude-opus-4-5-20250929",
        ),
    ],
)
def test_native_claude_aliases_resolve_only_to_discovered_inventory(
    tmp_path,
    requested_model,
    discovered_models,
    expected_model,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(id=model_id, provenance="discovered")
        for model_id in discovered_models
    ]
    config.sources[1].models = []

    resolution = resolve_model_hub_turn(config, "claude", requested_model)

    assert resolution.source is config.sources[0]
    assert resolution.target_model == expected_model
    assert resolution.mapping_applied is False


def test_native_aliases_do_not_blind_passthrough_or_use_manual_or_foreign_inventory(
    tmp_path,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    relay = config.sources[0]
    relay.base_url = "https://glm-relay.example.test"
    relay.models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        ),
        ModelHubModelConfig(
            id="claude-fable-5-20260701",
            provenance="manual",
        ),
    ]
    foreign = config.sources[1]
    foreign.vendor = "custom"
    foreign.models = [
        ModelHubModelConfig(
            id="claude-sonnet-5-20260720",
            provenance="discovered",
        )
    ]

    resolved = resolve_model_hub_turn(config, "claude", "claude-opus-4-5")
    fictional = resolve_model_hub_turn(config, "claude", "claude-fable-5")
    foreign_only = resolve_model_hub_turn(config, "claude", "claude-sonnet-5")

    assert resolved.target_model == "claude-opus-4-5-20251101"
    assert [source.id for source in resolved.matching_sources] == [relay.id]
    assert fictional.matching_sources == ()
    assert foreign_only.matching_sources == ()


def test_explicit_mapping_overrides_native_alias_resolution(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        ),
        ModelHubModelConfig(id="glm-5.2", provenance="discovered"),
    ]
    config.sources[1].models = []
    config.agents["claude"].mappings = [
        ModelHubMappingConfig(
            builtin_id="claude-opus-4-5",
            target_model_id="glm-5.2",
            enabled=True,
        )
    ]

    resolution = resolve_model_hub_turn(config, "claude", "claude-opus-4-5")

    assert resolution.target_model == "glm-5.2"
    assert resolution.mapping_applied is True


def test_wall_drawers_chain_probe_and_resolver_share_native_alias_supply_truth(
    tmp_path,
):
    effective_model = "claude-opus-4-5-20251101"
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(id=effective_model, provenance="discovered")
    ]
    config.sources[1].models = []
    service.store.requested_models["claude"] = "claude-opus-4-5"

    agent = service.get_agent_sources("claude")
    chain = service.agent_chain("claude", "claude-opus-4-5")
    resolution = resolve_model_hub_turn(
        config,
        "claude",
        "claude-opus-4-5",
        now=service.now(),
    )
    probe = asyncio.run(service.probe_agent("claude", "claude-opus-4-5"))

    # The row wall consumes model_supply. Both drawers consume the chain
    # endpoint. Probe and live resolution must carry that same resolved model.
    wall = next(
        row
        for row in agent["model_supply"]
        if row["model_id"] == "claude-opus-4-5"
    )
    assert wall["chain_length"] == len(chain["chain"]) == 1
    assert (
        chain["chain"][0]["resolved_model_id"]
        == resolution.target_model
        == probe["model_id"]
        == effective_model
    )
    assert chain["chain"][0]["via_mapping"] is False
    assert adapter.invocations == [
        ("src_primary01", effective_model, "claude")
    ]


def test_probe_flips_from_no_candidate_to_reachable_after_discovered_alias_arrives(
    tmp_path,
):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = []
    config.sources[1].models = []

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.probe_agent("claude", "claude-opus-4-5"))
    assert exc_info.value.code == "probe_no_candidate"

    config.sources[0].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        )
    ]
    probe = asyncio.run(service.probe_agent("claude", "claude-opus-4-5"))

    assert probe["reachable"] is True
    assert probe["model_id"] == "claude-opus-4-5-20251101"


@pytest.mark.parametrize(
    ("outcome", "expected_kind"),
    [
        (
            _outcome(RawOutcomeKind.HTTP_ERROR, status=429),
            "cooldown",
        ),
        (
            _outcome(RawOutcomeKind.HTTP_ERROR, status=403),
            "needs_action",
        ),
    ],
)
def test_probe_alias_failure_events_use_requested_menu_id(
    tmp_path,
    outcome,
    expected_kind,
):
    adapter = FakeAdapter([outcome])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        )
    ]
    config.sources[1].models = []

    probe = asyncio.run(service.probe_agent("claude", "claude-opus-4-5"))

    assert probe["reachable"] is False
    assert probe["model_id"] == "claude-opus-4-5-20251101"
    event = next(
        item
        for item in service.events.list(limit=20)
        if item["kind"] == expected_kind
    )
    assert event["model_id"] == "claude-opus-4-5"


def test_failover_uses_each_sources_effective_native_alias(tmp_path):
    adapter = FakeAdapter(
        [
            _outcome(RawOutcomeKind.HTTP_ERROR, status=429),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        )
    ]
    config.sources[1].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20250929",
            provenance="discovered",
        )
    ]

    result = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-5",
            request={},
        )
    )

    assert result.source_id == "src_backup001"
    assert result.model_id == "claude-opus-4-5-20250929"
    assert adapter.invocations == [
        ("src_primary01", "claude-opus-4-5-20251101", "claude"),
        ("src_backup001", "claude-opus-4-5-20250929", "claude"),
    ]
    failover_events = [
        event
        for event in service.events.list(limit=20)
        if event["kind"] in {"cooldown", "switch"}
    ]
    assert {event["model_id"] for event in failover_events} == {
        "claude-opus-4-5"
    }


def test_runtime_alias_blocker_event_uses_requested_menu_id(tmp_path):
    adapter = FakeAdapter(
        [
            _outcome(RawOutcomeKind.HTTP_ERROR, status=403),
            _outcome(RawOutcomeKind.SUCCESS, status=200),
        ]
    )
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20251101",
            provenance="discovered",
        )
    ]
    config.sources[1].models = [
        ModelHubModelConfig(
            id="claude-opus-4-5-20250929",
            provenance="discovered",
        )
    ]

    result = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-5",
            request={},
        )
    )

    assert result.source_id == "src_backup001"
    blocker = next(
        event
        for event in service.events.list(limit=20)
        if event["kind"] == "needs_action"
    )
    assert blocker["model_id"] == "claude-opus-4-5"


def test_opencode_provider_prefix_selects_matching_source(tmp_path):
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

    chain = service.agent_chain("opencode", "anthropic/claude-opus-4-6")
    resolved = asyncio.run(
        service.resolve(
            backend="opencode",
            model_id="anthropic/claude-opus-4-6",
            request={},
        )
    )

    assert chain["chain"][0]["source_id"] == "src_backup001"
    assert resolved.source_id == "src_backup001"
    assert adapter.invocations == [("src_backup001", "claude-opus-4-6", "opencode")]
    assert service.list_events(limit=10) == []
    assert config.sources[0].state.status == "cooldown"


def test_opencode_agent_supply_distinguishes_explicit_pin_from_resolver_pick(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    service.store.load().agents["opencode"].menu.checked = [
        "anthropic/claude-opus-4-6"
    ]

    resolver_picked = service.get_agent_sources("opencode")

    assert resolver_picked["selected_model_id"] == "anthropic/claude-opus-4-6"
    assert resolver_picked["selected_model_explicit"] is False

    service.requested_model_override = lambda backend: (
        "anthropic/claude-opus-4-6" if backend == "opencode" else None
    )
    explicit_pin = service.get_agent_sources("opencode")

    assert explicit_pin["selected_model_id"] == "anthropic/claude-opus-4-6"
    assert explicit_pin["selected_model_explicit"] is True


@pytest.mark.parametrize(
    ("requested_model", "expected_model", "expected_explicit"),
    [
        ("claude-opus-4-6", "claude-opus-4-6", True),
        ("", None, False),
    ],
)
def test_fixed_menu_agent_supply_marks_only_configured_selection_explicit(
    tmp_path,
    requested_model,
    expected_model,
    expected_explicit,
):
    service = _service(tmp_path, FakeAdapter([]))
    service.store.requested_models["claude"] = requested_model

    agent = service.get_agent_sources("claude")

    assert agent["selected_model_id"] == expected_model
    assert agent["selected_model_explicit"] is expected_explicit


def test_opencode_unknown_vendor_uses_custom_provider_identifier(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].vendor = "relaycorp"
    config.agents["opencode"].menu.checked = ["custom/claude-opus-4-6"]

    menu = asyncio.run(service.set_opencode_menu(config.agents["opencode"].menu.to_payload()))
    chain = service.agent_chain("opencode", "custom/claude-opus-4-6")
    resolved = asyncio.run(
        service.resolve(
            backend="opencode",
            model_id="custom/claude-opus-4-6",
            request={},
        )
    )

    assert menu["menu"]["checked"] == ["custom/claude-opus-4-6"]
    assert chain["chain"][0]["source_id"] == "src_primary01"
    assert resolved.source_id == "src_primary01"


def test_agent_supply_eligibility_is_complete_chain_and_process_inventory(
    tmp_path,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    native = config.sources[0]
    native.kind = "subscription"
    native.vendor = "anthropic"
    native.supply_channel = "native_cli"
    native.credential_ref = None
    outside_chain = _source("src_outside01", "Outside current chain")
    outside_chain.models = [
        ModelHubModelConfig(id="other-model", provenance="discovered")
    ]
    config.sources.append(outside_chain)
    service.native_source_ready = (
        lambda _backend, source: source.id != native.id
    )

    agent = service.get_agent_sources("claude")
    eligibility = {
        row["source_id"]: row
        for row in agent["sources"]["eligibility"]
    }

    assert set(eligibility) == {source.id for source in config.sources}
    assert eligibility[native.id] == {
        "source_id": native.id,
        "eligible": True,
        "reason_key": None,
        "in_current_model_chain": True,
        "process_availability_reason": "native_cli_unavailable",
    }
    assert eligibility["src_backup001"]["in_current_model_chain"] is True
    assert eligibility[outside_chain.id]["in_current_model_chain"] is False
    assert (
        eligibility[outside_chain.id]["process_availability_reason"]
        is None
    )

    service.store.requested_models["claude"] = ""
    unselected = service.get_agent_sources("claude")
    assert unselected["selected_model_id"] is None
    assert all(
        row["in_current_model_chain"] is None
        for row in unselected["sources"]["eligibility"]
    )
    assert next(
        row
        for row in unselected["sources"]["eligibility"]
        if row["source_id"] == native.id
    )["process_availability_reason"] == "native_cli_unavailable"


def test_opencode_resolution_rejects_models_outside_checked_menu(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    service.store.load().agents["opencode"].menu.checked = []

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.resolve(
                backend="opencode",
                model_id="anthropic/claude-opus-4-6",
                request={},
            )
        )

    assert exc_info.value.code == "mapping_target_unavailable"
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

    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert resolved.source_id == "src_primary01"
    assert [binding.source_id for binding in adapter.synced[0]] == [
        "src_primary01",
        "src_backup001",
    ]


@pytest.mark.parametrize(
    ("first_state", "second_state", "expected_status", "expected_source"),
    [
        (
            ModelHubSourceStateConfig(status="standby"),
            ModelHubSourceStateConfig(status="standby"),
            "ok",
            "src_primary01",
        ),
        (
            ModelHubSourceStateConfig(
                status="cooldown",
                retry_at="2026-07-23T03:05:00Z",
            ),
            ModelHubSourceStateConfig(status="standby"),
            "degraded",
            "src_backup001",
        ),
        (
            ModelHubSourceStateConfig(
                status="cooldown",
                retry_at="2026-07-23T03:05:00Z",
            ),
            ModelHubSourceStateConfig(
                status="cooldown",
                retry_at="2026-07-23T03:06:00Z",
            ),
            "waiting",
            None,
        ),
        (
            ModelHubSourceStateConfig(
                status="cooldown",
                retry_at="2026-07-23T03:05:00Z",
            ),
            ModelHubSourceStateConfig(
                status="needs_action",
                detail_key="models.source.needs_action.credential_revoked",
            ),
            "interrupted",
            None,
        ),
    ],
)
def test_supply_status_is_derived_from_blocker_causes(
    tmp_path,
    first_state,
    second_state,
    expected_status,
    expected_source,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.sources[0].state = first_state
    config.sources[1].state = second_state

    resolution = resolve_model_hub_turn(
        config,
        "claude",
        "claude-opus-4-6",
        now=datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    assert resolution.supply_status == expected_status
    assert (resolution.source.id if resolution.source is not None else None) == expected_source


def test_empty_enabled_subset_is_structurally_interrupted(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    config.agents["claude"].sources.order = []

    resolution = resolve_model_hub_turn(
        config,
        "claude",
        "claude-opus-4-6",
        now=datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    assert resolution.matching_sources == ()
    assert resolution.supply_status == "interrupted"
    assert resolution.source is None


def test_native_source_is_dispatched_before_hub_and_cooldown_falls_through(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    native = service.store.load().sources[0]
    native.kind = "subscription"
    native.supply_channel = "native_cli"

    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert resolved.source_id == "src_primary01"
    assert resolved.supply_channel == "native_cli"
    assert resolved.handle is None
    assert adapter.invocations == []

    native.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at="2026-07-23T03:05:00Z",
    )
    fallback = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

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

    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert resolved.source_id == "src_primary01"
    assert resolved.supply_channel == "native_cli"
    assert adapter.revoked == ["cred_deleted"]
    assert service.revocations.list() == []
    assert adapter.invocations == []


def test_pending_revoke_clears_when_credential_is_already_absent(tmp_path):
    class AlreadyRevokedAdapter(FakeAdapter):
        async def revoke_credential(self, credential_ref):
            self.revoked.append(credential_ref)
            raise EngineStateError("credential is unavailable")

    adapter = AlreadyRevokedAdapter(
        [_outcome(RawOutcomeKind.SUCCESS, status=200)]
    )
    service = _service(tmp_path, adapter)
    service.revocations.add("src_deleted", "cred_deleted")

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert resolved.source_id == "src_primary01"
    assert adapter.revoked == ["cred_deleted"]
    assert service.revocations.list() == []


def test_pending_revoke_failure_does_not_block_hub_routing(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    adapter.fail_revoke = True
    service = _service(tmp_path, adapter)
    service.revocations.add("src_deleted", "cred_deleted")

    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert resolved.source_id == "src_primary01"
    assert adapter.revoked == ["cred_deleted"]
    assert service.revocations.list()[0].credential_ref == "cred_deleted"
    assert adapter.invocations == [
        ("src_primary01", "claude-opus-4-6", "claude")
    ]


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
    assert service.store.load().sources[-1].id == created["source"]["id"]


def test_async_mutation_offloads_config_store_save(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    original_save = service.store.save
    save_threads: list[int] = []

    def blocking_save(config):
        save_threads.append(threading.get_ident())
        original_save(config)

    service.store.save = blocking_save

    async def exercise() -> int:
        event_loop_thread = threading.get_ident()
        await service.set_agent_mode("claude", "direct")
        return event_loop_thread

    event_loop_thread = asyncio.run(exercise())

    assert save_threads
    assert all(thread_id != event_loop_thread for thread_id in save_threads)


def test_v2_store_rejects_stale_patch_without_overwriting_concurrent_edit(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    initial = _service(tmp_path, FakeAdapter([])).store.load()
    persisted = default_config()
    persisted.model_hub = ModelHubConfig.from_payload(initial.to_payload())
    persisted.save()

    store = V2ModelHubConfigStore()
    original_save = store.save
    interleaved = False

    def save_after_concurrent_edit(config: ModelHubConfig) -> None:
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            concurrent = V2Config.load()
            concurrent.model_hub.sources[1].display_name = "Concurrent backup"
            concurrent.save()
        original_save(config)

    store.save = save_after_concurrent_edit  # type: ignore[method-assign]
    service = ModelHubService(
        store=store,
        adapter=FakeAdapter([]),
        events=BoundedEventLog(tmp_path / "v2-events.json", max_entries=5),
    )

    with pytest.raises(ModelHubError) as raised:
        asyncio.run(
            service.patch_source(
                "src_primary01",
                {"display_name": "Requested primary"},
            )
        )

    assert raised.value.code == "config_conflict"
    assert raised.value.status == 409
    current = V2Config.load().model_hub
    assert current.sources[0].display_name == "Primary"
    assert current.sources[1].display_name == "Concurrent backup"


def test_async_mutation_settles_config_save_before_propagating_cancellation(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    original_save = service.store.save
    save_entered = threading.Event()
    release_save = threading.Event()
    save_finished = threading.Event()

    def blocking_save(config):
        save_entered.set()
        assert release_save.wait(timeout=5)
        original_save(config)
        save_finished.set()

    service.store.save = blocking_save

    async def exercise() -> None:
        task = asyncio.create_task(service.set_agent_mode("claude", "direct"))
        assert await asyncio.to_thread(save_entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_save.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert save_finished.is_set()
    assert service.store.load().agents["claude"].mode == "direct"


def test_synced_mutation_settles_engine_projection_before_cancellation(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    original_save = service.store.save
    save_entered = threading.Event()
    release_save = threading.Event()

    def blocking_save(config):
        save_entered.set()
        assert release_save.wait(timeout=5)
        original_save(config)

    service.store.save = blocking_save

    async def exercise() -> None:
        task = asyncio.create_task(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Cancellation-safe source",
                    "key": "sk-test-settlement-only",
                }
            )
        )
        assert await asyncio.to_thread(save_entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_save.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    persisted = service.store.load()
    assert persisted.sources[-1].display_name == "Cancellation-safe source"
    assert adapter.synced
    assert adapter.synced[-1][-1].source_id == persisted.sources[-1].id


def test_synced_mutation_rolls_back_when_engine_awaitable_cancels_itself(tmp_path):
    class SelfCancellingAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            self.synced.append(tuple(bindings))
            if len(self.synced) == 1:
                raise asyncio.CancelledError("engine sync stopped")

    adapter = SelfCancellingAdapter([])
    service = _service(tmp_path, adapter)

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError) as raised:
            await service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        assert raised.value.args == ("engine sync stopped",)

    asyncio.run(exercise())

    assert service.store.load().sources[0].base_url is None
    assert len(adapter.synced) == 2


def test_synced_mutation_caller_cancel_precedes_rollback_save_failure(tmp_path):
    class FailFirstSyncAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            self.synced.append(tuple(bindings))
            if len(self.synced) == 1:
                raise RuntimeError("updated sync failed")

    adapter = FailFirstSyncAdapter([])
    service = _service(tmp_path, adapter)
    original_save = service.store.save
    save_entered = threading.Event()
    release_save = threading.Event()
    save_count = 0

    def fail_rollback_save(config):
        nonlocal save_count
        save_count += 1
        if save_count == 1:
            save_entered.set()
            assert release_save.wait(timeout=5)
            original_save(config)
            return
        raise OSError("rollback save failed")

    service.store.save = fail_rollback_save

    async def exercise() -> None:
        task = asyncio.create_task(
            service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        )
        assert await asyncio.to_thread(save_entered.wait, 5)
        task.cancel("mutation cancelled")
        await asyncio.sleep(0)
        release_save.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value.args == ("mutation cancelled",)
        assert isinstance(raised.value.__cause__, OSError)
        assert str(raised.value.__cause__) == "rollback save failed"

    asyncio.run(exercise())

    assert service._engine_synced is False
    assert len(adapter.synced) == 2


def test_synced_mutation_rollback_save_failure_chains_sync_failure(tmp_path):
    class FailFirstSyncAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            self.synced.append(tuple(bindings))
            if len(self.synced) == 1:
                raise RuntimeError("updated sync failed")

    adapter = FailFirstSyncAdapter([])
    service = _service(tmp_path, adapter)
    original_save = service.store.save
    save_count = 0

    def fail_rollback_save(config):
        nonlocal save_count
        save_count += 1
        if save_count == 1:
            original_save(config)
            return
        raise OSError("rollback save failed")

    service.store.save = fail_rollback_save

    with pytest.raises(OSError, match="rollback save failed") as raised:
        asyncio.run(
            service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        )

    assert isinstance(raised.value.__cause__, ModelHubError)
    assert service._engine_synced is False
    assert len(adapter.synced) == 2


def test_synced_mutation_rollback_self_cancel_preserves_sync_failure(tmp_path):
    class FailingThenCancellingAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            self.synced.append(tuple(bindings))
            if len(self.synced) == 1:
                raise RuntimeError("updated sync failed")
            raise asyncio.CancelledError("rollback sync stopped")

    adapter = FailingThenCancellingAdapter([])
    service = _service(tmp_path, adapter)

    with pytest.raises(ModelHubError) as raised:
        asyncio.run(
            service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        )

    assert raised.value.code == "engine_down"
    assert raised.value.__cause__ is None
    assert service.store.load().sources[0].base_url is None
    assert service._engine_synced is False


def test_synced_mutation_caller_cancel_chains_primary_not_rollback_sync_failure(
    tmp_path,
    caplog,
):
    rollback_entered = asyncio.Event()
    release_rollback = asyncio.Event()

    class FailingRollbackAdapter(FakeAdapter):
        async def sync_sources(self, bindings):
            self.synced.append(tuple(bindings))
            if len(self.synced) == 1:
                raise ModelHubError("updated_primary", status=503)
            rollback_entered.set()
            await release_rollback.wait()
            raise ModelHubError("rollback_secondary", status=503)

    adapter = FailingRollbackAdapter([])
    service = _service(tmp_path, adapter)

    async def exercise() -> None:
        task = asyncio.create_task(
            service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        )
        await rollback_entered.wait()
        task.cancel("mutation cancelled")
        await asyncio.sleep(0)
        release_rollback.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value.args == ("mutation cancelled",)
        assert isinstance(raised.value.__cause__, ModelHubError)
        assert raised.value.__cause__.code == "updated_primary"

    asyncio.run(exercise())

    assert "modelHub.errors.rollback_secondary" in caplog.text
    assert service.store.load().sources[0].base_url is None
    assert service._engine_synced is False


def test_synced_mutation_double_sync_failure_preserves_original(tmp_path):
    adapter = FakeAdapter([])
    adapter.fail_sync = True
    service = _service(tmp_path, adapter)

    with pytest.raises(ModelHubError) as raised:
        asyncio.run(
            service.patch_source(
                "src_primary01",
                {"base_url": "https://new.example.test"},
            )
        )

    assert raised.value.code == "engine_down"
    assert raised.value.__cause__ is None
    assert service.store.load().sources[0].base_url is None
    assert service._engine_synced is False


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
    resolved = asyncio.run(service.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

    assert resolved.source_id == "src_primary01"
    assert adapter.revoked == ["cred_test", "cred_test"]
    assert service.revocations.list() == []


def test_failed_create_rollback_attempts_revoke_when_journal_add_fails(tmp_path):
    adapter = FakeAdapter([])
    adapter.fail_sync = True
    service = _service(tmp_path, adapter)

    def fail_journal_add(source_id, credential_ref):
        raise OSError("journal write failed")

    service.revocations.add = fail_journal_add

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
    assert adapter.revoked == ["cred_test"]
    assert service.revocations.list() == []


def test_empty_discovery_rejects_source_creation_and_revokes_credential(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    before = _serialized_config(service)

    async def empty_discovery(vendor, protocol, base_url, credential_ref):
        return ()

    adapter.discover_models = empty_discovery

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.create_source(
                {
                    "kind": "api_key",
                    "vendor": "anthropic",
                    "display_name": "Empty source",
                    "key": "sk-test-empty-discovery",
                }
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert _serialized_config(service) == before
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


@pytest.mark.parametrize(("force", "mode"), [(False, "direct"), (True, "hub")])
def test_source_delete_cascades_agent_model_references(tmp_path, force, mode):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = _configure_referenced_manual_model(service)
    config.agents["claude"].mode = mode
    config.agents["opencode"].mode = mode

    asyncio.run(service.delete_source("src_primary01", force=force))

    persisted = service.store.load()
    assert [source.id for source in persisted.sources] == ["src_backup001"]
    assert all(agent.sources.order == ["src_backup001"] for agent in persisted.agents.values())
    _assert_no_references_to(service, "retired-model")
    if force:
        claude_chain = service.agent_chain("claude", "claude-opus-4-6")
        opencode_chain = service.agent_chain("opencode", "anthropic/claude-opus-4-6")
        assert claude_chain["chain"][0]["source_id"] == "src_backup001"
        assert opencode_chain["chain"][0]["source_id"] == "src_backup001"
        resolved = asyncio.run(
            service.resolve(
                backend="claude",
                model_id="claude-opus-4-6",
                request={},
            )
        )
        assert resolved.source_id == claude_chain["chain"][0]["source_id"]


def test_deleting_last_hub_source_syncs_empty_binding_set(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources = [config.sources[0]]
    for agent in config.agents.values():
        agent.sources.order = [config.sources[0].id]

    asyncio.run(service.delete_source("src_primary01", force=True))

    assert adapter.synced == [()]
    assert adapter.revoked == ["cred_src_primary01"]
    assert service.store.load().sources == []


def test_source_reference_survives_failed_credential_revoke(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    config = _configure_referenced_manual_model(service)
    config.sources[0].credential_ref = "cred_primary"
    config.sources[1].credential_ref = "cred_backup"
    before = _serialized_config(service)
    adapter.fail_revoke = True

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01", force=True))

    assert exc_info.value.code == "engine_down"
    assert _serialized_config(service) == before
    assert [source.id for source in service.store.load().sources] == ["src_primary01", "src_backup001"]
    assert service.store.load().agents["claude"].mappings[0].target_model_id == "retired-model"
    assert service.store.load().agents["opencode"].menu.checked[0] == "anthropic/retired-model"
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
    service.store.load().agents["codex"].sources.order = ["src_primary01"]
    service.store.load().agents["opencode"].sources.order = ["src_primary01"]

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
    resolved = asyncio.run(restarted.resolve(backend="claude", model_id="claude-opus-4-6", request={}))

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


def test_custom_model_delete_cascades_agent_model_references(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = _configure_referenced_manual_model(service)
    config.agents["claude"].mode = "direct"
    config.agents["opencode"].mode = "direct"

    asyncio.run(service.delete_custom_model("src_primary01", "retired-model"))

    persisted = service.store.load()
    assert all(model.id != "retired-model" for model in persisted.sources[0].models)
    _assert_no_references_to(service, "retired-model")
    assert persisted.agents["claude"].mappings == []
    assert persisted.agents["opencode"].menu.checked == ["anthropic/claude-opus-4-6"]


@pytest.mark.parametrize("operation", ["source", "custom_model"])
def test_delete_commit_failure_restores_agent_references_byte_for_byte(tmp_path, operation):
    adapter = FakeAdapter([])
    adapter.fail_sync = True
    service = _service(tmp_path, adapter)
    config = _configure_referenced_manual_model(service)
    if operation == "custom_model":
        config.agents["claude"].mode = "direct"
        config.agents["opencode"].mode = "direct"
    before = _serialized_config(service)

    with pytest.raises(ModelHubError) as exc_info:
        if operation == "source":
            asyncio.run(service.delete_source("src_primary01", force=True))
        else:
            asyncio.run(service.delete_custom_model("src_primary01", "retired-model"))

    assert exc_info.value.code == "engine_down"
    assert _serialized_config(service) == before
    restored = service.store.load()
    assert restored.agents["claude"].mappings[0].target_model_id == "retired-model"
    assert restored.agents["opencode"].menu.checked[0] == "anthropic/retired-model"


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
        service.set_opencode_menu({"view": "featured", "checked": ["openrouter/anthropic/claude-sonnet-4"]})
    )

    assert updated["models"][-1]["id"] == "anthropic/claude-sonnet-4"
    assert menu["menu"]["checked"] == ["openrouter/anthropic/claude-sonnet-4"]


def test_resolution_event_copy_comes_from_backend_i18n(tmp_path):
    event = build_resolution_event(
        agent="system",
        kind="cooldown",
        model_id="test-model",
        reason="network",
        from_source="src_primary01",
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
    config.agents["claude"].sources.order = ["src_primary01"]
    config.agents["codex"].sources.order = ["src_backup001"]
    config.agents["opencode"].sources.order = []

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

    config.agents["claude"].mappings = [ModelHubMappingConfig("claude-native", "claude-opus-4-6", True)]
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01"))
    assert exc_info.value.code == "source_last_supplier"

    config.agents["claude"].mode = "direct"
    asyncio.run(service.delete_source("src_primary01"))
    assert [source.id for source in service.store.load().sources] == ["src_backup001"]


def test_mapping_auto_enrolls_eligible_non_enrolled_source(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    added = _source("src_mapping01", "Mapping target")
    added.models = [
        ModelHubModelConfig(id="mapped-model", provenance="discovered")
    ]
    excluded = _source("src_mapping02", "Excluded mapping fallback")
    excluded.models = [
        ModelHubModelConfig(id="mapped-model", provenance="discovered")
    ]
    config.sources.extend([added, excluded])
    original_order = list(config.agents["claude"].sources.order)

    agent = asyncio.run(
        service.set_mappings(
            "claude",
            [
                {
                    "builtin_id": "claude-opus-4-6",
                    "target_model_id": "mapped-model",
                    "enabled": True,
                }
            ],
        )
    )

    assert agent["sources"]["policy"] == "custom"
    assert agent["sources"]["order"] == [*original_order, added.id]
    assert config.agents["claude"].mappings[0].target_model_id == "mapped-model"


def test_opencode_menu_auto_enrolls_eligible_non_enrolled_source(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    added = _source("src_openmenu01", "Open menu target")
    added.vendor = "openrouter"
    added.models = [
        ModelHubModelConfig(
            id="anthropic/claude-sonnet-4",
            provenance="manual",
        )
    ]
    excluded = _source("src_openmenu02", "Excluded menu fallback")
    excluded.vendor = "openrouter"
    excluded.models = [
        ModelHubModelConfig(
            id="anthropic/claude-sonnet-4",
            provenance="manual",
        )
    ]
    config.sources.extend([added, excluded])
    original_order = list(config.agents["opencode"].sources.order)

    agent = asyncio.run(
        service.set_opencode_menu(
            {
                "view": "featured",
                "checked": [
                    "openrouter/anthropic/claude-sonnet-4",
                ],
            }
        )
    )

    assert agent["sources"]["policy"] == "custom"
    assert agent["sources"]["order"] == [*original_order, added.id]
    assert agent["menu"]["checked"] == [
        "openrouter/anthropic/claude-sonnet-4"
    ]


@pytest.mark.parametrize("mutation", ["mapping", "menu"])
def test_menu_acceptance_preserves_excluded_supplier_when_target_is_enrolled(
    tmp_path,
    mutation,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    excluded = _source("src_excluded01", "Explicitly excluded fallback")
    config.sources.append(excluded)
    backend = "claude" if mutation == "mapping" else "opencode"
    original_order = list(config.agents[backend].sources.order)

    if mutation == "mapping":
        agent = asyncio.run(
            service.set_mappings(
                "claude",
                [
                    {
                        "builtin_id": "claude-opus-4-6",
                        "target_model_id": "claude-opus-4-6",
                        "enabled": True,
                    }
                ],
            )
        )
    else:
        agent = asyncio.run(
            service.set_opencode_menu(
                {
                    "view": "featured",
                    "checked": ["anthropic/claude-opus-4-6"],
                }
            )
        )

    assert agent["sources"]["order"] == original_order
    assert excluded.id not in agent["sources"]["order"]


@pytest.mark.parametrize("mutation", ["mapping", "menu"])
def test_menu_acceptance_incrementally_deduplicates_selected_supplier(
    tmp_path,
    mutation,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    selected = _source("src_multimap01", "Selected supplier")
    excluded = _source("src_multimap02", "Excluded fallback")
    for source in (selected, excluded):
        source.models = [
            ModelHubModelConfig(id="target-one", provenance="manual"),
            ModelHubModelConfig(id="target-two", provenance="manual"),
        ]
    config.sources.extend([selected, excluded])
    backend = "claude" if mutation == "mapping" else "opencode"
    original_order = list(config.agents[backend].sources.order)

    if mutation == "mapping":
        agent = asyncio.run(
            service.set_mappings(
                "claude",
                [
                    {
                        "builtin_id": "claude-opus-4-6",
                        "target_model_id": "target-one",
                        "enabled": True,
                    },
                    {
                        "builtin_id": "claude-sonnet-4-6",
                        "target_model_id": "target-two",
                        "enabled": True,
                    },
                ],
            )
        )
    else:
        agent = asyncio.run(
            service.set_opencode_menu(
                {
                    "view": "featured",
                    "checked": [
                        "anthropic/target-one",
                        "anthropic/target-two",
                    ],
                }
            )
        )

    assert agent["sources"]["order"] == [*original_order, selected.id]
    assert excluded.id not in agent["sources"]["order"]


def test_follow_order_exhaustively_enrolls_eligible_sources_and_stays_follow(
    tmp_path,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    anthropic_native = _source("src_claude01", "Claude native")
    anthropic_native.kind = "subscription"
    anthropic_native.supply_channel = "native_cli"
    anthropic_native.vendor = "anthropic"
    anthropic_native.credential_ref = None
    openai_native = _source("src_codex001", "Codex native")
    openai_native.kind = "subscription"
    openai_native.supply_channel = "native_cli"
    openai_native.vendor = "openai"
    openai_native.credential_ref = None
    config.sources.extend([anthropic_native, openai_native])
    for agent in config.agents.values():
        agent.sources.policy = "follow"
        agent.sources.order = []

    for backend, agent in config.agents.items():
        eligible = {
            source.id
            for source in config.sources
            if config.source_eligible_for_backend(source, backend)
        }
        assert set(config.effective_source_order(backend)) == eligible
        assert agent.sources.policy == "follow"

    claude = asyncio.run(
        service.set_mappings(
            "claude",
            [
                {
                    "builtin_id": "claude-opus-4-6",
                    "target_model_id": "claude-opus-4-6",
                    "enabled": True,
                }
            ],
        )
    )
    opencode = asyncio.run(
        service.set_opencode_menu(
            {
                "view": "featured",
                "checked": ["anthropic/claude-opus-4-6"],
            }
        )
    )

    assert claude["sources"]["policy"] == "follow"
    assert opencode["sources"]["policy"] == "follow"


@pytest.mark.parametrize("mutation", ["mapping", "menu"])
def test_menu_mutations_reject_ineligible_sources_without_enrolling(
    tmp_path,
    mutation,
):
    service = _service(tmp_path, FakeAdapter([]))
    config = service.store.load()
    candidate = _source("src_ineligible01", "Ineligible target")
    candidate.kind = "subscription"
    candidate.supply_channel = "native_cli"
    candidate.vendor = "openai"
    candidate.credential_ref = None
    candidate.models = [
        ModelHubModelConfig(id="ineligible-model", provenance="discovered")
    ]
    config.sources.append(candidate)
    before = _serialized_config(service)

    with pytest.raises(ModelHubError) as exc_info:
        if mutation == "mapping":
            asyncio.run(
                service.set_mappings(
                    "claude",
                    [
                        {
                            "builtin_id": "claude-opus-4-6",
                            "target_model_id": "ineligible-model",
                            "enabled": True,
                        }
                    ],
                )
            )
        else:
            asyncio.run(
                service.set_opencode_menu(
                    {
                        "view": "featured",
                        "checked": ["openai/ineligible-model"],
                    }
                )
            )

    assert exc_info.value.code == "mapping_target_unavailable"
    assert _serialized_config(service) == before


class NarrowingCredentialAdapter(FakeAdapter):
    def __init__(self, outcomes=()):
        super().__init__(outcomes)
        self.provision_count = 0
        self.fail_discovery = False
        self.fail_old_revoke_once = False

    async def provision_credential(self, vendor, protocol, secret, base_url):
        self.provision_count += 1
        credential_ref = f"cred_replacement_{self.provision_count}"
        self.provisioned.append((vendor, protocol, base_url))
        return credential_ref

    async def discover_models(self, vendor, protocol, base_url, credential_ref):
        if self.fail_discovery:
            raise ModelDiscoveryError("safe classified failure")
        return ("replacement-only-model",)

    async def revoke_credential(self, credential_ref):
        self.revoked.append(credential_ref)
        if self.fail_old_revoke_once and credential_ref == "cred_src_primary01":
            self.fail_old_revoke_once = False
            raise RuntimeError("old handle still busy")


def _repair_guard_service(tmp_path, *, enabled: bool, adapter=None):
    adapter = adapter or NarrowingCredentialAdapter()
    service = _service(tmp_path, adapter)
    config = service.store.load()
    config.sources[0].models = [ModelHubModelConfig(id="glm-5.2", provenance="discovered")]
    config.sources[1].models = [ModelHubModelConfig(id="claude-haiku-4-5", provenance="discovered")]
    config.agents["claude"].mappings = [
        ModelHubMappingConfig(
            builtin_id="claude-opus-4-6",
            target_model_id="glm-5.2",
            enabled=enabled,
        )
    ]
    service.store.requested_models["claude"] = "claude-haiku-4-5"
    return service, adapter


@pytest.mark.parametrize(
    ("route", "enabled", "blocked"),
    [
        ("delete", False, False),
        ("delete", True, True),
        ("credential", False, False),
        ("credential", True, True),
    ],
)
def test_supply_guard_uses_only_enabled_menu_mappings_from_fresh_fixtures(
    tmp_path,
    route,
    enabled,
    blocked,
):
    service, _adapter = _repair_guard_service(
        tmp_path / f"{route}-{enabled}",
        enabled=enabled,
    )

    if blocked:
        with pytest.raises(ModelHubError) as exc_info:
            if route == "delete":
                asyncio.run(service.delete_source("src_primary01"))
            else:
                asyncio.run(
                    service.replace_credential(
                        "src_primary01",
                        {"key": "sk-narrower-replacement"},
                    )
                )
        assert exc_info.value.code == "source_last_supplier"
        assert exc_info.value.data["would_interrupt"] == [
            {
                "backend": "claude",
                "model_id": "claude-opus-4-6",
                "agents": [],
            }
        ]
    elif route == "delete":
        asyncio.run(service.delete_source("src_primary01"))
        assert [source.id for source in service.store.load().sources] == ["src_backup001"]
    else:
        result = asyncio.run(
            service.replace_credential(
                "src_primary01",
                {"key": "sk-narrower-replacement"},
            )
        )
        assert result["interrupted_pairs"] == []
        assert result["source"]["credential_ref"] == "cred_replacement_1"


def test_supply_guard_reports_menu_identifier_and_effective_named_agents(tmp_path):
    service, _adapter = _repair_guard_service(tmp_path, enabled=True)
    service.named_agents_override = lambda backend: (
        [("pm", "claude-opus-4-6"), ("reviewer", "claude-opus-4-6")]
        if backend == "claude"
        else []
    )
    service.store.requested_models["claude"] = "claude-opus-4-6"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01"))

    assert exc_info.value.data["would_interrupt"] == [
        {
            "backend": "claude",
            "model_id": "claude-opus-4-6",
            "agents": ["pm", "reviewer"],
        }
    ]


def test_supply_guard_canonicalizes_opencode_effective_models(tmp_path):
    service, _adapter = _repair_guard_service(tmp_path, enabled=False)
    config = service.store.load()
    config.sources[0].vendor = "zhipuai"
    config.sources[0].models = [
        ModelHubModelConfig(id="glm-5.2", provenance="discovered")
    ]
    config.sources[1].vendor = "anthropic"
    config.sources[1].models = [
        ModelHubModelConfig(id="claude-haiku-4-5", provenance="discovered")
    ]
    config.agents["claude"].mode = "direct"
    config.agents["codex"].mode = "direct"
    config.agents["opencode"].menu.checked = ["zhipuai/glm-5.2"]
    service.named_agents_override = lambda backend: (
        [("builder", "glm-5.2")] if backend == "opencode" else []
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.delete_source("src_primary01"))

    assert exc_info.value.data["would_interrupt"] == [
        {
            "backend": "opencode",
            "model_id": "zhipuai/glm-5.2",
            "agents": ["builder"],
        }
    ]


def test_credential_force_commits_same_narrowing_request_and_reports_gaps(tmp_path):
    service, adapter = _repair_guard_service(tmp_path, enabled=True)
    before = _serialized_config(service)

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.replace_credential(
                "src_primary01",
                {"key": "sk-narrower-replacement"},
            )
        )

    assert exc_info.value.code == "source_last_supplier"
    assert _serialized_config(service) == before
    result = asyncio.run(
        service.replace_credential(
            "src_primary01",
            {"key": "sk-narrower-replacement", "force": True},
        )
    )
    assert result["recovered"] is False
    assert result["interrupted_pairs"] == exc_info.value.data["would_interrupt"]
    assert result["source"]["credential_ref"] == "cred_replacement_2"
    assert adapter.revoked == ["cred_replacement_1", "cred_src_primary01"]


def test_blocked_credential_repair_bypasses_refusal_and_reports_remaining_gaps(
    tmp_path,
):
    service, _adapter = _repair_guard_service(tmp_path, enabled=True)
    service.store.load().sources[0].state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.credential_revoked",
    )

    result = asyncio.run(
        service.replace_credential(
            "src_primary01",
            {"key": "sk-recovery-key"},
        )
    )

    assert result["recovered"] is True
    assert result["source"]["state"]["status"] == "standby"
    assert result["interrupted_pairs"][0]["model_id"] == "claude-opus-4-6"


@pytest.mark.parametrize("failure", ["discovery", "sync"])
def test_credential_replacement_failure_preserves_prior_source(tmp_path, failure):
    adapter = NarrowingCredentialAdapter()
    service, _adapter = _repair_guard_service(
        tmp_path,
        enabled=False,
        adapter=adapter,
    )
    before = _serialized_config(service)
    if failure == "discovery":
        adapter.fail_discovery = True
    else:
        adapter.fail_sync = True

    with pytest.raises(ModelHubError):
        asyncio.run(
            service.replace_credential(
                "src_primary01",
                {"key": "sk-failing-replacement"},
            )
        )

    assert _serialized_config(service) == before
    assert "cred_replacement_1" in adapter.revoked
    assert service.revocations.list() == []


def test_empty_discovery_rejects_credential_replacement(tmp_path):
    adapter = NarrowingCredentialAdapter()
    service, _adapter = _repair_guard_service(
        tmp_path,
        enabled=False,
        adapter=adapter,
    )
    before = _serialized_config(service)

    async def empty_discovery(vendor, protocol, base_url, credential_ref):
        return ()

    adapter.discover_models = empty_discovery

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.replace_credential(
                "src_primary01",
                {"key": "sk-empty-replacement"},
            )
        )

    assert exc_info.value.code == "discovery_failed"
    assert _serialized_config(service) == before
    assert adapter.revoked == ["cred_replacement_1"]
    assert service.revocations.list() == []


def test_credential_replacement_rolls_back_when_old_journal_cleanup_fails(
    tmp_path,
):
    adapter = NarrowingCredentialAdapter()
    service, _adapter = _repair_guard_service(
        tmp_path,
        enabled=False,
        adapter=adapter,
    )
    before = _serialized_config(service)
    original_remove = service.revocations.remove

    def fail_old_journal_cleanup(source_id, credential_ref):
        if credential_ref == "cred_src_primary01":
            raise OSError("journal cleanup failed")
        return original_remove(source_id, credential_ref)

    service.revocations.remove = fail_old_journal_cleanup
    adapter.fail_sync = True

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.replace_credential(
                "src_primary01",
                {"key": "sk-failing-replacement"},
            )
        )

    assert exc_info.value.code == "engine_down"
    assert _serialized_config(service) == before
    assert "cred_replacement_1" in adapter.revoked


def test_failed_old_credential_revoke_reconciles_after_service_restart(tmp_path):
    adapter = NarrowingCredentialAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    adapter.fail_old_revoke_once = True
    service, _adapter = _repair_guard_service(
        tmp_path,
        enabled=False,
        adapter=adapter,
    )

    result = asyncio.run(
        service.replace_credential(
            "src_primary01",
            {"key": "sk-rotated-key"},
        )
    )

    assert result["source"]["credential_ref"] == "cred_replacement_1"
    assert service.revocations.list()[0].credential_ref == "cred_src_primary01"
    restarted_adapter = NarrowingCredentialAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    restarted = ModelHubService(
        store=service.store,
        adapter=restarted_adapter,
        events=BoundedEventLog(tmp_path / "restarted-events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=lambda: datetime(2026, 7, 23, 3, 0, tzinfo=timezone.utc),
    )

    resolved = asyncio.run(
        restarted.resolve(
            backend="claude",
            model_id="replacement-only-model",
            request={},
        )
    )

    assert resolved.source_id == "src_primary01"
    assert restarted_adapter.revoked == ["cred_src_primary01"]
    assert restarted.revocations.list() == []
    assert restarted.store.load().sources[0].credential_ref == "cred_replacement_1"


def test_existing_source_refresh_clears_blocker_and_restores_runnable_supply(tmp_path):
    adapter = FakeAdapter([_outcome(RawOutcomeKind.SUCCESS, status=200)])
    service = _service(tmp_path, adapter)
    config = service.store.load()
    source = config.sources[0]
    source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.balance_exhausted",
    )
    config.sources = [source]
    for agent in config.agents.values():
        agent.sources.order = [source.id]

    updated, discovered = asyncio.run(service.refresh_source(source.id))
    resolved = asyncio.run(
        service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )

    assert discovered == 1
    assert updated["state"]["status"] == "standby"
    assert updated["last_discovered_at"] == "2026-07-23T03:00:00+00:00"
    assert service.store.load().sources[0].last_discovered_at == updated["last_discovered_at"]
    assert resolved.source_id == source.id


def test_existing_source_refresh_persists_safe_error_state_on_discovery_failure(
    tmp_path,
):
    adapter = NarrowingCredentialAdapter()
    adapter.fail_discovery = True
    service = _service(tmp_path, adapter)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.balance_exhausted",
    )
    source.last_discovered_at = "2026-07-22T03:00:00+00:00"

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.refresh_source(source.id))

    assert exc_info.value.code == "discovery_failed"
    assert service.store.load().sources[0].state.status == "error"
    assert service.store.load().sources[0].state.detail_key == (
        "models.source.error.unclassified"
    )
    assert service.store.load().sources[0].last_discovered_at == "2026-07-22T03:00:00+00:00"
    source_refresh_event = service.list_events(limit=10)[0]
    assert source_refresh_event["agent"] == "system"
    assert source_refresh_event["model_id"] is None

    turn_service = _service(
        tmp_path / "turn",
        FakeAdapter(
            [
                _outcome(RawOutcomeKind.HTTP_ERROR, status=418),
                _outcome(RawOutcomeKind.SUCCESS, status=200),
            ]
        ),
    )
    asyncio.run(
        turn_service.resolve(
            backend="claude",
            model_id="claude-opus-4-6",
            request={},
        )
    )
    turn_event = next(
        event
        for event in turn_service.list_events(limit=10)
        if event["kind"] == "needs_action"
    )
    fields = ("kind", "reason", "from_source", "severity")
    assert tuple(source_refresh_event[field] for field in fields) == tuple(
        turn_event[field] for field in fields
    )


def test_existing_source_refresh_rejects_empty_discovery_before_recovery(tmp_path):
    adapter = FakeAdapter([])
    service = _service(tmp_path, adapter)
    source = service.store.load().sources[0]
    source.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.balance_exhausted",
    )

    async def empty_discovery(vendor, protocol, base_url, credential_ref):
        return ()

    adapter.discover_models = empty_discovery

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.refresh_source(source.id))

    assert exc_info.value.code == "discovery_failed"
    persisted = service.store.load().sources[0]
    assert persisted.state.status == "error"
    assert persisted.state.detail_key == "models.source.error.unclassified"


def test_existing_source_refresh_preserves_health_on_engine_outage(tmp_path):
    adapter = NarrowingCredentialAdapter()
    service = _service(tmp_path, adapter)
    before = _serialized_config(service)

    async def engine_unavailable(vendor, protocol, base_url, credential_ref):
        raise RuntimeError("engine unavailable")

    adapter.discover_models = engine_unavailable
    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(service.refresh_source("src_primary01"))

    assert exc_info.value.code == "engine_down"
    assert _serialized_config(service) == before
    assert service.list_events(limit=10) == []


def test_mapping_write_rejects_disabled_unavailable_target(tmp_path):
    service = _service(tmp_path, FakeAdapter([]))

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            service.set_mappings(
                "claude",
                [
                    {
                        "builtin_id": "claude-opus-4-6",
                        "target_model_id": "missing-model",
                        "enabled": False,
                    }
                ],
            )
        )

    assert exc_info.value.code == "mapping_target_unavailable"
    assert service.store.load().agents["claude"].mappings == []


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
