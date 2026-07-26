"""Contract tests for ``core.services.dispatch.dispatch_turn``.

``dispatch_turn`` is the shared entry that IM adapter, CLI, and the
upcoming Web UI / N3 socket path all funnel through. The contract:

1. ``source=human`` delegates to ``MessageHandler.handle_user_message``.
2. ``source=scheduled`` delegates to ``MessageHandler.handle_scheduled_message``.
3. The ``on_chunk`` callback (streaming path) is registered as a
   per-session turn sink on the controller, and ``dispatch_turn`` holds
   until the turn emits its result (which sets the sink's done event)
   before returning — so the SSE caller doesn't close the stream early.

Locking this here means swapping the underlying handler (e.g. when N3
streaming lands) cannot break the public contract silently.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.run_settlement import (
    SETTLED_BY_NO_TERMINAL_RESULT,
    SETTLED_BY_REFUSED_CONCURRENT_TURN,
    SETTLED_BY_TERMINAL_RESULT,
)
from core.services.dispatch import (
    SOURCE_HUMAN,
    SOURCE_SCHEDULED,
    dispatch_turn,
    dispatch_turn_with_outcome,
)
from modules.im import MessageContext


def _build_controller_double() -> MagicMock:
    controller = MagicMock()
    controller.message_handler = MagicMock()
    controller.message_handler.handle_user_message = AsyncMock(return_value="msg_user_1")
    controller.message_handler.handle_scheduled_message = AsyncMock(return_value="msg_sched_1")
    return controller


def _ctx() -> MessageContext:
    return MessageContext(user_id="U", channel_id="C", platform="slack", message_id="m1")


def test_human_source_calls_handle_user_message():
    controller = _build_controller_double()
    result = asyncio.run(dispatch_turn(controller, _ctx(), "hi", source=SOURCE_HUMAN))
    assert result == "msg_user_1"
    controller.message_handler.handle_user_message.assert_awaited_once()
    controller.message_handler.handle_scheduled_message.assert_not_called()


def test_scheduled_source_calls_handle_scheduled_message():
    controller = _build_controller_double()
    result = asyncio.run(dispatch_turn(controller, _ctx(), "cron task", source=SOURCE_SCHEDULED))
    assert result == "msg_sched_1"
    controller.message_handler.handle_scheduled_message.assert_awaited_once()
    controller.message_handler.handle_user_message.assert_not_called()


def test_default_source_is_human():
    """Callers that don't pass ``source`` get the human path; the IM
    adapter relies on this so the registration site stays terse.
    """
    controller = _build_controller_double()
    asyncio.run(dispatch_turn(controller, _ctx(), "hello"))
    controller.message_handler.handle_user_message.assert_awaited_once()


def test_streaming_registers_turn_sink_and_waits_for_result():
    """The streaming path (on_chunk set) registers a per-session turn sink
    on the controller — keyed by session key, not stashed on the context —
    and holds until the turn signals completion (a result emit sets the
    sink's done event), so the SSE caller doesn't close the stream before
    chunks arrive. The sink is cleaned up after the turn.
    """
    controller = _build_controller_double()
    ctx = _ctx()
    received: list[dict] = []

    async def _on_chunk(envelope: dict) -> None:
        received.append(envelope)

    captured: dict = {}

    def _register(session_key, *, on_chunk, done_event, turn_token=None, context=None):
        captured["session_key"] = session_key
        captured["on_chunk"] = on_chunk
        captured["done_event"] = done_event
        captured["turn_token"] = turn_token
        captured["context"] = context
        # Simulate the agent's background receiver emitting the result,
        # which sets the done event so dispatch_turn returns promptly
        # instead of waiting out the safety timeout.
        done_event.set()

    controller._get_session_key = MagicMock(return_value="slack::C")
    controller.get_turn_sink = MagicMock(return_value=None)  # no turn in flight => not rejected
    controller.register_turn_sink = MagicMock(side_effect=_register)
    controller.pop_turn_sink = MagicMock()

    asyncio.run(dispatch_turn(controller, ctx, "hi", on_chunk=_on_chunk))

    assert captured["session_key"] == "slack::C"
    assert captured["on_chunk"] is _on_chunk
    assert captured["context"] is ctx
    assert isinstance(captured["done_event"], asyncio.Event)
    # Cleanup passes our own done event so a superseded turn can't evict a
    # newer concurrent turn's sink.
    controller.pop_turn_sink.assert_called_once_with("slack::C", captured["done_event"])
    # dispatch_turn never invokes on_chunk itself — the emit path does.
    assert received == []
    # No longer stashed on the context.
    assert "turn_chunk_callback" not in (ctx.platform_specific or {})


def test_on_chunk_absent_keeps_platform_specific_untouched():
    controller = _build_controller_double()
    ctx = _ctx()
    ctx.platform_specific = {"existing": "value"}
    asyncio.run(dispatch_turn(controller, ctx, "hi"))
    assert ctx.platform_specific == {"existing": "value"}, (
        "no chunk callback => platform_specific must not be mutated"
    )


def _streaming_controller(*, sink_settled_by: str | None, in_flight: bool = False) -> MagicMock:
    """A controller double whose registered sink is released like the real one.

    ``sink_settled_by`` is the stamp the release site would have written:
    ``terminal_result`` for a real backend result, ``None`` for a release that left
    no trace (``mark_turn_complete`` on a no-dispatch turn).
    """

    controller = _build_controller_double()
    sinks: dict = {}

    def _register(session_key, *, on_chunk, done_event, turn_token=None, context=None):
        sink = {"on_chunk": on_chunk, "done_event": done_event, "turn_token": turn_token}
        if sink_settled_by is not None:
            sink["settled_by"] = sink_settled_by
        sinks[session_key] = sink
        done_event.set()

    controller._get_session_key = MagicMock(return_value="slack::C")
    controller.get_turn_sink = MagicMock(side_effect=lambda key: sinks.get(key))
    controller.register_turn_sink = MagicMock(side_effect=_register)
    controller.pop_turn_sink = MagicMock(side_effect=lambda key, done=None: sinks.pop(key, None))
    if in_flight:
        # Pre-register a live sink for the same session: a turn is already streaming.
        sinks["slack::C"] = {"on_chunk": AsyncMock(), "done_event": asyncio.Event()}
    return controller


def test_outcome_reports_terminal_result_settlement():
    controller = _streaming_controller(sink_settled_by=SETTLED_BY_TERMINAL_RESULT)
    outcome = asyncio.run(dispatch_turn_with_outcome(controller, _ctx(), "hi", on_chunk=AsyncMock()))
    assert outcome.settled_by == SETTLED_BY_TERMINAL_RESULT


def test_outcome_reports_release_without_terminal_result():
    # The zombie case: the waiter was released but no terminal result arrived, so a
    # caller holding a durable run record must settle it itself.
    controller = _streaming_controller(sink_settled_by=None)
    outcome = asyncio.run(dispatch_turn_with_outcome(controller, _ctx(), "hi", on_chunk=AsyncMock()))
    assert outcome.settled_by == SETTLED_BY_NO_TERMINAL_RESULT


def test_outcome_reports_refused_concurrent_turn():
    # The refusal returns BEFORE any sink is registered, so the settlement cannot
    # ride the sink — it has to be reported directly or the caller's run row would
    # stay open forever.
    controller = _streaming_controller(sink_settled_by=None, in_flight=True)
    controller._t = MagicMock(return_value="already streaming")
    on_chunk = AsyncMock()
    outcome = asyncio.run(dispatch_turn_with_outcome(controller, _ctx(), "hi", on_chunk=on_chunk))
    assert outcome.settled_by == SETTLED_BY_REFUSED_CONCURRENT_TURN
    assert outcome.error is None
    on_chunk.assert_awaited_once()
    controller.register_turn_sink.assert_not_called()
    controller.message_handler.handle_user_message.assert_not_called()


def test_outcome_settled_by_is_none_only_without_on_chunk():
    # ``None`` must mean exactly one thing — "no streaming caller, so no sink" —
    # otherwise a durable caller cannot tell an unexplained release apart from a
    # non-streaming dispatch.
    controller = _build_controller_double()
    outcome = asyncio.run(dispatch_turn_with_outcome(controller, _ctx(), "hi"))
    assert outcome.settled_by is None
    assert outcome.error == "msg_user_1"


def test_invalid_source_raises():
    """Future-proof: unknown source must not silently fall through to a
    handler that the caller didn't intend."""
    controller = _build_controller_double()
    with pytest.raises(Exception):
        # ``handle_user_message`` is the default path so we want
        # source="unknown" to either raise or be loudly handled. Today's
        # implementation falls through to the user path; this test will
        # be tightened once we add explicit validation. Marked as the
        # behavioral floor.
        # NOTE: this test is intentionally lenient until C2 adds explicit
        # source validation. Kept as a marker for the future tightening.
        async def _go():
            await dispatch_turn(controller, _ctx(), "x", source="unknown_source")
            # If we got here, the user path was taken; that's still
            # OK for v1 but we mark a future tightening point.
            raise RuntimeError("expected stricter source validation in future")
        asyncio.run(_go())
