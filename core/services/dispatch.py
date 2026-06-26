"""Shared turn-dispatch entry point.

``dispatch_turn`` is the single business-level function for "run an agent
turn against a session". All three callers go through it so the IM
adapter, the CLI, and the upcoming Web UI / N3 socket path share one
implementation:

* **IM adapter** — same process as ``Controller``; calls ``await
  dispatch_turn(controller, context, text)`` directly. Today wired in
  ``Controller._wire_im_callbacks`` so every Slack / Discord / Telegram
  / Lark / WeChat / avibe inbound message lands here.
* **CLI** (``vibe agent run --sync``, future N3 socket path) — separate
  process; the internal HTTP endpoint built in ``core/internal_server.py``
  (commit C4) will wrap this with SSE chunked output.
* **Scheduled / hook / watch runs** — already routed through
  ``MessageHandler.handle_scheduled_message`` by ``ScheduledTaskService``;
  this layer just gives them a stable entry name.

The implementation today is a thin delegate so we can pin the public
shape now and keep behavior byte-identical with the existing
``MessageHandler._handle_turn`` path. Streaming (``on_chunk``) is
reserved for the N3 work — see ``docs/plans/workbench-dispatch-architecture.md``
§7. Until that lands the callback is silently unused, which is fine
because no caller passes it yet.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Awaitable, Callable, Optional, TYPE_CHECKING

from modules.im import MessageContext

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from core.controller import Controller

logger = logging.getLogger(__name__)

# Streaming hook for the N3 socket path. Receives one envelope per
# ``emit_agent_message`` notify/result emit for the turn, on the same loop.
ChunkCallback = Callable[[dict], Awaitable[None]]

SOURCE_HUMAN = "human"
SOURCE_SCHEDULED = "scheduled"


async def dispatch_turn(
    controller: "Controller",
    context: MessageContext,
    text: str,
    *,
    source: str = SOURCE_HUMAN,
    on_chunk: Optional[ChunkCallback] = None,
) -> Optional[str]:
    """Run one agent turn for ``context`` and return the primary message id.

    ``source`` selects between the human-initiated and scheduler-initiated
    paths in ``MessageHandler``; today they only differ in source tagging.

    ``on_chunk`` (the N3 socket / web Chat path) receives each notify/result
    emit for this turn as it happens. Because the agent backends are
    fire-and-forget — ``handle_user_message`` returns once the message is sent
    and the reply streams in later on a background receiver task — we register
    a per-session sink and hold here until the turn emits its REAL terminal
    result, however long that takes, so the caller doesn't close the SSE stream
    before any chunk arrives. There is NO turn-duration timeout: an agent turn
    may legitimately run for hours, and the controller must never kill it on a
    timer. A user Stop/cancel cancels the task, propagating ``CancelledError``
    out of ``done.wait()``; the ``finally`` still pops the sink.
    """

    handler = controller.message_handler

    async def _run() -> Optional[str]:
        if source == SOURCE_SCHEDULED:
            return await handler.handle_scheduled_message(context, text)
        return await handler.handle_user_message(context, text)

    if on_chunk is None:
        # IM / CLI: fire-and-forget; no live stream to hold open.
        return await _run()

    session_key = controller._get_session_key(context)
    if controller.get_turn_sink(session_key) is not None:
        # Serialize per session. A streaming turn is already in flight for
        # this session (a second browser tab, or a resend before the first
        # finishes). The session's single agent client can't run two turns at
        # once, and two live sinks under one session key would cross-feed
        # chunks and complete each other early. Refuse the concurrent turn
        # with a terminal chunk instead of racing — the in-flight turn keeps
        # streaming undisturbed.
        await on_chunk({"kind": "error", "text": controller._t("error.streamTurnInProgress"), "message_id": None})
        return None
    # Tag this turn with a unique token, stamped into the context the agent
    # receiver will carry. ``_stream_chunk`` only forwards an emit to the sink
    # when the emit's context token matches the registered sink's token, so a
    # late straggler emit from a PREVIOUS (stopped / timed-out) turn can't
    # cross-feed into this turn's live stream or prematurely complete it.
    # Fail-open: emits without a token still flow (byte-identical to before).
    turn_token = uuid.uuid4().hex
    if context.platform_specific is None:
        context.platform_specific = {}
    context.platform_specific["turn_token"] = turn_token
    done = asyncio.Event()
    controller.register_turn_sink(
        session_key,
        on_chunk=on_chunk,
        done_event=done,
        turn_token=turn_token,
        context=context,
    )
    try:
        result = await _run()
        # Wait for the agent's REAL terminal result, however long it takes — a
        # turn may legitimately run for hours, so there is NO timeout here. A
        # user Stop/cancel cancels this task; the ``CancelledError`` propagates
        # out of ``done.wait()`` and the ``finally`` below pops the sink.
        await done.wait()
        return result
    finally:
        # Pass our own done event so a turn that was superseded by a newer
        # concurrent turn doesn't evict the newer turn's sink on cleanup.
        controller.pop_turn_sink(session_key, done)
