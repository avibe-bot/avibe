"""Translates Codex app-server notifications into vibe-remote agent messages."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibe.i18n import t as i18n_t

if TYPE_CHECKING:
    from modules.agents.base import AgentRequest

logger = logging.getLogger(__name__)

_GENERATED_IMAGE_EXTENSIONS = {".jpeg", ".jpg", ".png", ".webp"}
_SAFE_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ImageSnapshot = dict[Path, tuple[int, int]]


class CodexEventHandler:
    """Maps codex app-server server notifications to ``emit_agent_message`` calls.

    Maintains a *pending assistant message* buffer so that intermediate
    ``agent_message`` items are emitted immediately while the final one is held
    back until ``turn/completed`` and emitted as the result message.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self._image_snapshots_by_turn: dict[str, tuple[str, _ImageSnapshot]] = {}
        self._pending_image_snapshots_by_session: dict[str, tuple[str, _ImageSnapshot]] = {}

    def snapshot_generated_images(self, thread_id: str, base_session_id: str) -> None:
        """Record generated images present before a Codex turn starts."""
        if thread_id and base_session_id:
            self._pending_image_snapshots_by_session[base_session_id] = (
                thread_id,
                self._list_generated_images(thread_id),
            )

    def bind_generated_image_snapshot(
        self,
        thread_id: str,
        turn_id: str,
        base_session_id: str,
    ) -> None:
        """Bind a pre-turn image snapshot to the concrete Codex turn id."""
        if not thread_id or not turn_id or not base_session_id:
            return
        pending = self._pending_image_snapshots_by_session.get(base_session_id)
        if not pending:
            return
        is_active_turn = getattr(self._agent._turn_registry, "is_active_turn", None)
        if callable(is_active_turn) and not is_active_turn(turn_id):
            return
        pending_thread_id, _ = pending
        if pending_thread_id != thread_id:
            return
        self._image_snapshots_by_turn[turn_id] = pending
        self._pending_image_snapshots_by_session.pop(base_session_id, None)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def handle_notification(
        self,
        method: str,
        params: dict[str, Any],
        request: AgentRequest,
    ) -> None:
        handler = self._DISPATCH.get(method)
        if handler:
            await handler(self, params, request)
        else:
            logger.debug("Unhandled Codex notification: %s", method)

    def _release_stream_turn(self, context) -> None:
        """Release turn state for terminal paths that do not emit a result.

        Result emits settle both the web-Chat SSE stream and the shared backend
        runtime gate through the outbound dispatcher. Interrupted/stale backend
        notifications intentionally skip a visible result, so they need the same
        release here. Both release calls are token-guarded by their owners.
        """
        mark = getattr(self._agent.controller, "mark_turn_complete", None)
        if callable(mark):
            mark(context)
        service = getattr(self._agent.controller, "agent_service", None)
        release = getattr(service, "release_runtime_turn", None)
        if callable(release):
            release(context)

    # ------------------------------------------------------------------
    # Notification handlers
    # ------------------------------------------------------------------

    async def _on_thread_started(self, params: dict[str, Any], request: AgentRequest) -> None:
        thread_obj = params.get("thread", {})
        thread_id = thread_obj.get("id", "") if isinstance(thread_obj, dict) else ""
        is_fork_correction_pending = getattr(self._agent, "is_fork_correction_pending", None)
        if callable(is_fork_correction_pending) and is_fork_correction_pending(request.base_session_id):
            logger.debug(
                "Skipping Codex thread/started auto-bind for pending fork correction: session=%s thread=%s",
                request.base_session_id,
                thread_id,
            )
            return
        if thread_id:
            self._agent._session_mgr.set_thread_id(request.base_session_id, thread_id)
            self._agent.bind_agent_session_id(request, thread_id)

        return

    async def _on_turn_started(self, params: dict[str, Any], request: AgentRequest) -> None:
        turn_obj = params.get("turn", {})
        turn_id = turn_obj.get("id", "") if isinstance(turn_obj, dict) else ""
        self._claim_generated_image_snapshot(params, request)
        logger.info(
            "Codex turn started: thread=%s turn=%s",
            params.get("threadId"),
            turn_id,
        )

    async def _on_turn_completed(self, params: dict[str, Any], request: AgentRequest) -> None:
        turn_obj = params.get("turn", {})
        turn_id = turn_obj.get("id", "") if isinstance(turn_obj, dict) else ""
        status = turn_obj.get("status", "") if isinstance(turn_obj, dict) else ""
        turn_state = self._agent._turn_registry.get_turn(turn_id)
        tracked_request = turn_state.request if turn_state else request
        should_emit_result = self._agent._turn_registry.should_emit_result(turn_id)
        should_emit_terminal_error = self._agent._turn_registry.should_emit_terminal_error(turn_id)

        if status == "interrupted":
            if not turn_state:
                logger.debug("Ignoring interrupted completion for unknown turn %s", turn_id)
                return
            self._clear_generated_image_snapshot(params)
            self._agent._turn_registry.pop_turn(turn_id)
            await self._agent._remove_ack_reaction(tracked_request)
            # Turn ended without a result — release any web-Chat stream waiter
            # (token-guarded, so a superseded turn won't close a newer stream).
            self._release_stream_turn(tracked_request.context)
            return

        if status == "failed":
            if not turn_state:
                logger.info("Ignoring failed completion for unknown turn %s", turn_id)
                return
            error_msg = turn_state.terminal_error if turn_state else None
            already_notified = turn_state.terminal_error_notified if turn_state else False
            error_was_user_visible = already_notified
            if not error_msg:
                error_obj = turn_obj.get("error", {}) if isinstance(turn_obj, dict) else {}
                error_msg = self._extract_error_message(error_obj)

            if should_emit_terminal_error and not already_notified:
                message = f"❌ Codex turn failed: {error_msg}"
                handled = await self._agent.controller.agent_auth_service.maybe_emit_auth_recovery_message(
                    tracked_request.context,
                    "codex",
                    message,
                )
                if not handled:
                    # Terminal failure → RESULT (error): the outbound status
                    # chokepoint turns the dot red. The auth-recovery branch
                    # settles it via its own terminal error result.
                    await self._agent.controller.emit_agent_message(
                        tracked_request.context,
                        "result",
                        message,
                        is_error=True,
                    )
                # handled == True persists the durable recovery notify centrally in
                # ``maybe_emit_auth_recovery_message`` (the auth service is the single
                # home for the reset-prompt text); the not-handled branch persists via
                # ``emit_agent_message`` above.
                error_was_user_visible = True
            else:
                logger.info("Suppressing inactive Codex turn failure for %s: %s", turn_id, error_msg)

            self._clear_generated_image_snapshot(params)
            self._agent._turn_registry.pop_turn(turn_id)
            if error_was_user_visible:
                await self._agent._remove_ack_reaction(tracked_request)
            # Failed turn surfaced an error notify but no result — close the
            # web-Chat stream now instead of waiting out the safety timeout.
            self._release_stream_turn(tracked_request.context)
            return

        if not should_emit_result:
            if not turn_state:
                logger.debug("Ignoring completion for unknown turn %s", turn_id)
                return
            self._clear_generated_image_snapshot(params)
            self._agent._turn_registry.pop_turn(turn_id)
            logger.debug("Ignoring inactive turn/completed for turn %s", turn_id)
            self._release_stream_turn(tracked_request.context)
            return

        pending = turn_state.pending_assistant if turn_state else None
        generated_image_fallback = None
        if not pending or not (pending[0] or "").strip():
            generated_image_fallback = self._build_generated_image_fallback(params, tracked_request)
        else:
            self._clear_generated_image_snapshot(params)
        self._agent._turn_registry.pop_turn(turn_id)
        if pending and (pending[0] or "").strip():
            pending_text, pending_parse_mode = pending
            await self._agent.emit_result_message(
                tracked_request.context,
                pending_text,
                subtype="success",
                started_at=tracked_request.started_at,
                parse_mode=pending_parse_mode or "markdown",
                request=tracked_request,
            )
        else:
            await self._agent.emit_result_message(
                tracked_request.context,
                generated_image_fallback,
                subtype="success",
                started_at=tracked_request.started_at,
                parse_mode="markdown",
                request=tracked_request,
            )
        thread_id = self._extract_thread_id(params) or self._agent._session_mgr.get_thread_id(
            tracked_request.base_session_id
        )
        if thread_id:
            self._agent._maybe_backfill_session_title(tracked_request, thread_id)

    async def _on_item_completed(self, params: dict[str, Any], request: AgentRequest) -> None:
        item = params.get("item", {})
        item_type = item.get("type")
        turn_id = params.get("turnId", "")

        if turn_id and not self._agent._turn_registry.should_emit_progress(turn_id):
            logger.debug("Ignoring stale/interrupted item/%s for turn %s", item_type, turn_id)
            return

        turn_state = self._agent._turn_registry.get_turn(turn_id) if turn_id else None

        if item_type == "agentMessage":
            text = item.get("text", "")
            if text:
                # Emit previous pending message as assistant, buffer this one
                prev = turn_state.pending_assistant if turn_state else None
                if prev:
                    prev_text, prev_pm = prev
                    await self._agent.controller.emit_agent_message(
                        request.context,
                        "assistant",
                        prev_text,
                        parse_mode=prev_pm or "markdown",
                    )
                if turn_state:
                    turn_state.pending_assistant = (text, "markdown")

        elif item_type == "commandExecution":
            command = item.get("command", "")
            status = item.get("status", "")
            exit_code = item.get("exitCode")
            output = item.get("aggregatedOutput", "")
            if command:
                toolcall = self._agent._get_formatter(request.context).format_toolcall(
                    "bash",
                    {
                        "command": command,
                        "status": status,
                        "exit_code": exit_code,
                        "output": output[:500] if output else "",
                    },
                )
                await self._agent.controller.emit_agent_message(
                    request.context,
                    "toolcall",
                    toolcall,
                    parse_mode="markdown",
                )

        elif item_type == "fileChange":
            changes = item.get("changes", [])
            for change in changes:
                if not isinstance(change, dict):
                    continue
                file_path = change.get("path", "")
                change_kind = change.get("kind", "")
                if file_path:
                    toolcall = self._agent._get_formatter(request.context).format_toolcall(
                        "file_change",
                        {"file": file_path, "type": change_kind},
                    )
                    await self._agent.controller.emit_agent_message(
                        request.context,
                        "toolcall",
                        toolcall,
                        parse_mode="markdown",
                    )

        elif item_type == "reasoning":
            # Extract from summary array (list of strings) or content array
            parts: list[str] = []
            for s in item.get("summary", []):
                if isinstance(s, str):
                    parts.append(s)
            if not parts:
                for c in item.get("content", []):
                    if isinstance(c, str):
                        parts.append(c)
            text = "\n".join(parts)
            if text:
                await self._agent.controller.emit_agent_message(
                    request.context,
                    "assistant",
                    f"_🧠 {text}_",
                    parse_mode="markdown",
                )

    async def _on_error(self, params: dict[str, Any], request: AgentRequest) -> None:
        error = params.get("error", {})
        message = self._extract_error_message(error)
        will_retry = params.get("willRetry") is True
        turn_id = params.get("turnId", "")

        if will_retry:
            logger.info("Suppressing transient Codex error for turn %s: %s", turn_id or "<unknown>", message)
            return

        if turn_id:
            turn_state = self._agent._turn_registry.get_turn(turn_id)
            if not turn_state:
                logger.info("Ignoring Codex error for unknown turn %s: %s", turn_id, message)
                return

            turn_state.terminal_error = message
            if (
                self._agent._turn_registry.should_emit_terminal_error(turn_id)
                and not turn_state.terminal_error_notified
            ):
                text = f"❌ Codex turn failed: {message}"
                handled = await self._agent.controller.agent_auth_service.maybe_emit_auth_recovery_message(
                    request.context,
                    "codex",
                    text,
                )
                if not handled:
                    # Terminal failure → error RESULT so the outbound chokepoint
                    # turns the dot red (the later completed handler suppresses a
                    # second message once terminal_error_notified is set).
                    await self._agent.controller.emit_agent_message(
                        request.context,
                        "result",
                        text,
                        is_error=True,
                    )
                turn_state.terminal_error_notified = True
            else:
                logger.info("Logging inactive Codex turn error for %s: %s", turn_id, message)
            return

        text = f"❌ Codex error: {message}"
        handled = await self._agent.controller.agent_auth_service.maybe_emit_auth_recovery_message(
            request.context,
            "codex",
            text,
        )
        if not handled:
            # No-turnId terminal error → error RESULT so the dot turns red.
            await self._agent.controller.emit_agent_message(
                request.context,
                "result",
                text,
                is_error=True,
            )

    def _extract_error_message(self, error: Any) -> str:
        if isinstance(error, dict):
            return error.get("message", "Unknown error")
        return str(error)

    def _build_generated_image_fallback(
        self,
        params: dict[str, Any],
        request: AgentRequest,
    ) -> str | None:
        self._claim_generated_image_snapshot(params, request)
        turn_id = self._extract_turn_id(params)
        if not turn_id:
            return None

        snapshot = self._image_snapshots_by_turn.pop(turn_id, None)
        if snapshot is None:
            return None
        thread_id, before = snapshot

        current = self._list_generated_images(thread_id)
        generated = sorted(
            (path for path, signature in current.items() if before.get(path) != signature),
            key=lambda path: current[path][0],
        )
        if not generated:
            return None

        heading = self._t("info.generatedImagesFallback", request)
        links = "\n".join(f"![generated image]({path.as_uri()})" for path in generated)
        return f"{heading}\n\n{links}"

    def _clear_generated_image_snapshot(self, params: dict[str, Any]) -> None:
        turn_id = self._extract_turn_id(params)
        if turn_id:
            self._image_snapshots_by_turn.pop(turn_id, None)

    def _claim_generated_image_snapshot(self, params: dict[str, Any], request: AgentRequest) -> None:
        self.bind_generated_image_snapshot(
            self._extract_thread_id(params),
            self._extract_turn_id(params),
            request.base_session_id,
        )

    def _list_generated_images(self, thread_id: str) -> _ImageSnapshot:
        thread_dir = self._generated_images_dir(thread_id)
        if thread_dir is None or not thread_dir.is_dir():
            return {}

        try:
            candidates = thread_dir.iterdir()
        except OSError as exc:
            logger.warning("Failed to list Codex generated images for %s: %s", thread_id, exc)
            return {}

        images: _ImageSnapshot = {}
        for path in candidates:
            if path.is_file() and path.suffix.lower() in _GENERATED_IMAGE_EXTENSIONS:
                resolved = path.resolve()
                try:
                    stat = resolved.stat()
                except OSError as exc:
                    logger.warning("Failed to stat Codex generated image %s: %s", resolved, exc)
                    continue
                images[resolved] = (stat.st_mtime_ns, stat.st_size)
        return images

    def _generated_images_dir(self, thread_id: str) -> Path | None:
        if not _SAFE_THREAD_ID_RE.fullmatch(thread_id):
            logger.warning("Ignoring unsafe Codex thread id for generated images: %s", thread_id)
            return None
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return codex_home / "generated_images" / thread_id

    def _extract_thread_id(self, params: dict[str, Any]) -> str:
        thread_id = params.get("threadId", "")
        if not thread_id:
            thread_obj = params.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        return thread_id

    def _extract_turn_id(self, params: dict[str, Any]) -> str:
        turn_id = params.get("turnId", "")
        if not turn_id:
            turn_obj = params.get("turn")
            if isinstance(turn_obj, dict):
                turn_id = turn_obj.get("id", "")
        return turn_id

    def _t(self, key: str, request: AgentRequest) -> str:
        controller = getattr(self._agent, "controller", None)
        translate = getattr(controller, "_t", None)
        if callable(translate):
            return translate(key)
        config = getattr(controller, "config", None)
        lang = getattr(config, "language", "en")
        return i18n_t(key, lang)

    async def _on_agent_message_delta(self, params: dict[str, Any], request: AgentRequest) -> None:
        # Streaming delta — currently we accumulate at item/completed level,
        # but we could implement progressive Slack message updates here.
        pass

    async def _on_command_output_delta(self, params: dict[str, Any], request: AgentRequest) -> None:
        # Streaming command output — could implement live output display.
        pass

    async def _on_reasoning_delta(self, params: dict[str, Any], request: AgentRequest) -> None:
        # Streaming reasoning — currently handled at item/completed level.
        pass

    async def _on_context_compacted(self, params: dict[str, Any], request: AgentRequest) -> None:
        return

    @staticmethod
    def _extract_context_tokens(params: dict[str, Any]) -> int:
        """Current context-window occupancy from a ``thread/tokenUsage/updated``
        notification.

        The app-server v2 payload nests it as ``tokenUsage.last.totalTokens`` —
        the "latest active context size" the Codex CLI's own context bar uses (it
        grows with the conversation and DROPS after a /compact). This is the SNAPSHOT
        ``last`` breakdown, NOT ``total`` (which is the monotonic cumulative billing
        figure). The legacy v1 event stream nests the same value as
        ``info.last_token_usage.total_tokens`` (snake_case); both are tried.

        Defensive: missing/malformed → 0 so a protocol change never breaks the turn."""
        if not isinstance(params, dict):
            return 0
        # v2 app-server (camelCase) → v1 event stream (snake_case under "info").
        last = None
        usage = params.get("tokenUsage")
        if isinstance(usage, dict) and isinstance(usage.get("last"), dict):
            last = usage["last"]
        else:
            info = params.get("info")
            if isinstance(info, dict) and isinstance(info.get("last_token_usage"), dict):
                last = info["last_token_usage"]
        if not isinstance(last, dict):
            return 0
        for name in ("totalTokens", "total_tokens"):
            value = last.get(name)
            try:
                if value and int(value) > 0:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    async def _on_token_usage_updated(self, params: dict[str, Any], request: AgentRequest) -> None:
        """``thread/tokenUsage/updated`` → set the status footer's session token
        figure to the current context-window occupancy. SET (not add): the value is
        a live snapshot of context size, not a cumulative total."""
        tokens = self._extract_context_tokens(params)
        if not tokens:
            return
        turn_id = params.get("turnId") or params.get("turn_id") or ""
        turn_state = self._agent._turn_registry.get_turn(turn_id) if turn_id else None
        ctx = (turn_state.request if turn_state else request).context
        self._agent.controller.note_session_tokens(ctx, total=tokens)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def clear_pending(self, turn_id: str) -> AgentRequest | None:
        """Hide a turn from user-facing output after interruption/replacement."""
        turn_state = self._agent._turn_registry.hide_turn(turn_id)
        return turn_state.request if turn_state else None

    # ------------------------------------------------------------------
    # Dispatch table
    # ------------------------------------------------------------------

    _DISPATCH: dict[str, Any] = {
        "thread/started": _on_thread_started,
        "turn/started": _on_turn_started,
        "turn/completed": _on_turn_completed,
        "thread/tokenUsage/updated": _on_token_usage_updated,
        "item/completed": _on_item_completed,
        "error": _on_error,
        "item/agentMessage/delta": _on_agent_message_delta,
        "item/commandExecution/outputDelta": _on_command_output_delta,
        "item/reasoning/summaryTextDelta": _on_reasoning_delta,
        "thread/compacted": _on_context_compacted,
    }
