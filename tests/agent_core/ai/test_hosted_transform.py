"""Hosted calls in history (``vibey-hosted-tools.md`` §4.4, §5). Scenario MH-HOSTED-VIBEY-003.

Until native replay is authorized, every hosted call goes back in portable form. A finished response's calls become
synthetic same-named calls and results, splitting the assistant message around them, with one record-to-text
function writing every result. A response its turn is still waiting on keeps each call in place as text. Citations
are never sent.
"""

from __future__ import annotations

import itertools

import pytest

from core.agent_core.ai.provider import HostedToolRoute, ModelEndpoint, ModelRequest
from tests.agent_core.ai._wire import build_wire_request
from core.agent_core.ai.transform import SYNTHETIC_TOOL_RESULT, transform_messages
from core.agent_core.messages import (
    AssistantMessage,
    Citation,
    HostedAction,
    HostedNative,
    HostedRecord,
    HostedSource,
    HostedSummary,
    HostedToolBlock,
    Origin,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
)

XAI = Origin("xai", "openai_responses", "grok-4.6")
SOURCE = "src_served00001"
RELEASES = HostedSource(
    url="https://github.com/router-for-me/CLIProxyAPI/releases",
    title="Releases",
    snippet="v8.0.27 was released on October 12.",
    page_age="1 day ago",
)


def _hosted(call_id: str, tool: str, action: HostedAction, record: HostedRecord) -> HostedToolBlock:
    return HostedToolBlock(
        id=call_id,
        tool=tool,  # type: ignore[arg-type]
        action=action,
        record=record,
        native=HostedNative(source_id=SOURCE, payload=({"type": "web_search_call", "id": call_id},)),
    )


SEARCH = _hosted(
    "ws_1",
    "web_search",
    HostedAction("search", queries=("CLIProxyAPI latest release", "CLIProxyAPI v8")),
    HostedRecord(status="completed", sources=(RELEASES,)),
)
ANSWER = TextBlock(
    text="v8.0.27, released October 12.",
    citations=(Citation(url=RELEASES.url, start=0, end=7),),
)


def _response(*content, stop_reason="stop", summary=HostedSummary()) -> AssistantMessage:
    return AssistantMessage(tuple(content), XAI, stop_reason, hosted_summary=summary)


def _follow_up() -> UserMessage:
    return UserMessage((TextBlock("And who published it?"),))


SEARCH_TEXT = (
    '[web_search: "CLIProxyAPI latest release"]\n'
    'Also searched: "CLIProxyAPI v8"\n'
    "1. Releases\n"
    "   https://github.com/router-for-me/CLIProxyAPI/releases\n"
    "   v8.0.27 was released on October 12.\n"
    "   Published: 1 day ago"
)


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [
        (
            "anthropic",
            [
                {"role": "user", "content": "search"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "ws_1", "name": "web_search", "input": {"query": "CLIProxyAPI latest release"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "ws_1", "content": [{"type": "text", "text": SEARCH_TEXT}], "is_error": False}
                    ],
                },
                {"role": "assistant", "content": [{"type": "text", "text": ANSWER.text}]},
                {"role": "user", "content": "And who published it?"},
            ],
        ),
        (
            "openai_responses",
            [
                {"role": "user", "content": [{"type": "input_text", "text": "search"}]},
                {"type": "function_call", "call_id": "ws_1", "name": "web_search", "arguments": '{"query": "CLIProxyAPI latest release"}'},
                {"type": "function_call_output", "call_id": "ws_1", "output": SEARCH_TEXT},
                {"role": "assistant", "content": [{"type": "output_text", "text": ANSWER.text}]},
                {"role": "user", "content": [{"type": "input_text", "text": "And who published it?"}]},
            ],
        ),
        (
            "openai_chat",
            [
                {"role": "user", "content": "search"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "ws_1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "CLIProxyAPI latest release"}'}}
                    ],
                },
                {"role": "tool", "tool_call_id": "ws_1", "content": SEARCH_TEXT},
                {"role": "assistant", "content": ANSWER.text},
                {"role": "user", "content": "And who published it?"},
            ],
        ),
    ],
)
async def test_a_replayed_search_is_a_synthetic_same_named_call_and_result_on_every_protocol(protocol, expected):
    history = (UserMessage((TextBlock("search"),)), _response(SEARCH, ANSWER), _follow_up())
    request = ModelRequest(
        endpoint=ModelEndpoint(protocol, "http://hub.invalid/vibey/v1", "model-x", "hub-token", provider="other"),  # type: ignore[arg-type]
        system="system",
        messages=history,
        tools=(),
        max_tokens=256,
        cache="none",
    )

    wire = await build_wire_request(request, gateway=True)

    sent = wire.body["messages"] if protocol != "openai_responses" else wire.body["input"]
    sent = [message for message in sent if message.get("role") != "system"]
    assert _comparable(sent, expected) == expected


