"""C-1 hosted-tool shapes (``vibey-hosted-tools.md`` §4.2 and §12 items 1-6, 22, 28).

The dataclasses in ``core.agent_core.messages`` are the producer; ``message.schema.json``
is the contract other components read. Known-tool shapes are checked by both the
schema and the reader, and what the producer writes must validate. Stored unknown
tools have a reader-only portable fallback; new writes remain strict.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator
from referencing import Registry, Resource

from core.agent_core.messages import (
    HOSTED_ERROR_CODES,
    MAX_HOSTED_COUNT,
    AssistantMessage,
    Citation,
    FailedAttempt,
    HostedAction,
    HostedCost,
    HostedFallback,
    HostedImage,
    HostedNative,
    HostedRecord,
    HostedResult,
    HostedSource,
    HostedSummary,
    HostedToolBlock,
    Origin,
    ProxyExecution,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    message_from_dict,
    message_to_dict,
)

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "plans" / "agent-core-contracts"


def _validator() -> Draft7Validator:
    resources = {}
    for path in CONTRACTS.glob("*.schema.json"):
        value = json.loads(path.read_text())
        resources[value["$id"]] = Resource.from_contents(value)
    return Draft7Validator({"$ref": "agent-core/message.schema.json"}, registry=Registry().with_resources(resources.items()))


VALIDATOR = _validator()
XAI = Origin(provider="xai", api="openai_responses", model="grok-4.6")
SOURCE = "src_x1a2b3c4d5"
POST = HostedSource(url="https://x.com/avibe/status/1", title="Avibe 本地优先", snippet="Agent OS 🚀 ships.")


def _native(*items: dict) -> HostedNative:
    return HostedNative(source_id=SOURCE, payload=tuple(items))


def _produced() -> list:
    """Messages as the adapters and the tool runtime will build them."""
    answer = "Avibe 是本地优先的 Agent OS。"
    native_turn = AssistantMessage(
        content=(
            HostedToolBlock(
                id="ws_1",
                tool="web_search",
                action=HostedAction(type="search", queries=("Avibe 本地优先", "avibe agent os")),
                record=HostedRecord(status="completed", sources=(HostedSource(url="https://avibe.bot", page_age="2 days"),)),
                native=_native({"type": "web_search_call", "id": "ws_1", "status": "completed"}),
            ),
            HostedToolBlock(
                id="op_1",
                tool="web_search",
                action=HostedAction(type="find_in_page", url="https://avibe.bot", pattern="local-first"),
                record=HostedRecord(status="incomplete", error="the page timed out"),
                native=_native({"type": "web_search_call", "id": "op_1"}),
            ),
            HostedToolBlock(
                id="xs_1",
                tool="x_search",
                action=HostedAction(type="x_search", variant="keyword", query="from:avibe", limit=10, mode="Latest"),
                record=HostedRecord(status="completed"),
                native=_native({"type": "custom_tool_call", "name": "x_keyword_search"}, {"type": "reasoning", "id": "tco_1"}),
            ),
            HostedToolBlock(
                id="xs_2",
                tool="x_search",
                action=HostedAction(type="other", name="x_bookmark_lookup"),
                record=HostedRecord(status="pending"),
                native=_native({"type": "custom_tool_call", "name": "x_bookmark_lookup"}),
            ),
            TextBlock(
                text=answer,
                citations=(
                    Citation(url="https://avibe.bot", title="Avibe", cited_text="本地优先", start=8, end=12),
                    Citation(url="https://x.com/avibe/status/1"),
                ),
            ),
        ),
        origin=XAI,
        stop_reason="stop",
        hosted_summary=HostedSummary(
            counts={"web_search_calls": 2, "x_search_calls": 1, "x_posts_fetched": 10},
            cited_sources=(POST,),
            cost=HostedCost(basis="reported", usd=0.0412),
        ),
    )
    proxy_call = AssistantMessage(
        content=(ToolCallBlock(id="call_7", name="x_search", arguments={"query": "from:avibe", "limit": 5}),),
        origin=Origin(provider="anthropic", api="anthropic", model="claude-opus-5-5"),
        stop_reason="tool_use",
        source_identity="sha256:" + "a1" * 32,
    )
    proxied = ToolResultMessage(
        tool_call_id="call_7",
        tool_name="x_search",
        content=(TextBlock(text="1. Avibe 本地优先 — https://x.com/avibe/status/1"),),
        hosted=HostedResult(
            record=HostedRecord(status="completed", sources=(POST,)),
            # The chain's first executor failed after billing, and its cost is in the sum.
            execution=ProxyExecution(
                executor_origin=XAI,
                executor_source_id=SOURCE,
                executor_billing="metered",
                cost=HostedCost(basis="list", usd=0.03),
                failed_attempts=(FailedAttempt("src_o9p8q7r6s5", "metered", HostedCost(basis="list", usd=0.01)),),
            ),
            # The turn's own Source refused X search natively, so the call ran as its function.
            fallback_from=HostedFallback("src_relaygpt01"),
        ),
    )
    included = ToolResultMessage(
        tool_call_id="call_8",
        tool_name="image_generate",
        content=(TextBlock(text="[image saved: /tmp/media/image-77.png · 1024x1024]"),),
        hosted=HostedResult(
            record=HostedRecord(status="completed", images=(HostedImage(path="/tmp/media/image-77.png", mime="image/webp"),)),
            execution=ProxyExecution(
                executor_origin=Origin(provider="openai", api="openai_responses", model="gpt-image-2"),
                executor_source_id="src_o9p8q7r6s5",
                executor_billing="monthly",
                cost=HostedCost(basis="included"),
                mode="direct",
            ),
            # What the image endpoint rendered: the model, its revised prompt and the resolved size and quality.
            action=HostedAction(
                type="generate", prompt="A lighthouse at dusk", size="1024x1024", quality="high", model="gpt-image-2"
            ),
        ),
    )
    refused_image = ToolResultMessage(
        tool_call_id="call_10",
        tool_name="image_generate",
        content=(TextBlock(text="[image_generate failed: grok-imagine-image takes no images argument]"),),
        is_error=True,
        hosted=HostedResult(
            record=HostedRecord(
                status="failed",
                error="grok-imagine-image takes no images argument",
                error_code="image_param_unsupported",
                images=(),
            ),
            action=HostedAction(type="generate", model="grok-imagine-image"),
        ),
    )
    unexecuted = ToolResultMessage(
        tool_call_id="call_9",
        tool_name="web_search",
        content=(TextBlock(text="[web_search failed: the gateway could not run the call (ConnectError)]"),),
        is_error=True,
        hosted=HostedResult(
            record=HostedRecord(
                status="failed",
                error="the gateway could not run the call (ConnectError)",
                error_code="gateway_unreachable",
            )
        ),
    )
    return [native_turn, proxy_call, proxied, included, unexecuted, refused_image]


@pytest.mark.parametrize(
    "message", _produced(), ids=["native", "proxy-call", "proxied", "included", "unexecuted", "refused-image"]
)
def test_hosted_messages_the_producer_writes_match_the_schema_and_round_trip(message):
    payload = message_to_dict(message)
    VALIDATOR.validate(payload)
    assert message_from_dict(payload) == message
    assert message_to_dict(message_from_dict(payload)) == payload


NATIVE = message_to_dict(_produced()[0])
PROXIED = message_to_dict(_produced()[2])
IMAGE = message_to_dict(_produced()[3])
REFUSED_IMAGE = message_to_dict(_produced()[5])


def _without(payload: dict, path: tuple, field: str) -> dict:
    out = copy.deepcopy(payload)
    target = out
    for step in path:
        target = target[step]
    del target[field]
    return out


MANDATORY = [
    (NATIVE, ("content", 0), "id"),
    (NATIVE, ("content", 0), "tool"),
    (NATIVE, ("content", 0), "action"),
    (NATIVE, ("content", 0), "record"),
    (NATIVE, ("content", 0), "native"),
    (NATIVE, ("content", 0, "native"), "source_id"),
    (NATIVE, ("content", 0, "native"), "payload"),
    (NATIVE, ("content", 0, "action"), "queries"),
    (NATIVE, ("content", 1, "action"), "pattern"),
    (NATIVE, ("content", 2, "action"), "variant"),
    (NATIVE, ("content", 2, "action"), "query"),
    (NATIVE, ("content", 3, "action"), "name"),
    (NATIVE, ("content", 0, "record"), "status"),
    (NATIVE, ("content", 0, "record"), "sources"),
    (NATIVE, ("content", 0, "record", "sources", 0), "url"),
    (IMAGE, ("hosted", "record", "images", 0), "path"),
    (IMAGE, ("hosted", "record", "images", 0), "mime"),
    (NATIVE, ("content", 4, "citations", 0), "url"),
    (NATIVE, ("hosted_summary",), "counts"),
    (NATIVE, ("hosted_summary",), "cited_sources"),
    (NATIVE, ("hosted_summary", "cost"), "basis"),
    (PROXIED, ("hosted",), "record"),
    (PROXIED, ("hosted", "execution"), "mode"),
    (PROXIED, ("hosted", "execution"), "executor_origin"),
    (PROXIED, ("hosted", "execution"), "executor_source_id"),
    (PROXIED, ("hosted", "execution"), "executor_billing"),
    (PROXIED, ("hosted", "execution", "failed_attempts", 0), "source_id"),
    (PROXIED, ("hosted", "execution", "failed_attempts", 0), "billing"),
]


@pytest.mark.parametrize("payload,path,field", MANDATORY, ids=[f"{'.'.join(map(str, p))}.{f}" for _, p, f in MANDATORY])
def test_every_mandatory_hosted_field_is_required_by_schema_and_reader(payload, path, field):
    broken = _without(payload, path, field)
    assert any(error.validator == "required" for error in VALIDATOR.iter_errors(broken) for error in [error, *(error.context or ())])
    with pytest.raises(ValueError):
        message_from_dict(broken)


def _set(payload: dict, path: tuple, value) -> dict:
    out = copy.deepcopy(payload)
    target = out
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    return out


VIOLATIONS = {
    "error-on-completed": _set(NATIVE, ("content", 0, "record", "error"), "boom"),
    "failed-without-error": _without(NATIVE, ("content", 1, "record"), "error"),
    "error-too-long": _set(NATIVE, ("content", 1, "record", "error"), "x" * 401),
    "error-code-without-error": _set(NATIVE, ("content", 0, "record", "error_code"), "image_not_saved"),
    "error-code-not-snake-case": _set(NATIVE, ("content", 1, "record", "error_code"), "Image Not Saved"),
    "empty-error-code": _set(NATIVE, ("content", 1, "record", "error_code"), ""),
    "unknown-status": _set(NATIVE, ("content", 0, "record", "status"), "done"),
    "images-on-web-search": _set(NATIVE, ("content", 0, "record", "images"), []),
    "no-images-on-image-generate": _without(IMAGE, ("hosted", "record"), "images"),
    "image-base64": _set(IMAGE, ("hosted", "record", "images", 0, "b64_json"), "iVBORw0KGgo="),
    "generate-on-web-search": _set(NATIVE, ("content", 0, "action"), {"type": "generate", "prompt": "p"}),
    "generate-on-proxied-search": _set(PROXIED, ("hosted", "action"), {"type": "generate", "prompt": "p"}),
    "search-on-x-search": _set(NATIVE, ("content", 2, "action"), {"type": "search", "queries": ["q"]}),
    "unknown-action": _set(NATIVE, ("content", 0, "action"), {"type": "browse", "url": "https://a"}),
    "field-of-another-action": _set(NATIVE, ("content", 0, "action", "url"), "https://a"),
    "empty-queries": _set(NATIVE, ("content", 0, "action", "queries"), []),
    "x-search-limit-zero": _set(NATIVE, ("content", 2, "action", "limit"), 0),
    "x-search-mode": _set(NATIVE, ("content", 2, "action", "mode"), "latest"),
    "empty-payload": _set(NATIVE, ("content", 0, "native", "payload"), []),
    "payload-not-objects": _set(NATIVE, ("content", 0, "native", "payload"), ["web_search_call"]),
    "native-replay-flag": _set(NATIVE, ("content", 0, "native", "replay"), True),
    "bad-source-id": _set(NATIVE, ("content", 0, "native", "source_id"), "xai-key"),
    "summary-without-hosted-block": _set(message_to_dict(_produced()[1]), ("hosted_summary",), {"counts": {}, "cited_sources": []}),
    "hosted-block-without-summary": _without(NATIVE, (), "hosted_summary"),
    "mixed-hosted-block-without-summary": _without(
        _set(NATIVE, ("content", 0, "tool"), "web_fetch"), (), "hosted_summary"
    ),
    "mixed-known-tool-with-invalid-action": _set(
        _set(NATIVE, ("content", 0, "tool"), "web_fetch"), ("content", 1, "action"), {"type": "fetch"}
    ),
    "unknown-summary-field": _set(NATIVE, ("hosted_summary", "future_total"), 3),
    "unknown-summary-source-field": _set(NATIVE, ("hosted_summary", "cited_sources", 0, "confidence"), 0.9),
    "included-with-usd": _set(NATIVE, ("hosted_summary", "cost"), {"basis": "included", "usd": 0.1}),
    "list-without-usd": _set(NATIVE, ("hosted_summary", "cost"), {"basis": "list"}),
    "negative-usd": _set(NATIVE, ("hosted_summary", "cost", "usd"), -0.01),
    "negative-count": _set(NATIVE, ("hosted_summary", "counts", "x_posts_fetched"), -1),
    "counter-name": _set(NATIVE, ("hosted_summary", "counts"), {"Web Search": 1}),
    "citation-start-only": _without(NATIVE, ("content", 4, "citations", 0), "end"),
    "citations-on-reference-text": _set(
        NATIVE, ("content", 4), {"type": "text", "ref": {"ref": "sha256:" + "a" * 64, "bytes": 9}, "citations": [{"url": "https://a"}]}
    ),
    "empty-citations": _set(NATIVE, ("content", 4, "citations"), []),
    "unexecuted-not-failed": _set(
        _set(_without(PROXIED, ("hosted",), "execution"), ("hosted", "record"), {"status": "incomplete", "error": "cut", "sources": []}),
        ("is_error",),
        True,
    ),
    "error-result-not-flagged": _set(
        _set(PROXIED, ("hosted", "record"), {"status": "failed", "error": "upstream 500", "sources": []}), ("is_error",), False
    ),
    "completed-result-flagged": _set(PROXIED, ("is_error",), True),
    "execution-mode": _set(PROXIED, ("hosted", "execution", "mode"), "native"),
    "proxy-billing": _set(PROXIED, ("hosted", "execution", "executor_billing"), "free"),
    "failed-attempt-billing": _set(PROXIED, ("hosted", "execution", "failed_attempts", 0, "billing"), "free"),
    "proxy-images-on-x-search": _set(PROXIED, ("hosted", "record", "images"), []),
    # Only a search tool runs as its function; an image call is always direct.
    "fallback-on-image-generate": _set(IMAGE, ("hosted", "fallback_from"), {"source_id": "src_relaygpt01"}),
    "fallback-names-no-source-id": _set(PROXIED, ("hosted", "fallback_from", "source_id"), "relay-gpt"),
    "fallback-with-a-name": _set(PROXIED, ("hosted", "fallback_from", "name"), "relay-gpt"),
}


@pytest.mark.parametrize("payload", list(VIOLATIONS.values()), ids=list(VIOLATIONS))
def test_schema_and_reader_refuse_the_same_hosted_violations(payload):
    assert not VALIDATOR.is_valid(payload)
    with pytest.raises(ValueError):
        message_from_dict(payload)


def test_a_stored_proxied_result_for_an_unknown_tool_loads_as_a_plain_result(caplog):
    stored = _set(_set(PROXIED, ("tool_name",), "future_unknown_tool"), ("is_error",), True)

    loaded = message_from_dict(stored)

    assert isinstance(loaded, ToolResultMessage)
    assert loaded.tool_name == "future_unknown_tool"
    assert loaded.hosted is None
    assert loaded.is_error is True
    assert message_to_dict(loaded) == {key: value for key, value in stored.items() if key != "hosted"}
    assert [record.levelname for record in caplog.records] == ["WARNING"]


@pytest.mark.parametrize("keep_known", [False, True], ids=["all-unknown", "mixed"])
@pytest.mark.parametrize(
    ("action", "record", "summary_extension", "expected"),
    [
        (
            {"type": "open_page", "url": "https://example.com/docs"},
            {"status": "completed", "sources": [{"url": "https://example.com/docs", "title": "Example Docs"}]},
            {},
            ("https://example.com/docs", "Example Docs"),
        ),
        ({"type": "fetch"}, {"status": "completed", "sources": []}, {}, ("result unavailable in this version",)),
        (
            {"type": "fetch", "query": "a failed query"},
            {"status": "failed", "error": "permission denied", "sources": []},
            {},
            ("failed", "permission denied", "a failed query"),
        ),
        (
            {"type": "open_page", "url": "https://example.com/docs"},
            {"status": "incomplete", "error": "timed out", "sources": [{"url": "https://example.com/partial"}]},
            {},
            ("incomplete", "timed out", "https://example.com/docs", "https://example.com/partial"),
        ),
        (
            {"type": "fetch", "queries": ["pending query"]},
            {"status": "pending", "sources": []},
            {},
            ("pending", "no result", "pending query"),
        ),
        (
            {"type": "fetch"},
            {"status": "failed", "error": "fetch refused", "sources": []},
            {},
            ("failed", "fetch refused"),
        ),
        (
            {"type": "fetch"},
            {"status": "completed", "sources": []},
            {"future_total": 3},
            ("result unavailable in this version",),
        ),
        (
            {"type": "fetch"},
            {"status": "completed", "sources": []},
            {"cited_sources": [{"url": "https://example.com/future", "confidence": 0.9}]},
            ("result unavailable in this version",),
        ),
    ],
    ids=["query-and-source", "placeholder", "failed", "incomplete", "pending", "error-only", "future-summary", "future-source"],
)
def test_a_native_unknown_hosted_block_loads_as_portable_text(
    action, record, summary_extension, expected, keep_known, caplog
):
    stored = copy.deepcopy(NATIVE)
    if not keep_known:
        stored["content"] = stored["content"][:1]
    stored["content"][0].update(tool="web_fetch", action=action, record=record)
    stored["hosted_summary"].update(summary_extension)
    original = copy.deepcopy(stored)

    loaded = message_from_dict(stored)

    assert isinstance(loaded, AssistantMessage)
    if keep_known:
        assert loaded.hosted_summary is not None
        assert loaded.hosted_summary.counts == {}
        assert loaded.hosted_summary.cited_sources == ()
        assert loaded.hosted_summary.cost is None
    else:
        assert loaded.hosted_summary is None
    portable = message_to_dict(loaded)
    assert portable["content"][1:] == stored["content"][1:]
    assert isinstance(loaded.content[0], TextBlock)
    assert all(fragment in loaded.content[0].text for fragment in expected)
    VALIDATOR.validate(portable)
    assert stored == original
    assert [record.levelname for record in caplog.records] == ["WARNING"]


def test_new_hosted_result_for_an_unknown_tool_stays_strict():
    with pytest.raises(ValueError, match="future_unknown_tool is not a hosted tool"):
        ToolResultMessage(
            tool_call_id="call_unknown",
            tool_name="future_unknown_tool",
            content=(TextBlock(text="result"),),
            hosted=_produced()[4].hosted,
        )


def test_a_count_past_the_bound_is_never_written_and_a_stored_one_is_dropped_on_read():
    """#2572: a count is at most 2^53 - 1, the largest integer every JSON reader holds exactly. A release before the
    bound may have stored a larger one: that counter alone is dropped, so its transcript still loads."""

    with pytest.raises(ValueError):
        HostedSummary(counts={"x_posts_fetched": MAX_HOSTED_COUNT + 1})
    stored = _set(NATIVE, ("hosted_summary", "counts", "x_posts_fetched"), MAX_HOSTED_COUNT + 1)
    assert not VALIDATOR.is_valid(stored)
    loaded = message_from_dict(stored)
    assert loaded.hosted_summary.counts == {"web_search_calls": 2, "x_search_calls": 1}
    VALIDATOR.validate(message_to_dict(loaded))
    at_bound = _set(NATIVE, ("hosted_summary", "counts", "x_posts_fetched"), MAX_HOSTED_COUNT)
    VALIDATOR.validate(at_bound)
    assert message_from_dict(at_bound).hosted_summary.counts["x_posts_fetched"] == MAX_HOSTED_COUNT


def test_a_record_with_an_error_code_this_build_does_not_know_still_loads():
    """#2589: a newer build may add codes; an older one reads the record and shows its English error."""

    newer = _set(NATIVE, ("content", 1, "record", "error_code"), "executor_overloaded")
    assert "executor_overloaded" not in HOSTED_ERROR_CODES
    VALIDATOR.validate(newer)
    assert message_from_dict(newer).content[1].record.error_code == "executor_overloaded"


