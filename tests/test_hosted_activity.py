"""How a hosted tool call is shown: the card payload and the IM line (``vibey-hosted-tools.md`` §6.4).

The payloads are pinned to ``ui/src/lib/hostedActivity.fixture.json``, the
fixture the Web card tests render, so producer and consumer read one shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_core.messages import (
    AssistantMessage,
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
)
from core.hosted_activity import hosted_line, native_activities, proxy_activity

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "ui/src/lib/hostedActivity.fixture.json").read_text(encoding="utf-8")
)
NAMES = {"src_relayclaude": "relay-claude", "src_grok0001": "Grok", "src_relaygpt1": "relay-gpt"}.get

DEVDAY = HostedSource(url="https://openai.com/index/devday-2026", title="DevDay 2026 recap", page_age="2 days ago")
CHANGELOG = HostedSource(url="https://platform.openai.com/docs/changelog", title="Responses API changelog")
POSTS = (
    HostedSource(
        url="https://x.com/OpenAI/status/1",
        title="DevDay 2026 recap: everything we shipped this week",
        snippet="Recordings and the launch list in one post.",
        page_age="2 days ago",
    ),
    HostedSource(url="https://x.com/OpenAIDevs/status/2", title="Web search in the Responses API now supports domain filters"),
)
PROMPT = "A wide photo of an open laptop on a desk at night, empty dark space in the upper right"
XAI = ProxyExecution(
    executor_origin=Origin(provider="xai", api="openai_responses", model="grok-4.6"),
    executor_source_id="src_grok0001",
    executor_billing="metered",
    cost=HostedCost(basis="reported", usd=0.11),
)


def _native(source_id: str) -> HostedNative:
    return HostedNative(source_id=source_id, payload=({"type": "provider_item"},))


def _search(
    call_id: str, *queries: str, sources=(), source_id="src_relayclaude", status="completed", error=None
) -> HostedToolBlock:
    return HostedToolBlock(
        id=call_id,
        tool="web_search",
        action=HostedAction(type="search", queries=tuple(queries)),
        record=HostedRecord(status=status, error=error, sources=tuple(sources)),
        native=_native(source_id),
    )


def _message(*content, summary: HostedSummary, provider="anthropic", api="anthropic", model="claude-opus-5-5"):
    return AssistantMessage(
        content=tuple(content),
        origin=Origin(provider=provider, api=api, model=model),
        stop_reason="stop",
        hosted_summary=summary,
    )


def _x_call(call_id: str, query: str) -> ToolCallBlock:
    return ToolCallBlock(id=call_id, name="x_search", arguments={"query": query})


def native_web_search():
    [activity] = native_activities(
        _message(
            TextBlock(text="Here is what I found."),
            _search("srvtoolu_01", "OpenAI announcements October 2026", sources=(DEVDAY, CHANGELOG)),
            summary=HostedSummary(counts={"web_search_requests": 1}, cost=HostedCost(basis="list", usd=0.01)),
        ),
        source_names=NAMES,
    )
    return activity


def proxy_x_search():
    return proxy_activity(
        _x_call("call_x1", "from:OpenAI within_time:7d"),
        HostedResult(record=HostedRecord(status="completed", sources=POSTS), execution=XAI),
        source_names=NAMES,
        duration_ms=31000,
    )


def native_image():
    [activity] = native_activities(
        _message(
            HostedToolBlock(
                id="ig_01",
                tool="image_generation",
                action=HostedAction(type="generate", prompt=PROMPT, size="1536x1024", quality="medium"),
                record=HostedRecord(
                    status="completed",
                    images=(HostedImage(path="/media/3f2a.png", mime="image/png", width=1536, height=1024, bytes=1993000),),
                ),
                native=_native("src_relaygpt1"),
            ),
            # Phase 1 knows no image price, so the response carries no cost at all.
            summary=HostedSummary(),
            provider="openai",
            api="openai_responses",
            model="gpt-6.1-sol",
        ),
        source_names=NAMES,
    )
    return activity


def proxy_failed():
    return proxy_activity(
        _x_call("call_x2", "from:OpenAI"),
        HostedResult(record=HostedRecord(status="failed", error="no Source is verified for X search")),
        source_names=NAMES,
    )


def test_a_proxied_image_shows_what_its_executor_rendered_as_a_native_one_does():
    """A proxied image's card reads the executor's rendered prompt and resolved size and quality from the result's
    action, as a native image's reads its block's action: one reading, no mode branch."""

    call = ToolCallBlock(id="call_i1", name="image_generation", arguments={"prompt": "a lighthouse", "size": "auto"})
    activity = proxy_activity(
        call,
        HostedResult(
            record=HostedRecord(status="completed", images=(HostedImage(path="/media/9c1d.jpg", mime="image/jpeg"),)),
            execution=XAI,
            action=HostedAction(type="generate", prompt=PROMPT, size="1536x1024", quality="high"),
        ),
        source_names=NAMES,
    )

    assert (activity.subject, activity.revised_prompt) == ("a lighthouse", PROMPT)
    assert (activity.size, activity.quality) == ("1536x1024", "high")


@pytest.mark.parametrize("build", [native_web_search, proxy_x_search, native_image, proxy_failed])
def test_payloads_match_the_shape_the_web_card_renders(build):
    """MH-HOSTED-UI-009: every mode and outcome the card distinguishes, from the canonical records each producer writes.

    A call that never reached an executor names no vendor or Source it never used.
    """

    assert build().to_payload() == FIXTURE[build.__name__]


def test_a_response_scoped_summary_is_shown_once():
    """The response's cost covers all its calls, and xAI's cited posts belong to its X search."""

    x_search = HostedToolBlock(
        id="xs_1",
        tool="x_search",
        action=HostedAction(type="x_search", variant="keyword", query="from:xai", limit=10, mode="Latest"),
        record=HostedRecord(status="completed"),
        native=_native("src_grok0001"),
    )
    activities = native_activities(
        _message(
            _search("ws_1", "grok 5 release", "grok 5 benchmarks", sources=(DEVDAY,), source_id="src_grok0001"),
            x_search,
            _search("ws_2", "xai api pricing", source_id="src_grok0001"),
            summary=HostedSummary(
                counts={"web_search_calls": 2, "x_search_calls": 1},
                cited_sources=POSTS,
                cost=HostedCost(basis="reported", usd=0.06),
            ),
            provider="xai",
            api="openai_responses",
            model="grok-4.6",
        ),
    )

    assert [(a.call_id, a.cost, a.cost_calls) for a in activities] == [
        ("ws_1", None, 3),
        ("xs_1", None, 3),
        ("ws_2", HostedCost(basis="reported", usd=0.06), 3),
    ]
    # Only the X search without per-call results shows the response's cited posts.
    assert [a.sources for a in activities] == [(DEVDAY,), POSTS, ()]
    assert activities[0].extra_queries == ("grok 5 benchmarks",)
    assert activities[1].subject == "from:xai"


@pytest.mark.parametrize(
    ("build", "lang", "line"),
    [
        (
            native_web_search,
            "en",
            "🔎 Web search “OpenAI announcements October 2026” · native (Anthropic) · 2 sources · ≈ $0.01",
        ),
        (
            native_web_search,
            "zh",
            "🔎 网页搜索 “OpenAI announcements October 2026” · 原生（Anthropic） · 2 个来源 · ≈ $0.01",
        ),
        (proxy_x_search, "zh", "🔎 X 搜索 “from:OpenAI within_time:7d” · 由 xAI 代理 · 2 条帖子 · $0.11"),
        (
            native_image,
            "en",
            "🖼 Image generation “A wide photo of an open laptop on a desk at night, empty da…”"
            " · native (OpenAI) · 1 image",
        ),
        (proxy_failed, "zh", "🔎 X 搜索 “from:OpenAI” · 失败：no Source is verified for X search"),
    ],
)
def test_the_im_line_states_tool_query_mode_count_and_known_cost(build, lang, line):
    """MH-HOSTED-UI-010: one line per hosted call, the same for either mode, with a cost only when one is known."""
    assert hosted_line(build(), lang) == line


@pytest.mark.parametrize(
    ("status", "error", "line"),
    [
        ("completed", None, "🔎 Web search “OpenAI news” · native (Anthropic) · 2 sources · ≈ $0.01"),
        ("pending", None, "🔎 Web search “OpenAI news” · cut off · ≈ $0.01"),
        ("failed", "upstream timeout", "🔎 Web search “OpenAI news” · failed: upstream timeout · ≈ $0.01"),
        ("incomplete", "response ended", "🔎 Web search “OpenAI news” · incomplete: response ended · ≈ $0.01"),
    ],
)
def test_the_im_line_claims_how_a_call_ran_and_what_it_found_only_once_it_completed(status, error, line):
    """MH-HOSTED-UI-014: a call that did not complete shows its status instead, and its known charge still shows.

    The same records the Web card renders (``HostedToolRow.test.tsx`` reads the rule from ``hostedCompleted``).
    """

    [activity] = native_activities(
        _message(
            _search("srvtoolu_01", "OpenAI news", sources=(DEVDAY, CHANGELOG), status=status, error=error),
            summary=HostedSummary(cost=HostedCost(basis="list", usd=0.01)),
        )
    )
    assert hosted_line(activity, "en") == line


def test_the_im_line_names_a_subscription_and_a_shared_response_cost():
    included = proxy_activity(
        _x_call("call_x3", "from:xai within_time:3d"),
        HostedResult(
            record=HostedRecord(status="completed", sources=POSTS[:1]),
            execution=ProxyExecution(
                executor_origin=Origin(provider="openai", api="openai_responses", model="gpt-6.1-sol"),
                executor_source_id="src_chatgpt01",
                executor_billing="monthly",
                cost=HostedCost(basis="included"),
            ),
        ),
    )
    assert hosted_line(included, "zh") == "🔎 X 搜索 “from:xai within_time:3d” · 由 OpenAI 代理 · 1 条帖子 · 订阅内含"

    shared = native_activities(
        _message(
            _search("ws_1", "a"),
            _search("ws_2", "b", sources=(DEVDAY,)),
            summary=HostedSummary(cost=HostedCost(basis="list", usd=0.02)),
        )
    )[-1]
    assert hosted_line(shared, "en") == "🔎 Web search “b” · native (Anthropic) · 1 source · ≈ $0.02 for 2 calls"


def test_the_im_line_escapes_the_quoted_subject_for_its_destination():
    activity = proxy_activity(
        _x_call("call_x4", "*bold* claim"),
        HostedResult(record=HostedRecord(status="completed"), execution=XAI),
    )
    assert hosted_line(activity, "en", escape=lambda text: text.replace("*", "\\*")).startswith(
        "🔎 X search “\\*bold\\* claim”"
    )
