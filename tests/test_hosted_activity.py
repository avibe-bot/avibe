"""How a hosted tool call is shown: the card payload and the IM line (``vibey-hosted-tools.md`` §6.4).

The payloads are pinned to ``ui/src/lib/hostedActivity.fixture.json``, the
fixture the Web card tests render, so producer and consumer read one shape.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.agent_core.messages import (
    HOSTED_ERROR_CODES,
    AssistantMessage,
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
)
from core.hosted_activity import hosted_line, native_activities, proxy_activity
from modules.im.formatters.discord_formatter import DiscordFormatter
from vibe.i18n import t as i18n_t

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


def direct_image():
    """An ``image_generate`` call, run directly on the image endpoint (§7.4)."""
    return proxy_activity(
        ToolCallBlock(id="call_ig1", name="image_generate", arguments={"model": "gpt-image-2", "prompt": PROMPT}),
        HostedResult(
            record=HostedRecord(
                status="completed",
                images=(HostedImage(path="/media/3f2a.png", mime="image/png", width=1536, height=1024, bytes=1993000),),
            ),
            # No image price is known here, so the call carries no cost at all.
            execution=ProxyExecution(
                executor_origin=Origin(provider="openai", api="openai_responses", model="gpt-image-2"),
                executor_source_id="src_relaygpt1",
                executor_billing="metered",
                mode="direct",
            ),
            action=HostedAction(type="generate", prompt=PROMPT, size="1536x1024", quality="medium", model="gpt-image-2"),
        ),
        source_names=NAMES,
    )


def proxy_failed():
    return proxy_activity(
        _x_call("call_x2", "from:OpenAI"),
        HostedResult(record=HostedRecord(status="failed", error="no Source is verified for X search")),
        source_names=NAMES,
    )


def test_a_proxied_image_shows_what_its_executor_rendered_as_a_native_one_does():
    """A proxied image's card reads the executor's rendered prompt and resolved size and quality from the result's
    action, as a native image's reads its block's action: one reading, no mode branch."""

    call = ToolCallBlock(id="call_i1", name="image_generate", arguments={"prompt": "a lighthouse", "size": "auto"})
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


def proxy_unreachable():
    return proxy_activity(
        _x_call("call_x5", "from:xai"),
        HostedResult(
            record=HostedRecord(
                status="failed",
                error="the gateway could not run the call (ConnectError)",
                error_code="gateway_unreachable",
            )
        ),
        source_names=NAMES,
    )


def proxy_retried():
    """An X search whose first executor failed after it billed and reported no price: the cost is a lower bound."""
    return proxy_activity(
        _x_call("call_x6", "from:OpenAI"),
        HostedResult(
            record=HostedRecord(status="completed", sources=POSTS[:1]),
            execution=replace(XAI, failed_attempts=(_attempt("src_relaygpt1", "metered", None),)),
        ),
        source_names=NAMES,
    )


def proxy_fallback():
    """A web search on a hop whose own Source (relay-gpt) refused it natively, so relay-claude ran it by proxy."""
    return proxy_activity(
        ToolCallBlock(id="call_w7", name="web_search", arguments={"query": "Anthropic announcements October 2026"}),
        HostedResult(
            record=HostedRecord(status="completed", sources=(DEVDAY,)),
            execution=ProxyExecution(
                executor_origin=Origin(provider="anthropic", api="anthropic", model="claude-haiku-5-5"),
                executor_source_id="src_relayclaude",
                executor_billing="metered",
                cost=HostedCost(basis="list", usd=0.01),
            ),
            fallback_from=HostedFallback("src_relaygpt1"),
        ),
        source_names=NAMES,
    )


@pytest.mark.parametrize(
    "build",
    [native_web_search, proxy_x_search, direct_image, proxy_failed, proxy_unreachable, proxy_retried, proxy_fallback],
)
def test_payloads_match_the_shape_the_web_card_renders(build):
    """MH-HOSTED-UI-009: every mode and outcome the card distinguishes, from the canonical records each producer writes.

    A call that never reached an executor names no vendor or Source it never used.
    """

    assert build().to_payload() == FIXTURE[build.__name__]


def test_a_source_no_browser_should_open_is_named_but_not_linked():
    """MH-HOSTED-UI-009: a source URL is linked only if a citation could link it (``safe_url``); any other string a
    provider gave is kept as the source's name, so the count still matches the IM line."""

    activity = proxy_activity(
        ToolCallBlock(id="call_w8", name="web_search", arguments={"query": "devday"}),
        HostedResult(
            record=HostedRecord(
                status="completed",
                sources=(
                    DEVDAY,
                    HostedSource(url="/docs/changelog", title="Responses API changelog"),
                    HostedSource(url="openai.com/index/devday-2026"),
                ),
            ),
            execution=XAI,
        ),
        source_names=NAMES,
    )

    assert activity.to_payload()["sources"] == [
        {"url": DEVDAY.url, "title": DEVDAY.title, "page_age": DEVDAY.page_age},
        {"title": "Responses API changelog"},
        {"title": "openai.com/index/devday-2026"},
    ]


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
        (proxy_x_search, "zh", "🔎 X 搜索 “from:OpenAI within_time:7d” · 由 xAI 代理（grok-4.6） · 2 条帖子 · $0.11"),
        (
            direct_image,
            "en",
            "🖼 Image generation “A wide photo of an open laptop on a desk at night, empty da…”"
            " · gpt-image-2 · 1536×1024",
        ),
        (proxy_failed, "zh", "🔎 X 搜索 “from:OpenAI” · 失败：no Source is verified for X search"),
        # A proxy that ran because the turn's own Source refused the tool says so, in place of how it ran.
        (proxy_fallback, "zh", "🔎 网页搜索 “Anthropic announcements October 2026” · relay-gpt 不支持，已改用 Anthropic · 1 个来源 · ≈ $0.01"),
        (proxy_fallback, "en", "🔎 Web search “Anthropic announcements October 2026” · relay-gpt doesn't support it, now using Anthropic · 1 source · ≈ $0.01"),
        # A compatible endpoint's vendor id is custom, which names no vendor: its Source names who ran it (#2873).
        (
            lambda: replace(native_web_search(), provider="custom", source_name="My relay"),
            "en",
            "🔎 Web search “OpenAI announcements October 2026” · native (My relay) · 2 sources · ≈ $0.01",
        ),
        (
            lambda: replace(proxy_x_search(), provider="custom", source_name="My relay"),
            "zh",
            "🔎 X 搜索 “from:OpenAI within_time:7d” · 由 My relay 代理（grok-4.6） · 2 条帖子 · $0.11",
        ),
        (
            lambda: replace(proxy_fallback(), provider="custom", source_name="My relay"),
            "en",
            "🔎 Web search “Anthropic announcements October 2026” · relay-gpt doesn't support it, now using My relay"
            " · 1 source · ≈ $0.01",
        ),
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
    assert hosted_line(included, "zh") == "🔎 X 搜索 “from:xai within_time:3d” · 由 OpenAI 代理（gpt-6.1-sol） · 1 条帖子 · 订阅内含"

    shared = native_activities(
        _message(
            _search("ws_1", "a"),
            _search("ws_2", "b", sources=(DEVDAY,)),
            summary=HostedSummary(cost=HostedCost(basis="list", usd=0.02)),
        )
    )[-1]
    assert hosted_line(shared, "en") == "🔎 Web search “b” · native (Anthropic) · 1 source · ≈ $0.02 for 2 calls"


