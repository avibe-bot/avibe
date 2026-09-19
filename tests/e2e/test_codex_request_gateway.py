"""Real Codex consumes the production launch and calls the production gateway."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.handlers.model_hub.adapter import RawOutcomeKind
from modules.agents.codex.agent import CodexAgent
from modules.agents.model_hub import bind_launch
from tests.e2e.drivers.mock_llm_upstream import _responses_stream_frames
from tests.e2e.test_codex_request_metadata import (
    _completed,
    _thread,
    _transport,
    metadata_runtime,  # noqa: F401 -- fixture dependency
)
from tests.e2e.test_model_hub_catalog_consumer import (
    codex_catalog_runtime,  # noqa: F401 -- fixture dependency
    rejected_external_proxy,  # noqa: F401 -- fixture dependency
)
from tests.scenario_harness.model_hub import ScenarioCallResult
from tests.test_codex_request_routing import (
    ALIASES,
    METADATA,
    _launch,
    _settle,
    runtime,  # noqa: F401 -- fixture dependency
)


pytestmark = pytest.mark.e2e_model_hub


async def test_codex_native_gateway_keeps_concurrent_route_ownership(runtime, metadata_runtime):
    """MH-CODEX-ROUTING-002: real launch, native requests, routing and settlement agree."""
    first = await _launch(runtime, "native-first", ALIASES[0])
    second = await _launch(runtime, "native-second", ALIASES[1])
    assert first.fingerprint == second.fingerprint
    runtime.adapter.invoke_results.clear()
    runtime.adapter.invoke_results.extend([
        ScenarioCallResult(
            RawOutcomeKind.SUCCESS, status=200, stream_started=True,
            body=b"".join(_responses_stream_frames("same-upstream")),
        ),
    ] * 2)
    # Only the external engine is doubled. Both actual native requests must
    # reach the actual gateway/service before either inference can complete.
    original_invoke = runtime.adapter.invoke
    admitted = asyncio.Event()
    arrivals = 0

    async def invoke(*args, **kwargs):
        nonlocal arrivals
        on_admitted = kwargs.pop("on_admitted", None)
        if on_admitted is not None:
            on_admitted()
        arrivals += 1
        if arrivals == 2:
            admitted.set()
        await asyncio.wait_for(admitted.wait(), 10)
        return await original_invoke(*args, **kwargs)

    runtime.adapter.invoke = invoke
    async with _transport(metadata_runtime, None, launch=first) as (transport, completed, _notifications):
        process = transport._process
        for_thread = await asyncio.gather(*(
            _thread(transport, launch.runtime_model) for launch in (first, second)
        ))
        agent = object.__new__(CodexAgent)
        agent.controller = SimpleNamespace()
        agent._prompt_state_agent_session_id = Mock(return_value=None)
        agent._resolve_codex_agent_settings = lambda request: (None, request.selected_model, None, None)
        agent._build_input = Mock(return_value=[{"type": "text", "text": "核对同进程两条路由 café"}])
        agent._write_caller_env_script = Mock()
        agent._turn_registry = SimpleNamespace(
            begin_turn_start=Mock(), get_bootstrapped_turn_id=Mock(return_value=None),
            finalize_turn_start_response=Mock(return_value=SimpleNamespace()),
        )
        requests = []
        for index, launch in enumerate((first, second)):
            context = SimpleNamespace(platform_specific={})
            bind_launch(context, launch)
            requests.append(SimpleNamespace(
                context=context, base_session_id=f"session-{index}", session_key=f"session-{index}",
                composite_session_id=f"session-{index}", selected_model=launch.runtime_model,
            ))
        await asyncio.gather(*(
            agent._start_turn(transport, request, thread)
            for request, thread in zip(requests, for_thread, strict=True)
        ))
        await _completed(completed)
        await _completed(completed)
        assert transport._process is process and process.returncode is None
    await _settle(runtime, "native-first")
    await _settle(runtime, "native-second")
    assert arrivals == 2
    for turn, source_id in (("native-first", "src_request01"), ("native-second", "src_request02")):
        record = runtime.service.get_turn_provenance(turn)
        assert record["outcome"] == "served"
        assert record["served"]["source_id"] == source_id
    for request in runtime.adapter.requests:
        assert request["model"] in ALIASES
        metadata = json.loads(request["client_metadata"][METADATA])
        assert "avibe_route_id" not in metadata
        assert "avibe_turn_id" not in metadata
        assert metadata["thread_id"] in for_thread
    assert {model for _source, model, _origin in runtime.adapter.invocations} == {"same-upstream"}