def _comparable(sent: list[dict], expected: list[dict]) -> list[dict]:
    """The sent items reduced to the keys the expectation names (ids and the like may carry more)."""

    def reduce(value, shape):
        if isinstance(shape, dict) and isinstance(value, dict):
            return {key: reduce(value.get(key), shape[key]) for key in shape}
        if isinstance(shape, list) and isinstance(value, list) and len(shape) == len(value):
            return [reduce(item, item_shape) for item, item_shape in zip(value, shape)]
        return value

    return [reduce(item, shape) for item, shape in zip(sent, expected)] + sent[len(expected) :]


def _transformed(*history):
    return list(transform_messages(history, target=Origin("anthropic", "anthropic", "claude-opus-5-5")))


def test_a_refused_pending_hosted_call_is_omitted_without_touching_signed_thinking():
    target = Origin("anthropic", "anthropic", "claude-opus-5-5")
    pending = _hosted("ws_pending", "web_search", HostedAction("search", queries=("q",)), HostedRecord(status="pending"))
    thinking = ThinkingBlock(text="", signature="signed")
    client = ToolCallBlock("toolu_read", "read", {"path": "a"})
    response = AssistantMessage(
        (thinking, pending, client), target, "tool_use", hosted_summary=HostedSummary(), source_identity="identity",
        hosted_render_epoch=2,
    )
    endpoint = ModelEndpoint(
        "anthropic", "http://hub.invalid/vibey/v1", target.model, "hub-token", provider="anthropic",
        source_id=SOURCE, source_identity="identity",
        hosted_tools={"web_search": HostedToolRoute("native", True, declaration={"type": "web_search_20250305"})},
        opaque_refused=frozenset({("identity", "hosted")}),
    )

    result = transform_messages(
        (UserMessage((TextBlock("go"),)), response, ToolResultMessage("toolu_read", "read", (TextBlock("contents"),))),
        target=target,
        protocol="anthropic",
        endpoint=endpoint,
    )

    sent = result.messages[1]
    assert isinstance(sent, AssistantMessage)
    assert sent.content == (thinking, client)
    assert not any(isinstance(block, TextBlock) and "no result" in block.text for block in sent.content)


def test_refused_pending_hosted_render_keeps_legacy_shape_until_later_signed_history_is_new():
    target = Origin("anthropic", "anthropic", "claude-opus-5-5")
    pending = _hosted("ws_pending", "web_search", HostedAction("search", queries=("q",)), HostedRecord(status="pending"))
    response = AssistantMessage(
        (ThinkingBlock(text="", signature="signed"), ToolCallBlock("toolu_read", "read", {}), pending),
        target, "tool_use", hosted_summary=HostedSummary(), source_identity="identity", hosted_render_epoch=2,
    )
    later = AssistantMessage(
        (ThinkingBlock(text="later", signature="older-signature"), TextBlock(text="later")), target, "stop"
    )
    endpoint = ModelEndpoint(
        "anthropic", "http://hub.invalid/vibey/v1", target.model, "hub-token", provider="anthropic",
        source_id=SOURCE, source_identity="identity",
        hosted_tools={"web_search": HostedToolRoute("native", True, declaration={"type": "web_search_20250305"})},
        opaque_refused=frozenset({("identity", "hosted")}),
    )

    result = transform_messages(
        (UserMessage((TextBlock("go"),)), response, ToolResultMessage("toolu_read", "read", (TextBlock("contents"),)), later),
        target=target,
        protocol="anthropic",
        endpoint=endpoint,
    )

    sent = result.messages[1]
    assert isinstance(sent, AssistantMessage)
    assert any(isinstance(block, TextBlock) and block.text.endswith("(no result)") for block in sent.content)