def _attempt(source_id: str, billing: str, cost: HostedCost | None):
    return FailedAttempt(source_id=source_id, billing=billing, cost=cost)


@pytest.mark.parametrize(
    ("failed", "last", "cost", "line_tail"),
    [
        # A metered failure before the call that ran: both charges, summed.
        ((("src_relaygpt1", "metered", HostedCost(basis="list", usd=0.02)),), HostedCost(basis="reported", usd=0.11),
         {"basis": "list", "usd": 0.13, "calls": 1}, "≈ $0.13"),
        ((("src_relaygpt1", "metered", HostedCost(basis="reported", usd=0.02)),), HostedCost(basis="reported", usd=0.11),
         {"basis": "reported", "usd": 0.13, "calls": 1}, "$0.13"),
        # A subscription attempt adds no money.
        ((("src_chatgpt01", "monthly", HostedCost(basis="included")),), HostedCost(basis="reported", usd=0.11),
         {"basis": "reported", "usd": 0.11, "calls": 1}, "$0.11"),
        # A billed attempt with no known price: the total is a lower bound, never shown as complete.
        ((("src_relaygpt1", "metered", None),), HostedCost(basis="reported", usd=0.11),
         {"basis": "reported", "usd": 0.11, "calls": 1, "at_least": True}, "at least $0.11"),
        # Nothing priced to sum: the cost is unknown, not "at least $0".
        ((("src_chatgpt01", "monthly", HostedCost(basis="included")),), None, None, None),
        ((("src_relaygpt1", "metered", None),), None, None, None),
    ],
)
def test_a_proxied_calls_cost_counts_every_billed_attempt(failed, last, cost, line_tail):
    """MH-HOSTED-GATEWAY-005: the card and the IM line show what the whole executor chain cost, never less, and a
    total an attempt left unpriced only as a lower bound."""

    activity = proxy_activity(
        _x_call("call_x5", "from:OpenAI"),
        HostedResult(
            record=HostedRecord(status="completed", sources=POSTS[:1]),
            execution=replace(XAI, cost=last, failed_attempts=tuple(_attempt(*item) for item in failed)),
        ),
    )
    payload = activity.to_payload()
    assert payload.get("cost") == cost
    line = hosted_line(activity, "en")
    assert line.endswith(f" · {line_tail}") if line_tail else "$" not in line


