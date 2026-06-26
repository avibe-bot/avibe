"""Unit test for ``core.message_dispatcher._stream_chunk``.

The hook bridges the in-process dispatcher to the SSE stream exposed by
``core.internal_server``. It resolves the live turn sink by *session key*
from ``controller.active_turn_sinks`` — not off the context — so a reused
agent session, whose long-lived receiver carries a stale per-turn context,
still streams to the current turn. It forwards the envelope to the
registered ``on_chunk``, swallows callback errors, and a ``result`` emit
sets the sink's done event so ``dispatch_turn`` can close the stream. No
sink (IM bots / CLI) => silent no-op.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.message_dispatcher import _stream_chunk
from modules.im import MessageContext


class _ControllerDouble:
    """Minimal controller exposing the real turn-sink registry surface."""

    def __init__(self):
        self.active_turn_sinks: dict = {}

    def _get_session_key(self, ctx):
        return f"{ctx.platform}::{ctx.channel_id}"

    def register_turn_sink(self, session_key, *, on_chunk, done_event, turn_token=None, context=None):
        self.active_turn_sinks[session_key] = {
            "on_chunk": on_chunk,
            "done_event": done_event,
            "turn_token": turn_token,
        }

    def get_turn_sink(self, session_key):
        return self.active_turn_sinks.get(session_key)


def _ctx(platform="avibe", channel="C", turn_token=None):
    spec = {"turn_token": turn_token} if turn_token is not None else None
    return MessageContext(user_id="U", channel_id=channel, platform=platform, platform_specific=spec)


def test_noop_when_no_sink():
    controller = _ControllerDouble()
    # No sink registered for this session => silent no-op (IM / CLI turn).
    asyncio.run(_stream_chunk(controller, _ctx(), text="hi", message_id="m1", kind="notify"))


def test_forwards_envelope_to_sink_on_chunk():
    controller = _ControllerDouble()
    cb = AsyncMock()
    controller.register_turn_sink("avibe::C", on_chunk=cb, done_event=asyncio.Event())
    asyncio.run(_stream_chunk(controller, _ctx(), text="agent reply", message_id="m_42", kind="result"))
    cb.assert_awaited_once_with({"text": "agent reply", "message_id": "m_42", "kind": "result"})


def test_result_kind_sets_done_event():
    controller = _ControllerDouble()
    done = asyncio.Event()
    controller.register_turn_sink("avibe::C", on_chunk=AsyncMock(), done_event=done)
    asyncio.run(_stream_chunk(controller, _ctx(), text="final", message_id="m1", kind="result"))
    assert done.is_set(), "a result emit must release the streaming dispatch"


def test_notify_kind_leaves_done_event_unset():
    controller = _ControllerDouble()
    done = asyncio.Event()
    controller.register_turn_sink("avibe::C", on_chunk=AsyncMock(), done_event=done)
    asyncio.run(_stream_chunk(controller, _ctx(), text="thinking", message_id="m1", kind="notify"))
    assert not done.is_set(), "notify is intermediate; the turn isn't done yet"


def test_resolves_sink_by_session_key_despite_stale_context():
    # The agent receiver emits with a context captured at session start; a
    # later turn registers its sink under the same session key. _stream_chunk
    # must resolve by session key so the reused-session reply still streams,
    # even though the emitting context object is a different instance.
    controller = _ControllerDouble()
    cb = AsyncMock()
    controller.register_turn_sink("avibe::sesX", on_chunk=cb, done_event=asyncio.Event())
    stale_ctx = _ctx(channel="sesX")
    asyncio.run(_stream_chunk(controller, stale_ctx, text="r", message_id="m", kind="notify"))
    cb.assert_awaited_once()


def test_result_with_matching_turn_token_completes():
    # The live turn's own result (token matches the sink) completes it.
    controller = _ControllerDouble()
    done = asyncio.Event()
    controller.register_turn_sink("avibe::C", on_chunk=AsyncMock(), done_event=done, turn_token="T2")
    asyncio.run(_stream_chunk(controller, _ctx(turn_token="T2"), text="final", message_id="m1", kind="result"))
    assert done.is_set(), "a result for the active turn must release the stream"


def test_stale_result_does_not_complete_active_turn_but_still_forwards():
    # A late result from a SUPERSEDED turn (token T1) resolves the CURRENT turn's
    # sink (token T2) by session key. It must NOT complete the active turn (Codex
    # P1) — otherwise dispatch pops in_flight / flushes the queue while the live
    # backend runs — but the chunk itself is still forwarded (forwarding stays
    # ungated so reused-receiver Claude chunks keep streaming).
    controller = _ControllerDouble()
    done = asyncio.Event()
    cb = AsyncMock()
    controller.register_turn_sink("avibe::C", on_chunk=cb, done_event=done, turn_token="T2")
    asyncio.run(_stream_chunk(controller, _ctx(turn_token="T1"), text="stale", message_id="m1", kind="result"))
    assert not done.is_set(), "a stale turn's result must not end the active turn"
    cb.assert_awaited_once()  # but the chunk was still forwarded


def test_absent_ctx_token_does_not_complete_tokened_turn():
    # When the live sink HAS a token, a result with NO turn_token is treated as stale
    # (a superseded/older turn that didn't carry this turn's token) and must NOT
    # complete the live turn — same rule as a mismatched token (Codex P2 #3331953260):
    # a stray tokenless emit must not fire a premature turn.end / queue flush on the
    # active turn. (Genuine fail-open — a TOKENLESS *sink* — is covered by
    # test_result_kind_sets_done_event.) NOTE for the FSM refactor: with the turn-
    # duration timeout removed, a turn whose OWN terminal result is tokenless would
    # hang here; the FSM must guarantee every terminal result carries the active
    # turn's token (bulletproof Claude adoption / FSM-attached token).
    controller = _ControllerDouble()
    done = asyncio.Event()
    controller.register_turn_sink("avibe::C", on_chunk=AsyncMock(), done_event=done, turn_token="T2")
    asyncio.run(_stream_chunk(controller, _ctx(turn_token=None), text="final", message_id="m1", kind="result"))
    assert not done.is_set(), "tokenless emit must NOT complete a tokened (live) turn"


def test_swallows_sink_on_chunk_exception():
    controller = _ControllerDouble()

    async def _raises(envelope):
        raise RuntimeError("UI server died")

    controller.register_turn_sink("avibe::C", on_chunk=_raises, done_event=asyncio.Event())
    # Must not propagate — a misbehaving UI consumer cannot kill the agent reply.
    asyncio.run(_stream_chunk(controller, _ctx(), text="x", message_id=None, kind="notify"))
