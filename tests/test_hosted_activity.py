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
    HOSTED_SKIP_REASONS,
    AssistantMessage,
    FailedAttempt,
    HostedAction,
    HostedAttempt,
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
    ServiceExecutorOrigin,
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


@pytest.mark.parametrize(
    ("error", "category", "en", "zh"),
    [
        ("Our servers are currently overloaded. Please try again later.", "overloaded", "upstream overloaded", "上游服务过载"),
        ("stream disconnected before completion", "disconnected", "stream disconnected", "连接中断"),
        ("private upstream error: https://secret.invalid/?token=private", "upstream", "upstream error", "上游服务异常"),
    ],
)
def test_pending_native_search_explains_response_interruption_without_a_provider_subject(error, category, en, zh):
    """MH-HOSTED-UI-014: pending calls inherit a sanitized response failure, not a fake query.

    Existing status tests cover only the call record, not an error on its owning response.
    """
    message = replace(
        _message(
            replace(_search("ws_cut", "unused", status="pending"), action=HostedAction(type="other", name="web_search_call")),
            summary=HostedSummary(),
        ),
        stop_reason="error", error_message=error,
    )
    [activity] = native_activities(message)
    assert activity.subject is None
    assert activity.to_payload() == {**FIXTURE["native_interrupted"], "interruption": category}
    assert error not in json.dumps(activity.to_payload())
    assert hosted_line(activity, "en") == f"🔎 Web search · Model response interrupted: {en}"
    assert hosted_line(activity, "zh") == f"🔎 网页搜索 · 模型回复中断：{zh}"
    # No reason is invented on success, and completed calls keep their own outcome.
    [successful] = native_activities(replace(message, stop_reason="stop", error_message=None))
    assert "interruption" not in successful.to_payload()
    [completed] = native_activities(replace(message, content=(_search("ws_done", "known query"),)))
    assert "interruption" not in completed.to_payload()
    assert completed.subject == "known query"


@pytest.mark.parametrize("stop_reason", ["tool_use", "error", "aborted"])
async def test_deferred_search_has_no_card_or_share_of_an_earlier_search_cost(stop_reason):
    """The producer omits only a deferred call; stream cuts keep interrupted cards.

    A response can also contain a finished search. Its summary must stay on that
    card, not disappear with the omitted use or be counted on a later completion.
    """
    from tests.agent_core.ai.test_anthropic_deferred import _paused

    _, message = await _paused("FSC")
    cost = HostedCost(basis="reported", usd=0.01)
    message = replace(message, stop_reason=stop_reason, hosted_summary=HostedSummary(cost=cost))
    activities = native_activities(message)
    assert [activity.call_id for activity in activities] == (
        ["srvtoolu_0"] if stop_reason == "tool_use" else ["srvtoolu_0", "srvtoolu_1"]
    )
    assert [activity.cost for activity in activities] == ([cost] if stop_reason == "tool_use" else [None, cost])
    assert all(activity.cost_calls == len(activities) for activity in activities)
    if stop_reason != "tool_use":
        assert activities[-1].status == "pending" and not activities[-1].completed


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
    # The card lists them as "Also searched".
    assert activities[0].to_payload()["extra_queries"] == ["grok 5 benchmarks"]
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
        "switched-from source": _chain(
            (_tried(TAVILY, error_code="service_key_invalid", usd=0.0), _tried(DEEPSEEK, "completed", usd=0.0)),
            execution=_service_ran(DEEPSEEK, "deepseek"),
            names={TAVILY: text, DEEPSEEK: "DeepSeek"},
            state=_now({TAVILY: {"needs_action": KEY}}),
        ),
        "repaired source": _chain(
            (_skip(TAVILY, "blocked", needs_action=KEY),), status="failed", names={TAVILY: text},
            state=_now({TAVILY: {"needs_action": KEY}}),
        ),
    }


