"""Consolidated agent message dispatcher.

Owns the main log/result/notify dispatch state machine that was previously
embedded in ``Controller.emit_agent_message``.
"""

from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

from config.platform_registry import get_platform_descriptor
from modules.im import MessageContext
from core.message_mirror import persist_agent_message
from core.reply_enhancer import process_reply, strip_file_links, strip_silent_blocks
from core.session_turns import emit_matches_active_turn
from storage.background import SQLiteBackgroundTaskStore
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)


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


class ConsolidatedMessageDispatcher:
    """Dispatch agent messages while preserving existing product behavior."""

    def __init__(self, controller):
        self.controller = controller
        self._consolidated_message_ids: dict[str, str] = {}
        self._consolidated_message_buffers: dict[str, str] = {}
        self._consolidated_message_locks: dict[str, asyncio.Lock] = {}
        self._thread_current_message_id: dict[str, str] = {}

    def _get_platform(self, context: MessageContext) -> str:
        return context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform

    def _capabilities(self, context: MessageContext):
        return get_platform_descriptor(self._get_platform(context)).capabilities

    def _get_settings_key(self, context: MessageContext) -> str:
        return self.controller._get_settings_key(context)

    def _get_session_key(self, context: MessageContext) -> str:
        return self.controller._get_session_key(context)

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

    async def _clear_consolidated_state(self, context: MessageContext) -> None:
        consolidated_key = self._get_consolidated_message_key(context)
        lock = self._get_consolidated_message_lock(consolidated_key)
        async with lock:
            self._consolidated_message_ids.pop(consolidated_key, None)
            self._consolidated_message_buffers.pop(consolidated_key, None)

    def _record_suppressed_run_message(
        self,
        context: MessageContext,
        text: str,
        message_id: str,
        *,
        terminal_status: Optional[str] = None,
    ) -> None:
        payload = context.platform_specific or {}
        run_id = str(payload.get("task_execution_id") or "").strip()
        if not run_id:
            return
        store = None
        try:
            store = SQLiteBackgroundTaskStore()
            store.record_run_message(
                run_id,
                text=text,
                message_id=message_id,
                terminal_status=terminal_status,
            )
        except Exception as err:
            logger.warning("Failed to record suppressed run output for %s: %s", run_id, err)
        finally:
            if store is not None:
                store.close()

    def _record_agent_run_terminal_result(
        self,
        context: MessageContext,
        text: str,
        message_id: str | None,
        *,
        is_error: bool,
    ) -> None:
        payload = context.platform_specific or {}
        if payload.get("task_trigger_kind") != "agent_run":
            return
        run_id = str(payload.get("task_execution_id") or "").strip()
        if not run_id:
            return
        store = None
        try:
            store = SQLiteBackgroundTaskStore()
            store.record_run_message(
                run_id,
                text=text,
                message_id=message_id,
                terminal_status="failed" if is_error else "succeeded",
            )
        except Exception as err:
            logger.warning("Failed to record agent run terminal result for %s: %s", run_id, err)
        finally:
            if store is not None:
                store.close()

    async def clear_consolidated_message_id(
        self,
        context: MessageContext,
        trigger_message_id: Optional[str] = None,
    ) -> None:
        session_key = self._get_session_key(context)
        thread_key = context.thread_id or context.channel_id
        msg_id = trigger_message_id if trigger_message_id else (context.message_id or "")
        key = f"{session_key}:{thread_key}:{msg_id}"

        lock = self._get_consolidated_message_lock(key)
        async with lock:
            self._consolidated_message_ids.pop(key, None)
            self._consolidated_message_buffers.pop(key, None)

    def _get_consolidated_max_bytes(self, context: MessageContext) -> int:
        platform = (
            context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform
        )
        if platform == "discord":
            return 2000
        if platform == "wechat":
            return _WECHAT_TEXT_LIMIT
        return 4000

    def _get_consolidated_split_threshold(self, context: MessageContext) -> int:
        platform = (
            context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform
        )
        if platform == "discord":
            return 1800
        if platform == "wechat":
            return _WECHAT_CONSOLIDATED_SPLIT_THRESHOLD
        return 3600

    @staticmethod
    def _get_text_byte_length(text: str) -> int:
        return len(text.encode("utf-8"))

    def _get_result_max_chars(self, context: MessageContext) -> int:
        platform = (
            context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform
        )
        if platform == "discord":
            return 1900
        return 30000

    def _get_result_max_bytes(self, context: MessageContext) -> Optional[int]:
        platform = (
            context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform
        )
        if platform == "wechat":
            return _WECHAT_TEXT_LIMIT
        return None

    def _should_split_long_result(self, context: MessageContext) -> bool:
        return (
            context.platform or (context.platform_specific or {}).get("platform") or self.controller.config.platform
        ) in {"discord", "wechat"}

    def _result_within_limit(self, context: MessageContext, text: str) -> bool:
        max_bytes = self._get_result_max_bytes(context)
        if max_bytes is not None:
            return self._get_text_byte_length(text) <= max_bytes
        return len(text) <= self._get_result_max_chars(context)

    def _supports_quick_replies(self, context: MessageContext) -> bool:
        return self._capabilities(context).supports_quick_replies

    def _is_wechat_context(self, context: MessageContext) -> bool:
        return (
            context.platform
            or (context.platform_specific or {}).get("platform")
            or self.controller.config.platform
        ) == "wechat"

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
        """
        settings_manager = self.controller.get_settings_manager_for_context(context)
        im_client = self._get_im_client(context)

        canonical_type = settings_manager._canonicalize_message_type(message_type or "")
        settings_key = self._get_settings_key(context)

        # OUTBOUND status chokepoint (one of exactly two — the other is the
        # inbound AgentService.handle_message). A terminal ``result`` ends the
        # turn, so settle the avibe sidebar dot here regardless of delivery
        # outcome. Non-avibe contexts resolve to no session id and are skipped;
        # ``getattr`` keeps it a no-op for controllers without the hook (mirrors
        # ``_signal_turn_complete``).
        if canonical_type == "result":
            # Settle the avibe dot for the ACTIVE turn's terminal result (idle, or
            # failed on is_error) via the turn owner, which applies the active-turn
            # guard + skips non-avibe contexts. ``getattr`` keeps it a no-op for stub
            # controllers without the owner.
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
            if canonical_type == "result":
                # A terminal result — even silent/empty — still means the turn
                # finished: release the streaming SSE waiter so it closes now
                # instead of hanging until the safety timeout, with no visible chunk.
                await self._clear_consolidated_state(context)
                self._record_agent_run_terminal_result(
                    context,
                    text,
                    None,
                    is_error=is_error,
                )
                self._signal_turn_complete(context)
            return None

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
            message_id = f"suppressed:{(context.platform_specific or {}).get('task_execution_id') or canonical_type}"
            terminal_status = None
            if (
                canonical_type == "result"
                and (context.platform_specific or {}).get("task_trigger_kind") == "agent_run"
            ):
                terminal_status = "failed" if is_error else "succeeded"
            if canonical_type == "result" or (context.platform_specific or {}).get("task_trigger_kind") != "agent_run":
                self._record_suppressed_run_message(
                    context,
                    text,
                    message_id,
                    terminal_status=terminal_status,
                )
            if canonical_type == "result":
                await self._clear_consolidated_state(context)
                self._signal_turn_complete(context)
            return message_id

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
            primary_message_id: Optional[str] = None
            scheduled_anchor_message_id: Optional[str] = None
            delivered_as_attachment = False

            # ``enhanced`` (extracted file links + quick-reply buttons) was
            # computed above for persistence; reuse it for delivery.
            display_text = enhanced.text if enhanced.text.strip() else text

            if self._result_within_limit(context, display_text):
                try:
                    primary_message_id = await self._send_result_inline(
                        im_client,
                        target_context,
                        display_text,
                        enhanced.buttons if enhanced else [],
                        parse_mode,
                    )
                    scheduled_anchor_message_id = primary_message_id
                except Exception as err:
                    if enhanced and enhanced.buttons and self._supports_quick_replies(context):
                        logger.warning("Failed to send result with quick replies, falling back: %s", err)
                        try:
                            primary_message_id = await im_client.send_message(
                                target_context, display_text, parse_mode=parse_mode
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
                    )
                    scheduled_anchor_message_id = primary_message_id
                except Exception as err:
                    logger.error("Failed to send split result messages: %s", err)
            else:
                summary = self._build_result_summary(display_text, self._get_result_max_chars(context))
                try:
                    primary_message_id = await im_client.send_message(target_context, summary, parse_mode=parse_mode)
                    scheduled_anchor_message_id = primary_message_id
                except Exception as err:
                    logger.error("Failed to send result summary: %s", err)

                if (
                    context.platform
                    or (context.platform_specific or {}).get("platform")
                    or self.controller.config.platform
                ) in {"slack", "discord", "telegram", "lark"} and hasattr(im_client, "upload_markdown"):
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

            # Final result closes the current turn: clear consolidated
            # assistant/tool/system message state so the next user turn starts
            # a fresh log message instead of appending to the previous one.
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

        if canonical_type not in {"system", "assistant", "toolcall"}:
            canonical_type = "assistant"

        # Persist the intermediate log row BEFORE the mute filter so muted
        # assistant / tool_call messages still land in the store (product
        # requirement: the process log is complete even when a channel hides it).
        persist_agent_message(target_context, canonical_type, persist_text)

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

        consolidated_key = self._get_consolidated_message_key(context)
        lock = self._get_consolidated_message_lock(consolidated_key)

        async with lock:
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
    ) -> str:
        keyboard = None
        if buttons and self._supports_quick_replies(context):
            keyboard = self._build_quick_reply_keyboard(context, buttons)

        native_markdown_sender = getattr(im_client, "send_markdown_message", None)
        if parse_mode == "markdown" and callable(native_markdown_sender):
            return await native_markdown_sender(context, text, keyboard=keyboard)

        if keyboard is not None:
            return await im_client.send_message_with_buttons(
                context,
                text,
                keyboard,
                parse_mode=parse_mode,
            )

        return await im_client.send_message(context, text, parse_mode=parse_mode)

    async def _send_split_result_messages(
        self,
        im_client,
        context: MessageContext,
        text: str,
        buttons,
        parse_mode,
    ) -> Optional[str]:
        chunks = self._split_result_text_for_context(context, text)
        first_message_id: Optional[str] = None

        for index, chunk in enumerate(chunks):
            is_last_chunk = index == len(chunks) - 1
            message_id: Optional[str] = None

            if is_last_chunk and buttons and self._supports_quick_replies(context):
                try:
                    message_id = await self._send_result_inline(
                        im_client,
                        context,
                        chunk,
                        buttons,
                        parse_mode,
                    )
                except Exception as err:
                    logger.warning("Failed to send split result chunk with quick replies, falling back: %s", err)

            if message_id is None:
                message_id = await self._send_result_inline(im_client, context, chunk, [], parse_mode)

            if first_message_id is None:
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
