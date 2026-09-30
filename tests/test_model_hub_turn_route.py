"""One Model Hub turn stays on the hop that serves it until that hop fails."""

from __future__ import annotations

import asyncio
import inspect
import itertools
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
    hops_passed,
    resolve_model_hub_turn,
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
SERVER_ERROR = ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=503, error_code="server_error")
INVALID_REQUEST = ScenarioCallResult(RawOutcomeKind.HTTP_ERROR, status=400, error_code="invalid_request_error")


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


def _hop(name):
    if name == "b-other-model":
        return ("src_turnrouteb", "another-upstream")
    return (f"src_turnroute{name}", UPSTREAM)


@pytest.mark.parametrize(("left", "cooling", "expected"), [
    ("", "", "abc"),
    ("a", "", "bca"),
    ("ab", "", "cab"),
    # A hop the turn has not left but cannot run moves the walk forward.
    ("a", "b", "ca"),
    # Hops the turn left stay reachable, last, so the order never fails a servable request.
    ("a", "bc", "a"),
    # Once the turn has left every hop, the last-resort walk uses route order.
    ("abc", "", "abc"),
    ("cb", "", "abc"),
    # Hops outside the current route play no part.
    ("d", "", "abc"),
    ("b-other-model", "", "abc"),
])
def test_turn_walk_tries_hops_the_turn_has_not_left_first(left, cooling, expected):
    """MH-ROUTING-014: the left set reorders the walk without changing its membership."""
    resolution = _route_resolution({name: "cooldown" for name in cooling})
    left_hops = frozenset([_hop(left)] if left in {"d", "b-other-model"} else map(_hop, left))
    walked = turn_ordered_candidate_hops(resolution, left_hops)
    assert [hop.source_id[-1] for hop in walked] == list(expected)
    assert {hop.source_id for hop in walked} == {hop.source_id for hop in resolution.candidate_hops}


@pytest.mark.parametrize(("left", "served", "passed"), [
    ("", "a", ""),
    ("", "c", "ab"),
    ("a", "b", ""),
    ("a", "a", "bc"),
    ("a", "d", ""),
])
def test_hops_a_request_passed_before_it_was_served(left, served, passed):
    route = tuple(_hop(name) for name in "abc")
    assert hops_passed(route, frozenset(map(_hop, left)), _hop(served)) == tuple(map(_hop, passed))


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
        for name in "abc"
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


def _gate_invocations(runtime, *counts):
    """Hold each listed upstream call, by its position in call order, until released."""

    gates = {count: asyncio.Event() for count in counts}
    invoke = runtime.adapter.invoke

    async def gated_invoke(*args, **kwargs):
        handle = await invoke(*args, **kwargs)
        gate = gates.get(len(runtime.adapter.invocations))
        if gate is not None:
            await gate.wait()
        return handle

    runtime.adapter.invoke = gated_invoke
    return gates


async def _invoked(runtime, count):
    async def reached():
        while len(runtime.adapter.invocations) < count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(reached(), timeout=5)


def _served_by(runtime):
    return runtime.adapter.invocations[-1][0][-1]


async def test_concurrent_first_requests_never_return_the_turn_to_a_left_hop(runtime):
    """MH-ROUTING-014: a request served before a peer fell past its hop cannot hold the turn there."""
    runtime.adapter.invoke_results.extend([SUCCESS, RATE_LIMITED, SUCCESS, SUCCESS, SUCCESS])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-race-first")
    gates = _gate_invocations(runtime, 1, 3)
    # Both requests start before either finishes; the one served by A finishes first.
    first = asyncio.create_task(_post(turn))
    await _invoked(runtime, 1)
    second = asyncio.create_task(_post(turn))
    await _invoked(runtime, 3)
    gates[1].set()
    assert (await asyncio.wait_for(first, timeout=10))[0] == 200
    gates[3].set()
    assert (await asyncio.wait_for(second, timeout=10))[0] == 200
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations] == ["a", "a", "b"]

    runtime.clock.elapsed += 3600
    for _ in range(2):
        assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
        assert _served_by(runtime) == "b"


async def test_slow_completion_after_a_wrap_does_not_move_the_turn(runtime):
    """MH-ROUTING-014: a request admitted before the turn left its hop changes nothing when it finishes late."""
    runtime.adapter.invoke_results.extend([
        SUCCESS,  # 1: the turn starts on A
        RATE_LIMITED, SUCCESS,  # 2-3: the slow request fails on A and is held on B
        SUCCESS,  # 4: a peer is served by B
        SERVER_ERROR, SERVER_ERROR, SUCCESS,  # 5-7: B and C fail, so a request wraps to the recovered A
        SUCCESS, SUCCESS,
    ])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-race-wrap")
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    gates = _gate_invocations(runtime, 3)
    slow = asyncio.create_task(_post(turn))
    await _invoked(runtime, 3)
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    assert _served_by(runtime) == "b"
    runtime.clock.elapsed += 120
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations[4:]] == ["b", "c", "a"]
    gates[3].set()
    assert (await asyncio.wait_for(slow, timeout=10))[0] == 200

    # The turn has left every hop, so it walks in route order, and the late
    # completion on B does not move it back to B.
    runtime.clock.elapsed += 3600
    for _ in range(2):
        assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
        assert _served_by(runtime) == "a"


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
async def test_concurrent_requests_converge_in_any_completion_order(runtime, order):
    """MH-ROUTING-014: the hops a turn has left do not depend on which request finishes first."""
    runtime.adapter.invoke_results.extend([
        SUCCESS,  # 1: the first request is served by A
        RATE_LIMITED, SUCCESS,  # 2-3: the second fails on A and is served by B
        SERVER_ERROR, SUCCESS,  # 4-5: the third starts after A was left, fails on B, is served by C
        SUCCESS, SUCCESS,
    ])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-permutation")
    gates = _gate_invocations(runtime, 1, 3, 5)
    requests = []
    for reached in (1, 3, 5):
        requests.append(asyncio.create_task(_post(turn)))
        await _invoked(runtime, reached)
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations] == ["a", "a", "b", "b", "c"]
    for index in order:
        gates[(1, 3, 5)[index]].set()
        assert (await asyncio.wait_for(requests[index], timeout=10))[0] == 200

    runtime.clock.elapsed += 3600
    for _ in range(2):
        assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
        assert _served_by(runtime) == "c"


async def test_a_request_class_failure_does_not_move_the_turn(runtime):
    """MH-ROUTING-014: only a Source-level failure that makes fallback move on leaves a hop."""
    runtime.adapter.invoke_results.extend([SUCCESS, INVALID_REQUEST, SUCCESS])
    turn = await runtime.router.resolve("codex", MODELS[0], process_scope=runtime.cwd, turn_id="turn-bad-request")
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    status, _body = await asyncio.wait_for(_post(turn), timeout=10)
    assert status == 400
    assert (await asyncio.wait_for(_post(turn), timeout=10))[0] == 200
    assert [invocation[0][-1] for invocation in runtime.adapter.invocations] == ["a", "a", "a"]