@pytest.mark.parametrize("text", HOSTILE)
@pytest.mark.parametrize(
    "slot",
    [
        "subject",
        "error",
        "fallback source",
        "executor model",
        "image model",
        "compatible endpoint",
        "switched-from source",
        "repaired source",
    ],
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


# A search chain with search-capable candidates (web-search-services.md §4.3, §6.5). The cards are pinned to
# ``ui/src/lib/hostedServiceActivity.fixture.json``, which the Web card tests render in both languages.
SERVICE_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "ui/src/lib/hostedServiceActivity.fixture.json").read_text(encoding="utf-8")
)
TAVILY, EXA, KEYLESS, DEEPSEEK, CLAUDE = "src_tavily01", "src_exa00001", "src_exakeyless", "src_deepseek1", "src_claude001"
LABELS = {TAVILY: "Tavily", EXA: "Exa", KEYLESS: "Exa", DEEPSEEK: "DeepSeek", CLAUDE: "Anthropic API key"}
NEEDS = "models.source.needs_action."
KEY, BALANCE, LIMIT = f"{NEEDS}credential_revoked", f"{NEEDS}balance_exhausted", f"{NEEDS}spend_limit_reached"
# Each Source as Model Hub holds it now, healthy; a test changes what it needs.
HEALTHY = {
    TAVILY: {"protocol": "search", "vendor": "tavily", "enabled": True},
    EXA: {"protocol": "search", "vendor": "exa", "enabled": True},
    KEYLESS: {"protocol": "search", "vendor": "exa-mcp", "enabled": True},
    DEEPSEEK: {"protocol": "openai_chat", "vendor": "deepseek", "enabled": True},
    CLAUDE: {"protocol": "anthropic", "vendor": "anthropic"},
}
#: Model Hub could not be read when the card was drawn.
UNREAD = None
RESULTS = (
    HostedSource(
        url="https://api-docs.deepseek.com/guides/search-tool",
        title="Search tool",
        snippet="One HTTPS request returns ranked results.",
    ),
    HostedSource(url="https://api-docs.deepseek.com/quick_start/pricing", title="Models & Pricing"),
    HostedSource(url="https://www.tavily.com/pricing", title="Tavily pricing"),
    HostedSource(url="https://exa.ai/pricing", title="Exa pricing"),
    HostedSource(
        url="https://news.example.com/deepseek-search", title="DeepSeek adds a search API", page_age="2 days ago"
    ),
)
CLAUDE_RAN = ProxyExecution(
    executor_origin=Origin(provider="anthropic", api="anthropic", model="claude-haiku-5-5"),
    executor_source_id=CLAUDE,
    executor_billing="metered",
    cost=HostedCost(basis="list", usd=0.01),
)


def _now(changes: dict) -> dict:
    """The Sources now: ``{TAVILY: {"needs_action": KEY}}`` changes one, ``{EXA: None}`` removes it."""
    current = {source_id: dict(state) for source_id, state in HEALTHY.items()}
    for source_id, change in changes.items():
        if change is None:
            del current[source_id]
        else:
            current[source_id].update(change)
    return current


def _tried(source_id, outcome="failed", *, kind="service", usd=None, basis="list", estimate=None, **facts):
    cost = HostedCost(basis=basis) if basis == "included" else (
        HostedCost(basis=basis, usd=usd, estimate=estimate) if usd is not None else None
    )
    return HostedAttempt(source_id=source_id, label=LABELS[source_id], kind=kind, outcome=outcome, cost=cost, **facts)


def _skip(source_id, reason, *, kind="service", **facts):
    return HostedAttempt(
        source_id=source_id, label=LABELS[source_id], kind=kind, outcome="skipped", skipped=reason, **facts
    )


def _service_ran(source_id, vendor, usd=None):
    return ProxyExecution(
        executor_origin=ServiceExecutorOrigin(provider=vendor, api="search_api", model=f"{vendor}/search"),
        executor_source_id=source_id,
        executor_billing="metered",
        cost=HostedCost(basis="list", usd=usd) if usd is not None else None,
    )


def _chain(attempts, *, status="completed", execution=None, state=HEALTHY, names=LABELS, sources=(), call_id="call_ws",
           error=None, error_code=None):
    record = HostedRecord(
        status=status,
        error=error or (None if status == "completed" else "the search failed"),
        error_code=error_code,
        sources=sources,
        attempts=attempts,
    )
    return proxy_activity(
        ToolCallBlock(id=call_id, name="web_search", arguments={"query": "DeepSeek search API pricing"}),
        HostedResult(record=record, execution=execution),
        source_names=names.get,
        source_state=state.get if state is not None else None,
    )