HOSTILE = ("@everyone", "<@123456789>", "[Models](https://example.invalid)")


def _slots(text: str):
    """The IM line's untrusted slots, each carrying *text*: what a model, a provider or a user wrote."""
    fallback = replace(proxy_fallback(), fallback_source_name=text)
    return {
        "subject": proxy_activity(
            _x_call("call_s1", text), HostedResult(record=HostedRecord(status="completed"), execution=XAI)
        ),
        "error": proxy_activity(
            _x_call("call_e1", "q"), HostedResult(record=HostedRecord(status="failed", error=f"refused: {text}"))
        ),
        "fallback source": fallback,
        "executor model": replace(proxy_x_search(), model=text),
        "image model": replace(direct_image(), model=text),
        "compatible endpoint": replace(native_web_search(), provider="custom", source_name=text),
    }


@pytest.mark.parametrize("text", HOSTILE)
@pytest.mark.parametrize(
    "slot", ["subject", "error", "fallback source", "executor model", "image model", "compatible endpoint"],
)
def test_the_im_line_leaves_no_mention_or_masked_link_live_in_untrusted_text(slot, text):
    """MH-HOSTED-UI-010: a query, a provider's error, and a user's Source or model name are not Avibe's words. The
    line neutralizes their mentions and masked links before the platform escape, which alone leaves Discord's
    ``@everyone``, ``<@id>`` and ``[label](url)`` live (DiscordBot sends without ``allowed_mentions``)."""

    line = hosted_line(_slots(text)[slot], "en", escape=DiscordFormatter().escape_special_chars)
    assert text not in line
    assert "@everyone" not in line and "<@1" not in line and "](" not in line


def test_the_im_line_escapes_the_quoted_subject_for_its_destination():
    activity = proxy_activity(
        _x_call("call_x4", "*bold* claim"),
        HostedResult(record=HostedRecord(status="completed"), execution=XAI),
    )
    assert hosted_line(activity, "en", escape=lambda text: text.replace("*", "\\*")).startswith(
        "🔎 X search “\\*bold\\* claim”"
    )


@pytest.mark.parametrize(
    ("code", "lang", "line"),
    [
        ("gateway_unreachable", "zh", "🔎 X 搜索 “from:xai” · 失败：无法连接模型网关"),
        ("gateway_unreachable", "en", "🔎 X search “from:xai” · failed: couldn't reach the model gateway"),
        # A code from a newer producer reads as its English reason, as a provider's own reason does.
        ("gateway_overheated", "zh", "🔎 X 搜索 “from:xai” · 失败：the gateway could not run the call (ConnectError)"),
    ],
)
def test_the_im_line_says_a_reason_avibe_worded_in_the_readers_language(code, lang, line):
    """MH-HOSTED-UI-015: a coded failure reads in the reader's language; the record's ``error`` stays model-facing English."""

    record = HostedRecord(status="failed", error="the gateway could not run the call (ConnectError)", error_code=code)
    activity = proxy_activity(_x_call("call_x5", "from:xai"), HostedResult(record=record))
    assert hosted_line(activity, lang) == line


UI_BUNDLES = Path(__file__).resolve().parents[1] / "ui/src/i18n"


@pytest.mark.parametrize("code", HOSTED_ERROR_CODES)
def test_every_known_hosted_error_code_has_copy_on_every_surface(code):
    """MH-HOSTED-UI-015: every code Avibe can set has words in both languages, for the IM line and the Web card.

    A new code without them would show a raw key on the IM line and fall back to English on the Web.
    """

    for lang in ("en", "zh"):
        key = f"hostedTools.errorCode.{code}"
        assert i18n_t(key, lang) != key, f"vibe/i18n/{lang}.json has no {key}"
        hosted = json.loads((UI_BUNDLES / f"{lang}.json").read_text(encoding="utf-8"))["chat"]["agentActivity"]["hosted"]
        assert hosted.get("errorCode", {}).get(code), f"ui/src/i18n/{lang}.json has no chat.agentActivity.hosted.errorCode.{code}"