@pytest.mark.parametrize(
    "payload",
    [_without(IMAGE, ("hosted", "action"), "prompt"), REFUSED_IMAGE],
    ids=["completed-unrevised", "refused-before-the-endpoint"],
)
def test_a_generate_action_names_a_prompt_only_when_the_endpoint_revised_it(payload):
    """The call's own arguments carry the model's prompt. The action's prompt is the endpoint's revision, which most
    image endpoints never send, and an image call refused before the endpoint ran has none."""

    assert "prompt" not in payload["hosted"]["action"]
    VALIDATOR.validate(payload)
    assert message_from_dict(payload).hosted.action.prompt is None


@pytest.mark.parametrize(
    "payload",
    json.loads((Path(__file__).parent / "fixtures/released_hosted_messages.json").read_text()),
    ids=["released-native", "released-model-executor"],
)
def test_released_hosted_rows_load_unchanged_without_service_fields(payload):
    """S2a: optional service fields cannot make a released transcript unreadable."""
    VALIDATOR.validate(payload)
    assert message_to_dict(message_from_dict(payload)) == payload
    # Service result-mapping limits must not narrow the released C-1 source shape.
    long_source = {"url": "https://a.test/" + "u" * 2048, "title": "t" * 301,
                   "snippet": "s" * 1001, "page_age": "p" * 65}
    expanded = copy.deepcopy(payload)
    record = expanded["content"][0]["record"] if payload["role"] == "assistant" else expanded["hosted"]["record"]
    record["sources"] = [long_source]
    VALIDATOR.validate(expanded)
    assert message_to_dict(message_from_dict(expanded)) == expanded