def test_legacy_refused_pending_response_stays_legacy_after_new_epoch_history():
    target = Origin("anthropic", "anthropic", "claude-opus-5-5")
    pending = _hosted("ws_pending", "web_search", HostedAction("search", queries=("q",)), HostedRecord(status="pending"))
    response = AssistantMessage(
        (ThinkingBlock(text="", signature="signed"), pending),
        target, "tool_use", hosted_summary=HostedSummary(), source_identity="identity",
    )
    later = AssistantMessage(
        (ThinkingBlock(text="later", signature="new-signature"), TextBlock(text="later")), target, "stop",
        hosted_render_epoch=2,
    )
    endpoint = ModelEndpoint(
        "anthropic", "http://hub.invalid/vibey/v1", target.model, "hub-token", provider="anthropic",
        source_id=SOURCE, source_identity="identity",
        hosted_tools={"web_search": HostedToolRoute("native", True, declaration={"type": "web_search_20250305"})},
        opaque_refused=frozenset({("identity", "hosted")}),
    )

    result = transform_messages(
        (UserMessage((TextBlock("go"),)), response, later),
        target=target,
        protocol="anthropic",
        endpoint=endpoint,
    )

    sent = result.messages[1]
    assert isinstance(sent, AssistantMessage)
    assert any(isinstance(block, ToolCallBlock) for block in sent.content)


def test_refused_pending_hosted_b_does_not_change_non_anthropic_targets():
    target = Origin("google", "openai_chat", "gemini-3-pro")
    pending = _hosted("ws_pending", "web_search", HostedAction("search", queries=("q",)), HostedRecord(status="pending"))
    response = AssistantMessage(
        (ThinkingBlock(text="", signature="signed"), pending, ToolCallBlock("toolu_read", "read", {})),
        Origin("anthropic", "anthropic", "claude-opus-5-5"), "tool_use", hosted_summary=HostedSummary(),
        source_identity="identity", hosted_render_epoch=2,
    )
    endpoint = ModelEndpoint(
        "openai_chat", "http://hub.invalid/v1", target.model, "hub-token", provider="google",
        source_id=SOURCE, source_identity="identity", hosted_tools={}, opaque_refused=frozenset({("identity", "hosted")}),
    )

    result = transform_messages(
        (UserMessage((TextBlock("go"),)), response, ToolResultMessage("toolu_read", "read", (TextBlock("contents"),))),
        target=target,
        protocol="openai_chat",
        endpoint=endpoint,
    )

    sent = result.messages[1]
    assert isinstance(sent, AssistantMessage)
    assert any(isinstance(block, TextBlock) and block.text.endswith("(no result)") for block in sent.content)


def test_every_hosted_call_is_its_own_synthetic_call_and_a_page_action_says_what_it_did():
    """§5 as implemented (option b): no action folds into another call's result. A page action is its own call whose
    query names it, and its completed result says what it did."""

    opened = _hosted(
        "ws_2",
        "web_search",
        HostedAction("open_page", url="https://example.test/notes"),
        HostedRecord(status="completed"),
    )

    _, calls, search, page, *_ = _transformed(UserMessage((TextBlock("go"),)), _response(SEARCH, opened, ANSWER), _follow_up())

    assert [(block.name, block.arguments) for block in calls.content] == [
        ("web_search", {"query": "CLIProxyAPI latest release"}),
        ("web_search", {"query": "open_page https://example.test/notes"}),
    ]
    assert search.content[0].text == SEARCH_TEXT
    assert page.content[0].text == '[web_search: "open_page https://example.test/notes"]\nOpened https://example.test/notes'


@pytest.mark.parametrize(
    ("record", "text", "is_error"),
    [
        (HostedRecord(status="failed", error="max_uses_exceeded"), '[web_search failed: max_uses_exceeded]\nQuery: "q"', True),
        (HostedRecord(status="incomplete", error="cut off"), '[web_search failed: cut off]\nQuery: "q"', True),
        (HostedRecord(status="pending"), SYNTHETIC_TOOL_RESULT, True),
        (HostedRecord(status="completed"), '[web_search: "q"]\nNo results were returned.', False),
    ],
    ids=["failed", "incomplete", "pending", "empty"],
)
def test_a_failed_or_cut_off_call_never_reads_as_a_successful_empty_result(record, text, is_error):
    block = _hosted("ws_9", "web_search", HostedAction("search", queries=("q",)), record)

    _, _, result, *_ = _transformed(UserMessage((TextBlock("go"),)), _response(block, ANSWER), _follow_up())

    assert (result.content[0].text, result.is_error) == (text, is_error)