@pytest.mark.parametrize("lang", ["en", "zh"])
@pytest.mark.parametrize("outcome", ["success", "rate_limited", "unavailable", "failed"])
def test_keyless_im_projection_names_free_service_and_network_remedy(lang, outcome):
    keyless_attempt = _tried(
        KEYLESS, "completed" if outcome == "success" else "failed", usd=0,
        **({} if outcome == "success" else {"error_code":
           "service_unavailable" if outcome == "unavailable" else "service_rate_limited"}),
    )
    fallback = outcome in {"rate_limited", "unavailable"}
    activity = _chain(
        (keyless_attempt, _tried(DEEPSEEK, "completed", usd=0.00075)) if fallback else (keyless_attempt,),
        status="failed" if outcome == "failed" else "completed",
        execution=_service_ran(DEEPSEEK, "deepseek", 0.00075) if fallback else _service_ran(KEYLESS, "exa-mcp", 0),
        sources=() if outcome == "failed" else RESULTS,
        error_code="service_rate_limited" if outcome == "failed" else None,
    )
    line = hosted_line(activity, lang)
    assert ("Exa（免费）" if lang == "zh" else "Exa (free)") in line
    assert not any(word in line.lower() for word in ("密钥", "api key", "credential", "included", "$0 ·"))
    if outcome == "success":
        assert ("通过 Exa（免费） · 5 个来源 · 免费" if lang == "zh" else "via Exa (free) · 5 sources · Free") in line
    elif outcome == "rate_limited":
        assert ("Exa（免费）限流，已改用 DeepSeek" if lang == "zh" else "Rate-limited: Exa (free); switched to DeepSeek") in line
    elif outcome == "unavailable":
        assert ("暂时不可用" if lang == "zh" else "unavailable") in line
    else:
        assert ("立即重试" if lang == "zh" else "Retry now") in line
        assert activity.attempts[0].remedy_key == "repair.retrySearch"


SERVICE_CARDS = {
    # Design example: Tavily answers with five results.
    "service_completed": lambda: _chain(
        (_tried(TAVILY, "completed", usd=0.008),),
        execution=_service_ran(TAVILY, "tavily", 0.008),
        sources=RESULTS,
        call_id="call_ws1",
    ),
    # Tavily answers 401, so its Source needs a new key, and DeepSeek answers in the same call.
    "service_switch": lambda: _chain(
        (_tried(TAVILY, error_code="service_key_invalid", usd=0.0), _tried(DEEPSEEK, "completed", usd=0.00075)),
        execution=_service_ran(DEEPSEEK, "deepseek", 0.00075),
        state=_now({TAVILY: {"needs_action": KEY}}),
        sources=RESULTS,
        call_id="call_ws2",
    ),
    # Tavily is cooling down, which is no failure of its own; Exa prices the request with its own estimate.
    "service_estimate": lambda: _chain(
        (
            _skip(TAVILY, "cooling", retry_at="2026-10-21T04:30:00Z"),
            _tried(EXA, "completed", basis="reported", usd=0.008, estimate=True),
        ),
        execution=_service_ran(EXA, "exa"),
        sources=RESULTS,
        call_id="call_ws3",
    ),
    # Two Sources blocked and the one tried unavailable before it sent anything: the blocked ones decide the reason.
    "service_blocked": lambda: _chain(
        (
            _skip(TAVILY, "blocked", needs_action=KEY),
            _skip(EXA, "blocked", needs_action=BALANCE),
            _tried(DEEPSEEK, error_code="service_unavailable", usd=0.0),
        ),
        status="failed",
        error="every search source is blocked or turned off",
        error_code="search_sources_blocked",
        state=_now({TAVILY: {"needs_action": KEY}, EXA: {"needs_action": BALANCE}}),
        call_id="call_ws4",
    ),
}


@pytest.mark.parametrize("case", SERVICE_CARDS)
def test_a_search_services_card_and_line_say_the_same_in_both_languages(case):
    """MH-HOSTED-UI-009, MH-HOSTED-UI-010: the card the Web renders from the shared fixture, and the IM line, from
    the records a search chain writes and its Sources' current state. The switch and repair notes are the very
    sentences the Web card shows."""

    activity = SERVICE_CARDS[case]()
    assert activity.to_payload() == SERVICE_FIXTURE[case]["payload"]
    for lang in ("en", "zh"):
        assert hosted_line(activity, lang) == SERVICE_FIXTURE[case][lang]["line"]


