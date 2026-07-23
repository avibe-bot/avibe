from __future__ import annotations

import asyncio
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config.v2_config import (
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
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubService
from modules.agents.model_hub import (
    ModelHubLaunch,
    ModelHubRuntimeRouter,
    bind_launch,
    build_claude_hub_env,
    build_codex_hub_launch,
    opencode_model_for_overlay,
    overlay_identifier_bytes,
)
from modules.agents.codex.agent import CodexAgent
from modules.agents.opencode.server import OpenCodeServerManager


class MemoryStore:
    def __init__(self, config: ModelHubConfig):
        self.config = config

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config


class LaunchAdapter:
    def __init__(self, prefixes: dict[str, str], *, token: str = "local-gateway-token"):
        self.prefixes = prefixes
        self.token = token
        self.starts = 0
        self.syncs = 0

    async def start(self):
        self.starts += 1
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 18443, None)

    async def gateway_token(self):
        return self.token

    async def sync_sources(self, bindings):
        self.syncs += 1

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
        state=ModelHubSourceStateConfig(status=state, retry_at=retry_at),
        models=[_model(model_id) for model_id in model_ids],
        credential_ref=f"cred_{source_id}" if channel == "hub" else None,
    )


def _agents(*, mode: str = "hub") -> dict[str, ModelHubAgentSupplyConfig]:
    return {
        backend: ModelHubAgentSupplyConfig.default(backend, mode=mode)
        for backend in ("claude", "codex", "opencode")
    }


def _service(
    tmp_path: Path,
    config: ModelHubConfig,
    adapter: LaunchAdapter,
    *,
    now,
) -> ModelHubService:
    return ModelHubService(
        store=MemoryStore(config),
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now,
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
    config = ModelHubConfig(
        sources=[native, hub],
        priority_order=[native.id, hub.id],
        agents=agents,
    )
    adapter = LaunchAdapter({hub.id: "route-hub"})
    service = _service(tmp_path, config, adapter, now=lambda: clock["now"])
    router = ModelHubRuntimeRouter(service=service, overlay_path=tmp_path / "overlay.json")

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
    assert kinds == ["cooldown", "channel_switch", "recover", "channel_switch"]
    assert [event["reason"] for event in service.events.list(limit=20) if event["kind"] == "channel_switch"] == [
        "recovery",
        "quota_exhausted",
    ]


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
    config = ModelHubConfig(
        sources=[native_claude, hub_openai],
        priority_order=[native_claude.id, hub_openai.id],
        agents=_agents(),
    )
    adapter = LaunchAdapter({hub_openai.id: "route-openai"})
    service = _service(
        tmp_path,
        config,
        adapter,
        now=lambda: datetime(2026, 7, 23, tzinfo=timezone.utc),
    )
    launch = asyncio.run(ModelHubRuntimeRouter(service=service).resolve("codex", "shared-model"))
    assert (launch.channel, launch.source_id) == ("hub", hub_openai.id)


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
    assert codex_env == {"PATH": "/bin", "AVIBE_MODEL_HUB_TOKEN": "gateway-only-token"}

    direct = ModelHubLaunch("claude", "direct", "model", "model", "model")
    base_env = {"ANTHROPIC_API_KEY": "native-value", "PATH": "/bin"}
    assert build_claude_hub_env(base_env, direct) == base_env
    assert build_codex_hub_launch(["--legacy"], base_env, direct) == (["--legacy"], None)
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
    return ModelHubConfig(
        sources=[anthropic, custom],
        priority_order=[anthropic.id, custom.id],
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
    router = ModelHubRuntimeRouter(service=service, overlay_path=tmp_path / "runtime" / "opencode.json")
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
    config.priority_order = [backup.id, *reversed(config.priority_order)]
    after_add_reorder = asyncio.run(router.prepare_opencode_overlay())

    backup.state = ModelHubSourceStateConfig(
        status="cooldown",
        retry_at=(datetime(2026, 7, 24, tzinfo=timezone.utc)).isoformat(),
    )
    after_cooldown = asyncio.run(router.prepare_opencode_overlay())
    config.sources.remove(backup)
    config.priority_order.remove(backup.id)
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

    async def capture(overlay_path: str | None) -> dict:
        manager = OpenCodeServerManager()
        manager._pid_file = tmp_path / f"pid-{bool(overlay_path)}.json"
        manager._model_hub_overlay_path = overlay_path
        manager._clear_pid_file = Mock()
        manager._write_pid_file = Mock()
        manager._apply_resource_governance = Mock()
        process = SimpleNamespace(pid=4321, returncode=None)
        with (
            patch("modules.agents.opencode.server.server_environment", return_value={"AVIBE_TEST": "1"}),
            patch("modules.agents.opencode.server.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn,
        ):
            await manager._start_server()
        return spawn.await_args.kwargs["env"]

    direct_env = asyncio.run(capture(None))
    hub_env = asyncio.run(capture(str(tmp_path / "overlay.json")))
    assert "OPENCODE_CONFIG" not in direct_env
    assert hub_env["OPENCODE_CONFIG"] == str(tmp_path / "overlay.json")