def test_the_posts_an_x_answer_cites_are_listed_once_in_its_first_x_search_result():
    post = HostedSource(url="https://x.com/OpenAI/status/1")
    searches = [
        _hosted(f"ctc_{n}", "x_search", HostedAction("x_search", variant="keyword", query=f"q{n}"), HostedRecord(status="completed"))
        for n in range(2)
    ]

    messages = _transformed(
        UserMessage((TextBlock("go"),)),
        _response(*searches, ANSWER, summary=HostedSummary(counts={"x_search_calls": 2}, cited_sources=(post,))),
        _follow_up(),
    )

    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert [post.url in result.content[0].text for result in results] == [True, False]
    assert results[0].content[0].text.endswith("Posts cited in the answer:\n1. https://x.com/OpenAI/status/1")


def test_a_response_its_turn_still_waits_on_keeps_each_call_in_place_as_text():
    """§4.4: within a turn, a hosted call stays where it was, as one text block, and signed thinking stays intact."""

    thinking = ThinkingBlock(text="", signature="signed")
    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    running = AssistantMessage((thinking, SEARCH, read), Origin("anthropic", "anthropic", "claude-opus-5-5"), "tool_use", hosted_summary=HostedSummary())

    user, sent, result = _transformed(
        UserMessage((TextBlock("go"),)), running, ToolResultMessage("toolu_1", "read", (TextBlock("contents"),))
    )

    assert sent.content == (thinking, TextBlock(text=SEARCH_TEXT), read)
    assert result.tool_call_id == "toolu_1"


@pytest.mark.parametrize(
    ("action", "record", "text"),
    [
        (
            HostedAction("open_page", url="https://a.test"),
            HostedRecord(status="failed", error="timeout"),
            '[web_search failed: timeout]\nQuery: "open_page https://a.test"',
        ),
        (HostedAction("find_in_page", url="https://a.test", pattern="v8"), HostedRecord(status="pending"), None),
        (
            HostedAction("open_page", url="https://a.test"),
            HostedRecord(status="completed"),
            '[web_search: "open_page https://a.test"]\nOpened https://a.test',
        ),
    ],
    ids=["failed", "pending", "completed"],
)
def test_an_in_flight_non_search_action_keeps_its_status_in_place(action, record, text):
    """A failed or unfinished page action in a running turn never reads as done."""

    block = _hosted("ws_5", "web_search", action, record)
    running = AssistantMessage(
        (block, ToolCallBlock(id="toolu_1", name="read", arguments={})),
        Origin("anthropic", "anthropic", "claude-opus-5-5"),
        "tool_use",
        hosted_summary=HostedSummary(),
    )

    _, sent, _ = _transformed(
        UserMessage((TextBlock("go"),)), running, ToolResultMessage("toolu_1", "read", (TextBlock("contents"),))
    )

    rendered = sent.content[0].text
    if text is None:
        assert rendered.endswith("(no result)") and "Opened" not in rendered and "Searched" not in rendered
    else:
        assert rendered == text


@pytest.mark.parametrize("status", ["failed", "pending"])
def test_a_page_action_that_did_not_complete_is_its_own_failed_call_never_a_line_in_the_previous_result(status):
    record = HostedRecord(status="failed", error="timeout") if status == "failed" else HostedRecord(status="pending")
    opened = _hosted("ws_2", "web_search", HostedAction("open_page", url="https://example.test/notes"), record)

    messages = _transformed(UserMessage((TextBlock("go"),)), _response(SEARCH, opened, ANSWER), _follow_up())

    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert [result.is_error for result in results] == [False, True]
    assert "Opened" not in results[0].content[0].text
    assert results[1].content[0].text in ('[web_search failed: timeout]\nQuery: "open_page https://example.test/notes"', SYNTHETIC_TOOL_RESULT)


_RECORDS = {
    "completed": HostedRecord(status="completed"),
    "failed": HostedRecord(status="failed", error="timeout"),
    "pending": HostedRecord(status="pending"),
}