@pytest.mark.parametrize(
    ("attempts", "execution", "status", "ran", "cost", "line_tail"),
    [
        pytest.param(
            (_tried(TAVILY, "completed", usd=0.008),), _service_ran(TAVILY, "tavily", 0.008), "completed",
            {"executor_kind": "service", "source_id": TAVILY, "source_name": "Tavily", "provider": "tavily"},
            {"basis": "list", "usd": 0.008, "calls": 1}, "≈ $0.008",
            id="a service, named as its Source, never by its service id",
        ),
        pytest.param(
            (_tried(TAVILY, error_code="service_unavailable", usd=0.0),
             _tried(CLAUDE, "completed", kind="model", usd=0.01)),
            CLAUDE_RAN, "completed",
            {"executor_kind": "model", "source_id": CLAUDE, "source_name": "Anthropic API key", "provider": "anthropic",
             "model": "claude-haiku-5-5"},
            {"basis": "list", "usd": 0.01, "calls": 1}, "≈ $0.01",
            id="a model executor last: its execution adds the model the entry does not store",
        ),
        pytest.param(
            (_tried(CLAUDE, "completed", kind="model", usd=0.01),), XAI, "completed",
            {"executor_kind": "model", "source_id": CLAUDE, "source_name": "Anthropic API key"},
            {"basis": "list", "usd": 0.01, "calls": 1}, "≈ $0.01",
            id="an execution of another Source adds nothing",
        ),
        pytest.param(
            (_skip(TAVILY, "blocked", needs_action=KEY), _skip(EXA, "disabled")), _service_ran(TAVILY, "tavily", 0.008),
            "failed", {}, None, None,
            id="nothing tried: a stale execution names no executor and no cost",
        ),
        pytest.param(
            (_tried(CLAUDE, kind="model", error_code="provider_timed_out", usd=0.01),
             _tried(TAVILY, error_code="service_unavailable", usd=0.0)),
            None, "failed", {"executor_kind": "service", "source_id": TAVILY, "source_name": "Tavily"},
            {"basis": "list", "usd": 0.01, "calls": 1}, "≈ $0.01",
            id="a pre-send $0 after a billed model keeps the model's charge",
        ),
        pytest.param(
            (_tried(DEEPSEEK, error_code="service_unreadable"),), None, "failed",
            {"executor_kind": "service", "source_id": DEEPSEEK, "source_name": "DeepSeek"}, None, None,
            id="every tried charge unknown: no cost",
        ),
        pytest.param(
            (_tried(TAVILY, error_code="service_unreadable", usd=0.008),
             _tried(DEEPSEEK, error_code="service_unreadable")),
            None, "failed", {"executor_kind": "service", "source_id": DEEPSEEK, "source_name": "DeepSeek"},
            {"basis": "list", "usd": 0.008, "calls": 1, "at_least": True}, "at least ≈ $0.008",
            id="a known total beside an unknown charge is a lower bound",
        ),
        pytest.param(
            (_skip(TAVILY, "cooling", retry_at="2026-10-21T04:30:00Z"),
             _tried(CLAUDE, "completed", kind="model", basis="included")),
            replace(CLAUDE_RAN, executor_billing="monthly", cost=HostedCost(basis="included")), "completed",
            {"executor_kind": "model", "source_id": CLAUDE, "source_name": "Anthropic API key", "provider": "anthropic",
             "model": "claude-haiku-5-5"},
            {"basis": "included", "calls": 1}, "included in subscription",
            id="a skip sent nothing and never makes a total a lower bound",
        ),
        pytest.param(
            (_tried(TAVILY, error_code="service_unavailable", usd=0.0),
             _tried(EXA, "completed", basis="reported", usd=0.005)),
            _service_ran(EXA, "exa"), "completed",
            {"executor_kind": "service", "source_id": EXA, "source_name": "Exa", "provider": "exa"},
            {"basis": "reported", "usd": 0.005, "calls": 1}, "$0.005",
            id="an exact $0 beside an exact reported figure is not approximate",
        ),
        pytest.param(
            (_tried(EXA, "completed", basis="reported", usd=0.008, estimate=True),), _service_ran(EXA, "exa"), "completed",
            {"executor_kind": "service", "source_id": EXA, "source_name": "Exa", "provider": "exa"},
            {"basis": "reported", "usd": 0.008, "calls": 1, "estimate": True}, "≈ $0.008",
            id="a vendor's estimate is approximate",
        ),
        pytest.param(
            (_tried(DEEPSEEK, "completed", usd=0.0),), _service_ran(DEEPSEEK, "deepseek", 0.0), "completed",
            {"executor_kind": "service", "source_id": DEEPSEEK, "source_name": "DeepSeek", "provider": "deepseek"},
            {"basis": "list", "usd": 0, "calls": 1}, "$0",
            id="an exact zero is never an estimate",
        ),
        pytest.param(
            (_tried(KEYLESS, "completed", usd=0.0),), _service_ran(KEYLESS, "exa-mcp", 0.0), "completed",
            {"executor_kind": "service", "source_id": KEYLESS, "source_name": "Exa", "provider": "exa-mcp"},
            {"basis": "list", "usd": 0, "calls": 1}, "Free",
            id="keyless exact zero is free",
        ),
        pytest.param(
            (_tried(KEYLESS, error_code="service_rate_limited", usd=0.0), _tried(DEEPSEEK, "completed", kind="model", usd=0.01)),
            ProxyExecution(
                executor_origin=Origin(provider="deepseek", api="openai_chat", model="deepseek-chat"),
                executor_source_id=DEEPSEEK,
                executor_billing="metered",
                cost=HostedCost(basis="list", usd=0.01),
            ), "completed",
            {"executor_kind": "model", "source_id": DEEPSEEK, "source_name": "DeepSeek", "provider": "deepseek", "model": "deepseek-chat"},
            {"basis": "list", "usd": 0.01, "calls": 1}, "≈ $0.01",
            id="keyless rate limit falls back without key wording",
        ),
        pytest.param(
            (_tried(TAVILY, "completed", usd=0.01), _tried(KEYLESS, "completed", usd=0.0)), _service_ran(KEYLESS, "exa-mcp", 0.0), "completed",
            {"executor_kind": "service", "source_id": KEYLESS, "source_name": "Exa", "provider": "exa-mcp"},
            {"basis": "list", "usd": 0.01, "calls": 1}, "≈ $0.01",
            id="keyless after a paid attempt keeps the paid total",
        ),
        pytest.param(
            (_tried(KEYLESS, error_code="service_unavailable", usd=0.0),
             _tried(DEEPSEEK, "completed", kind="model")),
            ProxyExecution(
                executor_origin=Origin(provider="deepseek", api="openai_chat", model="deepseek-chat"),
                executor_source_id=DEEPSEEK,
                executor_billing="metered",
                cost=None,
            ), "completed",
            {"executor_kind": "model", "source_id": DEEPSEEK, "source_name": "DeepSeek", "provider": "deepseek", "model": "deepseek-chat"},
            None, None,
            id="a free failed attempt does not become at least zero",
        ),
        pytest.param(
            (_tried(TAVILY, error_code="service_unavailable", usd=0.008),
             _tried(DEEPSEEK, "completed", kind="model")),
            ProxyExecution(
                executor_origin=Origin(provider="deepseek", api="openai_chat", model="deepseek-chat"),
                executor_source_id=DEEPSEEK,
                executor_billing="metered",
                cost=None,
            ), "completed", {"executor_kind": "model", "source_id": DEEPSEEK, "source_name": "DeepSeek", "provider": "deepseek", "model": "deepseek-chat"},
            {"basis": "list", "usd": 0.008, "calls": 1, "at_least": True}, "at least ≈ $0.008",
            id="a priced failed attempt remains a lower bound beside an unpriced success",
        ),

        pytest.param(
            None, _service_ran(TAVILY, "tavily", 0.008), "completed",
            {"executor_kind": "service", "source_id": TAVILY, "source_name": "Tavily", "provider": "tavily"},
            {"basis": "list", "usd": 0.008, "calls": 1}, "≈ $0.008",
            id="without attempts, a search_api origin is still a service",
        ),
    ],
)
def test_a_search_chain_alone_says_who_ran_it_and_what_it_cost(attempts, execution, status, ran, cost, line_tail):
    """§4.3: with ``attempts``, the last tried entry ran the call and the tried entries' costs are its total; a skip
    sent nothing. ``execution`` keeps its released meaning for older readers and lends only its model, and only to
    the entry it names."""

    activity = _chain(attempts, status=status, execution=execution, sources=RESULTS[:1] if status == "completed" else ())
    payload = activity.to_payload()
    named = ("executor_kind", "source_id", "source_name", "provider", "model")
    assert {key: payload[key] for key in named if key in payload} == ran
    assert payload.get("cost") == cost
    line = hosted_line(activity, "en")
    assert line.endswith(f" · {line_tail}") if line_tail else "$" not in line


