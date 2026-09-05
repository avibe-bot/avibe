from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import aiohttp
import pytest

from config.v2_config import (
    ModelHubAgentSourcesConfig,
    ModelHubAgentSupplyConfig,
    ModelHubBackendModelConfig,
    ModelHubConfig,
    ModelHubModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
    ModelHubSourceConfig,
    ModelHubSourceStateConfig,
    model_hub_fixed_menu_ids,
)
from core.handlers.model_hub.adapter import (
    EngineEnsureResult,
    EngineHealth,
    EngineStatus,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.events import BoundedEventLog
from core.handlers.model_hub.provenance import BoundedProvenanceStore
from core.handlers.model_hub.revocations import CredentialRevocationJournal
from core.handlers.model_hub.service import (
    PRE_ATTEMPT_SETTLEMENT_GENERATION,
    ModelHubService,
)
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import (
    ModelHubRuntimeRouter,
    bind_launch,
    bind_persisted_launch,
    persisted_launch_identity,
)


class MemoryStore:
    def __init__(self, config: ModelHubConfig) -> None:
        self.config = config

    def load(self) -> ModelHubConfig:
        return self.config

    def save(self, config: ModelHubConfig) -> None:
        self.config = config


def _requested_model(backend: str) -> str:
    if backend == "opencode":
        return "model-live"
    return model_hub_fixed_menu_ids(backend)[0]


def _source_model_id(backend: str) -> str:
    if backend == "opencode":
        return "model-live"
    return _requested_model(backend)


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
        self._stream = self._chunks(body) if body is not None else None

    @staticmethod
    async def _chunks(body: bytes) -> AsyncIterator[bytes]:
        yield body

    @property
    def stream(self) -> AsyncIterator[bytes] | None:
        return self._stream

    @property
    def observed(self) -> None:
        """This double reports nothing about the body until its consumer reads it."""

        return None

    @property
    def outcome_available(self) -> bool:
        return True

    async def close_stream(self) -> None:
        if self._stream is not None:
            await self._stream.aclose()

    async def outcome(self) -> RawCallOutcome:
        return self._outcome


class AdapterBoundaryFake:
    def __init__(
        self,
        results: list[AdapterResult],
        *,
        refreshable_credential_refs: tuple[str, ...] = (),
    ) -> None:
        self.results = deque(results)
        self.invocations: list[tuple[str, str, str]] = []
        self.requests: list[object] = []
        self.refreshable_credential_refs = frozenset(refreshable_credential_refs)

    async def ensure_installed(
        self,
        *,
        force: bool = False,
        offline: bool = False,
    ) -> EngineEnsureResult:
        return EngineEnsureResult(status=await self.start(), changed=force)

    async def start(self) -> EngineStatus:
        return EngineStatus(EngineHealth.OK, "test", True, "127.0.0.1", 18443, None)

    async def gateway_token(self) -> str:
        return "unused-engine-token"

    async def sync_sources(self, bindings) -> None:
        return None

    async def revoke_credential(self, credential_ref: str) -> None:
        return None

    async def credential_supports_refresh(self, credential_ref: str) -> bool:
        return credential_ref in self.refreshable_credential_refs

    async def invoke(self, source_id, model_id, request, stream, origin, *, on_admitted=None) -> InvokeHandle:
        if on_admitted is not None:
            on_admitted()
        self.invocations.append((source_id, model_id, origin))
        self.requests.append(request)
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
        models=[
            ModelHubModelConfig(id=model_id, provenance="discovered")
            for model_id in (
                _source_model_id("claude"),
                _source_model_id("codex"),
                _source_model_id("opencode"),
            )
        ],
        credential_ref=f"cred_{source_id}" if channel == "hub" else None,
    )


