"""Consolidated agent message dispatcher.

Owns the main log/result/notify dispatch state machine that was previously
embedded in ``Controller.emit_agent_message``.
"""

from __future__ import annotations

import logging
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin

from config.platform_registry import get_platform_descriptor
from config.v2_config import DEFAULT_AGENT_PROGRESS_STYLE
from modules.im import MessageContext
from modules.im.formatters.base_formatter import to_status_label
from core.message_mirror import persist_agent_message
from core.reply_enhancer import process_reply, strip_file_links, strip_silent_blocks
from core.session_turns import emit_matches_active_turn
from storage.background import SQLiteBackgroundTaskStore
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)


def _coalesced_task_execution_ids(payload: dict[str, Any]) -> list[str]:
    run_ids: list[str] = []
    primary = str(payload.get("task_execution_id") or "").strip()
    if primary:
        run_ids.append(primary)
    coalesced = payload.get("coalesced_queue")
    execution_ids = coalesced.get("execution_ids") if isinstance(coalesced, dict) else None
    if isinstance(execution_ids, list):
        for value in execution_ids:
            run_id = str(value or "").strip()
            if run_id and run_id not in run_ids:
                run_ids.append(run_id)
    return run_ids


def _run_is_cancelled(run: Any) -> bool:
    if not isinstance(run, dict):
        return False
    status = str(run.get("status") or "").strip().lower()
    return status in {"canceled", "cancelled"}


async def _stream_chunk(controller, context, *, text: str, message_id: Optional[str], kind: str) -> None:
    """Forward one durable agent message to the live streaming turn sink.

    A web Chat caller registers a per-session sink in
    ``controller.active_turn_sinks`` (see ``core.services.dispatch.dispatch_turn``)
    so the SSE response stream sees notify + result emits as they happen —
    even though the agent's receiver runs on a background task carrying a
    stale per-turn context. We resolve the sink by *session key* (stable
    across a session's turns) rather than off the context, so reused agent
    sessions stream correctly too. A ``result`` emit also marks the turn
    complete so ``dispatch_turn`` can close the stream right after it. No
    sink (IM / CLI turns) => no-op, byte-identical to master.
    """

    get_sink = getattr(controller, "get_turn_sink", None)
    get_key = getattr(controller, "_get_session_key", None)
    if not callable(get_sink) or not callable(get_key):
        # Controller has no streaming turn-sink registry (IM/CLI stubs, older
        # controllers) => nothing to stream to; stay a no-op.
        return
    sink = get_sink(get_key(context))
    if sink is None:
        return
    # NB: we deliberately do NOT gate forwarding on a per-turn token here.
    # Claude reuses ONE long-lived receiver across a session's turns, and it
    # emits the CURRENT turn's output carrying an EARLIER turn's context
    # (the documented "stale per-turn context"); a token gate would drop those
    # legitimate current-turn chunks. Resolution stays by session key. (The
    # cross-feed of a stopped turn's late straggler is handled at the
    # turn-completion layer / left as a known edge — see docs/plans.)
    try:
        await sink["on_chunk"]({"text": text, "message_id": message_id, "kind": kind})
    except Exception:
        # A misbehaving SSE consumer must not block the underlying agent
        # reply. Log + swallow, same posture as ``mirror_outbound``.
        logger.exception("turn on_chunk raised; dropping chunk kind=%s", kind)
    if kind == "result":
        # The result is the turn's final answer — release the streaming dispatch
        # so it can close the SSE stream right after this chunk. Unlike chunk
        # forwarding above, the COMPLETION signal IS turn-token-gated (mirrors
        # ``Controller.mark_turn_complete`` / ``_is_active_turn``): a late ``result``
        # from a SUPERSEDED or OLDER turn (stopped / timed-out / a scheduled-watch run
        # that carries no token) resolves the CURRENT turn's sink by session key, and
        # setting its ``done_event`` would pop ``in_flight`` / publish ``turn.end`` /
        # flush the queue while the active backend is still running (Codex P1/P2).
        # When the live sink HAS a token, only a result with the MATCHING token may
        # complete it — a different OR absent token is stale. Fail-open only when the
        # sink itself is tokenless. The reused-receiver Claude case keeps completing
        # because its result emit adopts the live turn's token (see ClaudeAgent).
        if not emit_matches_active_turn(sink, context):
            return
        done = sink.get("done_event")
        if done is not None:
            done.set()


_WECHAT_TEXT_LIMIT = 1900
_WECHAT_CONSOLIDATED_SPLIT_THRESHOLD = 1700
# Append the current action's own elapsed time to the status-bubble BODY only
# once it has been running this long (e.g. ``🔧 Bash · 2:30``). Below this a
# normal fast step stays a clean label. This is the always-moving "still running"
# signal that the heartbeat keeps ticking even when no new emit arrives.
_ACTION_TIME_HINT_S = 10.0