@pytest.mark.parametrize(
    ("attempt", "state", "shown"),
    [
        # Blocked: the skip stays as recorded, and the Source's current health chooses its repair.
        pytest.param(_skip(TAVILY, "blocked", needs_action=KEY), _now({TAVILY: {"needs_action": KEY}}),
                     ("reason.service_key_invalid", "repair.key", None), id="blocked: replace the key"),
        pytest.param(_skip(CLAUDE, "blocked", kind="model", needs_action=f"{NEEDS}oauth_expired"),
                     _now({CLAUDE: {"needs_action": f"{NEEDS}oauth_expired"}}),
                     ("skip.blocked", "repair.reauthorize", None), id="blocked: sign in again"),
        pytest.param(_skip(EXA, "blocked", needs_action=BALANCE), _now({EXA: {"needs_action": BALANCE}}),
                     ("reason.service_balance", "repair.balanceReenable", None),
                     id="blocked search Source: top up, re-enable"),
        pytest.param(_skip(DEEPSEEK, "blocked", needs_action=BALANCE), _now({DEEPSEEK: {"needs_action": BALANCE}}),
                     ("reason.service_balance", "repair.balance", None), id="blocked model Source: top up only"),
        pytest.param(_skip(EXA, "blocked", needs_action=LIMIT), _now({EXA: {"needs_action": LIMIT}}),
                     ("reason.service_limit", "repair.limitReenable", None),
                     id="blocked search Source: raise the limit, re-enable"),
        pytest.param(_skip(TAVILY, "blocked", needs_action=LIMIT), _now({TAVILY: {"needs_action": LIMIT}}),
                     ("reason.service_limit", "repair.tavilyLimit", None), id="Tavily: both of its limits"),
        pytest.param(_skip(CLAUDE, "blocked", kind="model", needs_action=f"{NEEDS}plan_excludes_product"),
                     _now({CLAUDE: {"needs_action": f"{NEEDS}plan_excludes_product"}}),
                     ("skip.blocked", "repair.plan", None), id="blocked: change the plan"),
        pytest.param(_skip(CLAUDE, "blocked", kind="model", needs_action=f"{NEEDS}account_banned"),
                     _now({CLAUDE: {"needs_action": f"{NEEDS}account_banned"}}),
                     ("skip.blocked", "repair.account", None), id="blocked: restore the account"),
        pytest.param(_skip(CLAUDE, "blocked", kind="model", needs_action="models.source.error.unclassified"),
                     _now({CLAUDE: {"needs_action": "models.source.error.unclassified"}}),
                     ("skip.blocked", "repair.error", None), id="blocked: a persistent error"),
        pytest.param(_skip(TAVILY, "blocked", needs_action=KEY), _now({TAVILY: {"needs_action": BALANCE}}),
                     ("reason.service_key_invalid", "repair.balanceReenable", None),
                     id="blocked, needing something else now"),
        pytest.param(_skip(TAVILY, "blocked", needs_action=KEY), HEALTHY,
                     ("reason.service_key_invalid", None, None), id="blocked, repaired since"),
        pytest.param(_skip(TAVILY, "blocked", needs_action=KEY), _now({TAVILY: None}),
                     ("reason.service_key_invalid", None, None), id="blocked, removed since"),
        pytest.param(_skip(TAVILY, "blocked", needs_action=KEY), UNREAD,
                     ("reason.service_key_invalid", None, None), id="blocked, Model Hub unread"),
        # Disabled: the remedy is turning search back on, while it is still off.
        pytest.param(_skip(EXA, "disabled"), _now({EXA: {"enabled": False}}),
                     ("skip.disabled", "repair.disabled", None), id="disabled, still off"),
        pytest.param(_skip(EXA, "disabled"), HEALTHY, ("skip.disabled", None, None), id="disabled, turned on since"),
        pytest.param(_skip(EXA, "disabled"), _now({EXA: None}), ("skip.disabled", None, None), id="disabled, removed since"),
        # Failed: the health its code set must still be the Source's, or a later action has superseded it.
        pytest.param(_tried(TAVILY, error_code="service_key_invalid", usd=0.0), _now({TAVILY: {"needs_action": KEY}}),
                     ("reason.service_key_invalid", "repair.key", None), id="failed: the health its code set"),
        pytest.param(_tried(TAVILY, error_code="service_limit", usd=0.0), _now({TAVILY: {"needs_action": LIMIT}}),
                     ("reason.service_limit", "repair.tavilyLimit", None), id="failed at Tavily's limit"),
        pytest.param(_tried(DEEPSEEK, error_code="service_balance", usd=0.0), _now({DEEPSEEK: {"needs_action": BALANCE}}),
                     ("reason.service_balance", "repair.balance", None), id="failed: a model Source's balance"),
        pytest.param(_tried(TAVILY, error_code="service_key_invalid", usd=0.0, superseded=True),
                     _now({TAVILY: {"needs_action": KEY}}), ("reason.superseded", None, True), id="superseded by a fence"),
        pytest.param(_tried(TAVILY, error_code="service_key_invalid", usd=0.0), HEALTHY,
                     ("reason.superseded", None, True), id="superseded: the key was replaced"),
        pytest.param(_tried(TAVILY, error_code="service_balance", usd=0.0), _now({TAVILY: {"needs_action": KEY}}),
                     ("reason.superseded", None, True), id="superseded: a later failure set other health"),
        pytest.param(_tried(TAVILY, error_code="service_unavailable", usd=0.0), _now({TAVILY: None}),
                     ("reason.superseded", None, True), id="superseded: the Source was removed"),
        pytest.param(_tried(TAVILY, error_code="service_key_invalid", usd=0.0), UNREAD,
                     ("reason.service_key_invalid", None, None), id="failed, Model Hub unread: no repair"),
        pytest.param(_tried(TAVILY, error_code="service_unavailable", usd=0.0), _now({TAVILY: {"needs_action": KEY}}),
                     ("reason.service_unavailable", None, None), id="a transient failure set no health"),
        pytest.param(_tried(CLAUDE, kind="model", error_code="provider_timed_out", usd=0.01), HEALTHY,
                     ("reason.failed", None, None), id="a model executor's failure"),
        pytest.param(_tried(CLAUDE, kind="model", usd=0.01), HEALTHY, ("reason.failed", None, None),
                     id="a model executor's failure without a code"),
        # Skips that are no failure of the Source's own, and need nothing.
        pytest.param(_skip(TAVILY, "cooling", retry_at="2026-10-21T04:30:00Z"), HEALTHY,
                     ("skip.cooling", None, None), id="cooling"),
        pytest.param(_skip(KEYLESS, "cooling"), _now({KEYLESS: {"enabled": True}}),
                     ("skip.cooling", "repair.retrySearch", None), id="keyless cooling: retry search, never credentials"),
        pytest.param(_tried(KEYLESS, error_code="service_rate_limited", usd=0.0), _now({KEYLESS: {"enabled": True}}),
                     ("reason.service_rate_limited", "repair.retrySearch", None), id="keyless rate limit: retry search, never credentials"),
        pytest.param(_skip(KEYLESS, "cooling"), _now({KEYLESS: {"enabled": False}}),
                     ("skip.cooling", None, None), id="keyless disabled: no credential repair"),
        pytest.param(_skip(DEEPSEEK, "query_too_long"), HEALTHY, ("skip.query_too_long", None, None), id="query too long"),
        pytest.param(_skip(TAVILY, "unavailable"), _now({TAVILY: None}), ("skip.unavailable", None, None), id="unavailable"),
    ],
)
def test_each_source_a_chain_reached_shows_what_it_needs_now(attempt, state, shown):
    """§4.3: the record keeps what happened; the Source's current state decides the repair and whether a failure
    still stands, once, for the card and the IM line alike. A Source repaired, re-enabled or removed since shows no
    repair, and without a Model Hub read none is offered."""

    [entry] = _chain((attempt,), status="failed", state=state).attempts
    assert (entry.reason_key, entry.remedy_key, entry.superseded) == shown
    assert (entry.outcome, entry.skipped, entry.needs_action, entry.error_code, entry.cost) == (
        attempt.outcome, attempt.skipped, attempt.needs_action, attempt.error_code, attempt.cost
    )