def _config(*sources: ModelHubSourceConfig) -> ModelHubConfig:
    config = ModelHubConfig(
        sources=list(sources),
        agents={
            backend: ModelHubAgentSupplyConfig.default(backend, mode="hub")
            for backend in ("claude", "codex", "opencode")
        },
    )
    for backend, agent in config.agents.items():
        eligible_sources = tuple(
            source
            for source in sources
            if ModelHubConfig.source_eligible_for_backend(source, backend)
        )
        agent.sources = ModelHubAgentSourcesConfig(
            order=[source.id for source in eligible_sources],
        )
        agent.routes[_requested_model(backend)] = ModelHubRouteConfig(
            hops=tuple(
                ModelHubRouteHopConfig(source.id, _source_model_id(backend))
                for source in eligible_sources
            )
        )
        if backend == "opencode":
            agent.models = [
                ModelHubBackendModelConfig(
                    id=_requested_model(backend),
                    native_protocol="anthropic",
                )
            ]
            assert agent.menu is not None
            agent.menu.checked = [_requested_model(backend)]
    return config


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
        provenance=BoundedProvenanceStore(tmp_path / "provenance.json"),
        revocations=CredentialRevocationJournal(tmp_path / "revocations.json"),
        now=now,
    )


async def _post_turn(
    launch,
    *,
    endpoint: str = "messages",
    stream: bool = False,
    payload: dict[str, object] | None = None,
) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {launch.gateway_token}"}
    request_payload = payload or {
        "model": launch.runtime_model,
        "messages": [],
        "stream": stream,
    }
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/{endpoint}",
            headers=headers,
            json=request_payload,
        ) as response:
            return response.status, await response.read()


