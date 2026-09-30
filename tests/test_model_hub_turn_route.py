"""One Model Hub turn stays on the hop that serves it until that hop fails."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import pytest

from config.v2_config import (
    ModelHubBackendModelConfig,
    ModelHubRouteConfig,
    ModelHubRouteHopConfig,
)
from core.handlers.model_hub.adapter import RawOutcomeKind
from core.handlers.model_hub.resolver import (
    resolve_model_hub_turn,
    turn_hop_advances,
    turn_ordered_candidate_hops,
)
from core.handlers.model_hub.retry import RecoveryPolicy
from core.handlers.model_hub.turn_gateway import ModelHubTurnGateway
from core.run_settlement import SETTLED_BY_TERMINAL_RESULT
from modules.agents.model_hub import ModelHubRuntimeRouter
from tests.scenario_harness.model_hub import (
    MemoryModelHubStore,
    ModelHubScenarioAdapter,
    ScenarioCallResult,
    config_with_sources,
    service_for,
    source,
)


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
METADATA = "x-codex-turn-metadata"
UPSTREAM = "same-upstream"
MODELS = ("主模型", "review-model")
SUCCESS = ScenarioCallResult(
    RawOutcomeKind.SUCCESS, status=200,
    body=b'{"id":"resp_fixture","status":"completed","output":[]}',
)
RATE_LIMITED = ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=429, error_code="rate_limit_exceeded")


def _route_resolution(statuses: dict[str, str]):
    sources = [
        source(
            f"src_turnroute{name}", [UPSTREAM], vendor="openai", protocol="openai_responses",
            status=statuses.get(name, "standby"),
            retry_at=(NOW + timedelta(minutes=5)).isoformat() if statuses.get(name) == "cooldown" else None,
        )
        for name in "abc"
    ]
    config = config_with_sources(sources, backend="codex", hops=[(item.id, UPSTREAM) for item in sources])
    model = next(iter(config.agents["codex"].routes))
    return resolve_model_hub_turn(config, "codex", model, now=NOW)


@pytest.mark.parametrize(("turn_hop", "cooling", "expected"), [
    (None, "", "abc"),
    ("a", "", "abc"),
    ("b", "", "bca"),
    ("c", "", "cab"),
    # A turn hop that cannot run moves the walk forward, not back.
    ("b", "b", "ca"),
    # Earlier hops stay reachable, last, so pinning never fails a servable request.
    ("b", "bc", "a"),
    # A hop that left the effective route no longer applies.
    ("d", "", "abc"),
    ("b-other-model", "", "abc"),
])
def test_turn_walk_starts_at_the_turn_hop_and_wraps(turn_hop, cooling, expected):
    """MH-ROUTING-014: the turn hop rotates the walk without changing its membership."""
    resolution = _route_resolution({name: "cooldown" for name in cooling})
    pinned = None
    if turn_hop == "b-other-model":
        pinned = ("src_turnrouteb", "another-upstream")
    elif turn_hop is not None:
        pinned = (f"src_turnroute{turn_hop}", UPSTREAM)
    walked = turn_ordered_candidate_hops(resolution, pinned)
    assert [hop.source_id[-1] for hop in walked] == list(expected)
    assert {hop.source_id for hop in walked} == {hop.source_id for hop in resolution.candidate_hops}


def _hop(name):
    return None if name is None else (f"src_turnroute{name}", UPSTREAM)


@pytest.mark.parametrize(("start", "current", "served", "advances"), [
    # The first request of a turn sets the turn hop.
    (None, None, "a", True),
    # Two requests that both started before any turn hop: the one that passed
    # the peer's hop moves the turn on; the one served before it cannot move it back.
    (None, "a", "b", True),
    (None, "b", "a", False),
    # Requests that started at the turn hop move it only forward, wrapping around.
    ("b", "b", "c", True),
    ("b", "b", "a", True),
    ("b", "a", "c", False),
    ("b", "c", "a", True),
    ("b", "b", "b", False),
    # A turn hop that left the route no longer holds the turn.
    ("b", "d", "a", True),
])
def test_turn_hop_moves_only_forward(start, current, served, advances):
    """MH-ROUTING-014: however concurrent requests finish, the turn hop never moves back."""
    route = tuple(_hop(name) for name in "abc")
    assert turn_hop_advances(route, _hop(start), _hop(current), _hop(served)) is advances


class _Clock:
    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> datetime:
        return NOW + timedelta(seconds=self.elapsed)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_MODEL_HUB_ENABLED", "1")
    sources = [
        source(f"src_turnroute{name}", [UPSTREAM], vendor="openai", protocol="openai_responses")
        for name in "ab"
    ]
    config = config_with_sources(sources, backend="codex")
    config.agents["codex"].models = [ModelHubBackendModelConfig(id=model) for model in MODELS]
    # Two menu models on the same exact hops: only the requested model keys a turn hop.
    config.agents["codex"].routes = {
        model: ModelHubRouteConfig(hops=tuple(ModelHubRouteHopConfig(item.id, UPSTREAM) for item in sources))
        for model in MODELS
    }
    clock = _Clock()
    adapter = ModelHubScenarioAdapter()
    service = service_for(tmp_path, MemoryModelHubStore(config), adapter, now=clock.now)
    service.recovery = RecoveryPolicy(now=clock.now, monotonic=lambda: clock.elapsed, jitter=lambda low, high: low)
    gateway = ModelHubTurnGateway(service)
    fixture = SimpleNamespace(
        adapter=adapter, service=service, gateway=gateway, clock=clock, cwd=str(tmp_path),
        router=ModelHubRuntimeRouter(service=service, turn_gateway=gateway),
    )
    try:
        yield fixture
    finally:
        await gateway.close()


async def _post(launch, model=None):
    runtime_model = model or launch.runtime_model
    metadata = json.dumps(launch.gateway_request_metadata)
    async with aiohttp.ClientSession(trust_env=False) as client:
        async with client.post(
            f"{launch.gateway_base_url}/v1/responses",
            headers={"Authorization": f"Bearer {launch.gateway_token}", METADATA: metadata},
            json={
                "model": runtime_model, "input": "继续", "stream": False,
                "client_metadata": {METADATA: metadata},
            },
        ) as response:
            # A walk that disagrees with admission revalidation retries forever.
            return response.status, await asyncio.wait_for(response.read(), timeout=5)


async def _settle(runtime, turn_id):
    completion = runtime.router.settle_turn(turn_id, settled_by=SETTLED_BY_TERMINAL_RESULT, ts=NOW.isoformat())
    if inspect.isawaitable(completion):
        await completion


async def test_turn_stays_on_its_hop_until_that_hop_fails(runtime):
    """MH-ROUTING-014: a recovered earlier hop never takes a running turn back."""
    runtime.adapter.invoke_results.extend([SUCCESS, RATE_LIMITED, SUCCESS, SUCCESS, SUCCESS, SUCCESS, SUCCESS])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-one")
    served = []

    async def post(model=None):
        status, body = await asyncio.wait_for(_post(turn, model), timeout=10)
        assert status == 200, body
        served.append(runtime.adapter.invocations[-1][0][-1])

    await post()
    await post()
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations] == ["a", "a", "b"]
    runtime.clock.elapsed += 3600
    await post()
    # Another model of the same turn walks its own route from the top.
    await post(MODELS[1])
    # That model's recovery of the earlier hop does not move this model back.
    await post()
    assert served == ["a", "b", "b", "a", "b"]
    await _settle(runtime, "turn-one")
    provenance = runtime.service.get_turn_provenance("turn-one")
    assert provenance["served"]["source_id"] == "src_turnrouteb"
    assert [attempt["source_id"] for attempt in provenance["failed_attempts"]] == ["src_turnroutea"]

    # The turn boundary ends the pin: the next turn starts from the first hop.
    following = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-two")
    await asyncio.wait_for(_post(following), timeout=10)
    assert runtime.adapter.invocations[-1][0] == "src_turnroutea"


async def test_concurrent_first_requests_leave_the_turn_on_the_furthest_hop(runtime):
    """MH-ROUTING-014: a request that fell past a peer's hop moves the turn, whichever finishes first."""
    runtime.adapter.invoke_results.extend([SUCCESS, RATE_LIMITED, SUCCESS, SUCCESS])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-concurrent")
    gates = {1: asyncio.Event(), 3: asyncio.Event()}
    invoke = runtime.adapter.invoke

    async def gated_invoke(*args, **kwargs):
        handle = await invoke(*args, **kwargs)
        gate = gates.get(len(runtime.adapter.invocations))
        if gate is not None:
            await gate.wait()
        return handle

    runtime.adapter.invoke = gated_invoke

    async def invoked(count):
        while len(runtime.adapter.invocations) < count:
            await asyncio.sleep(0.01)

    # Both requests start before either has set the turn hop.
    first = asyncio.create_task(_post(turn))
    await asyncio.wait_for(invoked(1), timeout=5)
    second = asyncio.create_task(_post(turn))
    await asyncio.wait_for(invoked(3), timeout=5)
    # The request served on the first hop finishes first, then the one that fell past it.
    gates[1].set()
    assert (await asyncio.wait_for(first, timeout=10))[0] == 200
    gates[3].set()
    assert (await asyncio.wait_for(second, timeout=10))[0] == 200
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations] == ["a", "a", "b"]

    runtime.clock.elapsed += 3600
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    assert runtime.adapter.invocations[-1][0] == "src_turnrouteb"