@pytest.mark.parametrize(
    ("attempts", "names", "state", "switch", "notes"),
    [
        pytest.param(
            (_tried(TAVILY, error_code="service_key_invalid", usd=0.0), _tried(DEEPSEEK, "completed", usd=0.0)), LABELS,
            _now({TAVILY: {"needs_action": KEY}}),
            {"sources": ["Tavily"], "reason_key": "reason.service_key_invalid", "to": "DeepSeek"},
            ("Tavily has an invalid key; switched to DeepSeek", "Tavily 密钥无效，已改用 DeepSeek"),
            id="one Source, with its reason",
        ),
        pytest.param(
            (_skip(TAVILY, "blocked", needs_action=KEY), _tried(EXA, error_code="service_unavailable", usd=0.0),
             _tried(DEEPSEEK, "completed", usd=0.0)), LABELS, _now({TAVILY: {"needs_action": KEY}}),
            {"sources": ["Tavily", "Exa"], "reason_key": "reason.failed", "to": "DeepSeek"},
            ("Tavily, Exa failed; switched to DeepSeek", "Tavily、Exa 失败，已改用 DeepSeek"),
            id="several Sources failed",
        ),
        pytest.param(
            (_tried(TAVILY, error_code="service_key_invalid", usd=0.0), _tried(DEEPSEEK, "completed", usd=0.0)),
            {TAVILY: "My Tavily"}, _now({DEEPSEEK: None}),
            {"sources": ["My Tavily"], "reason_key": "reason.superseded", "to": "DeepSeek"},
            ("My Tavily failed on this request; a later action has since updated its state; switched to DeepSeek",
             "My Tavily 这次请求失败，状态已由之后的操作更新，已改用 DeepSeek"),
            id="a Source renamed and repaired since, and one removed",
        ),
        pytest.param(
            (_skip(TAVILY, "cooling", retry_at="2026-10-21T04:30:00Z"), _skip(EXA, "query_too_long"),
             _skip(CLAUDE, "unavailable", kind="model"), _tried(DEEPSEEK, "completed", usd=0.0)), LABELS, HEALTHY,
            None, None, id="cooling, a long query and a Source gone since are no failure",
        ),
    ],
)
def test_a_completed_search_names_the_sources_it_moved_past(attempts, names, state, switch, notes):
    """§4.3, §6.5: the switch note names the failed Sources and the blocked and disabled skips before the one that
    answered, each Source by its current name, else the name it was recorded with."""

    activity = _chain(attempts, execution=_service_ran(DEEPSEEK, "deepseek"), names=names, state=state)
    assert activity.to_payload().get("switch") == switch
    for lang, note in zip(("en", "zh"), notes or (None, None)):
        line = hosted_line(activity, lang)
        assert (f" · {note} · " in line) if note else (" · DeepSeek · " in line)