def test_unpinned_hub_projection_is_null_while_explicit_turn_resolves(
    tmp_path: Path,
) -> None:
    """MH-SEL-001: no pinned selection is null while the request model runs."""

    async def exercise() -> None:
        source = _source("src_explicit1")
        store = MemoryStore(_config(source))
        adapter = AdapterBoundaryFake([])
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        projected = next(
            agent for agent in service.list_agents() if agent["backend"] == "claude"
        )
        assert projected["selected_by_agent"] is None
        assert projected["selected_model_id"] is None
        assert "current" not in projected
        assert projected["supply_status"] is None

        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            launch = await router.resolve("claude", _requested_model("claude"))
            assert launch.source_id == source.id
            assert launch.target_model == _source_model_id("claude")
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_codex_backend_model_id_survives_until_gateway_resolution(
    tmp_path: Path,
) -> None:
    """Codex selects catalog identity while the gateway invokes its routed target."""

    async def exercise() -> None:
        source = _source("src_alias_route")
        source.models.append(ModelHubModelConfig(id="model-b", provenance="manual"))
        config = _config(source)
        codex = config.agents["codex"]
        codex.models.append(ModelHubBackendModelConfig(id="alias-a", origin="manual"))
        codex.routes["alias-a"] = ModelHubRouteConfig(
            hops=(ModelHubRouteHopConfig(source.id, "model-b"),)
        )
        adapter = AdapterBoundaryFake(
            [AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}')]
        )
        service = _service(
            tmp_path,
            MemoryStore(config),
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            launch = await router.resolve(
                "codex",
                "alias-a",
                process_scope="/repo",
                turn_id="turn_alias",
            )
            assert launch.runtime_model == "alias-a"
            assert launch.target_model == "model-b"

            status, body = await _post_turn(launch, endpoint="responses")
            assert status == 200
            assert body == b'{"ok":true}'
            assert adapter.invocations == [(source.id, "model-b", "codex")]
        finally:
            await gateway.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("backend", "requested_model", "endpoint"),
    [
        ("claude", _requested_model("claude"), "messages"),
        ("codex", _requested_model("codex"), "responses"),
        ("opencode", _requested_model("opencode"), "chat/completions"),
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
        store = MemoryStore(_config(_source("src_primary1"), _source("src_backup01")))
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
            assert [call[0] for call in adapter.invocations] == ["src_primary1", "src_backup01"]
            events = service.list_events(limit=5)
            if failed.kind is RawOutcomeKind.NETWORK_ERROR:
                assert store.load().sources[0].state.status == "standby"
                assert [event["kind"] for event in events] == ["switch"]
            else:
                assert store.load().sources[0].state.status == "cooldown"
                assert [event["kind"] for event in events[:2]] == ["switch", "cooldown"]
                assert events[1]["reason"] == reason

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
            assert adapter.invocations[-1][0] == "src_primary1"
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
            ],
            refreshable_credential_refs=("cred_src_primary1",),
        )
        store = MemoryStore(
            _config(
                _source("src_primary1", kind="subscription"),
                _source("src_backup01", kind="subscription"),
            )
        )
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(
                await router.resolve("claude", _requested_model("claude"))
            )
            assert status == 200
            assert body == b'{"ok":true}'
            assert [call[0] for call in adapter.invocations] == ["src_primary1", "src_primary1"]
            assert store.load().sources[0].state.status == "standby"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_res_live_002_second_401_blocks_source_and_falls_back(
    tmp_path: Path,
) -> None:
    """MH-RES-LIVE-002: a second 401 leaves an actionable source blocker."""

    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(RawOutcomeKind.HTTP_ERROR, status=401),
                AdapterResult(RawOutcomeKind.HTTP_ERROR, status=401),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"backup":true}'),
            ],
            refreshable_credential_refs=("cred_src_primary1", "cred_src_backup01"),
        )
        store = MemoryStore(
            _config(
                _source("src_primary1", kind="subscription"),
                _source("src_backup01", kind="subscription"),
            )
        )
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, _body = await _post_turn(
                await router.resolve("claude", _requested_model("claude"))
            )
            assert status == 200
            assert [call[0] for call in adapter.invocations] == [
                "src_primary1",
                "src_primary1",
                "src_backup01",
            ]
            primary = store.load().sources[0]
            assert primary.state.status == "needs_action"
            assert (
                primary.state.detail_key
                == "models.source.needs_action.oauth_expired"
            )
            assert [
                event["kind"]
                for event in service.list_events(limit=10)
            ] == ["switch", "needs_action"]
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
        store = MemoryStore(_config(_source("src_primary1"), _source("src_backup01")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(
                await router.resolve("claude", _requested_model("claude")),
                stream=True,
            )
            assert status == 200
            prefix, terminal = body.split(b"event: error\ndata: ", 1)
            assert prefix == b"data: partial\n\n"
            terminal_event = json.loads(terminal.rstrip(b"\n"))
            assert terminal_event["type"] == "error"
            assert terminal_event["error"]["type"] == "api_error"
            assert terminal_event["error"]["message"]
            assert [call[0] for call in adapter.invocations] == ["src_primary1"]
            assert store.load().sources[0].state.status == "cooldown"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_res_live_004_hub_subscription_falls_back_to_api_key_within_turn(
    tmp_path: Path,
) -> None:
    """MH-RES-LIVE-004: a hub subscription quota failure serves the next hop in the same turn and marks the switch as entered_metered."""

    async def exercise() -> None:
        clock = [datetime(2026, 7, 25, tzinfo=timezone.utc)]
        subscription = _source("src_primary1", kind="subscription")
        api_key = _source("src_backup01")
        assert subscription.kind == "subscription"
        assert subscription.supply_channel == "hub"
        assert subscription.billing == "monthly"
        assert api_key.kind == "api_key"
        assert api_key.billing == "metered"
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    code="quota_exceeded",
                ),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}'),
                AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"recovered":true}'),
            ]
        )
        store = MemoryStore(_config(subscription, api_key))
        service = _service(tmp_path, store, adapter, now=lambda: clock[0])
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(
                await router.resolve("claude", _requested_model("claude"))
            )
            assert status == 200
            assert body == b'{"ok":true}'
            assert [call[0] for call in adapter.invocations] == [
                "src_primary1",
                "src_backup01",
            ]
            cooled = store.load().sources[0]
            assert cooled.id == "src_primary1"
            assert cooled.state.status == "cooldown"
            assert cooled.state.detail_key == "models.source.cooldown.quota_exhausted"
            assert cooled.state.retry_at == (clock[0] + timedelta(seconds=300)).isoformat()
            events = service.list_events(limit=5)
            assert [event["kind"] for event in events[:2]] == ["switch", "cooldown"]
            assert events[0]["from_source"] == "src_primary1"
            assert events[0]["to_source"] == "src_backup01"
            assert events[0]["reason"] == "quota_exhausted"
            assert events[0]["billing_note"] == "entered_metered"
            assert events[1]["reason"] == "quota_exhausted"

            clock[0] += timedelta(minutes=6)
            recovered_status, recovered_body = await _post_turn(
                await router.resolve("claude", _requested_model("claude"))
            )
            assert recovered_status == 200
            assert recovered_body == b'{"recovered":true}'
            assert adapter.invocations[-1][0] == "src_primary1"
            recovered = store.load().sources[0]
            assert recovered.state.status == "standby"
            assert recovered.state.retry_at is None
            recovered_events = service.list_events(limit=5)
            assert recovered_events[0]["kind"] == "recover"
            assert recovered_events[0]["reason"] == "recovery"
            assert recovered_events[0]["to_source"] == "src_primary1"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_effort_001_undeclared_effort_is_omitted_and_turn_completes(
    tmp_path: Path,
) -> None:
    """MH-EFFORT-001: an undeclared effort is omitted from the exact-hop request and the turn still completes."""

    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}')]
        )
        source = _source("src_codex001")
        store = MemoryStore(_config(source))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 8, 26, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            launch = await router.resolve("codex", _requested_model("codex"))
            status, body = await _post_turn(
                launch,
                endpoint="responses",
                payload={
                    "model": launch.runtime_model,
                    "messages": [],
                    "stream": False,
                    "reasoning": {"effort": "low", "summary": "auto"},
                },
            )
            assert status == 200
            assert body == b'{"ok":true}'
            assert len(adapter.requests) == 1
            assert "reasoning_effort" not in adapter.requests[0]
            assert adapter.requests[0]["reasoning"] == {"summary": "auto"}
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_turn_gateway_nonstream_buffer_surfaces_terminal_failure(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        adapter = AdapterBoundaryFake(
            [
                AdapterResult(
                    RawOutcomeKind.HTTP_ERROR,
                    status=429,
                    code="rate_limited",
                    body=b'{"partial":',
                    stream_started=True,
                ),
            ]
        )
        store = MemoryStore(_config(_source("src_primary1")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        router = ModelHubRuntimeRouter(service=service, turn_gateway=gateway)
        try:
            status, body = await _post_turn(
                await router.resolve("claude", _requested_model("claude")),
            )
            assert status == 429
            assert json.loads(body)["error"]["code"] == "stream_interrupted"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_turn_gateway_tokens_are_bound_to_backend_origin(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = AdapterBoundaryFake([AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}')])
        store = MemoryStore(_config(_source("src_primary1")))
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
                    json={"model": _requested_model("claude"), "messages": []},
                ) as response:
                    assert response.status == 401
            assert adapter.invocations == []
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_turn_gateway_preserves_only_protocol_capability_headers(tmp_path: Path) -> None:
    async def exercise() -> None:
        adapter = AdapterBoundaryFake([AdapterResult(RawOutcomeKind.SUCCESS, status=200, body=b'{"ok":true}')])
        store = MemoryStore(_config(_source("src_primary1")))
        service = _service(
            tmp_path,
            store,
            adapter,
            now=lambda: datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        gateway = ModelHubTurnGateway(service)
        try:
            base_url, token = await gateway.endpoint("claude")
            async with aiohttp.ClientSession(trust_env=False) as client:
                async with client.post(
                    f"{base_url}/v1/messages",
                    headers={
                        "x-api-key": token,
                        "Authorization": "Bearer upstream-must-not-cross",
                        "anthropic-beta": "interleaved-thinking",
                    },
                    json={"model": _requested_model("claude"), "messages": []},
                ) as response:
                    assert response.status == 200
            request = adapter.requests[0]
            assert getattr(request, "headers") == {
                "anthropic-beta": "interleaved-thinking",
            }
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_evt_002_switch_events_survive_router_and_service_restart(tmp_path: Path) -> None:
    """MH-EVT-002: persisted cooldown causality survives process-local state loss."""

    async def exercise() -> None:
        clock = datetime(2026, 7, 25, tzinfo=timezone.utc)
        native = _source("src_native01", channel="native_cli", kind="subscription")
        backup = _source("src_backup01")
        store = MemoryStore(_config(native, backup))
        first_adapter = AdapterBoundaryFake([])
        first_service = _service(tmp_path, store, first_adapter, now=lambda: clock)
        first_gateway = ModelHubTurnGateway(first_service)
        first_router = ModelHubRuntimeRouter(
            service=first_service,
            turn_gateway=first_gateway,
            native_cli_ready=lambda backend: True,
        )
        launch = await first_router.resolve("codex", _requested_model("codex"))
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
            fallback = await second_router.resolve(
                "codex",
                _requested_model("codex"),
            )
            assert fallback.channel == "hub"
            persisted = BoundedEventLog(tmp_path / "events.json").list(limit=10)
            assert [event["kind"] for event in persisted[:2]] == [
                "switch",
                "cooldown",
            ]
            assert persisted[0]["from_source"] == "src_native01"
            assert persisted[0]["to_source"] == "src_backup01"
        finally:
            await second_gateway.close()

    asyncio.run(exercise())


def _restart_runtime(
    tmp_path: Path,
    store: MemoryStore,
    clock: datetime,
) -> tuple[ModelHubService, ModelHubTurnGateway, ModelHubRuntimeRouter]:
    """Build one runtime over durable state, as a process restart would."""

    service = _service(tmp_path, store, AdapterBoundaryFake([]), now=lambda: clock)
    gateway = ModelHubTurnGateway(service)
    router = ModelHubRuntimeRouter(
        service=service,
        turn_gateway=gateway,
        native_cli_ready=lambda backend: True,
    )
    return service, gateway, router


def test_mh_set_001_restored_attempt_cannot_settle_health_after_a_newer_attempt(
    tmp_path: Path,
) -> None:
    """MH-SET-001: a pre-restart attempt never overwrites a newer attempt's Source."""

    async def exercise() -> None:
        clock = datetime(2026, 7, 25, tzinfo=timezone.utc)
        native = _source("src_native01", channel="native_cli", kind="subscription")
        store = MemoryStore(_config(native, _source("src_backup01")))

        first_service, first_gateway, first_router = _restart_runtime(
            tmp_path, store, clock
        )
        launch = await first_router.resolve(
            "codex",
            _requested_model("codex"),
            process_scope="/restored",
            turn_id="turn_pre_restart",
        )
        assert launch.channel == "native_cli"
        assert launch.settlement_generation is not None
        identity = persisted_launch_identity(launch)
        # The pre-restart turn already recorded its terminal history on disk.
        first_gateway.correlation.settle(
            "turn_pre_restart",
            settled_by=SETTLED_BY_TERMINAL_RESULT,
            ts=clock.isoformat(),
        )
        history_before_restart = first_service.provenance.get("turn_pre_restart")
        assert history_before_restart is not None
        await first_gateway.close()

        service, gateway, router = _restart_runtime(tmp_path, store, clock)
        try:
            context = SimpleNamespace(
                platform_specific={"turn_token": "turn_pre_restart"}
            )
            restored = bind_persisted_launch(context, identity)
            assert restored is not None
            assert restored.settlement_generation == PRE_ATTEMPT_SETTLEMENT_GENERATION

            newer = await router.resolve(
                "codex",
                _requested_model("codex"),
                process_scope="/newer",
                turn_id="turn_after_restart",
            )
            assert newer.source_id == native.id
            assert newer.settlement_generation is not None
            assert restored.settlement_generation < newer.settlement_generation

            # The restored context's late quota failure is not fresh enough to
            # judge the Source the newer attempt is using.
            assert await router.record_native_failure(
                context,
                "usage quota exceeded",
            ) is False

            current = next(
                source for source in store.load().sources if source.id == native.id
            )
            assert current.state.status == native.state.status == "standby"
            assert current.state.retry_at is None
            assert current.state.detail_key is None
            assert BoundedEventLog(tmp_path / "events.json").list(limit=10) == []
            # Refusing the health write is not punitive: durable history stays.
            assert service.provenance.get("turn_pre_restart") == history_before_restart

            # The live attempt still owns its Source and settles normally.
            newer_context = SimpleNamespace(
                platform_specific={"turn_token": "turn_after_restart"}
            )
            bind_launch(newer_context, newer)
            assert await router.record_native_failure(
                newer_context,
                "usage quota exceeded",
            ) is True
            assert next(
                source for source in store.load().sources if source.id == native.id
            ).state.status == "cooldown"
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_set_002_restored_attempt_settles_a_source_this_runtime_left_alone(
    tmp_path: Path,
) -> None:
    """MH-SET-002: a restored attempt still cools a Source no newer attempt claimed."""

    async def exercise() -> None:
        clock = datetime(2026, 7, 25, tzinfo=timezone.utc)
        native = _source("src_native01", channel="native_cli", kind="subscription")
        store = MemoryStore(_config(native, _source("src_backup01")))

        _first_service, first_gateway, first_router = _restart_runtime(
            tmp_path, store, clock
        )
        launch = await first_router.resolve(
            "codex",
            _requested_model("codex"),
            process_scope="/restored",
            turn_id="turn_pre_restart",
        )
        identity = persisted_launch_identity(launch)
        await first_gateway.close()

        _service_after_restart, gateway, router = _restart_runtime(
            tmp_path, store, clock
        )
        try:
            context = SimpleNamespace(
                platform_specific={"turn_token": "turn_pre_restart"}
            )
            restored = bind_persisted_launch(context, identity)
            assert restored is not None
            assert restored.settlement_generation == PRE_ATTEMPT_SETTLEMENT_GENERATION

            assert await router.record_native_failure(
                context,
                "usage quota exceeded",
            ) is True

            current = next(
                source for source in store.load().sources if source.id == native.id
            )
            assert current.state.status == "cooldown"
            assert current.state.detail_key == "models.source.cooldown.quota_exhausted"
            assert [
                event["kind"]
                for event in BoundedEventLog(tmp_path / "events.json").list(limit=10)
            ] == ["cooldown"]
        finally:
            await gateway.close()

    asyncio.run(exercise())


def test_mh_set_003_released_launch_identity_shape_restores_without_certifying_freshness(
    tmp_path: Path,
) -> None:
    """MH-SET-003: any released on-disk launch identity loads into a stale-safe attempt."""

    async def exercise() -> None:
        clock = datetime(2026, 7, 25, tzinfo=timezone.utc)
        native = _source("src_native01", channel="native_cli", kind="subscription")
        store = MemoryStore(_config(native, _source("src_backup01")))
        _service_instance, gateway, router = _restart_runtime(tmp_path, store, clock)
        try:
            newer = await router.resolve(
                "codex",
                _requested_model("codex"),
                process_scope="/newer",
                turn_id="turn_after_restart",
            )
            assert newer.source_id == native.id

            context = SimpleNamespace(
                platform_specific={"turn_token": "turn_pre_restart"}
            )
            # A released identity carries no generation, and a payload that does
            # carry one still cannot certify itself against this runtime's ledger.
            restored = bind_persisted_launch(
                context,
                {
                    "backend": "codex",
                    "channel": "native_cli",
                    "source_id": native.id,
                    "target_model": _requested_model("codex"),
                    "settlement_generation": 4096,
                },
            )

            assert restored is not None
            assert restored.settlement_generation == PRE_ATTEMPT_SETTLEMENT_GENERATION
            assert await router.record_native_failure(
                context,
                "usage quota exceeded",
            ) is False
            assert next(
                source for source in store.load().sources if source.id == native.id
            ).state.status == "standby"
            assert BoundedEventLog(tmp_path / "events.json").list(limit=10) == []
        finally:
            await gateway.close()

    asyncio.run(exercise())