@pytest.mark.parametrize("statuses", list(itertools.product(_RECORDS, repeat=3)), ids="-".join)
def test_every_hosted_call_is_one_result_that_shows_its_own_status(statuses):
    """For every status of a search and two page actions: each call is exactly one result; a completed page action's
    line appears once, in its own result; a call that did not complete is an error that says so."""

    actions = (
        HostedAction("search", queries=("q",)),
        HostedAction("open_page", url="https://a.test/1"),
        HostedAction("find_in_page", url="https://a.test/2", pattern="x"),
    )
    blocks = [
        _hosted(f"ws_{n}", "web_search", action, _RECORDS[status]) for n, (action, status) in enumerate(zip(actions, statuses))
    ]

    messages = _transformed(UserMessage((TextBlock("go"),)), _response(*blocks, ANSWER), _follow_up())

    results = [message for message in messages if isinstance(message, ToolResultMessage)]
    assert [result.tool_call_id for result in results] == ["ws_0", "ws_1", "ws_2"]
    lines = ("", "Opened https://a.test/1", 'Searched https://a.test/2 for "x"')
    for result, status, line in zip(results, statuses, lines):
        text = result.content[0].text
        assert result.is_error is (status != "completed")
        if status == "failed":
            assert text.startswith("[web_search failed: timeout]")
        elif status == "pending":
            assert text == SYNTHETIC_TOOL_RESULT
        if line:
            assert sum(other.content[0].text.count(line) for other in results) == (1 if status == "completed" else 0)
            assert (line in text) is (status == "completed")


def test_the_posts_an_answer_cites_show_even_when_its_first_x_search_failed():
    """Every line attached to a result shows whatever its status: the cited posts land on the first X search that was
    not cut off, and a failed one renders them after its status line."""

    post = HostedSource(url="https://x.com/OpenAI/status/1")
    failed = _hosted("ctc_0", "x_search", HostedAction("x_search", variant="keyword", query="a"), _RECORDS["failed"])
    found = _hosted("ctc_1", "x_search", HostedAction("x_search", variant="keyword", query="b"), _RECORDS["completed"])

    messages = _transformed(
        UserMessage((TextBlock("go"),)),
        _response(failed, found, ANSWER, summary=HostedSummary(cited_sources=(post,))),
        _follow_up(),
    )

    texts = [message.content[0].text for message in messages if isinstance(message, ToolResultMessage)]
    assert sum(text.count(post.url) for text in texts) == 1
    assert texts[0].startswith("[x_search failed: timeout]") and post.url in texts[0]


def test_a_hosted_call_after_a_client_call_stays_in_place_so_no_split_separates_the_client_call_from_its_result():
    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    after = TextBlock(text="reading it now")
    response = AssistantMessage(
        (read, SEARCH, after), XAI, "tool_use", hosted_summary=HostedSummary()
    )

    messages = _transformed(
        UserMessage((TextBlock("go"),)),
        response,
        ToolResultMessage("toolu_1", "read", (TextBlock("contents"),)),
        AssistantMessage((TextBlock(text="done"),), XAI, "stop"),
        _follow_up(),
    )

    _, sent, result, *_ = messages
    assert sent.content == (read, TextBlock(text=SEARCH_TEXT), after)
    assert result.tool_call_id == "toolu_1" and result.content[0].text == "contents"
    assert not any(
        isinstance(message, ToolResultMessage) and message.content[0].text == SYNTHETIC_TOOL_RESULT for message in messages
    )


@pytest.mark.parametrize("form", ["in-flight", "after-a-client-call"])
def test_the_posts_an_answer_cites_show_on_a_call_kept_in_place(form):
    post = HostedSource(url="https://x.com/OpenAI/status/1")
    search = _hosted("ctc_0", "x_search", HostedAction("x_search", variant="keyword", query="a"), _RECORDS["completed"])
    read = ToolCallBlock(id="toolu_1", name="read", arguments={"path": "a"})
    content = (search, read) if form == "in-flight" else (read, search)
    response = AssistantMessage(content, XAI, "tool_use", hosted_summary=HostedSummary(cited_sources=(post,)))
    later = () if form == "in-flight" else (AssistantMessage((TextBlock(text="done"),), XAI, "stop"), _follow_up())

    messages = _transformed(
        UserMessage((TextBlock("go"),)), response, ToolResultMessage("toolu_1", "read", (TextBlock("contents"),)), *later
    )

    in_place = [block.text for block in messages[1].content if isinstance(block, TextBlock)]
    assert len(in_place) == 1 and in_place[0].endswith("Posts cited in the answer:\n1. https://x.com/OpenAI/status/1")
