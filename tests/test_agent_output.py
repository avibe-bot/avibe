"""The typed output events and their one mapping onto the dispatcher (MESSAGE-DELIVERY-336).

Every backend says what an output is (``core.agent_output``); ``dispatch_call`` is the one
place it becomes the dispatcher's message type, lifecycle, and options. The mapping must
reproduce exactly the call each backend made before the events existed, so moving the
backends onto events changes no behavior.
"""

from __future__ import annotations

import pytest

from core.agent_output import (
    AgentError,
    DeliveryReceipt,
    Narration,
    Notice,
    Reply,
    ToolActivity,
    TurnEnd,
    TurnOutcome,
    dispatch_call,
    emit_output,
)
from core.citations import CitationBundle
from core.delivery_evidence import DeliveryEvidence
from core.message_output import MessageOutput

LINEAGE = MessageOutput(completes_turn=True, completes_run=True, run_id="run_1", idempotency_key="final")
TURN_ONLY = MessageOutput(completes_turn=True, completes_run=False)
ACTIVITY = MessageOutput(completes_turn=False, completes_run=True, detached=True, activity_id="act_1")
CITATIONS = CitationBundle(citations=(), markers=(), sources=())
EVIDENCE = DeliveryEvidence()
HOSTED = {"tool": "web_search"}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            Reply("4."),
            ("result", "4.", {"parse_mode": "markdown", "output": MessageOutput(completes_turn=False, completes_run=False)}),
            id="reply",
        ),
        pytest.param(
            Reply(
                "4.",
                citations=CITATIONS,
                footer="⏱ 2s",
                failed=True,
                output=MessageOutput(completes_turn=False, completes_run=False, idempotency_key="msg_2"),
            ),
            (
                "result",
                "4.",
                {
                    "parse_mode": "markdown",
                    "output": MessageOutput(completes_turn=False, completes_run=False, idempotency_key="msg_2"),
                    "is_error": True,
                    "result_footer": "⏱ 2s",
                    "citations": CITATIONS,
                },
            ),
            id="reply-identified-failed-detached-shape",
        ),
        pytest.param(
            Reply("", output=ACTIVITY),
            ("result", "", {"parse_mode": "markdown", "output": ACTIVITY}),
            id="detached-work-content-free-answer",
        ),
        pytest.param(
            Narration("Looking."),
            ("assistant", "Looking.", {"parse_mode": "markdown"}),
            id="narration-keeps-no-output",
        ),
        pytest.param(
            Narration(
                "_🧠 weighing_",
                process_only=True,
                citations=CITATIONS,
                status_label="thinking",
                output=MessageOutput(completes_turn=False, completes_run=False, persisted_row_id="row_1"),
            ),
            (
                "assistant",
                "_🧠 weighing_",
                {
                    "parse_mode": "markdown",
                    "level": "process",
                    "status_label": "thinking",
                    "output": MessageOutput(completes_turn=False, completes_run=False, persisted_row_id="row_1"),
                    "citations": CITATIONS,
                },
            ),
            id="narration-process-only-committed-row",
        ),
        pytest.param(
            ToolActivity("🔧 `rg lock`", status_label="rg lock", hosted=HOSTED),
            ("toolcall", "🔧 `rg lock`", {"parse_mode": "markdown", "status_label": "rg lock", "hosted": HOSTED}),
            id="tool-activity",
        ),
        pytest.param(
            TurnEnd(),
            ("result", "", {"parse_mode": "markdown", "output": MessageOutput(completes_turn=True, completes_run=True)}),
            id="content-free-end",
        ),
        pytest.param(
            TurnEnd(text="Done.", footer="⏱ 2s", citations=CITATIONS, output=LINEAGE),
            (
                "result",
                "Done.",
                {"parse_mode": "markdown", "output": LINEAGE, "result_footer": "⏱ 2s", "citations": CITATIONS},
            ),
            id="end-with-its-reply",
        ),
        pytest.param(
            TurnEnd(outcome=TurnOutcome.FAILED, silent=True, terminal_error="boom", output=LINEAGE),
            (
                "result",
                "",
                {"parse_mode": "markdown", "output": LINEAGE, "is_error": True, "level": "silent", "terminal_error": "boom"},
            ),
            id="failed-end",
        ),
        pytest.param(
            TurnEnd(outcome=TurnOutcome.STOPPED, silent=True, output=LINEAGE),
            (
                "result",
                "",
                {
                    "parse_mode": "markdown",
                    "output": MessageOutput(
                        completes_turn=True,
                        completes_run=False,
                        run_id="run_1",
                        idempotency_key="final",
                        settled_by="stopped",
                    ),
                    "level": "silent",
                },
            ),
            id="stopped-end-leaves-its-runs-to-the-stop",
        ),
        pytest.param(
            TurnEnd(outcome=TurnOutcome.TEARDOWN, silent=True),
            (
                "result",
                "",
                {
                    "parse_mode": "markdown",
                    "output": MessageOutput(completes_turn=True, completes_run=False, settled_by="backend_refresh"),
                    "is_error": True,
                    "level": "silent",
                },
            ),
            id="teardown-end",
        ),
        pytest.param(
            TurnEnd(output=TURN_ONLY),
            ("result", "", {"parse_mode": "markdown", "output": TURN_ONLY}),
            id="turn-only-release",
        ),
        pytest.param(
            AgentError("❌ Codex turn failed", output=TURN_ONLY, delivery=EVIDENCE),
            ("notify", "❌ Codex turn failed", {"parse_mode": "markdown", "output": TURN_ONLY, "delivery": EVIDENCE}),
            id="agent-error",
        ),
        pytest.param(
            Notice("Resuming interrupted OpenCode session after restart..."),
            ("notify", "Resuming interrupted OpenCode session after restart...", {"parse_mode": "markdown"}),
            id="notice",
        ),
    ],
)
def test_each_event_maps_to_one_dispatch_call(event, expected):
    """MESSAGE-DELIVERY-336: the mapping names the type and the lifecycle; an option a
    dispatcher stand-in may not accept is passed only when the event sets it."""
    assert dispatch_call(event) == expected


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(Reply("4.", output=MessageOutput(completes_turn=True)), id="a-reply-that-ends-the-turn"),
        pytest.param(TurnEnd(output=ACTIVITY), id="a-turn-end-that-does-not-end-it"),
    ],
)
def test_an_event_cannot_contradict_its_lifecycle(event):
    """MESSAGE-DELIVERY-336: a Reply never ends the Turn and a TurnEnd always does; an
    output that says otherwise is a programming error, not a silent re-typing."""
    with pytest.raises(ValueError):
        dispatch_call(event)


async def test_an_emit_answers_with_its_delivery_receipt():
    """MESSAGE-DELIVERY-336: the single entry hands the dispatcher the mapped call and
    returns what it proved as a receipt: the delivered id, or nothing delivered."""
    calls = []

    class _Controller:
        def __init__(self, answer):
            self.answer = answer

        async def emit_agent_message(self, context, message_type, text, **options):
            calls.append((context, message_type, text, options))
            return self.answer

    delivered = await emit_output(_Controller("msg_1"), "ctx", Narration("Looking."))
    missed = await emit_output(_Controller(None), "ctx", Notice("Resuming."))

    assert calls == [
        ("ctx", "assistant", "Looking.", {"parse_mode": "markdown"}),
        ("ctx", "notify", "Resuming.", {"parse_mode": "markdown"}),
    ]
    assert (delivered, delivered.delivered) == (DeliveryReceipt("msg_1"), True)
    assert (missed, missed.delivered) == (DeliveryReceipt(None), False)
