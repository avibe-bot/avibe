"""Cache facts at their owning boundary: request admission, not response commit.

The previous C-9 clock alone cannot explain expiry after a long response, and
nothing before PR3b preserves prefix comparisons or TTL write decompositions.
"""

from dataclasses import replace

import pytest

from core.agent_core.ai.anthropic import _anthropic_usage
from core.agent_core.ai.openai_responses import _responses_usage
from core.agent_core.ai.provider import Done, ModelRequest, ProviderError
from core.agent_core.agent.loop import RetryPolicy
from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.harness.context import ContextConfig, cache_cold
from core.agent_core.harness.store import ContextEntry
from core.agent_core.messages import Usage, usage_from_dict, usage_to_dict
from core.agent_core.tools.base import ToolSpec
from tests.agent_core.agent.test_compaction import Model, make_agent, run
from tests.agent_core.fakes import ENDPOINT, InMemoryTranscriptStore, assistant, user


def request():
    return ModelRequest(endpoint=ENDPOINT, system="fixed instructions", tools=(ToolSpec("read", "Read", {}),),
                        messages=(user("hello"),), max_tokens=100)


async def test_request_start_clock_survives_restart_without_changing_the_c9_response_end_clock():
    now = [100.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])

    def long_response(_):
        now[0] = 500.0
        return [Done(replace(assistant(), usage=Usage(input_tokens=100, output_tokens=10)))]

    model = Model(conversation=[long_response, [Done(replace(assistant(), usage=Usage(cache_write_tokens=100)))]],
                  checkpoint=[])
    config = ContextConfig(clock=lambda: now[0])
    await run(make_agent(model, store=store, context=config, system="fixed instructions"), row="first")
    first = next(row for row in store.rows["session"] if row.kind == "response")
    assert first.payload["cache"]["started_at"] == 100
    now[0] = 510.0
    assert not cache_cold(model.requests[0], store.rows["session"], now=now[0])
    await run(make_agent(model, store=store, context=config, system="fixed instructions"), row="after-restart")
    cache = [row.payload["cache"] for row in store.rows["session"] if row.kind == "response"][-1]
    assert cache["idle_s"] == 410
    assert cache["lifetime_s"] == 300
    assert cache["expected_read"] == 0
    assert cache["reusable_tokens"] == 100
    assert cache["expected_read_basis"] == "expired"
    assert cache["writes_by_ttl"] == {"unknown": 100}


@pytest.mark.parametrize("change,cause", [
    ("none", None), ("tools", "tools_changed"), ("route", "route_changed"),
    ("context_edit", "tool_results_cleared"), ("compaction", "checkpoint"),
])
def test_fingerprints_and_context_facts_explain_active_changes(change, cause):
    tracker = CacheTracker()
    original = request()
    first = tracker.begin(original, (), now=100)
    tracker.finish(first, replace(assistant(), usage=Usage(input_tokens=100)))
    candidate, rows = original, []
    if change == "tools":
        candidate = replace(original, tools=())
    elif change == "route":
        candidate = replace(original, endpoint=replace(ENDPOINT, source_id="new-source"))
    elif cause:
        rows = [ContextEntry("session", 1, change, "change", payload={})]
    after = tracker.begin(candidate, rows, now=101)
    assert {item["kind"] for item in after["causes"]} == ({cause} if cause else set())
    if change == "none":
        assert after["expected_read"] == 100 and after["expected_read_basis"] == "reported_input"
    if change in {"tools", "route"}:
        assert after["expected_read"] == 0
    # A different provider/model's most recent request must not explain this route.
    if change == "none":
        other = replace(original, endpoint=replace(original.endpoint, model_id="other-model"))
        other_start = tracker.begin(other, (), now=102)
        other_origin = replace(assistant().origin, model="other-model")
        tracker.finish(other_start, replace(assistant(), origin=other_origin, usage=Usage(input_tokens=100)))
        same_route = tracker.begin(original, (), now=103)
        assert {item["kind"] for item in same_route["causes"]} == set()
    # Fingerprints contain no prompt or tool text.
    assert all("fixed instructions" not in part[0] for part in after["prefix"])


