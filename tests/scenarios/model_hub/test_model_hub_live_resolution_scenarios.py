from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import aiohttp
import pytest

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
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import ModelHubService
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from modules.agents.model_hub import ModelHubRuntimeRouter, bind_launch


class MemoryStore:
    def __init__(self, config: ModelHubConfig) -> None:
        self.config = config

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config


@dataclass(frozen=True)
class AdapterResult:
    kind: RawOutcomeKind
    status: int | None = None
    code: str | None = None
    body: bytes | None = None
    stream_started: bool = False


class InvokeHandle:
    def __init__(self, outcome: RawCallOutcome, body: bytes | None) -> None:
        self._outcome = outcome
        self._body = body

    @property
    def stream(self) -> AsyncIterator[bytes] | None:
        if self._body is None:
            return None

        async def chunks() -> AsyncIterator[bytes]:
            yield self._body

        return chunks()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class AdapterBoundaryFake:
    def __init__(self, results: list[AdapterResult]) -> None:
        self.results = deque(results)
        self.invocations: list[tuple[str, str, str]] = []

    async def start(self) -> EngineStatus:
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 18443, None)

    async def gateway_token(self) -> str:
        return "unused-engine-token"

    async def sync_sources(self, bindings) -> None:
        return None

    async def revoke_credential(self, credential_ref: str) -> None:
        return None

    async def invoke(self, source_id, model_id, request, stream, origin) -> InvokeHandle:
        self.invocations.append((source_id, model_id, origin))
        result = self.results.popleft()
        outcome = RawCallOutcome(
            kind=result.kind,
            http_status=result.status,
            error_code=result.code,
            redacted_message=None,
            stream_started=result.stream_started,
            model_id=model_id,
            source_id=source_id,
        )
        return InvokeHandle(outcome, result.body)


def _source(
    source_id: str,
    *,
    channel: str = "hub",
    kind: str = "api_key",
) -> ModelHubSourceConfig:
    return ModelHubSourceConfig(
        id=source_id,
        kind=kind,
        vendor="openai" if kind == "subscription" else "anthropic",
        display_name=source_id,
        protocol="openai_responses" if kind == "subscription" else "anthropic",
        supply_channel=channel,
        billing="monthly" if kind == "subscription" else "metered",
        state=ModelHubSourceStateConfig(status="standby"),
        models=[ModelHubModelConfig(id="model-live", provenance="discovered")],
        credential_ref=f"cred_{source_id}" if channel == "hub" else None,
    )


def _config(*sources: ModelHubSourceConfig) -> ModelHubConfig:
    return ModelHubConfig(
        sources=list(sources),
        priority_order=[source.id for source in sources],
        agents={
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
            for backend in ("claude", "codex", "opencode")
        },
    )


def _service(
    tmp_path: Path,
    store: MemoryStore,
    adapter: AdapterBoundaryFake,
    *,
    now,
) -> ModelHubService:
    return ModelHubService(
        store=store,
        adapter=adapter,
        events=BoundedEventLog(tmp_path / "events.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now,
    )


async def _post_turn(
    launch,
    *,
    endpoint: str = "messages",
    stream: bool = False,
) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {launch.gateway_token}"}
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/{endpoint}",
            headers=headers,
            json={"model": launch.runtime_model, "messages": [], "stream": stream},
        ) as response:
            return response.status, await response.read()