class ConsolidatedMessageDispatcher:
    """Dispatch agent messages while preserving existing product behavior."""

    def __init__(self, controller):
        self.controller = controller
        self._consolidated_message_ids: dict[str, str] = {}
        self._consolidated_message_buffers: dict[str, str] = {}
        self._consolidated_message_locks: dict[str, asyncio.Lock] = {}
        self._thread_current_message_id: dict[str, str] = {}
        # NB: the concise status bubble is a single short replace-not-append line
        # (to_status_label caps at ~60 chars, well under any platform limit), so it
        # never splits into multiple messages — the "overflow" (case 3) the design
        # doc lists is unreachable by construction and needs no tidy state.
        # P1 liveness: per-turn (consolidated_key) start + last-activity timestamps
        # drive the footer (turn elapsed) and the body's current-action runtime
        # (now − last activity); one heartbeat task per turn; cancelled before the
        # final edit-into-result so a stale tick can't stomp the result.
        self._status_started_at: dict[str, float] = {}
        self._status_last_activity_at: dict[str, float] = {}
        # Per-turn render counter so the running footer's hourglass glyph cycles
        # (⏳ ⌛ ⏳ ⌛) across heartbeats/emits without any external event — a
        # zero-width "still alive" motion that replaces the old "working…" dots.
        self._status_render_tick: dict[str, int] = {}
        # Per-turn (consolidated_key) count of real action emits (toolcall +
        # assistant) so the footer can show a monotonically-growing "{n} st"
        # progress signal. Heartbeat re-renders do NOT go through the emit path,
        # so they never inflate it. Dropped per turn in ``_drop_status_keys``.
        self._status_step_count: dict[str, int] = {}
        # Current context-window occupancy (keyed by SESSION key, not turn-key) so
        # the footer can show "{n} tok" of context the session is using. Backends
        # report the latest snapshot via ``note_session_tokens(total=…)`` (Claude:
        # last assistant message usage; Codex: thread/tokenUsage/updated last). It
        # tracks the live context size — growing with the conversation and dropping
        # after a /compact — so it persists across turns, NOT turn-scoped state.
        self._session_token_total: dict[str, int] = {}
        self._status_heartbeat_tasks: dict[str, asyncio.Task] = {}
        # Turn-keys whose bubble has already been finalized (edited into the
        # result or collapsed to a terminal marker). A late in-flight process
        # emit for one of these keys must NOT resurrect/overwrite the terminal
        # bubble — see ``_render_concise_status``. Mark + read happens under the
        # per-key consolidated lock so finalize and a concurrent process render
        # can't interleave (C1).
        self._status_finalized: set[str] = set()
        # Turn-keys whose tracked ``_consolidated_message_ids`` entry is a CONCISE
        # status bubble (as opposed to a verbose consolidated process-log message,
        # which shares the same dict). Cleanup keys on this — not the current
        # progress style — so a mid-turn concise->off/verbose flip still retires
        # the bubble, while a genuine verbose log is never mistaken for a bubble
        # and deleted. Dropped per turn in ``_drop_status_keys``.
        self._concise_bubble_keys: set[str] = set()
        # Per-turn (consolidated_key) snapshot of the effective progress style, so
        # a single turn cannot flip concise<->verbose<->off mid-way even if the Web
        # UI setting changes while the backend is still emitting. Resolved lazily on
        # first use per turn (see ``_concise_progress_style``), dropped in
        # ``_drop_status_keys``.
        self._turn_progress_style: dict[str, str] = {}
        # Injectable monotonic-ish clock (wall time) so tests get deterministic
        # elapsed/stale values without sleeping.
        self._now = time.time

    def _get_platform(self, context: MessageContext) -> str:
        return context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform

    def _capabilities(self, context: MessageContext):
        return get_platform_descriptor(self._get_platform(context)).capabilities

    def _get_settings_key(self, context: MessageContext) -> str:
        return self.controller._get_settings_key(context)

    def _get_session_key(self, context: MessageContext) -> str:
        return self.controller._get_session_key(context)

    def _supports_toolcall_delivery(self, context: MessageContext) -> bool:
        return self._capabilities(context).supports_toolcall_delivery

    def _get_im_client(self, context: MessageContext):
        getter = getattr(self.controller, "get_im_client_for_context", None)
        if callable(getter):
            return getter(context)
        return self.controller.im_client

    def _signal_turn_complete(self, context: MessageContext) -> None:
        """Release a live streaming SSE waiter for this turn when a result is
        finalized without streaming a visible chunk (empty/silent result), so
        the stream closes promptly instead of hanging until the timeout. No-op
        for non-streaming turns or controllers without the registry."""
        mark = getattr(self.controller, "mark_turn_complete", None)
        if callable(mark):
            mark(context)

    def _release_runtime_turn(self, context: MessageContext) -> None:
        service = getattr(self.controller, "agent_service", None)
        release = getattr(service, "release_runtime_turn", None)
        if callable(release):
            release(context)

    async def _finish_processing_indicator_turn(self, context: MessageContext) -> None:
        service = getattr(self.controller, "processing_indicator", None)
        finish_terminal = getattr(service, "finish_terminal_turn", None)
        if not callable(finish_terminal):
            return
        try:
            await finish_terminal(context)
        except Exception:
            logger.debug("terminal processing-indicator cleanup failed", exc_info=True)

    def _is_current_runtime_turn(self, context: MessageContext) -> bool:
        service = getattr(self.controller, "agent_service", None)
        matches = getattr(service, "emit_matches_runtime_turn", None)
        if not callable(matches):
            return True
        try:
            return bool(matches(context))
        except Exception:
            logger.debug("runtime turn guard failed open", exc_info=True)
            return True

    def _t(self, key: str, **kwargs) -> str:
        translator = getattr(self.controller, "_t", None)
        if callable(translator):
            return translator(key, **kwargs)
        lang = getattr(getattr(self.controller, "config", None), "language", "en")
        return i18n_t(key, lang, **kwargs)

    def _get_target_context(self, context: MessageContext) -> MessageContext:
        payload = dict(context.platform_specific or {})
        delivery_override = payload.get("delivery_override")
        if isinstance(delivery_override, dict):
            next_payload = dict(payload)
            next_payload["is_dm"] = delivery_override.get("is_dm", next_payload.get("is_dm", False))
            return MessageContext(
                user_id=str(delivery_override.get("user_id") or context.user_id),
                channel_id=str(delivery_override.get("channel_id") or context.channel_id),
                platform=delivery_override.get("platform") or context.platform,
                thread_id=delivery_override.get("thread_id"),
                message_id=context.message_id,
                platform_specific=next_payload,
            )
        if self._get_im_client(context).should_use_thread_for_reply() and context.thread_id:
            return MessageContext(
                user_id=context.user_id,
                channel_id=context.channel_id,
                platform=context.platform,
                thread_id=context.thread_id,
                message_id=context.message_id,
                platform_specific=context.platform_specific,
            )
        return context

    def _get_consolidated_message_key(self, context: MessageContext) -> str:
        session_key = self._get_session_key(context)
        thread_key = context.thread_id or context.channel_id
        tracking_key = f"{session_key}:{thread_key}"
        trigger_id = self._thread_current_message_id.get(tracking_key) or context.message_id or ""
        return f"{session_key}:{thread_key}:{trigger_id}"

    def update_thread_message_id(self, context: MessageContext) -> None:
        if not context.message_id:
            return
        session_key = self._get_session_key(context)
        thread_key = context.thread_id or context.channel_id
        tracking_key = f"{session_key}:{thread_key}"
        self._thread_current_message_id[tracking_key] = context.message_id

    def _get_consolidated_message_lock(self, key: str) -> asyncio.Lock:
        if key not in self._consolidated_message_locks:
            self._consolidated_message_locks[key] = asyncio.Lock()
        return self._consolidated_message_locks[key]

    async def _drop_status_keys(self, key: str) -> None:
        """Stop the turn's heartbeat and drop ALL per-turn status state for ``key``.

        Single owner of the teardown sequence so the message id, buffer, and the
        two timestamp dicts can never drift between the two clear call sites.
        Heartbeat is cancelled BEFORE the lock — the heartbeat render also takes
        this lock, so awaiting it while holding the lock would deadlock.
        """
        await self._stop_status_heartbeat(key)
        async with self._get_consolidated_message_lock(key):
            self._consolidated_message_ids.pop(key, None)
            self._consolidated_message_buffers.pop(key, None)
            self._status_started_at.pop(key, None)
            self._status_last_activity_at.pop(key, None)
            self._status_render_tick.pop(key, None)
            self._status_step_count.pop(key, None)
            self._concise_bubble_keys.discard(key)
            self._turn_progress_style.pop(key, None)
            # NOTE: the finalized marker is intentionally NOT discarded here. The
            # runtime gate is released only AFTER this teardown (in the agent
            # service's finally), so a late same-token process emit during that
            # window still passes _is_current_runtime_turn; keeping the key
            # finalized makes _render_concise_status bail instead of re-opening a
            # bubble after the answer was posted (P2). The set is bounded in
            # _finalize_status_key.
        # Drop the lock itself LAST — only after the ``async with`` block above
        # has released it — so the per-key lock dict can't grow unbounded across
        # many turns (C6). Popping a still-held lock would orphan the held lock.
        self._consolidated_message_locks.pop(key, None)

    async def _clear_consolidated_state(self, context: MessageContext) -> None:
        await self._drop_status_keys(self._get_consolidated_message_key(context))

    # ------------------------------------------------------------------
    # Concise status bubble (Slack / Discord)
    # ------------------------------------------------------------------

    def _progress_style(self, context: MessageContext) -> str:
        """Resolve the per-channel progress style: ``concise`` | ``verbose`` | ``off``.

        Defaults to ``off`` unless config explicitly enables progress display. A
        controller may expose ``get_progress_style_for_context`` once the
        settings/UI plumbing lands; until then this returns the default.
        """
        getter = getattr(self.controller, "get_progress_style_for_context", None)
        if callable(getter):
            try:
                value = getter(context)
                if value in {"concise", "verbose", "off"}:
                    return value
            except Exception:
                logger.debug("get_progress_style_for_context failed; defaulting off", exc_info=True)
        return DEFAULT_AGENT_PROGRESS_STYLE

    def _concise_progress_style(self, context: MessageContext) -> str:
        """Effective progress style for the process-message path, SNAPSHOTTED per
        turn.

        Only platforms with the ``supports_status_bubble`` capability (Slack/
        Discord today) opt into concise/off; every other platform keeps the
        existing ``verbose`` append path, so their output stays byte-identical.

        The effective style is resolved ONCE per turn (keyed by the turn's
        consolidated key) and cached for the rest of that turn. ``self.config`` may
        still be hot-reloaded mid-turn (via ``_t()`` or the controller getters) so
        the NEXT turn starts fresh — that satisfies the "scheduled/background turns
        pick up Web UI changes at their START" requirement — but a single turn can
        never flip style halfway. That intra-turn stability is what keeps the
        concise-bubble vs verbose-log lifecycle self-consistent (a mid-turn flip
        would otherwise strand a bubble, delete a verbose log, or leave a heartbeat
        stamping a log)."""
        if not self._capabilities(context).supports_status_bubble:
            return "verbose"
        key = self._get_consolidated_message_key(context)
        cached = self._turn_progress_style.get(key)
        if cached is not None:
            return cached
        style = self._progress_style(context)
        # Bound the cache: entries are dropped per turn in ``_drop_status_keys``,
        # but clear proactively if it ever grows large so a missed teardown can't
        # leak unboundedly (mirrors the ``_status_finalized`` guard).
        if len(self._turn_progress_style) > 512:
            self._turn_progress_style.clear()
        self._turn_progress_style[key] = style
        return style

    async def _render_concise_status(
        self,
        im_client,
        context: MessageContext,
        chunk: str,
        status_label: Optional[str] = None,
        *,
        allow_empty_body: bool = False,
    ) -> Optional[str]:
        """Render ONE status bubble that REPLACES (not appends) the latest action.

        The process bubble stays a single short line (`🔧 <action>`) plus a
        liveness footer; it never splits and never leaves ``continued below``
        fragments. The same bubble is later edited into the final result (see the
        ``result`` branch). Persistence already happened upstream — this only
        shapes the IM view.

        ``status_label`` (when non-empty) is a backend-computed clean tool-call
        label (claude-pipe style: ``🔧 Read: message_dispatcher.py``) used for the
        bubble body in place of the raw ``to_status_label(chunk)`` fallback. It
        never touches the persisted/verbose text — only the concise bubble view.

        ``allow_empty_body`` posts a footer-only bubble (no action label yet, e.g.
        turn start / pure thinking); the adapters render an empty body as the
        footer alone.
        """
        label = status_label or to_status_label(chunk)
        if not label and not allow_empty_body:
            return None

        consolidated_key = self._get_consolidated_message_key(context)
        lock = self._get_consolidated_message_lock(consolidated_key)
        target_context = self._get_target_context(context)
        now = self._now()

        async with lock:
            if consolidated_key in self._status_finalized:
                # The turn already finalized (result edited the bubble into the
                # answer, or it was collapsed to a terminal marker) while this
                # process emit was in flight. Bailing here keeps the terminal
                # bubble from being resurrected back to a "working" line (C1).
                return None
            # Only reuse the tracked id when it was itself created as a concise
            # bubble. If the turn started in ``verbose`` and the Web UI flipped to
            # ``concise`` mid-turn, the tracked id is the verbose consolidated
            # process-log message; reusing (and later retiring) it would delete the
            # user's log. Treat that as "no bubble yet" and post a FRESH concise
            # bubble, leaving the verbose log untracked and intact.
            existing_id = (
                self._consolidated_message_ids.get(consolidated_key)
                if consolidated_key in self._concise_bubble_keys
                else None
            )
            # Buffer holds the latest rendered label so the heartbeat can
            # re-render it with an elapsed-time footer without a new event.
            self._consolidated_message_buffers[consolidated_key] = label
            self._status_started_at.setdefault(consolidated_key, now)
            self._status_last_activity_at[consolidated_key] = now
            # Count this as a step only for a real action emit (a non-empty label);
            # the footer-only turn-start bubble (empty label) is not a step.
            if label:
                self._status_step_count[consolidated_key] = self._status_step_count.get(consolidated_key, 0) + 1
            body, footer = self._compose_status_message(context, consolidated_key)

            message_id = existing_id
            if existing_id:
                try:
                    ok = await im_client.edit_message(
                        target_context,
                        existing_id,
                        text=body,
                        parse_mode="markdown",
                        subtext=footer,
                    )
                except Exception as err:
                    logger.warning(f"Failed to edit status bubble: {err}")
                    ok = False
                if not ok:
                    # Edit failed (message gone / perms): drop the id and re-send.
                    self._consolidated_message_ids.pop(consolidated_key, None)
                    message_id = None

            if message_id is None:
                try:
                    message_id = await im_client.send_message(
                        target_context, body, parse_mode="markdown", subtext=footer
                    )
                    self._consolidated_message_ids[consolidated_key] = message_id
                except Exception as err:
                    logger.error(f"Failed to send status bubble: {err}", exc_info=True)
                    return None

            # Mark this turn-key as a concise bubble so terminal cleanup retires it
            # by identity even after a mid-turn style flip, without mistaking a
            # verbose process-log id (same dict) for a bubble.
            if message_id:
                self._concise_bubble_keys.add(consolidated_key)

        # Keep the elapsed timer alive even when the agent goes quiet (long tool).
        if message_id:
            self._start_status_heartbeat(context, im_client, consolidated_key)
        return message_id

    async def begin_status_bubble(self, context: MessageContext) -> None:
        """Post the status bubble IMMEDIATELY at turn start (footer-only).

        Without this the bubble only appears on the first process emit, leaving
        an early gap while the backend spins up. This posts an initial bubble so
        the user sees activity at once; the first real process emit then finds
        the existing id and EDITS it in place (no duplicate bubble).

        The body is EMPTY — the running footer ("⏳ 0s") already conveys that the
        agent started, so a redundant "starting agent" body line is dropped. The
        first real process emit fills the body in.

        No-op unless the channel is in ``concise`` style (so only Slack/Discord
        concise; ``off``/``verbose`` and non-status-bubble platforms are skipped),
        and idempotent: if a bubble already exists for this turn it does nothing.
        Wrapped so a bubble failure never blocks the turn.

        C2: this is awaited from ``AgentService._begin_turn_status`` WHILE the
        runtime gate lock is held, so a slow IM post (e.g. a Slack rate-limit)
        would freeze every queued turn behind it. Bound the post at 5s — if it
        doesn't land in time we log and return; the first real process emit then
        creates the bubble via the idempotent guard, so nothing is lost.
        """
        try:
            # No-delivery runs (scheduled / watch / agent-run targeting a Slack or
            # Discord no-delivery session) suppress every emitted message at the
            # chokepoint; this turn-start post runs BEFORE that branch, so guard it
            # here too or a private run would leak a visible ⏳ bubble to the channel.
            if (context.platform_specific or {}).get("suppress_delivery"):
                return
            if self._concise_progress_style(context) != "concise":
                return
            consolidated_key = self._get_consolidated_message_key(context)
            if self._consolidated_message_ids.get(consolidated_key):
                return
            im_client = self._get_im_client(context)
            # Reuse the exact process-emit render path so the starting bubble is
            # created, stored, and heartbeated identically to a real emit, but with
            # an empty body (footer only). The first real emit later EDITS this id.
            try:
                await asyncio.wait_for(
                    self._render_concise_status(im_client, context, "", allow_empty_body=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                # Don't hold the gate on a slow post; the idempotent first-emit
                # path will create the bubble once the backend starts emitting.
                logger.debug("begin_status_bubble timed out; first emit will create the bubble")
        except Exception:
            logger.debug("begin_status_bubble failed; turn continues", exc_info=True)

    # ---- liveness footer + heartbeat ----

    def _heartbeat_interval_s(self, context: MessageContext) -> float:
        getter = getattr(self.controller, "get_heartbeat_interval_ms_for_context", None)
        if callable(getter):
            try:
                value = getter(context)
                if value and value > 0:
                    return float(value) / 1000.0
            except Exception:
                logger.debug("get_heartbeat_interval_ms_for_context failed; default 15s", exc_info=True)
        return 15.0

    def _no_output_hint_after_s(self, context: MessageContext) -> float:
        getter = getattr(self.controller, "get_no_output_hint_after_ms_for_context", None)
        if callable(getter):
            try:
                value = getter(context)
                if value and value > 0:
                    return float(value) / 1000.0
            except Exception:
                logger.debug("get_no_output_hint_after_ms_for_context failed; default 180s", exc_info=True)
        return 180.0

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        if total < 60:
            return f"{total}s"
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def _format_token_count(tokens: int) -> str:
        """Compact token count: ``999`` / ``12.3k`` / ``248k`` / ``1.4M``.

        One decimal below 100k and below 10M; whole units above (the number is
        already large enough that a decimal adds noise, not precision)."""
        n = max(0, int(tokens))
        if n < 1000:
            return str(n)
        if n < 100_000:
            return f"{n / 1000:.1f}k"
        if n < 1_000_000:
            return f"{round(n / 1000)}k"
        if n < 10_000_000:
            return f"{n / 1_000_000:.1f}M"
        return f"{round(n / 1_000_000)}M"

    def _token_session_key(self, context: MessageContext) -> str:
        """Key for context-window occupancy: scoped per CONVERSATION (thread / DM),
        not per channel. The channel-level ``_get_session_key`` alone would let one
        thread's token update overwrite every other thread's footer in the same
        channel, since distinct agent sessions are derived from the thread anchor (P2)."""
        return f"{self._get_session_key(context)}:{context.thread_id or context.channel_id}"

    def note_session_tokens(self, context: MessageContext, *, total: int) -> None:
        """SET the session's current context-window occupancy shown in the footer.

        Both backends report ``total`` as a live snapshot (Claude: latest assistant
        usage; Codex: ``thread/tokenUsage/updated`` last). A pure SET — never an
        accumulation — so the figure tracks the live context size and drops after a
        /compact. Invalid/negative values are ignored. The next footer render (emit
        or heartbeat) picks it up.
        """
        try:
            value = int(total)
        except (TypeError, ValueError):
            return
        if value >= 0:
            self._session_token_total[self._token_session_key(context)] = value

    def _backend_dead(self, context: MessageContext) -> bool:
        """Best-effort backend-liveness probe (B1). Returns True ONLY when the
        controller can definitively say the backend is gone; unknown → False so
        we never false-alarm. Controller dispatches by backend (Claude receiver
        task / Codex transport)."""
        probe = getattr(self.controller, "backend_alive", None)
        if not callable(probe):
            return False
        try:
            alive = probe(context)
        except Exception:
            logger.debug("backend_alive probe failed; treating as alive", exc_info=True)
            return False
        return alive is False

    def _show_duration(self) -> bool:
        """Whether the terminal/done footer should show the elapsed TIME.

        Mirrors the existing ``config.show_duration`` (default False) that gates
        duration in result messages, so the completion footer no longer double-
        surfaces the duration. The RUNNING footer is unaffected (its elapsed time
        is live liveness, not a final duration summary)."""
        return bool(getattr(getattr(self.controller, "config", None), "show_duration", False))

    def _token_field(self, context: MessageContext) -> str:
        """The ``{n} tok`` footer field for the session's current context-window
        occupancy, or "" when unknown/zero (a backend that does not report usage
        shows nothing)."""
        tokens = self._session_token_total.get(self._token_session_key(context), 0)
        if tokens <= 0:
            return ""
        return self._t("status.tokens", count=self._format_token_count(tokens))

    def _status_footer_text(
        self,
        context: MessageContext,
        *,
        elapsed_s: float,
        done: bool = False,
        reason: str = "done",
        backend_dead: bool = False,
        hourglass: str = "⏳",
        steps: int = 0,
    ) -> str:
        elapsed = self._format_elapsed(elapsed_s)
        token_field = self._token_field(context)
        if done:
            # Terminal footer: a reason word with a marker (✅ clean "done", ⏹
            # "stopped"/"failed"). Elapsed time is appended only when show_duration
            # is on (matches result-message duration gating); the session token
            # total is kept so the final bubble still reports usage.
            marker = "✅" if reason == "done" else "⏹"
            footer = f"{marker} {self._t('status.' + reason)}"
            if self._show_duration():
                footer += f" · {elapsed}"
            if token_field:
                footer += f" · {token_field}"
            return footer
        if backend_dead:
            return f"⚠️ {self._t('status.backendUnresponsive')} · {elapsed}"
        # Running footer (compact, one mobile line): ``{hourglass} {elapsed}[ ·
        # {n} st][ · {tokens}]``. The hourglass glyph cycles ⏳/⌛ across renders
        # for a zero-width "alive" motion; 0-value fields are omitted so turn
        # start is just ``⏳ 0s``. The "time since last activity" lives in the
        # BODY (attached to the current action) instead of a standalone field.
        footer = f"{hourglass} {elapsed}"
        if steps > 0:
            footer += f" · {self._t('status.steps', count=steps)}"
        if token_field:
            footer += f" · {token_field}"
        return footer

    def _decorate_body_with_action_time(
        self, context: MessageContext, body: str, action_elapsed_s: float, *, backend_dead: bool
    ) -> str:
        """Append the current action's own runtime to the body once it crosses
        ``_ACTION_TIME_HINT_S`` (e.g. ``🔧 Bash · 2:30``). This value is driven by
        the heartbeat clock, so it keeps climbing during a single long operation
        and reads as "actively running" rather than "stuck". A ⚠️ is added once it
        exceeds the no-output threshold while the backend is still alive."""
        if not body or action_elapsed_s < _ACTION_TIME_HINT_S:
            return body
        emphasis = "⚠️ " if (not backend_dead and action_elapsed_s >= self._no_output_hint_after_s(context)) else ""
        return f"{body} · {emphasis}{self._format_elapsed(action_elapsed_s)}"

    def _compose_status_message(
        self,
        context: MessageContext,
        consolidated_key: str,
        *,
        done: bool = False,
        reason: str = "done",
        result_body: Optional[str] = None,
    ) -> tuple[str, str]:
        """Return ``(body, footer)`` for the status bubble.

        The IM adapters own footer styling (Slack native context block, Discord
        ``-#`` subtext), so core hands them the two pieces separately via the
        ``subtext`` parameter rather than merging them into one string. The body
        may be empty (no action label yet) — adapters then render footer-only.
        """
        body = result_body if result_body is not None else self._consolidated_message_buffers.get(consolidated_key, "")
        now = self._now()
        started = self._status_started_at.get(consolidated_key, now)
        elapsed_s = now - started
        if done:
            footer = self._status_footer_text(context, elapsed_s=elapsed_s, done=True, reason=reason)
        else:
            last = self._status_last_activity_at.get(consolidated_key, started)
            tick = self._status_render_tick.get(consolidated_key, 0)
            self._status_render_tick[consolidated_key] = tick + 1
            hourglass = "⏳" if tick % 2 == 0 else "⌛"
            backend_dead = self._backend_dead(context)
            body = self._decorate_body_with_action_time(
                context, body, now - last, backend_dead=backend_dead
            )
            footer = self._status_footer_text(
                context,
                elapsed_s=elapsed_s,
                backend_dead=backend_dead,
                hourglass=hourglass,
                steps=self._status_step_count.get(consolidated_key, 0),
            )
        return body, footer

    def _start_status_heartbeat(
        self,
        context: MessageContext,
        im_client,
        consolidated_key: str,
    ) -> None:
        # One heartbeat per turn-key. The loop re-reads the CURRENT bubble id each
        # tick, so a re-sent bubble (after an edit failure) keeps its timer without
        # churning tasks (Nit N1).
        existing = self._status_heartbeat_tasks.get(consolidated_key)
        if existing and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._status_heartbeat_loop(context, im_client, consolidated_key)
            )
        except RuntimeError:
            # No running loop (sync test context) — heartbeat is optional liveness.
            return
        self._status_heartbeat_tasks[consolidated_key] = task

    async def _status_heartbeat_loop(
        self,
        context: MessageContext,
        im_client,
        consolidated_key: str,
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_s(context))
                # Stop if the turn was superseded or the bubble is gone.
                if not self._is_current_runtime_turn(context):
                    return
                # Stop if the key is no longer a concise bubble — e.g. a mid-turn
                # concise->verbose flip reused this message as the verbose log; the
                # heartbeat must not keep stamping a status footer onto a log.
                if consolidated_key not in self._concise_bubble_keys:
                    return
                message_id = self._consolidated_message_ids.get(consolidated_key)
                if not message_id:
                    return
                await self._status_heartbeat_render_once(context, im_client, consolidated_key, message_id)
        except asyncio.CancelledError:
            return

    async def _status_heartbeat_render_once(
        self,
        context: MessageContext,
        im_client,
        consolidated_key: str,
        message_id: str,
    ) -> None:
        lock = self._get_consolidated_message_lock(consolidated_key)
        async with lock:
            # Bail if the turn finalized (result delivered → bubble about to be
            # deleted/collapsed) since this tick was scheduled, so a stray tick
            # can't re-render a bubble that's being retired (mirrors the C1 guard
            # in _render_concise_status).
            if consolidated_key in self._status_finalized:
                return
            # Also bail if the key stopped being a concise bubble since this tick
            # was scheduled (mid-turn concise->verbose flip reused the message).
            if consolidated_key not in self._concise_bubble_keys:
                return
            if self._consolidated_message_ids.get(consolidated_key) != message_id:
                return
            body, footer = self._compose_status_message(context, consolidated_key)
            target_context = self._get_target_context(context)
            try:
                await im_client.edit_message(
                    target_context, message_id, text=body, parse_mode="markdown", subtext=footer
                )
            except asyncio.CancelledError:
                # Blocker: must propagate so task.cancel() actually terminates the
                # loop; swallowing it makes _stop_status_heartbeat's await hang.
                raise
            except Exception as err:
                logger.debug("heartbeat edit failed for %s: %s", message_id, err)

    async def _stop_status_heartbeat(self, consolidated_key: str) -> None:
        task = self._status_heartbeat_tasks.pop(consolidated_key, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def _concise_status_bubble_id(self, context: MessageContext) -> Optional[str]:
        """The live concise status-bubble id for this turn, if any (else None).

        Keyed on whether a CONCISE bubble was actually posted for this turn, not
        the current progress style: a Web UI ``concise`` -> ``off``/``verbose``
        change mid-turn (now visible immediately via the getter self-refresh) must
        still let the result path retire an already-posted bubble. Gating on the
        concise-bubble key set (rather than plain ``_consolidated_message_ids``
        membership) avoids mistaking a verbose consolidated process-log id — which
        lives in the same dict — for a status bubble and deleting it."""
        key = self._get_consolidated_message_key(context)
        if key not in self._concise_bubble_keys:
            return None
        return self._consolidated_message_ids.get(key)

    async def _finalize_status_key(self, consolidated_key: str) -> Optional[str]:
        """Atomically mark a turn-key finalized and capture its current bubble id.

        Acquiring the per-key consolidated lock makes "add to ``_status_finalized``
        + read the bubble id" indivisible against a concurrent
        ``_render_concise_status`` (which takes the same lock and bails when the
        key is already finalized). So a late in-flight process emit either runs
        BEFORE this finalize (its bubble id is then the one we collapse/edit) or
        sees the key finalized and bails — it can never land after the terminal
        edit and stomp the bubble back to "working" (C1)."""
        async with self._get_consolidated_message_lock(consolidated_key):
            # Finalized markers persist past _drop_status_keys (see note there) so
            # they must be bounded. A marker only needs to outlive its own turn's
            # teardown→gate-release window (sub-second), so when the set grows large
            # every entry in it is from a turn finished long ago and safe to drop;
            # clear before re-adding THIS turn's key so the live one always remains.
            if len(self._status_finalized) > 512:
                self._status_finalized.clear()
            self._status_finalized.add(consolidated_key)
            return self._consolidated_message_ids.get(consolidated_key)

    async def _collapse_status_bubble(
        self, context: MessageContext, im_client, *, reason: str = "done"
    ) -> None:
        """Collapse a still-open concise status bubble to its terminal marker.

        Single helper for every terminal path that DOESN'T edit the bubble into a
        visible answer: a terminal result that delivered nothing visible (empty /
        silent / suppressed), and the non-inline orphan path (B3) where the result
        was delivered as a separate message. Without it, an eagerly-posted
        footer-only ``begin_status_bubble`` (or a still-running process bubble)
        stays stuck on its last state when the turn ends (missing agent,
        exception, user stop, attachment-only delivery).

        It stops the heartbeat first (so no late tick stomps the marker), then
        marks the key finalized + captures the bubble id atomically (C1), then —
        if a bubble exists — edits it to the terminal footer. The footer marker
        (✅ for ``done`` else ⏹, time only when ``show_duration``) is owned by
        ``_status_footer_text``. ``reason`` ∈ {"done","stopped","failed"}."""
        key = self._get_consolidated_message_key(context)
        # Gate on whether a CONCISE bubble was posted for this turn, not the current
        # style: a Web UI concise -> off/verbose change mid-turn must still collapse
        # an already-posted bubble. Keying on the concise-bubble set (not plain
        # _consolidated_message_ids membership) avoids collapsing a verbose
        # consolidated process-log message, which shares that dict.
        if key not in self._concise_bubble_keys:
            return
        # Heartbeat first: the render also takes the per-key lock, so stopping it
        # before _finalize_status_key avoids contending with a live tick.
        await self._stop_status_heartbeat(key)
        bubble_id = await self._finalize_status_key(key)
        if not bubble_id:
            return
        elapsed_s = self._now() - self._status_started_at.get(key, self._now())
        marker = self._status_footer_text(context, elapsed_s=elapsed_s, done=True, reason=reason)
        target_context = self._get_target_context(context)
        try:
            # Render the marker as the footer (empty body → footer-only bubble) so
            # the edit goes through the status-block path and REPLACES the bubble's
            # blocks. A plain text-only chat.update would leave the old "running"
            # Slack blocks visible, hiding the terminal marker (P2).
            await im_client.edit_message(target_context, bubble_id, text="", subtext=marker, parse_mode="markdown")
        except Exception as err:
            logger.debug("Failed to collapse status bubble %s: %s", bubble_id, err)

    async def _retire_status_bubble(
        self,
        context: MessageContext,
        im_client,
        bubble_id: str,
        *,
        delivered: bool,
        reason: str = "done",
    ) -> None:
        """Retire the transient status bubble once the result is delivered as its
        own message. DELETE it (so the turn ends as just the fresh, notifying
        result) when the platform supports deletion and the result was delivered;
        otherwise collapse it to a terminal marker (the prior behavior) so it never
        lingers as "running"."""
        if delivered and self._capabilities(context).supports_message_deletion:
            try:
                if await im_client.delete_message(self._get_target_context(context), bubble_id):
                    return
            except Exception as err:
                logger.debug("Failed to delete status bubble %s; collapsing instead: %s", bubble_id, err)
        await self._collapse_status_bubble(context, im_client, reason=reason)

    def _record_suppressed_run_message(
        self,
        context: MessageContext,
        text: str,
        message_id: str,
        *,
        terminal_status: Optional[str] = None,
    ) -> None:
        payload = context.platform_specific or {}
        run_ids = _coalesced_task_execution_ids(payload)
        if not run_ids:
            return
        store = None
        try:
            store = SQLiteBackgroundTaskStore()
            for run_id in run_ids:
                store.record_run_message(
                    run_id,
                    text=text,
                    message_id=message_id,
                    terminal_status=terminal_status,
                )
        except Exception as err:
            logger.warning("Failed to record suppressed run output for %s: %s", ",".join(run_ids), err)
        finally:
            if store is not None:
                store.close()

    def _record_agent_run_terminal_for_ids(
        self,
        *,
        store: Any,
        run_ids: list[str],
        text: str,
        message_id: str | None,
        terminal_status: str | None,
    ) -> None:
        for run_id in run_ids:
            get_run = getattr(store, "get_run", None)
            if callable(get_run) and _run_is_cancelled(get_run(run_id)):
                continue
            store.record_run_message(
                run_id,
                text=text,
                message_id=message_id,
                terminal_status=terminal_status,
            )

    def _record_agent_run_terminal_result(
        self,
        context: MessageContext,
        text: str,
        message_id: str | None,
        *,
        is_error: bool,
        log_label: str = "agent run terminal result",
    ) -> None:
        payload = context.platform_specific or {}
        if payload.get("task_trigger_kind") != "agent_run":
            return
        run_ids = _coalesced_task_execution_ids(payload)
        if not run_ids:
            return
        store = None
        try:
            store = SQLiteBackgroundTaskStore()
            self._record_agent_run_terminal_for_ids(
                store=store,
                run_ids=run_ids,
                text=text,
                message_id=message_id,
                terminal_status="failed" if is_error else "succeeded",
            )
        except Exception as err:
            logger.warning("Failed to record %s for %s: %s", log_label, ",".join(run_ids), err)
        finally:
            if store is not None:
                store.close()

    def _record_suppressed_agent_run_terminal_result(
        self,
        context: MessageContext,
        text: str,
        message_id: str | None,
        *,
        is_error: bool,
    ) -> None:
        self._record_agent_run_terminal_result(
            context,
            text,
            message_id,
            is_error=is_error,
            log_label="suppressed agent run terminal result",
        )

    async def clear_consolidated_message_id(
        self,
        context: MessageContext,
        trigger_message_id: Optional[str] = None,
    ) -> None:
        session_key = self._get_session_key(context)
        thread_key = context.thread_id or context.channel_id
        msg_id = trigger_message_id if trigger_message_id else (context.message_id or "")
        key = f"{session_key}:{thread_key}:{msg_id}"
        await self._drop_status_keys(key)

    def _get_consolidated_max_bytes(self, context: MessageContext) -> int:
        platform = self._get_platform(context)
        if platform == "discord":
            return 2000
        if platform == "wechat":
            return _WECHAT_TEXT_LIMIT
        return 4000

    def _get_consolidated_split_threshold(self, context: MessageContext) -> int:
        platform = self._get_platform(context)
        if platform == "discord":
            return 1800
        if platform == "wechat":
            return _WECHAT_CONSOLIDATED_SPLIT_THRESHOLD
        return 3600

    @staticmethod
    def _get_text_byte_length(text: str) -> int:
        return len(text.encode("utf-8"))

    def _get_result_max_chars(self, context: MessageContext) -> int:
        if self._get_platform(context) == "discord":
            return 1900
        return 30000

    def _get_result_max_bytes(self, context: MessageContext) -> Optional[int]:
        if self._get_platform(context) == "wechat":
            return _WECHAT_TEXT_LIMIT
        return None

    def _should_split_long_result(self, context: MessageContext) -> bool:
        return self._get_platform(context) in {"discord", "wechat"}

    def _result_within_limit(self, context: MessageContext, text: str) -> bool:
        max_bytes = self._get_result_max_bytes(context)
        if max_bytes is not None:
            return self._get_text_byte_length(text) <= max_bytes
        return len(text) <= self._get_result_max_chars(context)

    def _supports_quick_replies(self, context: MessageContext) -> bool:
        return self._capabilities(context).supports_quick_replies

    def _is_wechat_context(self, context: MessageContext) -> bool:
        return self._get_platform(context) == "wechat"

    def _supports_message_editing(self, im_client, context: MessageContext) -> bool:
        supports_editing = getattr(im_client, "supports_message_editing", None)
        if callable(supports_editing):
            try:
                return bool(supports_editing(context))
            except TypeError:
                return bool(supports_editing())
        return self._capabilities(context).supports_message_editing

    def _attachment_id_can_anchor_delivery(self, context: MessageContext) -> bool:
        # Only treat attachment uploads as scheduled anchors on platforms where
        # upload_markdown() returns the posted message ID rather than a file ID.
        return self._capabilities(context).markdown_upload_returns_message_id

    @staticmethod
    def _is_video_path(path: str) -> bool:
        return Path(path).suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

    @staticmethod
    def _build_result_summary(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        prefix = "Result too long; showing a summary.\n\n"
        suffix = "\n\n…(truncated; see result.md for full output)"
        keep = max(0, max_chars - len(prefix) - len(suffix))
        return f"{prefix}{text[:keep]}{suffix}"

    @staticmethod
    def _find_result_split_index(text: str, max_chars: int) -> int:
        minimum_boundary = max_chars // 2
        for separator in ("\n\n", "\n", " "):
            index = text.rfind(separator, 0, max_chars + 1)
            if index >= minimum_boundary:
                candidate = index + len(separator)
                return candidate if candidate <= max_chars else index
        return max_chars

    def _split_result_text(self, text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        remaining = text

        while len(remaining) > max_chars:
            split_at = self._find_result_split_index(remaining, max_chars)
            if split_at <= 0:
                split_at = max_chars
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        if remaining:
            chunks.append(remaining)

        return chunks

    def _split_result_text_by_bytes(self, text: str, max_bytes: int) -> list[str]:
        if self._get_text_byte_length(text) <= max_bytes:
            return [text]

        chunks: list[str] = []
        remaining = text

        while self._get_text_byte_length(remaining) > max_bytes:
            prefix = remaining.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            minimum_boundary = max(1, len(prefix) // 2)
            split_at = len(prefix)
            for separator in ("\n\n", "\n", " "):
                index = prefix.rfind(separator)
                if index >= minimum_boundary:
                    candidate = index + len(separator)
                    if self._get_text_byte_length(remaining[:candidate]) <= max_bytes:
                        split_at = candidate
                        break
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]

        if remaining:
            chunks.append(remaining)

        return chunks

    def _split_result_text_for_context(self, context: MessageContext, text: str) -> list[str]:
        max_bytes = self._get_result_max_bytes(context)
        if max_bytes is not None:
            return self._split_result_text_by_bytes(text, max_bytes)
        return self._split_result_text(text, self._get_result_max_chars(context))

    def _truncate_consolidated(self, text: str, max_bytes: int) -> str:
        if self._get_text_byte_length(text) <= max_bytes:
            return text
        ellipsis = "…"
        target_bytes = max_bytes - len(ellipsis.encode("utf-8"))
        encoded = text.encode("utf-8")
        truncated = encoded[:target_bytes].decode("utf-8", errors="ignore")
        return truncated.rstrip() + ellipsis

    async def _send_unconsolidated_log_message(
        self,
        im_client,
        context: MessageContext,
        text: str,
    ) -> Optional[str]:
        target_context = self._get_target_context(context)
        max_bytes = self._get_consolidated_max_bytes(context)
        chunks = self._split_result_text_by_bytes(text, max_bytes)
        first_message_id: Optional[str] = None

        for chunk in chunks:
            try:
                message_id = await im_client.send_message(target_context, chunk, parse_mode="markdown")
            except Exception as err:
                logger.error("Failed to send Log Message: %s", err, exc_info=True)
                return first_message_id
            if first_message_id is None:
                first_message_id = message_id

        return first_message_id

    async def emit_agent_message(
        self,
        context: MessageContext,
        message_type: str,
        text: str,
        parse_mode: Optional[str] = "markdown",
        *,
        is_error: bool = False,
        level: str = "normal",
        status_label: Optional[str] = None,
    ) -> Optional[str]:
        """Centralized dispatch for agent messages.

        Message Types:
        - Log Messages (system/assistant/toolcall): consolidated into a single
          editable message per conversation round. Can be hidden by user settings.
        - Result Message: final output, always sent immediately, not hideable.
        - Notify Message: notifications, always sent immediately.

        ``is_error`` marks a terminal ``result`` as a FAILED turn. It is the only
        signal the sidebar dot needs on the way out: a terminal result settles the
        session to ``idle`` (or ``failed`` when ``is_error``). Callers that hit a
        terminal failure emit it as ``result`` + ``is_error=True`` instead of a
        bare ``notify`` — that routes the failure through this one outbound
        chokepoint (dot + SSE stream release), so no caller pokes the dot directly.

        ``level`` is the visibility grade — orthogonal to ``message_type``. The
        type says what role the message plays (and drives the dot + unread); the
        level says whether the user should SEE it:
        - ``"normal"`` (default): delivered / persisted / streamed as usual.
        - ``"silent"``: settles the dot + releases the SSE waiter for a terminal
          ``result``, then returns WITHOUT delivering, persisting, or streaming.
          Used for intentional, non-noteworthy lifecycle events (e.g. a user-
          initiated stop) so the turn ends cleanly with no user-facing bubble —
          replacing the old "fake it with empty text" trick with an explicit flag.

        ``status_label`` is an optional backend-computed clean tool-call label
        (claude-pipe style) used ONLY as the concise status-bubble body for the
        process path (assistant/toolcall). The persisted row and the verbose
        append path keep using the original ``text`` unchanged; when it is empty
        the bubble falls back to ``to_status_label(text)`` as before.
        """
        settings_manager = self.controller.get_settings_manager_for_context(context)
        im_client = self._get_im_client(context)

        canonical_type = settings_manager._canonicalize_message_type(message_type or "")
        settings_key = self._get_settings_key(context)

        # Terminal status-bubble reason word for the done/orphan footer:
        # a clean turn is "done" (✅); a failure is "stopped" (⏹) when it was an
        # intentional silent stop (e.g. user stop), else "failed" (⏹). Computed
        # here where both is_error + level are known, then threaded into the
        # compose/tidy helpers so the footer marker stays consistent.
        terminal_reason = "done" if not is_error else ("stopped" if level == "silent" else "failed")

        # OUTBOUND status chokepoint (one of exactly two — the other is the
        # inbound AgentService.handle_message). A terminal ``result`` ends the
        # turn, so settle the avibe sidebar dot here regardless of delivery
        # outcome. Non-avibe contexts resolve to no session id and are skipped;
        # ``getattr`` keeps it a no-op for controllers without the hook (mirrors
        # ``_signal_turn_complete``).
        if canonical_type == "result":
            if not self._is_current_runtime_turn(context):
                logger.info("Dropping stale result emit for superseded runtime turn in %s", self._get_session_key(context))
                return None
            # Settle the avibe dot for the ACTIVE turn's terminal result (idle, or
            # failed on is_error) via the turn owner, which applies the active-turn
            # guard + skips non-avibe contexts. Runtime gate release happens after
            # the result path clears/persists/streams its own state.
            manager = getattr(self.controller, "session_turns", None)
            if manager is not None:
                manager.on_terminal_result(context, is_error=is_error)
        text = strip_silent_blocks(text)
        # ``level="silent"`` is the explicit visibility control (orthogonal to type):
        # the message already settled the dot above (for a terminal result), so here
        # we release the SSE waiter and return BEFORE any delivery / persistence /
        # streaming — no user-facing bubble, regardless of body. An empty/stripped
        # body (e.g. a ``<silent>`` directive reduced to nothing) is silent too.
        if level == "silent" or not text or not text.strip():
            try:
                if canonical_type == "result":
                    # A terminal result — even silent/empty — still means the turn
                    # finished: release the streaming SSE waiter so it closes now
                    # instead of hanging until the safety timeout, with no visible chunk.
                    # Collapse any eagerly-posted footer-only bubble first so it
                    # doesn't stay stuck (missing agent / exception / user stop).
                    await self._collapse_status_bubble(context, im_client, reason=terminal_reason)
                    await self._clear_consolidated_state(context)
                    self._record_agent_run_terminal_result(
                        context,
                        text,
                        None,
                        is_error=is_error,
                    )
                    self._signal_turn_complete(context)
                return None
            finally:
                if canonical_type == "result":
                    await self._finish_processing_indicator_turn(context)
                    self._release_runtime_turn(context)

        # Resolve the delivery target once. Routed / post_to / thread replies
        # land in a different channel than the source context, and the persisted
        # row must follow the reply to where it was actually delivered (IM
        # cross-platform history) — persist_agent_message attributes IM rows to
        # this target's scope.
        target_context = self._get_target_context(context)

        # For a result, persist the SAME cleaned text the user receives:
        # process_reply() strips file:// markdown links + the trailing
        # quick-reply button block before delivery/streaming, so persisting the
        # raw text would surface markup in the inbox preview / chat transcript
        # that was never shown. Computed once here and reused for delivery below.
        enhanced = None
        persist_text = text
        if canonical_type == "result":
            quick_replies_on = getattr(self.controller.config, "reply_enhancements", True)
            enhanced = process_reply(text, include_quick_replies=quick_replies_on)
            persist_text = enhanced.text if enhanced.text.strip() else text

        # Persistence is decided per delivery path below, not here, so that:
        #   * suppressed scheduled runs (intentionally private) never leak into
        #     the cross-platform messages history,
        #   * a user-facing result/notify that fails every IM send isn't recorded
        #     as if the user received it (matches the old success-only mirror),
        #   * intermediate assistant/tool_call log rows STILL persist pre-mute so
        #     muted process messages land in the store.
        # avibe always persists its result/notify: the SSE stream is the delivery
        # and the persisted row is the inbox/transcript source of truth.
        persists_without_delivery = target_context.platform == "avibe"

        if (context.platform_specific or {}).get("suppress_delivery"):
            try:
                message_id = f"suppressed:{(context.platform_specific or {}).get('task_execution_id') or canonical_type}"
                terminal_status = None
                if (
                    canonical_type == "result"
                    and (context.platform_specific or {}).get("task_trigger_kind") == "agent_run"
                ):
                    self._record_suppressed_agent_run_terminal_result(
                        context,
                        text,
                        message_id,
                        is_error=is_error,
                    )
                elif canonical_type == "result" or (context.platform_specific or {}).get("task_trigger_kind") != "agent_run":
                    self._record_suppressed_run_message(
                        context,
                        text,
                        message_id,
                        terminal_status=terminal_status,
                    )
                if canonical_type == "result":
                    # A suppressed result still ends the turn; collapse any concise
                    # status bubble posted to a real channel so it doesn't stay stuck.
                    await self._collapse_status_bubble(context, im_client, reason=terminal_reason)
                    await self._clear_consolidated_state(context)
                    self._signal_turn_complete(context)
                return message_id
            finally:
                if canonical_type == "result":
                    await self._finish_processing_indicator_turn(context)
                    self._release_runtime_turn(context)

        if canonical_type == "notify":
            try:
                message_id = await im_client.send_message(target_context, text, parse_mode=parse_mode)
                # Record only once delivered (avibe always, via SSE) so a failed
                # IM send isn't stored as if the user received it.
                if persists_without_delivery or message_id is not None:
                    persist_agent_message(target_context, "notify", text)
                # Live SSE turn stream for the web Chat page (no-op for IM/CLI).
                await _stream_chunk(self.controller, context, text=text, message_id=message_id, kind="notify")
                return message_id
            except Exception as err:
                logger.error("Failed to send notify message: %s", err)
            return None

        if canonical_type == "result":
            try:
                primary_message_id: Optional[str] = None
                scheduled_anchor_message_id: Optional[str] = None
                delivered_as_attachment = False

                # Concise status bubble (Slack/Discord): the live process bubble for
                # this turn. The result is ALWAYS delivered as a NEW message (never
                # an edit of the bubble) so the IM fires a push notification; the
                # transient bubble is then deleted (or collapsed to a marker when the
                # platform can't delete) by ``_retire_status_bubble`` below.
                status_bubble_id = self._concise_status_bubble_id(context)
                status_consolidated_key = self._get_consolidated_message_key(context) if status_bubble_id else None
                # S3: stop the heartbeat + finalize the key BEFORE delivery so a late
                # heartbeat tick / in-flight process emit can't resurrect the bubble
                # while we retire it (C1).
                if status_consolidated_key:
                    await self._stop_status_heartbeat(status_consolidated_key)
                    await self._finalize_status_key(status_consolidated_key)

                # ``enhanced`` (extracted file links + quick-reply buttons) was
                # computed above for persistence; reuse it for delivery.
                display_text = enhanced.text if enhanced.text.strip() else text

                # The concise done-footer (``✅ done · 248k tok``) is attached to the
                # fresh result message as platform subtext so the turn's final
                # outcome + context-window usage survives the bubble's deletion.
                done_footer: Optional[str] = None
                if status_consolidated_key:
                    _, done_footer = self._compose_status_message(
                        context, status_consolidated_key, done=True, reason=terminal_reason, result_body=display_text
                    )
                # Pass subtext to RAW send_message calls only when set, so an adapter
                # whose send_message predates the subtext kwarg is never handed it
                # (the helper paths apply the same guard internally).
                footer_kwargs = {"subtext": done_footer} if done_footer else {}

                # Deliver the result as a NEW message: inline / split / summarized.
                if self._result_within_limit(context, display_text):
                    try:
                        primary_message_id = await self._send_result_inline(
                            im_client,
                            target_context,
                            display_text,
                            enhanced.buttons if enhanced else [],
                            parse_mode,
                            subtext=done_footer,
                        )
                        scheduled_anchor_message_id = primary_message_id
                    except Exception as err:
                        if enhanced and enhanced.buttons and self._supports_quick_replies(context):
                            logger.warning("Failed to send result with quick replies, falling back: %s", err)
                            try:
                                primary_message_id = await im_client.send_message(
                                    target_context, display_text, parse_mode=parse_mode, **footer_kwargs
                                )
                                scheduled_anchor_message_id = primary_message_id
                            except Exception as fallback_err:
                                logger.error("Failed to send fallback result message: %s", fallback_err)
                        else:
                            logger.error("Failed to send result message: %s", err)
                elif self._should_split_long_result(context):
                    try:
                        primary_message_id = await self._send_split_result_messages(
                            im_client,
                            target_context,
                            display_text,
                            enhanced.buttons if enhanced else [],
                            parse_mode,
                            subtext=done_footer,
                        )
                        scheduled_anchor_message_id = primary_message_id
                    except Exception as err:
                        logger.error("Failed to send split result messages: %s", err)
                else:
                    # Summary path (too big to send inline, not splittable): post a
                    # short summary AND attach the full content as a .md file. The
                    # attachment supplements ONLY this path — inline/split already
                    # deliver the full result, so they don't upload.
                    summary = self._build_result_summary(display_text, self._get_result_max_chars(context))
                    try:
                        primary_message_id = await im_client.send_message(
                            target_context, summary, parse_mode=parse_mode, **footer_kwargs
                        )
                        scheduled_anchor_message_id = primary_message_id
                    except Exception as err:
                        logger.error("Failed to send result summary: %s", err)

                    if self._get_platform(context) in {"slack", "discord", "telegram", "lark"} and hasattr(
                        im_client, "upload_markdown"
                    ):
                        try:
                            attachment_message_id = await im_client.upload_markdown(
                                target_context,
                                title="result.md",
                                content=display_text,
                                filetype="markdown",
                            )
                            if primary_message_id is None:
                                primary_message_id = attachment_message_id
                                delivered_as_attachment = True
                                if self._attachment_id_can_anchor_delivery(context):
                                    scheduled_anchor_message_id = attachment_message_id
                        except Exception as err:
                            logger.warning(f"Failed to upload result attachment: {err}")
                            await im_client.send_message(
                                target_context,
                                self._t("error.resultAttachmentUploadFailed"),
                                parse_mode=parse_mode,
                            )

                # --- Fallback: card content rejected (e.g. table over limit) ---
                if primary_message_id is None and display_text:
                    logger.warning("All direct result sends failed; attempting fallback delivery")
                    file_uploaded = False

                    # Fallback 1: upload full content as .md file.
                    if hasattr(im_client, "upload_markdown"):
                        try:
                            primary_message_id = await im_client.upload_markdown(
                                target_context,
                                title="result.md",
                                content=display_text,
                                filetype="markdown",
                            )
                            file_uploaded = True
                            delivered_as_attachment = True
                            if self._attachment_id_can_anchor_delivery(context):
                                scheduled_anchor_message_id = primary_message_id
                            logger.info("Result delivered as .md file attachment (fallback)")
                        except Exception as upload_err:
                            logger.warning("upload_markdown fallback failed: %s", upload_err)

                    # Fallback 2: split into multiple messages.
                    if not file_uploaded:
                        try:
                            primary_message_id = await self._send_split_result_messages(
                                im_client,
                                target_context,
                                display_text,
                                enhanced.buttons if enhanced else [],
                                parse_mode,
                                subtext=done_footer,
                            )
                            scheduled_anchor_message_id = primary_message_id
                            logger.info("Result delivered via split messages (fallback)")
                        except Exception as split_err:
                            logger.error("Split message fallback also failed: %s", split_err)

                # Explain attachment-only delivery or total failure once all attempts settle.
                try:
                    if delivered_as_attachment:
                        notice = self._t("info.resultDeliveredAsAttachment")
                    elif primary_message_id is None and display_text:
                        notice = self._t("error.resultDeliveryFailed")
                    else:
                        notice = None
                    if notice:
                        await im_client.send_message(target_context, notice, parse_mode="markdown")
                except Exception:
                    logger.error("Failed to send delivery status notification")

                # Upload extracted file attachments
                if enhanced and enhanced.files:
                    await self._upload_file_links(im_client, target_context, enhanced.files)

                if scheduled_anchor_message_id:
                    try:
                        self.controller.session_handler.finalize_scheduled_delivery(context, scheduled_anchor_message_id)
                    except Exception as err:
                        logger.warning("Failed to finalize scheduled delivery anchor: %s", err)

                # Retire the transient status bubble now the result is delivered as
                # its own (notifying) message: DELETE it so the turn ends as just the
                # fresh result. When the platform can't delete (or the delete fails,
                # or nothing was delivered) fall back to collapsing it to a terminal
                # marker (✅ done / ⏹ stopped|failed) so it never reads as running.
                if status_bubble_id:
                    await self._retire_status_bubble(
                        context,
                        im_client,
                        status_bubble_id,
                        delivered=primary_message_id is not None,
                        reason=terminal_reason,
                    )

                # Final result closes the current turn: clear consolidated
                # assistant/tool/system message state so the next user turn starts
                # a fresh log message instead of appending to the previous one.
                # Use the key captured at the top of this branch when present so
                # teardown can't drift onto a recomputed key (C5).
                if status_consolidated_key:
                    await self._drop_status_keys(status_consolidated_key)
                else:
                    await self._clear_consolidated_state(context)

                self._record_agent_run_terminal_result(
                    context,
                    display_text,
                    primary_message_id,
                    is_error=is_error,
                )

                # Persist the delivered result (cleaned text == what was shown).
                # avibe always persists (SSE is its delivery); for IM a result that
                # failed every send/upload (primary_message_id is None) is NOT
                # recorded, matching the old outbound mirror's success-only rule.
                if persists_without_delivery or primary_message_id is not None:
                    # A failed terminal result persists as type='error' so it shows in
                    # the transcript/inbox like any terminal message but is NOT counted
                    # as an unread agent reply (unread queries are result-only). Codex P2.
                    result_type = "error" if is_error else "result"
                    if target_context.platform == "avibe":
                        # Keep the ``file://`` links in the persisted avibe text so the
                        # workbench media-proxy rewrite (in ``persist_agent_message``)
                        # can turn them into inline images / file cards. ``persist_text``
                        # already has them stripped to plain labels for IM delivery.
                        # Also carry the parsed quick-reply labels so the workbench can
                        # render the button group (IM channels render native buttons
                        # from the same ``enhanced.buttons``).
                        avibe_enhanced = process_reply(
                            text, include_quick_replies=quick_replies_on, keep_file_links=True
                        )
                        avibe_text = avibe_enhanced.text or persist_text
                        persist_agent_message(
                            target_context,
                            result_type,
                            avibe_text,
                            quick_replies=[b.text for b in avibe_enhanced.buttons] or None,
                        )
                    else:
                        persist_agent_message(target_context, result_type, persist_text)

                if primary_message_id and display_text:
                    # Stream the delivered result to live consumers (avibe SSE).
                    await _stream_chunk(
                        self.controller, context, text=display_text, message_id=primary_message_id, kind="result"
                    )
                else:
                    # A terminal result still completes the turn even if every IM
                    # delivery path failed and therefore produced no durable message id.
                    # Without this release, direct agent_run and avibe turn waiters keep
                    # waiting forever despite the backend having already finished.
                    self._signal_turn_complete(context)

                return primary_message_id
            finally:
                await self._finish_processing_indicator_turn(context)
                self._release_runtime_turn(context)

        if canonical_type not in {"system", "assistant", "toolcall"}:
            canonical_type = "assistant"

        if not self._is_current_runtime_turn(context):
            logger.info(
                "Dropping stale %s emit for superseded runtime turn in %s",
                canonical_type,
                self._get_session_key(context),
            )
            return None

        # Persist the intermediate log row BEFORE the mute filter so muted
        # assistant / tool_call messages still land in the store (product
        # requirement: the process log is complete even when a channel hides it).
        persist_agent_message(target_context, canonical_type, persist_text)

        # Target platform toolcall-delivery gate stays in FRONT of the concise
        # shortcut: when a turn is routed via ``delivery_override`` to a target
        # that cannot deliver toolcalls (e.g. the WeChat override flow), the
        # toolcall stays persisted-only and must NOT attempt a status bubble.
        if canonical_type == "toolcall" and not self._supports_toolcall_delivery(target_context):
            logger.info(
                "Skipping toolcall delivery for platform %s; persisted local process log only.",
                self._get_platform(target_context),
            )
            return None

        # The concise status bubble is a single ephemeral status line, NOT the
        # verbose per-message log that ``show_message_types`` governs. When a
        # status-bubble platform (Slack/Discord) is in ``concise`` style, render
        # tool-step / muttering emits into the bubble regardless of the
        # ``is_message_type_hidden`` visibility toggle below: persistence already
        # happened above so nothing is lost, and users who want zero process
        # output use ``off``. This bypasses ONLY the visibility toggle — the
        # target toolcall-delivery gate above still applies. ``system`` emits
        # carry no status label and stay on the gated path.
        if canonical_type in {"assistant", "toolcall"} and self._concise_progress_style(context) == "concise":
            # ``_render_concise_status`` renders a footer-only bubble when the
            # stripped body is empty but ``status_label`` is present, so do not
            # drop empty-body toolcall emits here.
            concise_chunk = strip_file_links(text).strip()
            return await self._render_concise_status(
                im_client, context, concise_chunk, status_label=status_label
            )

        if settings_manager.is_message_type_hidden(settings_key, canonical_type):
            preview = text if len(text) <= 500 else f"{text[:500]}…"
            logger.info(
                "Skipping %s message for settings %s (hidden). Preview: %s",
                canonical_type,
                settings_key,
                preview,
            )
            return None

        chunk = strip_file_links(text).strip()

        if not chunk:
            return None

        if not self._supports_message_editing(im_client, context):
            return await self._send_unconsolidated_log_message(im_client, context, chunk)

        # Concise status bubble (Slack/Discord): one replace-not-append line that
        # is later edited into the result. ``off`` shows no process bubble at all
        # (typing + final result only). ``verbose`` falls through to the legacy
        # append/split consolidation below (and is the path for all other platforms).
        progress_style = self._concise_progress_style(context)
        if progress_style == "off":
            return None
        if progress_style == "concise":
            return await self._render_concise_status(im_client, context, chunk, status_label=status_label)

        consolidated_key = self._get_consolidated_message_key(context)
        lock = self._get_consolidated_message_lock(consolidated_key)

        async with lock:
            # Verbose consolidation path. Because the progress style is snapshotted
            # per turn (see ``_concise_progress_style``), a turn that reaches here is
            # verbose for its whole lifetime — it never posted a concise bubble — so
            # ``_consolidated_message_ids[key]`` here is only ever a verbose log id,
            # never a status bubble to reconcile.
            max_bytes = self._get_consolidated_max_bytes(context)
            split_threshold = self._get_consolidated_split_threshold(context)
            existing = self._consolidated_message_buffers.get(consolidated_key, "")
            existing_message_id = self._consolidated_message_ids.get(consolidated_key)

            separator = "\n\n---\n\n" if existing else ""
            updated = f"{existing}{separator}{chunk}" if existing else chunk

            target_context = self._get_target_context(context)
            continuation_notice = "\n\n---\n\n_(continued below...)_"
            continuation_bytes = self._get_text_byte_length(continuation_notice)

            if existing_message_id and self._get_text_byte_length(updated) > split_threshold:
                old_text = existing + continuation_notice
                old_text = self._truncate_consolidated(old_text, max_bytes)

                try:
                    await im_client.edit_message(
                        target_context,
                        existing_message_id,
                        text=old_text,
                        parse_mode="markdown",
                    )
                except Exception as err:
                    logger.warning(f"Failed to finalize old Log Message: {err}")

                self._consolidated_message_buffers[consolidated_key] = chunk
                self._consolidated_message_ids.pop(consolidated_key, None)
                updated = chunk
                existing_message_id = None
                logger.info(
                    "Log Message exceeded %d bytes, starting new message",
                    split_threshold,
                )

            while self._get_text_byte_length(updated) > max_bytes:
                target_bytes = split_threshold - continuation_bytes
                first_part = self._truncate_consolidated(updated, target_bytes)
                first_part = first_part.rstrip("…") + continuation_notice

                send_ok = False
                if existing_message_id:
                    try:
                        await im_client.edit_message(
                            target_context,
                            existing_message_id,
                            text=first_part,
                            parse_mode="markdown",
                        )
                        send_ok = True
                    except Exception as err:
                        logger.warning(f"Failed to edit oversized Log Message: {err}")
                else:
                    try:
                        await im_client.send_message(target_context, first_part, parse_mode="markdown")
                        send_ok = True
                    except Exception as err:
                        logger.error(f"Failed to send oversized Log Message: {err}")

                if not send_ok:
                    logger.warning("Stopping split loop due to send failure, truncating remainder")
                    break

                sent_chars = len(first_part) - len(continuation_notice)
                updated = updated[sent_chars:]
                existing_message_id = None
                self._consolidated_message_ids.pop(consolidated_key, None)
                logger.info(
                    "Log Message chunk exceeded %d bytes, split and continuing",
                    max_bytes,
                )

            updated = self._truncate_consolidated(updated, max_bytes)
            self._consolidated_message_buffers[consolidated_key] = updated

            if existing_message_id:
                try:
                    ok = await im_client.edit_message(
                        target_context,
                        existing_message_id,
                        text=updated,
                        parse_mode="markdown",
                    )
                except Exception as err:
                    logger.warning(f"Failed to edit Log Message: {err}")
                    ok = False
                if ok:
                    return existing_message_id
                self._consolidated_message_ids.pop(consolidated_key, None)

            try:
                new_id = await im_client.send_message(target_context, updated, parse_mode="markdown")
                self._consolidated_message_ids[consolidated_key] = new_id
                return new_id
            except Exception as err:
                logger.error(f"Failed to send Log Message: {err}", exc_info=True)
                return None

    # ------------------------------------------------------------------
    # Reply-enhancement helpers
    # ------------------------------------------------------------------

    async def _send_with_quick_replies(
        self,
        im_client,
        context: MessageContext,
        text: str,
        buttons,
        parse_mode,
    ) -> str:
        """Send a message with quick-reply buttons appended."""
        keyboard = self._build_quick_reply_keyboard(context, buttons)
        return await im_client.send_message_with_buttons(
            context,
            text,
            keyboard,
            parse_mode=parse_mode,
        )

    def _build_quick_reply_keyboard(self, context: MessageContext, buttons):
        from modules.im.base import InlineButton, InlineKeyboard

        row = []
        for btn in buttons:
            callback = f"quick_reply:{btn.text}"
            row.append(InlineButton(text=btn.text, callback_data=callback))

        rows = [[button] for button in row] if self._capabilities(context).quick_reply_single_column else [row]
        return InlineKeyboard(buttons=rows)

    async def _send_result_inline(
        self,
        im_client,
        context: MessageContext,
        text: str,
        buttons,
        parse_mode,
        subtext: Optional[str] = None,
    ) -> str:
        keyboard = None
        if buttons and self._supports_quick_replies(context):
            keyboard = self._build_quick_reply_keyboard(context, buttons)

        # ``subtext`` (the concise done-footer) is only set for status-bubble
        # platforms; pass it as a kwarg ONLY when present so non-bubble adapters
        # whose senders don't accept ``subtext`` are never handed it.
        footer = {"subtext": subtext} if subtext else {}

        native_markdown_sender = getattr(im_client, "send_markdown_message", None)
        if parse_mode == "markdown" and callable(native_markdown_sender):
            return await native_markdown_sender(context, text, keyboard=keyboard, **footer)

        if keyboard is not None:
            return await im_client.send_message_with_buttons(
                context,
                text,
                keyboard,
                parse_mode=parse_mode,
                **footer,
            )

        return await im_client.send_message(context, text, parse_mode=parse_mode, **footer)

    async def _send_split_result_messages(
        self,
        im_client,
        context: MessageContext,
        text: str,
        buttons,
        parse_mode,
        subtext: Optional[str] = None,
    ) -> Optional[str]:
        """Deliver a long result as multiple fresh messages.

        Quick-reply buttons and the ``subtext`` done-footer both ride on the LAST
        chunk (a mid-stream footer would read wrong); every chunk is a new send so
        the result notifies. Returns the first chunk's id (the delivery anchor).
        """
        chunks = self._split_result_text_for_context(context, text)
        first_message_id: Optional[str] = None

        for index, chunk in enumerate(chunks):
            is_last_chunk = index == len(chunks) - 1
            want_buttons = is_last_chunk and buttons and self._supports_quick_replies(context)
            chunk_subtext = subtext if is_last_chunk else None
            message_id: Optional[str] = None

            if want_buttons:
                try:
                    message_id = await self._send_result_inline(
                        im_client, context, chunk, buttons, parse_mode, subtext=chunk_subtext
                    )
                except Exception as err:
                    logger.warning("Failed to send split result chunk with quick replies, falling back: %s", err)

            if message_id is None:
                # A later-chunk failure must NOT propagate so an earlier delivered
                # chunk isn't lost — best-effort, log, continue.
                try:
                    message_id = await self._send_result_inline(
                        im_client, context, chunk, [], parse_mode, subtext=chunk_subtext
                    )
                except Exception as err:
                    logger.warning("Failed to send split result chunk %d: %s", index, err)
                    message_id = None

            if index == 0 and message_id is None:
                # The HEAD chunk failed: abandon the split and return None so the
                # caller's fallback delivers the COMPLETE content (e.g. as a .md
                # upload). Returning a later chunk's id here would mark a head-less
                # partial — the user seeing only the tail — as a successful delivery.
                logger.warning("Split result head chunk failed to send; abandoning partial split")
                return None

            if first_message_id is None and message_id is not None:
                first_message_id = message_id

        return first_message_id

    async def _upload_file_links(
        self,
        im_client,
        context: MessageContext,
        files,
    ) -> None:
        """Upload local files referenced by ``file://`` links."""
        import os
        from pathlib import Path

        if not hasattr(im_client, "upload_file_from_path"):
            logger.debug("IM client does not support upload_file_from_path; skipping file uploads")
            return

        notify_wechat_failure = self._is_wechat_context(context)

        for fl in files:
            if not os.path.isfile(fl.path):
                logger.warning("File not found, skipping upload: %s", fl.path)
                continue

            try:
                resolved = Path(fl.path).resolve(strict=True)
            except (OSError, ValueError):
                logger.warning("Cannot resolve file path, skipping: %s", fl.path)
                continue

            # Use link label as title, but preserve file extension so users can
            # download/open files correctly on all platforms.
            upload_title = (fl.label or "").strip() or os.path.basename(fl.path)
            src_ext = resolved.suffix
            if src_ext and not Path(upload_title).suffix:
                upload_title = f"{upload_title}{src_ext}"

            try:
                upload_result = None
                if self._is_video_path(str(resolved)):
                    upload_result = await im_client.upload_video_from_path(
                        context,
                        file_path=str(resolved),
                        title=upload_title,
                    )
                elif getattr(fl, "is_image", False):
                    try:
                        upload_result = await im_client.upload_image_from_path(
                            context,
                            file_path=str(resolved),
                            title=upload_title,
                        )
                        if notify_wechat_failure and not upload_result:
                            raise RuntimeError("image upload returned no message id")
                    except Exception as image_err:
                        logger.warning(
                            "Image upload failed for %s, fallback to file upload: %r",
                            fl.path,
                            image_err,
                        )
                        upload_result = await im_client.upload_file_from_path(
                            context,
                            file_path=str(resolved),
                            title=upload_title,
                        )
                else:
                    upload_result = await im_client.upload_file_from_path(
                        context,
                        file_path=str(resolved),
                        title=upload_title,
                    )
                if notify_wechat_failure and not upload_result:
                    await self._send_file_upload_failure_notice(
                        im_client,
                        context,
                        file_path=str(resolved),
                        file_name=upload_title,
                    )
            except NotImplementedError:
                logger.debug("IM client does not implement file uploads; skipping")
                return
            except Exception as err:
                logger.warning("Failed to upload file %s: %r", fl.path, err)
                if notify_wechat_failure:
                    await self._send_file_upload_failure_notice(
                        im_client,
                        context,
                        file_path=str(resolved),
                        file_name=upload_title,
                    )

    def _register_public_file_download_url(
        self,
        context: MessageContext,
        *,
        file_path: str,
        file_name: str,
    ) -> Optional[str]:
        """Register a local file under the existing media proxy and return a public URL."""
        try:
            from core.avibe_cloud import base_public_url
            from core.message_mirror import DEFAULT_SCOPE_TYPE
            from core.workbench_media import register_agent_reply_media
            from storage import settings_service
            from storage.db import create_sqlite_engine
            from sqlalchemy import select
            from storage.models import agent_sessions

            base = base_public_url(getattr(self.controller, "config", None))
            if not base:
                return None

            engine = create_sqlite_engine()
            with engine.begin() as conn:
                scope_id = settings_service.upsert_scope(
                    conn,
                    platform=context.platform or "wechat",
                    scope_type=DEFAULT_SCOPE_TYPE,
                    native_id=context.channel_id or context.user_id or "wechat",
                    now=datetime.now(timezone.utc).isoformat(),
                    supports_threads=bool(context.thread_id),
                )
                session_id = (context.platform_specific or {}).get("agent_session_id")
                if session_id:
                    existing_session_id = conn.execute(
                        select(agent_sessions.c.id).where(agent_sessions.c.id == str(session_id))
                    ).scalar_one_or_none()
                    session_id = existing_session_id
                token = register_agent_reply_media(
                    conn,
                    scope_id=scope_id,
                    session_id=session_id,
                    kind="file",
                    local_path=file_path,
                    file_name=file_name,
                )
        except Exception:
            logger.warning("Failed to register fallback download link for %s", file_path, exc_info=True)
            return None

        return urljoin(base.rstrip("/") + "/", f"api/media/{token}?download=1")

    async def _send_file_upload_failure_notice(
        self,
        im_client,
        context: MessageContext,
        *,
        file_path: str,
        file_name: str,
    ) -> None:
        """Tell WeChat users when native file upload failed instead of leaving only a filename."""
        public_url = self._register_public_file_download_url(
            context,
            file_path=file_path,
            file_name=file_name,
        )
        key = "error.fileAttachmentUploadFailedWithLink" if public_url else "error.fileAttachmentUploadFailedNoLink"
        message = self._t(key, filename=file_name, url=public_url or "")
        try:
            await im_client.send_message(context, message, parse_mode="plain")
        except Exception:
            logger.warning("Failed to send file upload failure notice for %s", file_path, exc_info=True)
