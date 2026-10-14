"""C-1 hosted-tool shapes (``vibey-hosted-tools.md`` §4.2 and §12 items 1-6, 22, 28).

The dataclasses in ``core.agent_core.messages`` are the producer; ``message.schema.json``
is the contract other components read. Every invariant is checked twice: the schema and
the reader must refuse the same shapes, and what the producer writes must validate.
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
    AssistantMessage,
    Citation,
    HostedAction,
    HostedCost,
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
            HostedToolBlock(
                id="ig_1",
                tool="image_generation",
                action=HostedAction(type="generate", prompt="一只猫, watercolor", size="1536x1024", quality="low"),
                record=HostedRecord(
                    status="completed",
                    images=(HostedImage(path="/tmp/media/image-3f2a.png", mime="image/png", width=1536, height=1024),),
                ),
                native=_native({"type": "image_generation_call", "result": "[image saved: /tmp/media/image-3f2a.png]"}),
            ),
            HostedToolBlock(
                id="ig_2",
                tool="image_generation",
                action=HostedAction(type="generate"),
                record=HostedRecord(status="failed", error="moderation_blocked", images=()),
                native=_native({"type": "image_generation_call", "status": "failed"}),
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
    )
    proxied = ToolResultMessage(
        tool_call_id="call_7",
        tool_name="x_search",
        content=(TextBlock(text="1. Avibe 本地优先 — https://x.com/avibe/status/1"),),
        hosted=HostedResult(
            record=HostedRecord(status="completed", sources=(POST,)),
            execution=ProxyExecution(
                executor_origin=XAI, executor_source_id=SOURCE, executor_billing="metered", cost=HostedCost(basis="list", usd=0.03)
            ),
        ),
    )
    included = ToolResultMessage(
        tool_call_id="call_8",
        tool_name="image_generation",
        content=(TextBlock(text="[image saved: /tmp/media/image-77.png · 1024x1024]"),),
        hosted=HostedResult(
            record=HostedRecord(status="completed", images=(HostedImage(path="/tmp/media/image-77.png", mime="image/webp"),)),
            execution=ProxyExecution(
                executor_origin=Origin(provider="openai", api="openai_responses", model="gpt-6-astra"),
                executor_source_id="src_o9p8q7r6s5",
                executor_billing="monthly",
                cost=HostedCost(basis="included"),
            ),
            # What the executor rendered: its revised prompt and the resolved size and quality.
            action=HostedAction(type="generate", prompt="A lighthouse at dusk", size="1024x1024", quality="high"),
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
    return [native_turn, proxy_call, proxied, included, unexecuted]


@pytest.mark.parametrize("message", _produced(), ids=["native", "proxy-call", "proxied", "included", "unexecuted"])
def test_hosted_messages_the_producer_writes_match_the_schema_and_round_trip(message):
    payload = message_to_dict(message)
    VALIDATOR.validate(payload)
    assert message_from_dict(payload) == message
    assert message_to_dict(message_from_dict(payload)) == payload


NATIVE = message_to_dict(_produced()[0])
PROXIED = message_to_dict(_produced()[2])


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
    (NATIVE, ("content", 4, "record", "images", 0), "path"),
    (NATIVE, ("content", 4, "record", "images", 0), "mime"),
    (NATIVE, ("content", 6, "citations", 0), "url"),
    (NATIVE, ("hosted_summary",), "counts"),
    (NATIVE, ("hosted_summary",), "cited_sources"),
    (NATIVE, ("hosted_summary", "cost"), "basis"),
    (PROXIED, ("hosted",), "record"),
    (PROXIED, ("hosted", "execution"), "mode"),
    (PROXIED, ("hosted", "execution"), "executor_origin"),
    (PROXIED, ("hosted", "execution"), "executor_source_id"),
    (PROXIED, ("hosted", "execution"), "executor_billing"),
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
    "failed-without-error": _without(NATIVE, ("content", 5, "record"), "error"),
    "error-too-long": _set(NATIVE, ("content", 1, "record", "error"), "x" * 401),
    "error-code-without-error": _set(NATIVE, ("content", 0, "record", "error_code"), "image_not_saved"),
    "error-code-not-snake-case": _set(NATIVE, ("content", 5, "record", "error_code"), "Image Not Saved"),
    "empty-error-code": _set(NATIVE, ("content", 5, "record", "error_code"), ""),
    "unknown-status": _set(NATIVE, ("content", 0, "record", "status"), "done"),
    "images-on-web-search": _set(NATIVE, ("content", 0, "record", "images"), []),
    "no-images-on-image-generation": _without(NATIVE, ("content", 4, "record"), "images"),
    "image-base64": _set(NATIVE, ("content", 4, "record", "images", 0, "b64_json"), "iVBORw0KGgo="),
    "generate-on-web-search": _set(NATIVE, ("content", 0, "action"), {"type": "generate", "prompt": "p"}),
    "search-on-x-search": _set(NATIVE, ("content", 2, "action"), {"type": "search", "queries": ["q"]}),
    "completed-generate-without-prompt": _without(NATIVE, ("content", 4, "action"), "prompt"),
    "unknown-action": _set(NATIVE, ("content", 0, "action"), {"type": "browse", "url": "https://a"}),
    "field-of-another-action": _set(NATIVE, ("content", 0, "action", "url"), "https://a"),
    "empty-queries": _set(NATIVE, ("content", 0, "action", "queries"), []),
    "x-search-limit-zero": _set(NATIVE, ("content", 2, "action", "limit"), 0),
    "x-search-mode": _set(NATIVE, ("content", 2, "action", "mode"), "latest"),
    "unknown-tool": _set(NATIVE, ("content", 0, "tool"), "code_interpreter"),
    "empty-payload": _set(NATIVE, ("content", 0, "native", "payload"), []),
    "payload-not-objects": _set(NATIVE, ("content", 0, "native", "payload"), ["web_search_call"]),
    "native-replay-flag": _set(NATIVE, ("content", 0, "native", "replay"), True),
    "bad-source-id": _set(NATIVE, ("content", 0, "native", "source_id"), "xai-key"),
    "summary-without-hosted-block": _set(message_to_dict(_produced()[1]), ("hosted_summary",), {"counts": {}, "cited_sources": []}),
    "hosted-block-without-summary": _without(NATIVE, (), "hosted_summary"),
    "included-with-usd": _set(NATIVE, ("hosted_summary", "cost"), {"basis": "included", "usd": 0.1}),
    "list-without-usd": _set(NATIVE, ("hosted_summary", "cost"), {"basis": "list"}),
    "negative-usd": _set(NATIVE, ("hosted_summary", "cost", "usd"), -0.01),
    "negative-count": _set(NATIVE, ("hosted_summary", "counts", "x_posts_fetched"), -1),
    "counter-name": _set(NATIVE, ("hosted_summary", "counts"), {"Web Search": 1}),
    "citation-start-only": _without(NATIVE, ("content", 6, "citations", 0), "end"),
    "citations-on-reference-text": _set(
        NATIVE, ("content", 6), {"type": "text", "ref": {"ref": "sha256:" + "a" * 64, "bytes": 9}, "citations": [{"url": "https://a"}]}
    ),
    "empty-citations": _set(NATIVE, ("content", 6, "citations"), []),
    "unexecuted-not-failed": _set(
        _set(_without(PROXIED, ("hosted",), "execution"), ("hosted", "record"), {"status": "incomplete", "error": "cut", "sources": []}),
        ("is_error",),
        True,
    ),
    "error-result-not-flagged": _set(
        _set(PROXIED, ("hosted", "record"), {"status": "failed", "error": "upstream 500", "sources": []}), ("is_error",), False
    ),
    "completed-result-flagged": _set(PROXIED, ("is_error",), True),
    "hosted-on-local-tool": _set(PROXIED, ("tool_name",), "bash"),
    "proxy-mode": _set(PROXIED, ("hosted", "execution", "mode"), "native"),
    "proxy-billing": _set(PROXIED, ("hosted", "execution", "executor_billing"), "free"),
    "proxy-images-on-x-search": _set(PROXIED, ("hosted", "record", "images"), []),
}


@pytest.mark.parametrize("payload", list(VIOLATIONS.values()), ids=list(VIOLATIONS))
def test_schema_and_reader_refuse_the_same_hosted_violations(payload):
    assert not VALIDATOR.is_valid(payload)
    with pytest.raises(ValueError):
        message_from_dict(payload)


def test_a_record_with_an_error_code_this_build_does_not_know_still_loads():
    """#2589: a newer build may add codes; an older one reads the record and shows its English error."""

    newer = _set(NATIVE, ("content", 5, "record", "error_code"), "executor_overloaded")
    assert "executor_overloaded" not in HOSTED_ERROR_CODES
    VALIDATOR.validate(newer)
    assert message_from_dict(newer).content[5].record.error_code == "executor_overloaded"


def test_a_failed_generate_may_lack_the_revised_prompt():
    # Item 28: OpenAI may return a failed image_generation_call with no revised_prompt.
    failed = NATIVE["content"][5]
    assert "prompt" not in failed["action"] and failed["record"]["status"] == "failed"
    VALIDATOR.validate(NATIVE)


def test_a_citation_range_must_lie_within_its_text():
    # The schema cannot compare a range with the text length; the producer and reader own it.
    with pytest.raises(ValueError):
        TextBlock(text="短", citations=(Citation(url="https://a", start=0, end=2),))
    with pytest.raises(ValueError):
        message_from_dict(_set(NATIVE, ("content", 6, "citations", 0, "end"), 99))
