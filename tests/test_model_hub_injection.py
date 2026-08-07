from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubConfig,
    ModelHubMappingConfig,
    ModelHubMenuConfig,
    ModelHubModelConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
)
from core.handlers.model_hub.adapter import EngineHealth, EngineStatus
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.resolver import resolve_model_hub_turn
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubError, ModelHubService
from core.handlers.session_handler import SessionHandler
from modules.agents.model_hub import (
    ModelHubLaunch,
    ModelHubRuntimeRouter,
    bind_persisted_launch,
    bind_launch,
    build_claude_hub_env,
    build_codex_hub_launch,
    claude_setting_sources_for_launch,
    launch_for_context,
    opencode_model_for_overlay,
    overlay_identifier_bytes,
    persisted_launch_identity,
    resolve_model_hub_launch,
    resolve_opencode_overlay_launch,
)
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.server import OpenCodeServerManager


class MemoryStore:
    def __init__(
        self,
        config: ModelHubConfig,
        *,
        requested_models: dict[str, str] | None = None,
    ):
        self.config = config
        self.requested_models = requested_models or {}

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config

    def requested_model(self, backend: str) -> str:
        return self.requested_models.get(backend, "")


class LaunchAdapter:
    def __init__(self, prefixes: dict[str, str], *, token: str = "local-gateway-token"):
        self.prefixes = prefixes
        self.token = token
        self.starts = 0
        self.syncs = 0
        self.revoked: list[str] = []

    async def start(self):
        self.starts += 1
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 18443, None)

    async def gateway_token(self):
        return self.token

    async def sync_sources(self, bindings):
        self.syncs += 1

    async def revoke_credential(self, credential_ref: str):
        self.revoked.append(credential_ref)

    def source_prefix(self, source_id: str) -> str:
        return self.prefixes[source_id]


def _model(model_id: str, display_name: str | None = None) -> ModelHubModelConfig:
    return ModelHubModelConfig(
        id=model_id,
        display_name=display_name,
        provenance="discovered",
    )


def _source(
    source_id: str,
    *,
    kind: str,
    vendor: str,
    protocol: str,
    channel: str,
    model_ids: tuple[str, ...],
    state: str = "standby",
    retry_at: str | None = None,
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor=vendor,
        display_name=source_id,
        protocol=protocol,
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(
            status=state,
            retry_at=retry_at,
            detail_key=("models.source.error.unclassified" if state == "error" else None),
        ),
        models=[_model(model_id) for model_id in model_ids],
        credential_ref=f"cred_{source_id}" if channel == "hub" else None,
    )


def _agents(*, mode: str = "hub") -> dict[str, ModelHubAgentSupplyConfig]:
    return {
        backend: ModelHubAgentSupplyConfig.default(backend, mode=mode) for backend in ("claude", "codex", "opencode")
    }


def _hub_config(
    *,
    sources: list[ModelHubSourceConfig],
    agents: dict[str, ModelHubAgentSupplyConfig],
    order: list[str] | None = None,
) -> ModelHubConfig:
    config = ModelHubConfig(sources=sources, agents=agents)
    requested_order = order if order is not None else [source.id for source in sources]
    by_id = {source.id: source for source in sources}
    for backend, agent in agents.items():
        agent.sources = ModelHubAgentSourcesConfig(
            policy="custom",
            order=[
                source_id
                for source_id in requested_order
                if source_id in by_id and config.source_eligible_for_backend(by_id[source_id], backend)
            ],
        )
    return config


def _service(
    tmp_path: Path,
    config: ModelHubConfig,
    adapter: LaunchAdapter,
    *,
    now,
) -> ModelHubService:
    store = MemoryStore(config)
    return ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now,
        requested_model_override=store.requested_model,
    )


def _router(
    service: ModelHubService,
    *,
    overlay_path: Path | None = None,
    native_cli_ready=None,
) -> ModelHubRuntimeRouter:
    return ModelHubRuntimeRouter(
        service=service,
        overlay_path=overlay_path,
        native_cli_ready=native_cli_ready or (lambda _backend: True),
    )