@pytest.mark.parametrize("outcome", ["completed", "failed", "skipped"])
def test_service_attempts_and_origin_load_and_round_trip(outcome):
    """S2a: the new writer shape is accepted by both strict persisted readers."""
    payload = copy.deepcopy(PROXIED)
    payload["tool_name"] = "web_search"
    payload["hosted"]["execution"]["executor_origin"] = {
        "provider": "exa", "api": "search_api", "model": "exa/search",
    }
    attempt = {"source_id": SOURCE, "label": "Exa", "kind": "service", "outcome": outcome}
    if outcome == "skipped":
        attempt.update(skipped="cooling", retry_at="2026-10-20T12:00:00Z")
    else:
        attempt["cost"] = {"basis": "reported", "usd": 0.007, "estimate": True}
        if outcome == "failed":
            attempt.update(error_code="service_unreadable", superseded=True)
    payload["hosted"]["record"]["attempts"] = [attempt]
    VALIDATOR.validate(payload)
    assert message_to_dict(message_from_dict(payload)) == payload


def test_a_citation_range_must_lie_within_its_text():
    # The schema cannot compare a range with the text length; the producer and reader own it.
    with pytest.raises(ValueError):
        TextBlock(text="短", citations=(Citation(url="https://a", start=0, end=2),))
    with pytest.raises(ValueError):
        message_from_dict(_set(NATIVE, ("content", 4, "citations", 0, "end"), 99))