def test_ttl_details_are_transient_usage_metadata_and_have_one_persisted_source_of_truth():
    initial = _anthropic_usage({"input_tokens": 10, "cache_creation_input_tokens": 15,
                                "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 10}})
    later = _anthropic_usage({"output_tokens": 3}, current=initial)
    assert later.cache_writes_by_ttl == {"5m": 5, "1h": 10}
    assert later.cache_write_tokens == 15
    canonical = usage_to_dict(later)
    assert canonical == {"input_tokens": 10, "output_tokens": 3, "cache_read_tokens": 0, "cache_write_tokens": 15}
    assert usage_from_dict(canonical).cache_writes_by_ttl is None
    tracker = CacheTracker()
    cache = tracker.begin(request(), (), now=100)
    tracker.finish(cache, replace(assistant(), usage=later))
    assert cache["writes_by_ttl"] == {"5m": 5, "1h": 10}


@pytest.mark.parametrize("details,expected", [
    ({"ttl_30m_tokens": 10, "ttl_12h_tokens": 20}, {"30m": 10, "12h": 20}),
    ({"ttl_30m_tokens": 10}, {"30m": 10}),
    ({"ttl_30m_tokens": 10, "ttl_12h_tokens": 20, "future_ttl_tokens": 1}, {"30m": 10, "12h": 20}),
    ({"ttl_30m_tokens": 100}, None),
    ({"ttl_12h_tokens": "invalid"}, None),
    ([], None),
])
def test_bad_or_incomplete_optional_ttl_details_do_not_invent_counts(details, expected):
    usage = _responses_usage({"input_tokens": 100, "input_tokens_details": {
        "cached_tokens": 50, "cache_write_tokens": 30, "cache_write_tokens_details": details,
    }})
    assert usage.cache_writes_by_ttl == expected
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (20, 50, 30)


def test_missing_openai_write_total_is_marked_incomplete_even_when_ttl_details_are_valid():
    usage = _responses_usage({"input_tokens": 100, "input_tokens_details": {
        "cached_tokens": 50,
        "cache_write_tokens_details": {"ttl_30m_tokens": 10, "ttl_12h_tokens": 20},
    }})
    assert usage.cache_write_tokens == 0
    assert usage.input_tokens == 50
    assert usage.cache_writes_by_ttl == {"30m": 10, "12h": 20}
    assert usage.cache_write_total_reported is False
    assert usage_to_dict(usage)["cache_write_tokens"] == 0
    tracker = CacheTracker()
    cache = tracker.begin(request(), (), now=100)
    tracker.finish(cache, replace(assistant(), usage=usage))
    assert cache["writes_by_ttl"] == {"30m": 10, "12h": 20}
    assert cache["cache_write_basis"] == "incomplete_total"


def test_missing_openai_write_total_with_partial_details_does_not_fail_the_response():
    usage = _responses_usage({"input_tokens": 100, "input_tokens_details": {
        "cached_tokens": 50, "cache_write_tokens_details": {"ttl_30m_tokens": 10},
    }})
    assert usage.input_tokens == 50
    assert usage.cache_write_tokens == 0
    assert usage.cache_write_total_reported is False


def test_corrupt_optional_clock_is_ignored():
    tracker = CacheTracker()
    for bad in (None, [], {}, {"version": 1, "started_at": 10**400}):
        tracker.restore((), (bad,))
    assert tracker.begin(request(), (), now=100)["idle_s"] is None


def test_sampled_prefix_is_bounded_but_keeps_exact_reuse_and_fork_clock():
    # Long histories used to append every message hash to every response row.
    tracker = CacheTracker()
    original = replace(request(), messages=tuple(user(f"message {i}") for i in range(600)))
    first = tracker.begin(original, (), now=100)
    tracker.finish(first, replace(assistant(), usage=Usage(input_tokens=10000)))
    assert len(first["prefix"]) <= 32
    assert len(__import__("json").dumps(first["prefix"])) < 2000
    restored = CacheTracker()
    restored.restore((ContextEntry("parent", 1, "response", "row", payload={"cache": first}),), ())
    following = replace(original, messages=(*original.messages, user("new tail")))
    warm = restored.begin(following, (), now=150, fork=True)
    assert warm["idle_s"] == 50
    assert warm["expected_read"] == 10000
    assert warm["expected_read_basis"] == "reported_input"
    assert warm["causes"] == [{"kind": "fork_start"}]
    changed = replace(original, messages=(*original.messages[:300], user("changed"), *original.messages[301:]))
    partial = restored.begin(changed, (), now=151)
    assert 0 < partial["expected_read"] < 10000
    assert partial["expected_read_basis"] == "estimated_prefix"


async def test_usage_less_retry_preserves_expiry_and_prompt_cause_until_a_billed_request():
    # Audit-only failures used to refresh the clock and consume the pending reason.
    now = [100.0]
    store = InMemoryTranscriptStore(clock=lambda: now[0])
    model = Model(conversation=[
        [Done(replace(assistant(), usage=Usage(input_tokens=2000)))],
        [ProviderError("rate_limit", "busy", True, status=429)],
        [Done(replace(assistant(), usage=Usage(cache_write_tokens=2000)))],
    ])
    context = ContextConfig(clock=lambda: now[0])
    await run(make_agent(model, store=store, context=context), row="initial")
    now[0] = 500
    restarted = make_agent(model, store=store, context=context, retry=RetryPolicy(initial_delay_s=0))
    restarted.cache_causes = ({"kind": "prompt_applied", "reason": "tier1"},)
    await run(restarted, row="retry")
    cache = [row.payload["cache"] for row in store.rows["session"] if row.kind == "response"][-1]
    assert cache["idle_s"] == 400
    assert cache["expected_read_basis"] == "expired"
    assert {item["reason"] for item in cache["causes"] if item["kind"] == "prompt_applied"} == {"tier1"}
    assert restarted.cache_causes == ()