def test_mh_chan_001_native_quota_falls_back_then_recovers_next_turn(tmp_path: Path) -> None:
    """MH-CHAN-001: healthy -> exhausted -> recovering is decided per turn."""

    clock = {"now": datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)}
    native = _source(
        "src_native01",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    hub = _source(
        "src_hub0001",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    agents = _agents()
    agents["codex"].mappings = [ModelHubMappingConfig("default", "gpt-5", True)]
    config = _hub_config(
        sources=[native, hub],
        order=[native.id, hub.id],
        agents=agents,
    )
    adapter = LaunchAdapter({hub.id: "route-hub"})
    service = _service(tmp_path, config, adapter, now=lambda: clock["now"])
    router = _router(service, overlay_path=tmp_path / "overlay.json")

    healthy = asyncio.run(router.resolve("codex", "gpt-5"))
    assert (healthy.channel, healthy.source_id, adapter.starts) == ("native_cli", native.id, 0)

    context = SimpleNamespace()
    bind_launch(context, healthy)
    assert asyncio.run(router.record_native_failure(context, "usage quota exceeded")) is True
    exhausted = asyncio.run(router.resolve("codex", "gpt-5"))
    assert exhausted.channel == "hub"
    assert exhausted.runtime_model == "route-hub/gpt-5"

    clock["now"] += timedelta(seconds=301)
    recovered = asyncio.run(router.resolve("codex", "gpt-5"))
    assert (recovered.channel, recovered.source_id) == ("native_cli", native.id)
    kinds = [event["kind"] for event in reversed(service.events.list(limit=20))]
    assert kinds == ["cooldown", "switch", "recover"]


def test_native_failure_is_forwarded_to_turn_correlation(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_native01",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(sources=[native], agents=_agents()),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    correlation = Mock()
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=SimpleNamespace(correlation=correlation),
        native_cli_ready=lambda _backend: True,
    )
    context = SimpleNamespace(
        platform_specific={"turn_token": "turn_native_failure"}
    )
    bind_launch(
        context,
        ModelHubLaunch(
            backend="codex",
            channel="native_cli",
            requested_model="gpt-5",
            target_model="gpt-5",
            runtime_model="gpt-5",
            source_id=native.id,
        ),
    )

    assert (
        asyncio.run(
            router.record_native_failure(
                context,
                "usage quota exceeded",
            )
        )
        is True
    )
    correlation.fail_native_attempt.assert_called_once_with(
        "turn_native_failure",
        reason="quota_exhausted",
    )

    correlation.fail_native_attempt.reset_mock()
    unclassified_context = SimpleNamespace(
        platform_specific={"turn_token": "turn_native_unclassified"}
    )
    native_launch = launch_for_context(context)
    assert native_launch is not None
    bind_launch(unclassified_context, native_launch)
    assert (
        asyncio.run(
            router.record_native_failure(
                unclassified_context,
                "provider rejected the local request",
            )
        )
        is False
    )
    correlation.fail_native_attempt.assert_called_once_with(
        "turn_native_unclassified",
        reason="unclassified_error",
    )

    hub_context = SimpleNamespace(
        platform_specific={"turn_token": "turn_hub_terminal"}
    )
    bind_launch(
        hub_context,
        ModelHubLaunch(
            backend="codex",
            channel="hub",
            requested_model="gpt-5",
            target_model="gpt-5",
            runtime_model="gpt-5",
            source_id="src_hub0001",
        ),
    )
    assert (
        asyncio.run(
            router.record_native_failure(
                hub_context,
                "backend rejected the completion",
            )
        )
        is False
    )
    correlation.fail_hub_attempt.assert_called_once_with(
        "turn_hub_terminal"
    )


def test_mh_chan_001_hub_failure_cools_source_and_selects_backup(tmp_path: Path) -> None:
    first = _source(
        "src_hub1001",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    backup = _source(
        "src_hub1002",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    config = _hub_config(
        sources=[first, backup],
        order=[first.id, backup.id],
        agents=_agents(),
    )
    adapter = LaunchAdapter({first.id: "route-first", backup.id: "route-backup"})
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)

    initial = asyncio.run(router.resolve("codex", "gpt-5"))
    context = SimpleNamespace()
    bind_launch(context, initial)
    assert asyncio.run(router.record_native_failure(context, "Provider API returned HTTP 503")) is True
    fallback = asyncio.run(router.resolve("codex", "gpt-5"))

    assert (fallback.channel, fallback.source_id, fallback.runtime_model) == (
        "hub",
        backup.id,
        "route-backup/gpt-5",
    )
    assert first.state.status == "cooldown"
    assert [event["kind"] for event in reversed(service.events.list(limit=10))] == ["cooldown", "switch"]


def test_mh_chan_001_hub_to_native_switch_keeps_failure_reason(tmp_path: Path) -> None:
    hub = _source(
        "src_hub_reason",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    native = _source(
        "src_native_reason",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[hub, native],
            order=[hub.id, native.id],
            agents=_agents(),
        ),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)
    initial = asyncio.run(router.resolve("codex", "gpt-5"))
    context = SimpleNamespace()
    bind_launch(context, initial)

    assert asyncio.run(router.record_native_failure(context, "usage quota exceeded")) is True
    assert asyncio.run(router.resolve("codex", "gpt-5")).channel == "native_cli"

    switch = next(event for event in service.events.list(limit=10) if event["kind"] == "switch")
    assert switch["reason"] == "quota_exhausted"


def test_mh_chan_001_native_launch_replays_pending_revocations(tmp_path: Path) -> None:
    native = _source(
        "src_native11",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    config = _hub_config(
        sources=[native],
        order=[native.id],
        agents=_agents(),
    )
    adapter = LaunchAdapter({})
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    service.revocations.add("src_removed", "cred_removed")

    launch = asyncio.run(_router(service).resolve("codex", "gpt-5"))

    assert launch.channel == "native_cli"
    assert adapter.revoked == ["cred_removed"]
    assert service.revocations.list() == []


def test_mh_chan_001_native_sources_only_dispatch_to_sanctioned_client(tmp_path: Path) -> None:
    native_claude = _source(
        "src_native02",
        kind="subscription",
        vendor="anthropic",
        protocol="anthropic",
        channel="native_cli",
        model_ids=("shared-model",),
    )
    hub_openai = _source(
        "src_hub0002",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("shared-model",),
    )
    config = _hub_config(
        sources=[native_claude, hub_openai],
        order=[native_claude.id, hub_openai.id],
        agents=_agents(),
    )
    adapter = LaunchAdapter({hub_openai.id: "route-openai"})
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    launch = asyncio.run(_router(service).resolve("codex", "shared-model"))
    assert (launch.channel, launch.source_id) == ("hub", hub_openai.id)


def test_runtime_launch_uses_discovered_native_alias_target(tmp_path: Path) -> None:
    relay = _source(
        "src_alias001",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus-4-5-20251101",),
    )
    config = _hub_config(
        sources=[relay],
        order=[relay.id],
        agents=_agents(),
    )
    service = _service(
        tmp_path,
        config,
        LaunchAdapter({relay.id: "route-alias"}),
        now=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    launch = asyncio.run(
        _router(service).resolve("claude", "claude-opus-4-5")
    )

    assert launch.source_id == relay.id
    assert launch.target_model == "claude-opus-4-5-20251101"
    assert launch.runtime_model == "route-alias/claude-opus-4-5-20251101"


@pytest.mark.parametrize("requested_model", ["opus", "sonnet[1m]"])
def test_native_cli_preserves_exact_cli_alias(
    tmp_path: Path,
    requested_model: str,
) -> None:
    native = _source(
        "src_native_alias",
        kind="subscription",
        vendor="anthropic",
        protocol="anthropic",
        channel="native_cli",
        model_ids=(
            requested_model,
            "claude-opus-5",
            "claude-sonnet-5",
        ),
    )
    config = _hub_config(
        sources=[native],
        order=[native.id],
        agents=_agents(),
    )
    service = _service(
        tmp_path,
        config,
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
    )

    launch = asyncio.run(_router(service).resolve("claude", requested_model))

    assert launch.channel == "native_cli"
    assert launch.source_id == native.id
    assert launch.target_model == requested_model
    assert launch.runtime_model == requested_model


def test_runtime_alias_failover_correlates_by_requested_menu_id(tmp_path: Path) -> None:
    requested_model = "claude-opus-4-5"
    primary = _source(
        "src_alias_primary",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus-4-5-20251101",),
    )
    backup = _source(
        "src_alias_backup",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus-4-5-20250929",),
    )
    config = _hub_config(
        sources=[primary, backup],
        order=[primary.id, backup.id],
        agents=_agents(),
    )
    service = _service(
        tmp_path,
        config,
        LaunchAdapter(
            {
                primary.id: "route-primary",
                backup.id: "route-backup",
            }
        ),
        now=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    router = _router(service)

    primary_launch = asyncio.run(router.resolve("claude", requested_model))
    context = SimpleNamespace()
    bind_launch(context, primary_launch)
    assert asyncio.run(router.record_native_failure(context, "429 rate limit")) is True

    backup_launch = asyncio.run(router.resolve("claude", requested_model))

    assert primary_launch.target_model == "claude-opus-4-5-20251101"
    assert backup_launch.target_model == "claude-opus-4-5-20250929"
    assert backup_launch.source_id == backup.id
    transition_events = [
        event
        for event in service.events.list(limit=20)
        if event["kind"] in {"cooldown", "switch"}
    ]
    assert [event["kind"] for event in transition_events] == [
        "switch",
        "cooldown",
    ]
    assert {event["model_id"] for event in transition_events} == {
        requested_model
    }


def test_mh_chan_001_unconfigured_hub_is_interrupted(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        _hub_config(sources=[], order=[], agents=_agents()),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(_router(service).resolve("codex", "gpt-5"))

    assert exc_info.value.code == "mapping_target_unavailable"
    event = service.events.list(limit=10)[0]
    assert (event["kind"], event["reason"]) == (
        "supply_interrupted",
        "no_enabled_source",
    )


def test_hub_with_only_ineligible_sources_names_structural_cause(
    tmp_path: Path,
) -> None:
    native_claude = _source(
        "src_claudenative",
        kind="subscription",
        vendor="anthropic",
        protocol="anthropic",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[native_claude],
            order=[native_claude.id],
            agents=_agents(),
        ),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(ModelHubError, match="mapping_target_unavailable"):
        asyncio.run(_router(service).resolve("codex", "gpt-5"))

    event = service.events.list(limit=10)[0]
    assert (event["kind"], event["reason"]) == (
        "supply_interrupted",
        "no_eligible_source",
    )


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
def test_prelaunch_supply_failure_copy_is_shared_across_backends(
    tmp_path: Path,
    backend: str,
) -> None:
    service = _service(
        tmp_path,
        _hub_config(sources=[], order=[], agents=_agents()),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    controller = SimpleNamespace(
        model_hub_runtime=_router(service),
        config=SimpleNamespace(language="zh"),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            resolve_model_hub_launch(
                controller,
                backend,
                "missing-model",
            )
        )

    assert str(exc_info.value) == (
        "模型 missing-model 没有已启用的来源。请前往 Models 配置。"
    )
    assert exc_info.value.data["copy_key"] == "no_enabled_source"


def test_prelaunch_blocker_details_are_localized(
    tmp_path: Path,
) -> None:
    blocked = _source(
        "src_blocked01",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    blocked.state = ModelHubSourceStateConfig(
        status="needs_action",
        detail_key="models.source.needs_action.oauth_expired",
    )
    service = _service(
        tmp_path,
        _hub_config(sources=[blocked], agents=_agents()),
        LaunchAdapter({blocked.id: "route-blocked"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    controller = SimpleNamespace(
        model_hub_runtime=_router(service),
        config=SimpleNamespace(language="zh"),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            resolve_model_hub_launch(
                controller,
                "codex",
                "gpt-5",
            )
        )

    assert "凭证已过期" in str(exc_info.value)
    assert "models.source.needs_action.oauth_expired" not in str(
        exc_info.value
    )


def test_native_unavailability_blocker_is_localized(
    tmp_path: Path,
) -> None:
    native = _source(
        "src_native01",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(sources=[native], agents=_agents()),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    controller = SimpleNamespace(
        model_hub_runtime=_router(
            service,
            native_cli_ready=lambda _backend: False,
        ),
        config=SimpleNamespace(language="zh"),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(
            resolve_model_hub_launch(
                controller,
                "codex",
                "gpt-5",
            )
        )

    assert "当前进程无法使用 CLI 登录" in str(exc_info.value)
    assert "src_native01：standby" not in str(exc_info.value)


def test_hub_fallback_event_survives_direct_mode_history(tmp_path: Path) -> None:
    """MH-EVT-002: mode history does not suppress or duplicate degradation."""

    agents = _agents(mode="direct")
    adapter = LaunchAdapter({})
    config = _hub_config(sources=[], order=[], agents=agents)
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)

    assert asyncio.run(router.resolve("codex", "gpt-5")).channel == "direct"
    assert service.events.list(limit=10) == []

    agents["codex"].mode = "hub"
    with pytest.raises(ModelHubError, match="mapping_target_unavailable"):
        asyncio.run(router.resolve("codex", "gpt-5"))
    event = service.events.list(limit=10)[0]
    assert (event["kind"], event["reason"]) == (
        "supply_interrupted",
        "no_enabled_source",
    )

    hub = _source(
        "src_hub_after_direct",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    config.sources.append(hub)
    config.agents["codex"].sources.order.append(hub.id)
    adapter.prefixes[hub.id] = "route-hub"
    assert asyncio.run(router.resolve("codex", "gpt-5")).channel == "hub"
    assert len(service.events.list(limit=10)) == 1
    assert service.events.list(limit=10)[0]["reason"] == "no_enabled_source"


def test_recovered_turn_uses_post_wait_mode_for_events(tmp_path: Path) -> None:
    """MH-EVT-002: recovered resolution and telemetry use one config snapshot."""

    initial = _hub_config(
        sources=[],
        order=[],
        agents=_agents(mode="hub"),
    )
    recovered = _hub_config(
        sources=[],
        order=[],
        agents=_agents(mode="direct"),
    )
    service = _service(
        tmp_path,
        initial,
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)
    resolution = resolve_model_hub_turn(recovered, "codex", "gpt-5")
    router._resolve_turn = AsyncMock(return_value=(recovered, resolution))

    launch = asyncio.run(router.resolve("codex", "gpt-5"))

    assert launch.channel == "direct"
    assert service.events.list(limit=10) == []


def test_direct_to_healthy_hub_switch_is_manual(tmp_path: Path) -> None:
    """MH-EVT-002: a mapped Direct-to-Hub mode switch stays manual."""

    hub = _source(
        "src_hub_manual",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("custom-gpt-5",),
    )
    agents = _agents(mode="direct")
    agents["codex"].mappings = [ModelHubMappingConfig("gpt-5", "custom-gpt-5", True)]
    service = _service(
        tmp_path,
        _hub_config(sources=[hub], order=[hub.id], agents=agents),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)

    assert asyncio.run(router.resolve("codex", "gpt-5")).channel == "direct"
    agents["codex"].mode = "hub"
    launch = asyncio.run(router.resolve("codex", "gpt-5"))

    assert (launch.channel, launch.source_id) == ("hub", hub.id)
    event = service.events.list(limit=10)[0]
    assert (event["kind"], event["reason"]) == ("mapping_applied", "mapping")


def test_mh_chan_001_other_backend_source_does_not_activate_hub(tmp_path: Path) -> None:
    codex_native = _source(
        "src_native_other",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[codex_native],
            order=[codex_native.id],
            agents=_agents(),
        ),
        LaunchAdapter({}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(ModelHubError, match="mapping_target_unavailable"):
        asyncio.run(_router(service).resolve("claude", "claude-opus"))


def test_agent_projection_matches_interrupted_unmapped_fixed_backend(tmp_path: Path) -> None:
    """MH-CHAN-001: an unmapped Hub backend remains fail closed."""

    hub = _source(
        "src_hub_display",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("codex-auto-review",),
    )
    adapter = LaunchAdapter({hub.id: "route-hub"})
    service = _service(
        tmp_path,
        _hub_config(
            sources=[hub],
            order=[hub.id],
            agents=_agents(),
        ),
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    service.store.requested_models["claude"] = "claude-opus-4-6"

    projected = next(agent for agent in service.list_agents() if agent["backend"] == "claude")
    router = _router(service)
    with pytest.raises(ModelHubError, match="mapping_target_unavailable"):
        asyncio.run(router.resolve("claude", "claude-opus-4-6"))
    assert "current" not in projected
    assert projected["supply_status"] == "interrupted"
    assert adapter.starts == 0
    event = service.events.list(limit=10)[0]
    assert (event["kind"], event["reason"]) == (
        "supply_interrupted",
        "model_unsupported",
    )


def test_agent_projection_uses_explicit_default_vibe_agent_model(tmp_path: Path) -> None:
    """MH-CHAN-001: the default Vibe Agent supplies its explicit model."""

    hub = _source(
        "src_hub_agent_model",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("agent-model",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[hub],
            order=[hub.id],
            agents=_agents(),
        ),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    service.requested_model_override = lambda backend: "agent-model" if backend == "codex" else None
    router = _router(service)

    projected = next(agent for agent in service.list_agents() if agent["backend"] == "codex")
    chain = service.agent_chain("codex", "agent-model")
    candidate = next(item for item in chain["chain"] if item["runnable"])
    launch = asyncio.run(router.resolve("codex", "agent-model"))

    assert "current" not in projected
    assert projected["selected_model_id"] == "agent-model"
    assert {
        "model_id": candidate["resolved_model_id"] or chain["model_id"],
        "source_id": candidate["source_id"],
        "channel": candidate["channel"],
    } == {
        "model_id": launch.target_model,
        "source_id": launch.source_id,
        "channel": launch.channel,
    }


def test_agent_chain_and_runtime_router_share_resolution_table(tmp_path: Path) -> None:
    """MH-CHAN-001/MH-MAP-001/MH-OC-001: chain equals runtime resolution."""

    fixed_hub = _source(
        "src_hub_mapped",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("mapped-model",),
    )
    fixed_agents = _agents()
    fixed_agents["claude"].mappings = [ModelHubMappingConfig("claude-native", "mapped-model", True)]

    direct_hub = _source(
        "src_hub_direct",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    direct_agents = _agents()
    direct_agents["codex"].mode = "direct"

    opencode_hub = _source(
        "src_hub_open",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus",),
    )
    opencode_agents = _agents()
    opencode_agents["opencode"].menu = ModelHubMenuConfig(
        view="featured",
        checked=["anthropic/claude-opus", "custom/mapped-open"],
    )
    opencode_agents["opencode"].mappings = [ModelHubMappingConfig("anthropic/claude-opus", "custom/mapped-open", True)]
    opencode_mapped = _source(
        "src_hub_open_mapped",
        kind="api_key",
        vendor="custom",
        protocol="openai_compatible",
        channel="hub",
        model_ids=("mapped-open",),
    )
    unavailable_open = _source(
        "src_hub_open_error",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("blocked-model",),
        state="error",
    )
    available_open = _source(
        "src_hub_open_ready",
        kind="api_key",
        vendor="custom",
        protocol="openai_compatible",
        channel="hub",
        model_ids=("ready-model",),
    )
    default_open_agents = _agents()
    default_open_agents["opencode"].menu = ModelHubMenuConfig(
        view="featured",
        checked=["anthropic/blocked-model", "custom/ready-model"],
    )

    native = _source(
        "src_native_table",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )

    cases = [
        (
            "claude",
            "claude-native",
            _hub_config(
                sources=[fixed_hub],
                order=[fixed_hub.id],
                agents=fixed_agents,
            ),
            {"model_id": "mapped-model", "source_id": fixed_hub.id, "channel": "hub"},
        ),
        (
            "codex",
            "gpt-5",
            _hub_config(
                sources=[direct_hub],
                order=[direct_hub.id],
                agents=direct_agents,
            ),
            None,
        ),
        (
            "opencode",
            "claude-opus",
            _hub_config(
                sources=[opencode_hub, opencode_mapped],
                order=[opencode_mapped.id, opencode_hub.id],
                agents=opencode_agents,
            ),
            {
                "model_id": "claude-opus",
                "source_id": opencode_hub.id,
                "channel": "hub",
            },
        ),
        (
            "opencode",
            "",
            _hub_config(
                sources=[unavailable_open, available_open],
                order=[unavailable_open.id, available_open.id],
                agents=default_open_agents,
            ),
            {
                "model_id": "ready-model",
                "source_id": available_open.id,
                "channel": "hub",
            },
        ),
        (
            "codex",
            "gpt-5",
            _hub_config(
                sources=[native],
                order=[native.id],
                agents=_agents(),
            ),
            {"model_id": "gpt-5", "source_id": native.id, "channel": "native_cli"},
        ),
    ]

    for backend, requested_model, config, expected in cases:
        adapter = LaunchAdapter(
            {source.id: f"route-{source.id}" for source in config.sources if source.supply_channel == "hub"}
        )
        service = _service(
            tmp_path,
            config,
            adapter,
            now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
        )
        service.store.requested_models[backend] = requested_model
        router = _router(service)

        projected = next(agent for agent in service.list_agents() if agent["backend"] == backend)
        launch = asyncio.run(router.resolve(backend, requested_model))
        actual = (
            None
            if launch.channel == "direct"
            else {
                "model_id": launch.target_model,
                "source_id": launch.source_id,
                "channel": launch.channel,
            }
        )

        assert "current" not in projected
        if expected is not None and requested_model:
            chain = service.agent_chain(backend, requested_model)
            candidate = next(item for item in chain["chain"] if item["runnable"])
            assert {
                "model_id": candidate["resolved_model_id"] or chain["model_id"],
                "source_id": candidate["source_id"],
                "channel": candidate["channel"],
            } == expected
        assert actual == expected


def test_mh_chan_001_configured_hub_stays_fail_closed_for_unavailable_model(tmp_path: Path) -> None:
    hub = _source(
        "src_hub0099",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    agents = _agents()
    agents["codex"].mappings = [ModelHubMappingConfig("unavailable-model", "configured-but-missing", True)]
    service = _service(
        tmp_path,
        _hub_config(sources=[hub], order=[hub.id], agents=agents),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    with pytest.raises(ModelHubError) as exc_info:
        asyncio.run(_router(service).resolve("codex", "unavailable-model"))

    assert exc_info.value.code == "mapping_target_unavailable"


def test_mh_chan_001_invalid_native_runtime_skips_to_hub(tmp_path: Path) -> None:
    native = _source(
        "src_native_invalid",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
    )
    hub = _source(
        "src_hub_fallback",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[native, hub],
            order=[native.id, hub.id],
            agents=_agents(),
        ),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )

    launch = asyncio.run(
        _router(service, native_cli_ready=lambda backend: backend != "codex").resolve("codex", "gpt-5")
    )

    assert (launch.channel, launch.source_id, launch.runtime_model) == (
        "hub",
        hub.id,
        "route-hub/gpt-5",
    )
    assert native.state.status == "standby"


def test_projection_rechecks_expired_native_readiness_before_recovery(
    tmp_path: Path,
) -> None:
    """MH-CHAN-001: projection uses post-recovery native readiness."""

    clock = datetime(2026, 7, 23, tzinfo=timezone.utc)
    native = _source(
        "src_native_expired",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
        state="cooldown",
        retry_at=(clock - timedelta(seconds=1)).isoformat(),
    )
    hub = _source(
        "src_hub_after_expired",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("gpt-5",),
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[native, hub],
            order=[native.id, hub.id],
            agents=_agents(),
        ),
        LaunchAdapter({hub.id: "route-hub"}),
        now=lambda: clock,
    )
    service.store.requested_models["codex"] = "gpt-5"
    router = _router(service, native_cli_ready=lambda _backend: False)

    projected = next(agent for agent in service.list_agents() if agent["backend"] == "codex")
    chain = service.agent_chain("codex", "gpt-5")
    candidate = next(item for item in chain["chain"] if item["runnable"])
    launch = asyncio.run(router.resolve("codex", "gpt-5"))

    assert "current" not in projected
    assert {
        "model_id": candidate["resolved_model_id"] or chain["model_id"],
        "source_id": candidate["source_id"],
        "channel": candidate["channel"],
    } == {
        "model_id": "gpt-5",
        "source_id": hub.id,
        "channel": "hub",
    }
    assert (launch.channel, launch.source_id) == ("hub", hub.id)
    assert native.state.status == "active"


def test_mh_chan_001_codex_native_runtime_requires_chatgpt_oauth(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    config_path = codex_home / "config.toml"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "fixture-token"}}))
    config_path.write_text("")
    codex_executable = tmp_path / "codex"
    codex_executable.write_text("#!/bin/sh\nexit 0\n")
    codex_executable.chmod(0o755)
    codex_runtime = SimpleNamespace(
        enabled=True,
        cli_path=str(codex_executable),
    )
    monkeypatch.setattr(
        "modules.agents.model_hub.load_config_or_default",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(codex=codex_runtime),
        ),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is True

    codex_runtime.enabled = False
    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is False
    codex_runtime.enabled = True
    codex_runtime.cli_path = str(tmp_path / "missing-codex")
    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is False
    codex_runtime.cli_path = str(codex_executable)

    config_path.write_text(
        'model_provider = "relay"\n\n[model_providers.relay]\nbase_url = "https://relay.invalid/v1"\n'
    )
    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is False

    config_path.write_text("")
    auth_path.write_text(
        json.dumps(
            {
                "tokens": {"access_token": "fixture-token"},
                "OPENAI_API_KEY": "fixture-api-key",
            }
        )
    )
    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is False

    auth_path.write_text("{}")
    assert ModelHubRuntimeRouter._default_native_cli_ready("codex") is False
    assert (
        ModelHubRuntimeRouter._default_native_cli_ready(
            "codex",
            verified_oauth=True,
        )
        is True
    )


def test_mh_chan_001_claude_native_runtime_requires_enabled_executable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    claude_executable = tmp_path / "claude"
    claude_executable.write_text("#!/bin/sh\nexit 0\n")
    claude_executable.chmod(0o755)
    claude_runtime = SimpleNamespace(
        enabled=True,
        cli_path=str(claude_executable),
    )
    monkeypatch.setattr(
        "modules.agents.model_hub.load_config_or_default",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(claude=claude_runtime),
        ),
    )
    monkeypatch.setattr(
        "vibe.claude_config.read_claude_settings_env",
        lambda: {},
    )
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)

    assert ModelHubRuntimeRouter._default_native_cli_ready("claude") is True

    claude_runtime.enabled = False
    assert ModelHubRuntimeRouter._default_native_cli_ready("claude") is False
    claude_runtime.enabled = True
    claude_runtime.cli_path = str(tmp_path / "missing-claude")
    assert ModelHubRuntimeRouter._default_native_cli_ready("claude") is False


def test_mh_chan_001_verified_codex_keyring_source_remains_routable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = {"now": datetime(2026, 7, 25, tzinfo=timezone.utc)}
    codex_home = tmp_path / "codex-keyring-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (codex_home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    codex_executable = tmp_path / "codex"
    codex_executable.write_text("#!/bin/sh\nexit 0\n")
    codex_executable.chmod(0o755)
    monkeypatch.setattr(
        "modules.agents.model_hub.load_config_or_default",
        lambda: SimpleNamespace(
            agents=SimpleNamespace(
                codex=SimpleNamespace(
                    enabled=True,
                    cli_path=str(codex_executable),
                )
            ),
        ),
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    native = _source(
        "src_native_keyring",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("gpt-5",),
        state="active",
    )
    service = _service(
        tmp_path,
        _hub_config(
            sources=[native],
            order=[native.id],
            agents=_agents(),
        ),
        LaunchAdapter({}),
        now=lambda: clock["now"],
    )
    router = ModelHubRuntimeRouter(
        service=service,
        overlay_path=tmp_path / "overlay.json",
    )

    launch = asyncio.run(router.resolve("codex", "gpt-5"))

    assert (launch.channel, launch.source_id) == ("native_cli", native.id)

    context = SimpleNamespace()
    bind_launch(context, launch)
    assert asyncio.run(router.record_native_failure(context, "usage limit reached")) is True
    assert native.state.status == "cooldown"

    clock["now"] += timedelta(seconds=301)
    service.store.requested_models["codex"] = "gpt-5"
    projected = next(agent for agent in service.list_agents() if agent["backend"] == "codex")
    chain = service.agent_chain("codex", "gpt-5")
    candidate = next(item for item in chain["chain"] if item["runnable"])
    assert native.state.status == "cooldown"
    recovered = asyncio.run(router.resolve("codex", "gpt-5"))

    assert "current" not in projected
    assert {
        "model_id": candidate["resolved_model_id"] or chain["model_id"],
        "source_id": candidate["source_id"],
        "channel": candidate["channel"],
    } == {
        "model_id": "gpt-5",
        "source_id": native.id,
        "channel": "native_cli",
    }
    assert (recovered.channel, recovered.source_id) == ("native_cli", native.id)
    assert native.state.status == "active"


def test_mh_chan_001_persisted_launch_identity_is_non_secret_and_restorable() -> None:
    launch = ModelHubLaunch(
        backend="opencode",
        channel="hub",
        requested_model="openai/gpt-5",
        target_model="gpt-5",
        runtime_model="route-openai/gpt-5",
        source_id="src_hub_restore",
        gateway_base_url="http://127.0.0.1:18443",
        gateway_token="gateway-secret",
    )

    identity = persisted_launch_identity(launch)
    assert identity == {
        "backend": "opencode",
        "channel": "hub",
        "source_id": "src_hub_restore",
        "target_model": "gpt-5",
    }
    restored_context = SimpleNamespace()
    restored = bind_persisted_launch(restored_context, identity)

    assert restored == launch_for_context(restored_context)
    assert restored is not None
    assert restored.gateway_base_url is None
    assert restored.gateway_token is None


def test_mh_inj_runtime_injection_never_writes_native_configs(tmp_path: Path, monkeypatch) -> None:
    """MH-INJ-*-001 + MH-INJ-DIRECT-001: injections are process-local only."""

    home = tmp_path / "home"
    native_paths = (
        home / ".claude" / "settings.json",
        home / ".codex" / "config.toml",
        home / ".config" / "opencode" / "opencode.json",
    )
    for index, path in enumerate(native_paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"sentinel-{index}".encode())
    before = {path: path.read_bytes() for path in native_paths}
    monkeypatch.setenv("HOME", str(home))

    hub_launch = ModelHubLaunch(
        backend="claude",
        channel="hub",
        requested_model="claude-opus",
        target_model="claude-opus",
        runtime_model="route-claude/claude-opus",
        source_id="src_hub0003",
        gateway_base_url="http://127.0.0.1:18443",
        gateway_token="gateway-only-token",
    )
    claude_env = build_claude_hub_env(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "upstream-key",
            "ANTHROPIC_AUTH_TOKEN": "upstream-token",
            "ANTHROPIC_BASE_URL": "https://upstream.invalid",
            "ANTHROPIC_CUSTOM_HEADERS": "x-upstream-auth: stale",
            "ANTHROPIC_MODEL": "stale-model",
        },
        hub_launch,
    )
    assert claude_env == {
        "PATH": "/bin",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18443",
        "ANTHROPIC_AUTH_TOKEN": "gateway-only-token",
    }

    codex_launch = ModelHubLaunch(**{**hub_launch.__dict__, "backend": "codex"})
    codex_args, codex_env = build_codex_hub_launch(
        [],
        {"PATH": "/bin", "OPENAI_API_KEY": "upstream", "OPENAI_API_BASE": "https://old.invalid"},
        codex_launch,
    )
    assert codex_args[:2] == ["-c", 'model_provider="avibe_model_hub"']
    assert "model_providers.avibe_model_hub.supports_websockets=false" in codex_args
    assert codex_env == {"PATH": "/bin", "AVIBE_MODEL_HUB_TOKEN": "gateway-only-token"}

    direct = ModelHubLaunch("claude", "direct", "model", "model", "model")
    native = ModelHubLaunch("claude", "native_cli", "model", "model", "model", "src_native")
    base_env = {"ANTHROPIC_API_KEY": "native-value", "PATH": "/bin"}
    assert build_claude_hub_env(base_env, direct) == base_env
    assert build_codex_hub_launch(["--legacy"], base_env, direct) == (["--legacy"], None)
    assert claude_setting_sources_for_launch(hub_launch) == ["project", "local"]
    assert claude_setting_sources_for_launch(direct) == ["user", "project", "local"]
    assert claude_setting_sources_for_launch(native) == ["user", "project", "local"]
    assert CodexAgent._is_managed_provider_transition("openai", "avibe_model_hub") is True
    assert CodexAgent._is_managed_provider_transition("avibe_model_hub", "openai") is True
    assert {path: path.read_bytes() for path in native_paths} == before


def _opencode_config() -> ModelHubConfig:
    anthropic = _source(
        "src_hub0004",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus",),
    )
    custom = _source(
        "src_hub0005",
        kind="api_key",
        vendor="relay-vendor",
        protocol="openai_compatible",
        channel="hub",
        model_ids=("local-model",),
    )
    agents = _agents()
    agents["opencode"].menu = ModelHubMenuConfig(
        view="featured",
        checked=["anthropic/claude-opus", "custom/local-model"],
    )
    return _hub_config(
        sources=[anthropic, custom],
        order=[anthropic.id, custom.id],
        agents=agents,
    )


def test_mh_ovl_001_identifiers_stay_stable_across_all_perturbations(tmp_path: Path) -> None:
    """MH-OC-001/MH-OVL-001: visible identifiers never inherit source identity."""

    config = _opencode_config()
    adapter = LaunchAdapter(
        {
            "src_hub0004": "route-anthropic-a",
            "src_hub0005": "route-custom-a",
            "src_hub0006": "route-anthropic-b",
        }
    )
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service, overlay_path=tmp_path / "runtime" / "opencode.json")
    baseline = asyncio.run(router.prepare_opencode_overlay())
    assert baseline is not None
    stable_projection = overlay_identifier_bytes(baseline.content)
    assert json.loads(stable_projection) == ["anthropic/claude-opus", "custom/local-model"]
    assert opencode_model_for_overlay(None, baseline) == "anthropic/claude-opus"

    config.agents["opencode"].mode = "direct"
    assert asyncio.run(router.prepare_opencode_overlay()) is None
    config.agents["opencode"].mode = "hub"
    after_mode_switch = asyncio.run(router.prepare_opencode_overlay())

    backup = _source(
        "src_hub0006",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus",),
    )
    config.sources.append(backup)
    opencode_order = config.agents["opencode"].sources.order
    config.agents["opencode"].sources.order = [backup.id, *reversed(opencode_order)]
    after_add_reorder = asyncio.run(router.prepare_opencode_overlay())

    backup.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at=(datetime(2026, 7, 24, tzinfo=timezone.utc)).isoformat(),
    )
    after_cooldown = asyncio.run(router.prepare_opencode_overlay())
    config.sources.remove(backup)
    config.agents["opencode"].sources.order.remove(backup.id)
    after_remove = asyncio.run(router.prepare_opencode_overlay())
    after_engine_restart = asyncio.run(router.prepare_opencode_overlay())

    overlays = (
        after_mode_switch,
        after_add_reorder,
        after_cooldown,
        after_remove,
        after_engine_restart,
    )
    assert all(overlay is not None for overlay in overlays)
    assert {overlay_identifier_bytes(overlay.content) for overlay in overlays if overlay} == {stable_projection}
    payload = json.loads(after_remove.content)  # type: ignore[union-attr]
    assert set(payload["provider"]) == {"anthropic", "custom"}
    assert payload["provider"]["anthropic"]["npm"] == "@ai-sdk/anthropic"
    assert payload["provider"]["custom"]["npm"] == "@ai-sdk/openai-compatible"
    assert stat.S_IMODE(router.overlay_path.stat().st_mode) == 0o600


def test_mh_oc_001_open_menu_ignores_stale_fixed_mapping(tmp_path: Path) -> None:
    """MH-OC-001: OpenCode menu identifiers, not fixed mappings, define routes."""

    config = _opencode_config()
    config.agents["opencode"].mappings = [
        ModelHubMappingConfig(
            "anthropic/claude-opus",
            "custom/local-model",
            True,
        )
    ]
    service = _service(
        tmp_path,
        config,
        LaunchAdapter(
            {
                "src_hub0004": "route-anthropic",
                "src_hub0005": "route-custom",
            }
        ),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service, overlay_path=tmp_path / "overlay.json")

    overlay = asyncio.run(router.prepare_opencode_overlay())
    assert overlay is not None
    assert json.loads(overlay_identifier_bytes(overlay.content)) == [
        "anthropic/claude-opus",
        "custom/local-model",
    ]
    launch = asyncio.run(
        router.resolve_opencode_overlay_launch(
            overlay,
            "anthropic/claude-opus",
        )
    )
    assert launch.source_id == "src_hub0004"
    assert launch.target_model == "claude-opus"
    assert launch.runtime_model == "route-anthropic/claude-opus"


def test_mh_ovl_001_unavailable_menu_entry_does_not_block_healthy_model(tmp_path: Path) -> None:
    config = _opencode_config()
    anthropic = config.sources[0]
    anthropic.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at=datetime(2026, 7, 24, tzinfo=timezone.utc).isoformat(),
    )
    adapter = LaunchAdapter(
        {
            "src_hub0004": "route-anthropic",
            "src_hub0005": "route-custom",
        }
    )
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service, overlay_path=tmp_path / "overlay.json")

    cooling_overlay = asyncio.run(router.prepare_opencode_overlay())
    assert cooling_overlay is not None
    assert json.loads(overlay_identifier_bytes(cooling_overlay.content)) == [
        "anthropic/claude-opus",
        "custom/local-model",
    ]
    assert cooling_overlay.available_identifiers == ("custom/local-model",)
    assert opencode_model_for_overlay(None, cooling_overlay) == "custom/local-model"
    assert opencode_model_for_overlay("local-model", cooling_overlay) == "custom/local-model"
    assert opencode_model_for_overlay("custom/local-model", cooling_overlay) == "custom/local-model"
    with pytest.raises(ModelHubError):
        asyncio.run(router.resolve("opencode", "anthropic/claude-opus"))

    config.sources.remove(anthropic)
    config.agents["opencode"].sources.order.remove(anthropic.id)
    reduced_overlay = asyncio.run(router.prepare_opencode_overlay())
    assert reduced_overlay is not None
    assert reduced_overlay.checked_identifiers == ("custom/local-model",)
    assert opencode_model_for_overlay("custom/local-model", reduced_overlay) == "custom/local-model"


def test_mh_ovl_001_turn_uses_overlay_source_snapshot(tmp_path: Path) -> None:
    primary = _source(
        "src_hub_snapshot_a",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus",),
    )
    backup = _source(
        "src_hub_snapshot_b",
        kind="api_key",
        vendor="anthropic",
        protocol="anthropic",
        channel="hub",
        model_ids=("claude-opus",),
    )
    agents = _agents()
    agents["opencode"].menu = ModelHubMenuConfig(
        view="featured",
        checked=["anthropic/claude-opus"],
    )
    config = _hub_config(
        sources=[primary, backup],
        order=[primary.id, backup.id],
        agents=agents,
    )
    adapter = LaunchAdapter(
        {
            primary.id: "route-snapshot-a",
            backup.id: "route-snapshot-b",
        }
    )
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service, overlay_path=tmp_path / "overlay.json")
    overlay = asyncio.run(router.prepare_opencode_overlay())
    assert overlay is not None

    config.agents["opencode"].sources.order = [backup.id, primary.id]
    launch = asyncio.run(
        resolve_opencode_overlay_launch(
            SimpleNamespace(model_hub_runtime=router),
            "anthropic/claude-opus",
            overlay,
        )
    )

    assert launch.source_id == primary.id
    assert launch.runtime_model == "route-snapshot-a/claude-opus"
    assert asyncio.run(router.resolve("opencode", "anthropic/claude-opus")).source_id == backup.id
    context = SimpleNamespace()
    bind_launch(context, launch)
    assert asyncio.run(router.record_native_failure(context, "429 rate limit")) is True
    assert primary.state.status == "cooldown"
    assert backup.state.status == "standby"


def test_mh_chan_001_switch_telemetry_is_isolated_per_model_route(tmp_path: Path) -> None:
    native_a = _source(
        "src_native_a",
        kind="subscription",
        vendor="openai",
        protocol="openai_responses",
        channel="native_cli",
        model_ids=("model-a",),
    )
    hub_a = _source(
        "src_hub_a",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("model-a",),
    )
    hub_b = _source(
        "src_hub_b",
        kind="api_key",
        vendor="openai",
        protocol="openai_responses",
        channel="hub",
        model_ids=("model-b",),
    )
    config = _hub_config(
        sources=[native_a, hub_a, hub_b],
        order=[native_a.id, hub_a.id, hub_b.id],
        agents=_agents(),
    )
    service = _service(
        tmp_path,
        config,
        LaunchAdapter({hub_a.id: "route-a", hub_b.id: "route-b"}),
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    router = _router(service)

    native_launch = asyncio.run(router.resolve("codex", "model-a"))
    assert asyncio.run(router.resolve("codex", "model-b")).channel == "hub"
    assert not [event for event in service.events.list(limit=20) if event["kind"] == "channel_switch"]

    context = SimpleNamespace()
    bind_launch(context, native_launch)
    assert asyncio.run(router.record_native_failure(context, "usage quota exceeded")) is True
    asyncio.run(router.resolve("codex", "model-b"))
    assert not [event for event in service.events.list(limit=20) if event["kind"] == "switch"]

    assert asyncio.run(router.resolve("codex", "model-a")).source_id == hub_a.id
    switch_events = [event for event in service.events.list(limit=20) if event["kind"] in {"switch", "channel_switch"}]
    assert {event["model_id"] for event in switch_events} == {"model-a"}


def test_mh_inj_opencode_overlay_change_drains_then_restarts_and_records_hash(tmp_path: Path) -> None:
    """MH-INJ-OPENCODE-001: active work drains before overlay restart."""

    manager = OpenCodeServerManager()
    manager._pid_file = tmp_path / "opencode_server.json"
    manager._active_run_sessions.add("active-session")
    manager._is_healthy = AsyncMock(return_value=True)
    manager._restart_for_auth_refresh_locked = AsyncMock()
    overlay = SimpleNamespace(path=tmp_path / "overlay.json", content_hash="abc123")

    async def exercise() -> None:
        async def finish_active_work() -> None:
            await asyncio.sleep(0.02)
            manager._active_run_sessions.clear()

        finisher = asyncio.create_task(finish_active_work())
        await manager.configure_model_hub_overlay(overlay)
        await finisher

    asyncio.run(exercise())
    manager._restart_for_auth_refresh_locked.assert_awaited_once()
    manager._write_pid_file(12345)
    metadata = json.loads(manager._pid_file.read_text())
    assert metadata["model_hub_overlay_hash"] == "abc123"
    assert metadata["model_hub_overlay_path"] == str(overlay.path)


def test_mh_inj_opencode_matching_pid_overlay_is_cached_for_crash_recovery(tmp_path: Path) -> None:
    manager = OpenCodeServerManager()
    manager._pid_file = tmp_path / "opencode_server.json"
    overlay = SimpleNamespace(path=tmp_path / "overlay.json", content_hash="same-hash")
    manager._read_pid_file = Mock(
        return_value={
            "pid": 12345,
            "port": manager.port,
            "active_run_sessions": [],
            "model_hub_overlay_path": str(overlay.path),
            "model_hub_overlay_hash": overlay.content_hash,
        }
    )
    manager._pid_file_references_current_server = Mock(return_value=True)
    manager._is_healthy = AsyncMock(return_value=True)

    asyncio.run(manager.configure_model_hub_overlay(overlay))

    assert manager._model_hub_overlay_path == str(overlay.path)
    assert manager._model_hub_overlay_hash == overlay.content_hash
    manager._is_healthy.assert_not_awaited()


def test_mh_inj_opencode_stale_persisted_active_run_is_bounded(tmp_path: Path) -> None:
    manager = OpenCodeServerManager()
    manager._pid_file = tmp_path / "opencode_server.json"
    manager._model_hub_overlay_drain_timeout_seconds = 0
    manager._read_pid_file = Mock(
        return_value={
            "pid": 12345,
            "port": manager.port,
            "active_run_sessions": ["stale-run"],
            "model_hub_overlay_path": "/old-overlay.json",
            "model_hub_overlay_hash": "old-hash",
        }
    )
    manager._pid_file_references_current_server = Mock(return_value=True)
    manager._is_healthy = AsyncMock(return_value=True)
    manager._restart_for_auth_refresh_locked = AsyncMock()
    overlay = SimpleNamespace(path=tmp_path / "overlay.json", content_hash="new-hash")

    asyncio.run(manager.configure_model_hub_overlay(overlay))

    manager._restart_for_auth_refresh_locked.assert_awaited_once()
    assert manager._model_hub_overlay_hash == "new-hash"


def test_mh_inj_direct_opencode_overlay_is_a_noop(tmp_path: Path) -> None:
    """MH-INJ-DIRECT-001: unchanged direct mode does not inspect the live CLI."""

    manager = OpenCodeServerManager()
    manager._pid_file = tmp_path / "opencode_server.json"
    manager._is_healthy = AsyncMock(return_value=True)
    asyncio.run(manager.configure_model_hub_overlay(None))
    manager._is_healthy.assert_not_awaited()


def test_mh_inj_opencode_config_env_is_hub_only(tmp_path: Path, monkeypatch) -> None:
    """MH-INJ-DIRECT-001: direct serve launch has no OPENCODE_CONFIG injection."""

    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)

    async def capture(overlay_content: bytes | None) -> dict:
        manager = OpenCodeServerManager()
        manager._pid_file = tmp_path / f"pid-{bool(overlay_content)}.json"
        if overlay_content is not None:
            overlay_path = tmp_path / "overlay.json"
            overlay_path.write_bytes(overlay_content)
            manager._model_hub_overlay_path = str(overlay_path)
            manager._model_hub_overlay_hash = hashlib.sha256(overlay_content).hexdigest()
        manager._clear_pid_file = Mock()
        manager._write_pid_file = Mock()
        manager._apply_resource_governance = Mock()
        manager._is_healthy = AsyncMock(return_value=True)
        process = SimpleNamespace(pid=4321, returncode=None)
        with (
            patch("modules.agents.opencode.server.server_environment", return_value={"AVIBE_TEST": "1"}),
            patch(
                "modules.agents.opencode.server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)
            ) as spawn,
        ):
            await manager._start_server()
        return spawn.await_args.kwargs["env"]

    overlay_content = b'{"provider":{"openai":{}}}\n'
    direct_env = asyncio.run(capture(None))
    hub_env = asyncio.run(capture(overlay_content))
    assert "OPENCODE_CONFIG" not in direct_env
    assert hub_env["OPENCODE_CONFIG"] == str(tmp_path / "overlay.json")
    assert hub_env["OPENCODE_CONFIG_CONTENT"] == overlay_content.decode()


def test_mh_chan_001_codex_resolves_only_after_session_queue_lock() -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        lock = asyncio.Lock()
        await lock.acquire()
        resolver = AsyncMock()
        agent._session_locks = {"session": lock}
        agent.controller = SimpleNamespace(model_hub_runtime=SimpleNamespace(resolve=resolver))
        request = SimpleNamespace(base_session_id="session")

        task = asyncio.create_task(agent.handle_message(request))
        await asyncio.sleep(0.02)
        resolver.assert_not_awaited()
        task.cancel()
        lock.release()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_mh_inj_codex_runtime_change_waits_for_shared_active_turn(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        active = {"value": True}
        old_transport = SimpleNamespace(
            is_initialized=True,
            runtime_fingerprint="direct",
            stop=AsyncMock(),
        )
        new_transport = SimpleNamespace(
            start=AsyncMock(),
            on_notification=Mock(),
            on_server_request=Mock(),
            pid=12345,
        )
        agent._transport_locks = {}
        agent._transports = {str(tmp_path): old_transport}
        agent._transport_last_activity = {}
        agent._transport_cwd_inodes = {}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value={"other-session"}),
            invalidate_thread=Mock(),
        )
        agent._turn_registry = SimpleNamespace(
            get_active_turn=lambda _session_id: "turn" if active["value"] else None,
            clear_session=Mock(),
        )
        agent._clear_thread_developer_instructions = Mock()
        agent._on_notification = Mock()
        agent._on_server_request = AsyncMock()
        agent.codex_config = SimpleNamespace(binary="codex", extra_args=[])
        agent.controller = SimpleNamespace(resource_governor=None)
        agent._runtime_ownership_snapshot_for_cwd = Mock(
            return_value=SimpleNamespace(blocks_transport_replacement=False)
        )
        launch = ModelHubLaunch(
            "codex",
            "hub",
            "gpt-5",
            "gpt-5",
            "route/gpt-5",
            "src_hub",
            "http://127.0.0.1:18443",
            "token",
        )

        with (
            patch("modules.agents.codex.agent.CodexTransport", return_value=new_transport),
            patch(
                "modules.agents.codex.agent.governor_from_controller",
                return_value=SimpleNamespace(apply_to_pid=Mock()),
            ),
        ):
            task = asyncio.create_task(agent._get_or_create_transport(str(tmp_path), launch))
            await asyncio.sleep(0.02)
            old_transport.stop.assert_not_awaited()
            active["value"] = False
            assert await task is new_transport

        old_transport.stop.assert_awaited_once()
        new_transport.start.assert_awaited_once()

    asyncio.run(exercise())


def test_mh_inj_codex_hub_to_direct_retires_gateway_scope(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        old_transport = SimpleNamespace(
            is_initialized=True,
            runtime_fingerprint="hub:http://127.0.0.1:18443:token-hash",
            stop=AsyncMock(),
        )
        new_transport = SimpleNamespace(
            start=AsyncMock(),
            on_notification=Mock(),
            on_server_request=Mock(),
            pid=12345,
        )
        retire_scope = Mock()
        agent._transport_locks = {}
        agent._transports = {str(tmp_path): old_transport}
        agent._transport_last_activity = {}
        agent._transport_cwd_inodes = {}
        agent._session_mgr = SimpleNamespace(
            sessions_for_cwd=Mock(return_value=set()),
            invalidate_thread=Mock(),
        )
        agent._turn_registry = SimpleNamespace(clear_session=Mock())
        agent._clear_thread_developer_instructions = Mock()
        agent._on_notification = Mock()
        agent._on_server_request = AsyncMock()
        agent.codex_config = SimpleNamespace(binary="codex", extra_args=[])
        agent.controller = SimpleNamespace(
            resource_governor=None,
            model_hub_runtime=SimpleNamespace(
                retire_process_scope=retire_scope,
            ),
        )
        agent._runtime_ownership_snapshot_for_cwd = Mock(
            return_value=SimpleNamespace(blocks_transport_replacement=False)
        )
        launch = ModelHubLaunch(
            "codex",
            "direct",
            "gpt-5",
            "gpt-5",
            "gpt-5",
        )

        with (
            patch("modules.agents.codex.agent.CodexTransport", return_value=new_transport),
            patch(
                "modules.agents.codex.agent.governor_from_controller",
                return_value=SimpleNamespace(apply_to_pid=Mock()),
            ),
        ):
            assert await agent._get_or_create_transport(str(tmp_path), launch) is new_transport

        old_transport.stop.assert_awaited_once()
        new_transport.start.assert_awaited_once()
        retire_scope.assert_called_once_with("codex", str(tmp_path))

    asyncio.run(exercise())


def test_mh_inj_codex_runtime_change_interrupts_current_session_first() -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        active = {"turn": "turn-current"}
        transport = SimpleNamespace(
            is_initialized=True,
            runtime_fingerprint="direct",
            send_request=AsyncMock(return_value={}),
        )
        interrupted_request = SimpleNamespace(context=object())

        def clear_pending(turn_id: str):
            assert turn_id == "turn-current"
            active["turn"] = None
            return interrupted_request

        agent._transports = {"/work": transport}
        agent._session_mgr = SimpleNamespace(get_thread_id=Mock(return_value="thread-current"))
        agent._turn_registry = SimpleNamespace(get_active_turn=lambda _base: active["turn"])
        agent._event_handler = SimpleNamespace(
            clear_pending=Mock(side_effect=clear_pending),
            _release_stream_turn=Mock(),
        )
        agent._remove_ack_reaction = AsyncMock()
        request = SimpleNamespace(
            working_path="/work",
            base_session_id="session-current",
        )
        launch = ModelHubLaunch(
            "codex",
            "hub",
            "gpt-5",
            "gpt-5",
            "route/gpt-5",
            "src_hub",
            "http://127.0.0.1:18443",
            "token",
        )

        await agent._interrupt_active_turn_before_runtime_change(request, launch)

        transport.send_request.assert_awaited_once_with(
            "turn/interrupt",
            {"threadId": "thread-current", "turnId": "turn-current"},
        )
        assert active["turn"] is None
        agent._remove_ack_reaction.assert_awaited_once_with(interrupted_request)
        agent._event_handler._release_stream_turn.assert_called_once_with(interrupted_request.context)

    asyncio.run(exercise())


def test_mh_inj_codex_runtime_change_replaces_transport_when_interrupt_fails() -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        active = {"turn": "turn-current"}
        transport = SimpleNamespace(
            is_initialized=True,
            runtime_fingerprint="direct",
            send_request=AsyncMock(side_effect=ConnectionError("transport closed")),
        )
        interrupted_request = SimpleNamespace(context=object())

        def clear_pending(turn_id: str):
            assert turn_id == "turn-current"
            active["turn"] = None
            return interrupted_request

        agent._transports = {"/work": transport}
        agent._session_mgr = SimpleNamespace(get_thread_id=Mock(return_value="thread-current"))
        agent._turn_registry = SimpleNamespace(get_active_turn=lambda _base: active["turn"])
        agent._event_handler = SimpleNamespace(
            clear_pending=Mock(side_effect=clear_pending),
            _release_stream_turn=Mock(),
        )
        agent._remove_ack_reaction = AsyncMock()
        request = SimpleNamespace(
            working_path="/work",
            base_session_id="session-current",
        )
        launch = ModelHubLaunch(
            "codex",
            "hub",
            "gpt-5",
            "gpt-5",
            "route/gpt-5",
            "src_hub",
            "http://127.0.0.1:18443",
            "token",
        )

        await agent._interrupt_active_turn_before_runtime_change(request, launch)

        assert active["turn"] is None
        agent._remove_ack_reaction.assert_awaited_once_with(interrupted_request)
        agent._event_handler._release_stream_turn.assert_called_once_with(interrupted_request.context)

    asyncio.run(exercise())


def test_mh_inj_codex_direct_resume_clears_implicit_hub_provider() -> None:
    async def exercise() -> None:
        agent = object.__new__(CodexAgent)
        transport = SimpleNamespace(
            send_request=AsyncMock(
                side_effect=[
                    {"config": {}},
                    {"thread": {"id": "thread-existing", "modelProvider": "avibe_model_hub"}},
                ]
            )
        )
        request = SimpleNamespace(working_path="/work")

        provider = await agent._resolve_resume_model_provider_override(
            transport,
            request,
            "thread-existing",
        )

        assert provider == "openai"

    asyncio.run(exercise())


def test_mh_inj_claude_channel_change_waits_for_active_turn() -> None:
    async def exercise() -> None:
        handler = object.__new__(SessionHandler)
        handler.controller = SimpleNamespace(runtime_activation=None)
        key = "session:/work"
        client = SimpleNamespace(_vibe_model_hub_fingerprint="native_cli:src_native")
        handler.claude_sessions = {key: client}
        handler.active_sessions = {key}
        handler._cleanup_session_locked = AsyncMock()
        launch = ModelHubLaunch(
            "claude",
            "hub",
            "claude-opus",
            "claude-opus",
            "route/claude-opus",
            "src_hub",
            "http://127.0.0.1:18443",
            "token",
        )

        task = asyncio.create_task(
            handler._reuse_cached_claude_session_if_available(
                composite_key=key,
                base_session_id="session",
                working_path="/work",
                context=SimpleNamespace(),
                session_key="settings",
                stored_claude_session_id=None,
                current_model="claude-opus",
                agent_system_prompt=None,
                model_hub_launch=launch,
            )
        )
        await asyncio.sleep(0.02)
        handler._cleanup_session_locked.assert_not_awaited()
        handler.active_sessions.clear()
        assert await task is None
        handler._cleanup_session_locked.assert_awaited_once_with(
            key,
            retire_model_hub_scope=False,
        )

    asyncio.run(exercise())


def test_mh_inj_cached_claude_subagent_uses_resolved_native_model() -> None:
    native = ModelHubLaunch(
        "claude",
        "native_cli",
        "builtin-opus",
        "claude-opus-4-6",
        "claude-opus-4-6",
        "src_native",
    )
    direct = ModelHubLaunch("claude", "direct", "builtin-opus", "builtin-opus", "builtin-opus")

    assert SessionHandler._cached_claude_subagent_model("builtin-opus", native) == "claude-opus-4-6"
    assert SessionHandler._cached_claude_subagent_model(None, direct) is None