ATTEMPT_COPY = (
    "reason.failed",
    "reason.superseded",
    *(f"reason.{code}" for code in HOSTED_ERROR_CODES if code.startswith("service_")),
    *(f"skip.{reason}" for reason in HOSTED_SKIP_REASONS),
    *(f"repair.{repair}" for repair in (
        "key", "reauthorize", "balance", "balanceReenable", "limit", "limitReenable", "tavilyLimit", "plan",
        "account", "error", "disabled",
    )),
    "repair.retrySearch",
    "listSeparator",
    "itemSeparator",
    "switch",
)


@pytest.mark.parametrize("key", ATTEMPT_COPY)
def test_every_word_a_search_chain_is_shown_with_has_copy_on_every_surface(key):
    """§6.5: each reason and repair the projection can choose reads in both languages on the IM line and the Web
    card, which name it by the same key under their hosted-tool copy."""

    for lang in ("en", "zh"):
        im = f"hostedTools.{key}"
        assert i18n_t(im, lang) != im, f"vibe/i18n/{lang}.json has no {im}"
        node = json.loads((UI_BUNDLES / f"{lang}.json").read_text(encoding="utf-8"))["chat"]["agentActivity"]["hosted"]
        for part in key.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        assert isinstance(node, str) and node, f"ui/src/i18n/{lang}.json has no chat.agentActivity.hosted.{key}"