@pytest.mark.parametrize(
    ("backend", "requested_model", "endpoint"),
    [
        ("claude", "model-live", "messages"),
        ("codex", "model-live", "responses"),
        ("opencode", "anthropic/model-live", "messages"),
    ],
)
@pytest.mark.parametrize(
    ("failed", "reason"),
    [
        (AdapterResult(RawOutcomeKind.HTTP_ERROR, status=429), "rate_limited"),
        (AdapterResult(RawOutcomeKind.HTTP_ERROR, status=403, code="quota_exhausted"), "quota_exhausted"),
        (AdapterResult(RawOutcomeKind.HTTP_ERROR, status=503), "server_error"),
        (AdapterResult(RawOutcomeKind.NETWORK_ERROR), "network"),
    ],
)
def test_mh_res_live_001_pre_stream_failure_falls_back_within_turn(
    tmp_path: Path,
    backend: str,
    requested_model: str,
    endpoint: str,
    failed: AdapterResult,
    reason: str,
) -> None:
    """MH-RES-LIVE-001: the real turn gateway completes on candidate two."""

    async def exercise() -> None:
        clock = [datetime(2026, 7, 25, tzinfo=timezone.utc)]
        adapter = AdapterBoundaryFake(
            [
                failed,
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}'),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"recovered":true}'),
            ]
        )
        store = MemoryStore(_config(_source("src_primary"), _source("src_backup")))
        assert store.config.agents["opencode"].menu is not None
        store.config.agents["opencode"].menu.checked = ["anthropic/model-live"]
        service = _service(tmp_path, store, adapter, now=lambda: clock[0])
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            if backend == "opencode":
                overlay = await router.prepare_opencode_overlay()
                assert overlay is not None
                launch = await router.resolve_opencode_overlay_launch(overlay, requested_model)
            else:
                launch = await router.resolve(backend, requested_model)
            status, body = await _post_turn(launch, endpoint=endpoint)
            assert status == 200
            assert body == b'{"ok":true}'
            assert [call[0] for call in adapter.invocations] == ["src_primary", "src_backup"]
            assert store.load().sources[0].state.status == "cooldown"
            assert [event["kind"] for event in service.list_events(limit=5)[:2]] == ["switch", "cooldown"]
            assert service.list_events(limit=5)[1]["reason"] == reason

            clock[0] += timedelta(minutes=6)
            if backend == "opencode":
                recovered_overlay = await router.prepare_opencode_overlay()
                assert recovered_overlay is not None
                recovered_launch = await router.resolve_opencode_overlay_launch(
                    recovered_overlay,
                    requested_model,
                )
            else:
                recovered_launch = await router.resolve(backend, requested_model)
            recovered_status, recovered_body = await _post_turn(
                recovered_launch,
                endpoint=endpoint,
            )
            assert recovered_status == 200
            assert recovered_body == b'{"recovered":true}'
            assert adapter.invocations[-1][0] == "src_primary"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_res_live_002_401_refreshes_once_in_live_turn(tmp_path: Path) -> None:
    """MH-RES-LIVE-002: a 401 retries the same source exactly once."""

    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(RawOutcomeKind.HTTP_ERROR, status=401),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}'),
            ]
        )
        store = MemoryStore(_config(_source("src_primary"), _source("src_backup")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(await router.resolve("claude", "model-live"))
            assert status == 200
            assert body == b'{"ok":true}'
            assert [call[0] for call in adapter.invocations] == ["src_primary", "src_primary"]
            assert store.load().sources[0].state.status == "standby"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_res_live_002_second_401_surfaces_without_fallback(tmp_path: Path) -> None:
    """MH-RES-LIVE-002: a second 401 is terminal for the selected source."""

    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(RawOutcomeKind.HTTP_ERROR, status=401),
                AdapterResult(RawOutcomeKind.HTTP_ERROR, status=401),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"backup":true}'),
            ]
        )
        store = MemoryStore(_config(_source("src_primary"), _source("src_backup")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, _body = await _post_turn(await router.resolve("claude", "model-live"))
            assert status == 401
            assert [call[0] for call in adapter.invocations] == ["src_primary", "src_primary"]
            assert store.load().sources[0].state.status == "standby"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_res_live_003_started_stream_never_retries(tmp_path: Path) -> None:
    """MH-RES-LIVE-003: bytes already emitted make the source terminal."""

    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    body=b"data: partial\n\n",
                    stream_started=True,
                ),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b"data: backup\n\n"),
            ]
        )
        store = MemoryStore(_config(_source("src_primary"), _source("src_backup")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(await router.resolve("claude", "model-live"), stream=True)
            assert status == 200
            assert body == b"data: partial\n\n"
            assert [call[0] for call in adapter.invocations] == ["src_primary"]
            assert store.load().sources[0].state.status == "standby"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_turn_gateway_tokens_are_bound_to_backend_origin(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}')]
        )
        store = MemoryStore(_config(_source("src_primary")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        try:
            claude_url, _claude_token = await gateway.endpoint("claude")
            _codex_url, codex_token = await gateway.endpoint("codex")
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(
                    f"{claude_url}/v1/messages",
                    headers={"Authorization": f"Bearer {codex_token}"},
                    json={"model": "model-live", "messages": []},
                ) as response:
                    assert response.status == 401
            assert adapter.invocations == []
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_evt_002_switch_events_survive_router_and_service_restart(tmp_path: Path) -> None:
    """MH-EVT-002: persisted cooldown causality survives process-local state loss."""

    async def exercise() -> None:
        clock = datetime(2026, 7, 25, tzinfo=timezone.utc)
        native = _source("src_native", channel="native_cli", kind="subscription")
        backup = _source("src_backup")
        store = MemoryStore(_config(native, backup))
        first_adapter = AdapterBoundaryFake([])
        first_service = _service(tmp_path, store, first_adapter, now=lambda: clock)
        first_gateway = ModelHubTurnGateway(first_service)
        first_router = ModelHubRuntimeRouter(
            service=first_service,
            turn_gateway=first_gateway,
            native_cli_ready=lambda backend: True,
        )
        launch = await first_router.resolve("codex", "model-live")
        assert launch.channel == "native_cli"
        context = SimpleNamespace()
        bind_launch(context, launch)
        assert await first_router.record_native_failure(context, "usage quota exceeded") is True
        await first_gateway.close()

        second_adapter = AdapterBoundaryFake([])
        second_service = _service(tmp_path, store, second_adapter, now=lambda: clock)
        second_gateway = ModelHubTurnGateway(second_service)
        second_router = ModelHubRuntimeRouter(
            service=second_service,
            turn_gateway=second_gateway,
            native_cli_ready=lambda backend: True,
        )
        try:
            fallback = await second_router.resolve("codex", "model-live")
            assert fallback.channel == "hub"
            persisted = BoundedEventLog(tmp_path / "events.json").list(limit=10)
            assert [event["kind"] for event in persisted[:3]] == [
                "channel_switch",
                "switch",
                "cooldown",
            ]
            assert persisted[0]["from_source"] == "src_native"
            assert persisted[0]["to_source"] == "src_backup"
        finally:
            await second_gateway.close()

    asyncio.run(exercise())
