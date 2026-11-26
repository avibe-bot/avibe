"""One Anthropic usage revision feeds ordinary responses, thread creates and discarded sends."""

import json
from dataclasses import replace

import httpx
import pytest

from core.agent_core.ai.anthropic import AnthropicAdapter, _anthropic_usage
from core.agent_core.ai.provider import Done, ThreadObservation
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.cache_metrics import CacheTracker
from core.agent_core.messages import TextBlock, UserMessage
from tests.agent_core.ai.test_anthropic_prompt_cache import SUCCESS, _request


def counters(total=None, **buckets):
    fields = {} if total is None else {"cache_creation_input_tokens": total}
    if buckets:
        fields["cache_creation"] = {f"ephemeral_{ttl}_input_tokens": n for ttl, n in buckets.items()}
    return fields


START = [counters(), counters(0), counters(**{"5m": 5}), counters(5, **{"5m": 5})]
DELTA = [counters(), counters(0), counters(**{"1h": 9}), counters(9, **{"1h": 9})]


@pytest.mark.parametrize("start", range(4))
@pytest.mark.parametrize("delta", range(4))
def test_write_revision_matrix(start, delta):
    usage = _anthropic_usage(DELTA[delta], current=_anthropic_usage(START[start]))
    if delta == 0:
        expected = [(0, False, None), (0, True, None), (5, False, {"5m": 5}), (5, True, {"5m": 5})][start]
    elif delta == 1:
        expected = (0, True, None)
    elif delta == 2:
        expected = (14, False, {"5m": 5, "1h": 9}) if start >= 2 else (9, False, {"1h": 9})
    else:
        expected = (9, True, {"1h": 9})
    assert (usage.cache_write_tokens, usage.cache_write_total_reported, usage.cache_writes_by_ttl) == expected


@pytest.mark.parametrize(
    "revision,expected",
    [
        (counters(15, **{"1h": 9}), (15, True, {"5m": 5, "1h": 9})),
        (counters(10, **{"1h": 9}), (10, True, {"1h": 9})),
        (counters(8, **{"1h": 9}), (8, True, None)),
        (counters(8, **{"1h": 5, "5m": 5}), (8, True, None)),
        (counters(**{"5m": 0}), (0, False, {"5m": 0})),
        (
            {"cache_creation_input_tokens": None, "cache_creation": {"ephemeral_1h_input_tokens": None}},
            (5, True, {"5m": 5}),
        ),
    ],
)
def test_partial_incompatible_and_null_revisions(revision, expected):
    usage = _anthropic_usage(revision, current=_anthropic_usage(START[3]))
    assert (usage.cache_write_tokens, usage.cache_write_total_reported, usage.cache_writes_by_ttl) == expected


@pytest.mark.parametrize("mode", ["ordinary", "create", "fallback"])
@pytest.mark.parametrize(
    "total,expected,basis",
    [
        (9, {"1h": 9}, "reported"),
        (None, {"5m": 5, "1h": 9}, "incomplete_total"),
        (10, {"unknown": 1, "1h": 9}, "reported"),
    ],
)
async def test_http_revision_reaches_usage_and_ttl_accounting(mode, total, expected, basis):
    start = SUCCESS.split('data: {"type":"content_block_start"')[0].replace(
        '"usage":{"input_tokens":1}', '"id":"msg_fixture","usage":' + json.dumps(START[3])
    )
    revision = counters(total, **{"1h": 9})
    delta = "data: " + json.dumps({"type": "message_delta", "delta": {}, "usage": revision}) + "\n\n"
    seen, settled = [], []

    async def settle(row):
        settled.append(dict(row))

    def handle(wire):
        seen.append(json.loads(wire.content).get("thread", {}).get("type", "full"))
        if mode == "fallback" and len(seen) == 1:
            return httpx.Response(200, text=start + delta + "event: error\ndata: interrupted\n\n")
        rest = 'data: {"type":"content_block_start"' + SUCCESS.split('data: {"type":"content_block_start"', 1)[1]
        return httpx.Response(
            200, text=start + rest.replace('"usage":{"output_tokens":1}', '"usage":' + json.dumps(revision))
        )

    req = replace(
        _request(messages=(UserMessage((TextBlock("short"),)),)),
        thread_observation=ThreadObservation(
            "write-revision", settle_discard=settle, ordinary_retry=mode == "ordinary"
        ),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        events = [event async for event in AnthropicAdapter(client).stream(req, CancelToken())]
    assert seen == {"ordinary": ["full"], "create": ["create"], "fallback": ["create", "full"]}[mode]
    assert isinstance(events[-1], Done)
    message = events[-1].message
    tracker = CacheTracker()
    cache = tracker.begin(req, (), now=100)
    tracker.finish(cache, message)
    assert message.usage.cache_write_tokens == sum(expected.values())
    assert (cache["writes_by_ttl"], cache["cache_write_basis"]) == (expected, basis)
    if mode == "fallback":
        assert len(settled) == 1
        assert settled[0]["discarded_usage"]["cache_write_tokens"] == sum(expected.values())
        assert settled[0]["writes_by_ttl"] == {k: v for k, v in expected.items() if k != "unknown"}
        assert settled[0]["cache_write_basis"] == basis
        assert req.thread_observation.attempts[-1]["ordinary_baseline_bytes"] == 0
    else:
        assert not settled
