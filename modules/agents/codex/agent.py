"""Codex agent — persistent app-server mode with JSON-RPC 2.0 transport."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from config.v2_config import (
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS,
    DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER,
)
from core.avibe_cloud import avibe_cloud_url_available
from core.services.session_fork import fork_source_state, pending_native_fork
from core.system_prompt_injection import (
    build_forked_session_correction_prompt,
    build_system_prompt_injection,
    get_enabled_agents_for_prompt,
)
from modules.agents.base import AgentRequest, BaseAgent
from modules.agents.subagent_router import SubagentDefinition, load_codex_subagent
from modules.agents.codex.event_handler import CodexEventHandler
from modules.agents.codex.session import CodexSessionManager
from modules.agents.codex.transport import CodexTransport
from modules.agents.codex.turn_state import CodexTurnRegistry
from vibe.codex_config import LEGACY_MANAGED_PROVIDER_IDS, MANAGED_PROVIDER_ID

logger = logging.getLogger(__name__)

_CODEX_MANAGED_PROVIDER_IDS = frozenset((MANAGED_PROVIDER_ID, *LEGACY_MANAGED_PROVIDER_IDS))


class CodexResumeUnavailableError(RuntimeError):
    """The Codex thread associated with this session can no longer be resumed.

    Raised instead of silently starting a fresh thread, so the user is told their
    conversation context is gone rather than landing in an empty thread without
    knowing (product decision: no silent fallbacks)."""

    def __init__(self, thread_id: str, detail: str = "") -> None:
        self.thread_id = thread_id
        msg = (
            f"Could not resume the previous Codex conversation ({thread_id}); it may have expired. "
            "Not starting a new conversation to avoid silently losing context — start a new session to continue."
        )
        super().__init__(f"{msg} ({detail})" if detail else msg)


class CodexAgent(BaseAgent):
    """Codex CLI integration via persistent ``codex app-server`` subprocess.

    One transport (subprocess) is maintained per unique working directory.
    Multiple Slack threads in the same channel share a transport but each
    gets its own Codex thread.
    """

    name = "codex"

    def __init__(self, controller: Any, codex_config: Any) -> None:
        super().__init__(controller)
        self.codex_config = codex_config

        # cwd → CodexTransport (one persistent process per working dir)
        self._transports: Dict[str, CodexTransport] = {}
        self._transport_locks: Dict[str, asyncio.Lock] = {}
        self._transport_last_activity: Dict[str, float] = {}
        # cwd inode at app-server spawn time, keyed like ``_transports``. A
        # cached app-server whose directory was deleted (even if re-created
        # with the same path) sits in a dead inode and fails every
        # ``thread/start`` with a misleading "failed to load configuration:
        # No such file or directory" (#561); the inode comparison detects
        # that staleness BEFORE paying a failed RPC.
        self._transport_cwd_inodes: Dict[str, Optional[int]] = {}

        self._session_mgr = CodexSessionManager()
        self._turn_registry = CodexTurnRegistry()
        self._event_handler = CodexEventHandler(self)

        # base_session_id → asyncio.Lock (serialize turn lifecycle per session)
        self._session_locks: Dict[str, asyncio.Lock] = {}
        # base_session_id → (thread_id, developer_instructions)
        self._thread_developer_instructions: Dict[str, tuple[str, str]] = {}
        self._fork_correction_pending_base_sessions: set[str] = set()

    # ------------------------------------------------------------------
    # BaseAgent interface
    # ------------------------------------------------------------------

    async def handle_message(self, request: AgentRequest) -> None:
        """Process a user message by routing it through app-server.

        Flow:
        1. Get or create transport for the working directory
        2. Get or create a Codex thread for this Slack thread
        3. If a turn is active → interrupt it first
        4. Start a new turn with the user's message
        """
        try:
            transport = await self._get_or_create_transport(request.working_path)
        except FileNotFoundError:
            # Terminal failure → emit as a RESULT (error): the outbound chokepoint
            # turns the dot red and releases the SSE waiter (no separate latch).
            await self.controller.emit_agent_message(
                request.context,
                "result",
                "❌ Codex CLI not found. Please install it or set CODEX_CLI_PATH.",
                is_error=True,
            )
            await self._remove_ack_reaction(request)
            self._event_handler._release_stream_turn(request.context)
            return
        except Exception as e:
            logger.error("Failed to start Codex transport: %s", e, exc_info=True)
            await self.controller.emit_agent_message(
                request.context,
                "result",
                f"❌ Failed to start Codex CLI: {e}",
                is_error=True,
            )
            await self._remove_ack_reaction(request)
            self._event_handler._release_stream_turn(request.context)
            return

        # Track session_key and cwd for scoped invalidation
        self._session_mgr.set_session_key(request.base_session_id, request.session_key)
        self._session_mgr.set_cwd(request.base_session_id, request.working_path)
        self._touch_transport_activity(request.working_path)

        await self._delete_ack(request)

        # Serialize turn lifecycle per session
        if request.base_session_id not in self._session_locks:
            self._session_locks[request.base_session_id] = asyncio.Lock()

        async with self._session_locks[request.base_session_id]:
            self._turn_registry.remember_request(request)
            try:
                # Get or create thread (with resume support)
                thread_id = self._session_mgr.get_thread_id(request.base_session_id)

                if not thread_id:
                    thread_id = await self._start_or_resume_thread(transport, request)

                # If a turn is active, interrupt it first
                active_turn = self._turn_registry.get_active_turn(request.base_session_id)
                if active_turn:
                    try:
                        await transport.send_request(
                            "turn/interrupt",
                            {"threadId": thread_id, "turnId": active_turn},
                        )
                    except Exception as e:
                        if self._is_recoverable_transport_error(e):
                            raise
                        logger.warning("Failed to interrupt turn %s: %s", active_turn, e)
                        await self.controller.emit_agent_message(
                            request.context,
                            "result",
                            f"❌ Failed to interrupt previous Codex turn: {e}",
                            is_error=True,
                        )
                        await self._remove_ack_reaction(request)
                        self._event_handler._release_stream_turn(request.context)
                        return
                    interrupted_request = self._event_handler.clear_pending(active_turn)
                    if interrupted_request:
                        await self._remove_ack_reaction(interrupted_request)

                await self._refresh_thread_developer_instructions_if_needed(transport, request, thread_id)
                thread_id = await self._start_turn(transport, request, thread_id)

            except Exception as e:
                # Safety net: if the thread is stale (e.g. Codex server-side
                # expiry, or the proactive invalidation in _get_or_create_transport
                # was bypassed by a race), invalidate and retry once.
                if self._is_recoverable_transport_error(e):
                    logger.warning(
                        "Recoverable Codex transport failure for session %s, restarting transport and retrying: %s",
                        request.base_session_id,
                        e,
                    )
                    await self._drop_transport_after_failure(request.working_path, transport, request)
                    try:
                        transport = await self._get_or_create_transport(request.working_path)
                        self._touch_transport_activity(request.working_path)
                        thread_id = await self._start_or_resume_thread(transport, request)
                        await self._start_turn(transport, request, thread_id)
                        return  # retry succeeded
                    except Exception as retry_err:
                        e = retry_err  # fall through to normal error handling

                # FAIL LOUD on a server-side "thread not found": the conversation is
                # gone, so surface the error instead of silently clearing the
                # mapping and forking a fresh thread (which hid the context loss).
                # The mapping is kept so the failure is consistent until the user
                # explicitly starts a new session (product decision: no silent
                # fallbacks).
                self._turn_registry.clear_pending_turn_start(request.base_session_id, request)
                logger.error("Error in Codex handle_message: %s", e, exc_info=True)
                error_text = f"❌ Codex error: {e}"
                handled = await self.controller.agent_auth_service.maybe_emit_auth_recovery_message(
                    request.context,
                    "codex",
                    error_text,
                )
                if not handled:
                    # Terminal failure → RESULT (error): the outbound chokepoint
                    # turns the dot red. The auth-recovery branch (handled) emits
                    # its own terminal error result inside
                    # ``maybe_emit_auth_recovery_message``, so both paths settle
                    # the dot via the same outbound — no separate latch.
                    await self.controller.emit_agent_message(
                        request.context,
                        "result",
                        error_text,
                        is_error=True,
                    )
                await self._remove_ack_reaction(request)
                # The turn never started (all retries failed) — release the
                # web-Chat working/Stop state instead of leaving it until the
                # fallback timeout (Codex P2).
                self._event_handler._release_stream_turn(request.context)

    async def handle_stop(self, request: AgentRequest) -> bool:
        """Gracefully interrupt the active turn."""
        thread_id = self._session_mgr.get_thread_id(request.base_session_id)
        turn_id = self._turn_registry.get_active_turn(request.base_session_id)

        if not thread_id or not turn_id:
            request.stop_failure_reason = "not_active"
            return False

        transport = self._transports.get(request.working_path)
        if not transport or not transport.is_alive:
            request.stop_failure_reason = "runtime_unavailable"
            return False

        try:
            await transport.send_request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
            )
            interrupted_request = self._event_handler.clear_pending(turn_id)
            if interrupted_request:
                await self._remove_ack_reaction(interrupted_request)
            # A user-initiated stop is terminal but intentional, so it carries NO
            # user-facing message: a single SILENT result settles the dot to idle +
            # releases the SSE waiter through the outbound chokepoint without a
            # bubble. The user already knows they stopped it (avibe shows the dot go
            # idle; IM shows the ack reaction removed above). ``level="silent"`` is
            # the explicit visibility grade rather than faking it via empty text.
            await self.controller.emit_agent_message(
                request.context, "result", "", level="silent"
            )
            logger.info("Codex turn %s interrupted via /stop", turn_id)
            return True
        except Exception as e:
            request.stop_failure_reason = "interrupt_failed"
            logger.error("Failed to interrupt Codex turn: %s", e)
            return False

    async def clear_sessions(self, session_key: str) -> int:
        """Clear sessions scoped to a specific session_key."""
        self.sessions.clear_agent_sessions(session_key, self.name)

        # Use session_key index (not _threads) so sessions with
        # invalidated threads are still cleaned up properly.
        to_clear = self._session_mgr.get_sessions_by_session_key(session_key)

        count = self._session_mgr.clear_by_session_key(session_key)

        # Clean up in-memory turn state and session locks for cleared sessions
        for bid in to_clear:
            self._turn_registry.clear_session(bid)
            self._session_locks.pop(bid, None)
            self._clear_thread_developer_instructions(bid)

        return count

    def runtime_turn_keys(self) -> set[str]:
        return {
            self._runtime_turn_key_for_base_session(base_session_id)
            for base_session_id in self._session_mgr.all_base_sessions()
        }

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        return {
            self._runtime_turn_key_for_base_session(base_session_id)
            for base_session_id in self._session_mgr.get_sessions_by_session_key(session_key)
        }

    def _runtime_turn_key_for_base_session(self, base_session_id: str) -> str:
        cwd = self._session_mgr.get_cwd(base_session_id)
        return f"{base_session_id}:{cwd}" if cwd else base_session_id

    async def refresh_auth_state(self) -> None:
        """Drop app-server runtime state so future turns pick up fresh auth."""
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        base_session_ids = list(self._session_mgr.all_base_sessions())
        controller = getattr(self, "controller", None)
        turn_manager = getattr(controller, "session_turns", None)
        release_for_backend_refresh = getattr(turn_manager, "release_for_backend_refresh", None)
        if callable(release_for_backend_refresh):
            try:
                await release_for_backend_refresh(
                    backend=self.name,
                    base_session_ids=set(base_session_ids),
                )
            except Exception:
                logger.warning("Failed to release Workbench turns during Codex refresh", exc_info=True)
        transports = list(self._transports.values())
        self._transports.clear()
        self._transport_last_activity.clear()

        for transport in transports:
            try:
                await transport.stop()
            except Exception as exc:
                logger.warning("Failed to stop Codex transport during auth refresh: %s", exc)

        for base_session_id in base_session_ids:
            self._session_mgr.invalidate_thread(base_session_id)
            self._turn_registry.clear_session(base_session_id)
            self._clear_thread_developer_instructions(base_session_id)

        logger.info("Refreshed Codex auth state across %d transport(s)", len(transports))

    async def refresh_runtime_config(self, codex_config: Any) -> None:
        """Reload persisted runtime config before respawning app-server transports."""
        self.codex_config = codex_config
        self.controller.config.codex = codex_config
        await self.refresh_auth_state()

    async def prepare_resume_binding(
        self,
        *,
        base_session_id: str,
        session_key: str,
        working_path: str,
    ) -> None:
        """Restart a Codex transport only when the resumed session owns that cwd."""
        transport = self._transports.get(working_path)
        if transport is None:
            return

        affected_sessions = self._session_mgr.sessions_for_cwd(working_path)
        other_sessions = [session_id for session_id in affected_sessions if session_id != base_session_id]
        if other_sessions:
            logger.info(
                "Skipping Codex resume preparation for %s; cwd=%s is shared by %d other session(s)",
                base_session_id,
                working_path,
                len(other_sessions),
            )
            return

        try:
            await transport.stop()
        except Exception as exc:
            logger.warning("Failed to stop Codex transport during resume preparation: %s", exc)
            return

        self._transports.pop(working_path, None)
        self._transport_last_activity.pop(working_path, None)
        self._session_mgr.invalidate_thread(base_session_id)
        self._turn_registry.clear_session(base_session_id)
        self._clear_thread_developer_instructions(base_session_id)
        logger.info("Prepared Codex runtime for resumed session %s", base_session_id)

    async def shutdown_runtime(self) -> None:
        """Stop all app-server transports during vibe-remote shutdown."""
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        if not hasattr(self, "_session_locks"):
            self._session_locks = {}
        transports = list(self._transports.values())
        self._transports.clear()
        self._transport_last_activity.clear()
        self._transport_locks.clear()

        for transport in transports:
            try:
                await transport.stop()
            except Exception as exc:
                logger.warning("Failed to stop Codex transport during shutdown: %s", exc)

        for base_session_id in list(self._session_mgr.all_base_sessions()):
            session_key = self._session_mgr.get_session_key(base_session_id)
            if session_key:
                self.sessions.clear_agent_session_mapping(session_key, self.name, base_session_id)
            self._session_mgr.clear(base_session_id)
            self._turn_registry.clear_session(base_session_id)
            self._clear_thread_developer_instructions(base_session_id)

        self._session_locks.clear()
        logger.info("Stopped Codex runtime across %d transport(s)", len(transports))

    async def evict_idle_transports(self, idle_timeout: float) -> int:
        """Stop idle Codex transports and invalidate stale thread mappings."""
        if idle_timeout <= 0:
            return 0
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if not hasattr(self, "_transport_locks"):
            self._transport_locks = {}
        if not hasattr(self, "_session_locks"):
            self._session_locks = {}

        # Absolute-time backstop: a transport whose turn is stuck "active"
        # forever (turn/completed never arrives — wedged/silently-disconnected
        # app-server) would otherwise be vetoed from eviction indefinitely and
        # leak its app-server process until restart (#622/#623 analog). Once it
        # has been idle past this cap, force-evict it despite the active turn.
        stuck_active_cap = self._stuck_active_idle_eviction_cap(idle_timeout)

        now = time.monotonic()
        evicted = 0

        for cwd, last_activity in list(self._transport_last_activity.items()):
            transport = self._transports.get(cwd)
            if transport is None:
                self._transport_last_activity.pop(cwd, None)
                continue
            idle_for = now - last_activity
            has_active = self._has_active_turns_for_cwd(cwd)
            if not self._is_transport_evictable(
                has_active=has_active,
                idle_for=idle_for,
                idle_timeout=idle_timeout,
                stuck_active_cap=stuck_active_cap,
            ):
                continue

            lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
            async with lock:
                current_transport = self._transports.get(cwd)
                current_last_activity = self._transport_last_activity.get(cwd)
                if current_transport is None or current_transport is not transport:
                    continue
                if current_last_activity is None:
                    continue
                # Recheck from CURRENT state inside the lock: activity (and the
                # active-turn flag) may have changed between the two passes.
                idle_for = time.monotonic() - current_last_activity
                has_active = self._has_active_turns_for_cwd(cwd)
                if not self._is_transport_evictable(
                    has_active=has_active,
                    idle_for=idle_for,
                    idle_timeout=idle_timeout,
                    stuck_active_cap=stuck_active_cap,
                ):
                    continue

                if has_active:
                    logger.warning(
                        "Force-evicting stuck-active Codex transport for cwd=%s after %.1fs idle "
                        "(active turn exceeded stuck-active cap of %.1fs; app-server presumed wedged)",
                        cwd,
                        idle_for,
                        stuck_active_cap,
                    )
                else:
                    logger.info("Evicting idle Codex transport for cwd=%s after %.1fs idle", cwd, idle_for)
                try:
                    await transport.stop()
                except Exception as exc:
                    logger.warning("Failed to stop idle Codex transport for cwd=%s: %s", cwd, exc)
                    continue

                self._transports.pop(cwd, None)
                self._transport_last_activity.pop(cwd, None)
                self._cwd_inodes().pop(cwd, None)

                for base_session_id in list(self._session_mgr.sessions_for_cwd(cwd)):
                    # A force-evicted stuck-active turn never emitted a terminal
                    # result, so the Workbench status and SSE/runtime gate are
                    # still owned by that turn. Settle it through the same
                    # terminal-result path as normal completions before dropping
                    # turn state. No-op for sessions with no active turn.
                    await self._settle_stuck_active_request(base_session_id)
                    # Keep the persisted thread mapping so a later transport restart
                    # can resume the same Codex conversation for this Slack thread.
                    self._session_mgr.invalidate_thread(base_session_id)
                    self._turn_registry.clear_session(base_session_id)
                    self._session_locks.pop(base_session_id, None)
                    self._clear_thread_developer_instructions(base_session_id)

                evicted += 1

        return evicted

    async def _settle_stuck_active_request(self, base_session_id: str) -> None:
        """Settle a turn we are about to force-reap.

        ``_start_turn`` marks the AgentService runtime turn started; it is
        normally settled by a terminal result, which also flips Workbench
        ``agent_status`` out of ``running``. The stuck-active force-eviction path
        has no backend terminal event, so emit a silent error result here. The
        terminal-result path is token-guarded by its owner, so a no-op
        (already-settled or no active turn) is safe.
        """
        get_active = getattr(self._turn_registry, "get_active_turn", None)
        active_turn = get_active(base_session_id) if callable(get_active) else None
        if not active_turn:
            return

        request = None
        get_for_turn = getattr(self._turn_registry, "get_request_for_turn", None)
        if callable(get_for_turn):
            request = get_for_turn(active_turn)
        if request is None:
            get_latest = getattr(self._turn_registry, "get_latest_request", None)
            if callable(get_latest):
                request = get_latest(base_session_id)

        context = getattr(request, "context", None)
        if context is None:
            return
        controller = getattr(self, "controller", None)
        emit = getattr(controller, "emit_agent_message", None)
        if callable(emit):
            try:
                await emit(context, "result", "", is_error=True, level="silent")
                return
            except Exception:
                logger.warning(
                    "Failed to emit silent terminal result for force-evicted Codex turn %s",
                    active_turn,
                    exc_info=True,
                )

        # Best-effort fallback for narrow test doubles or partial controllers:
        # release the runtime gate even if the Workbench status path is absent.
        release = getattr(self._event_handler, "_release_stream_turn", None)
        if callable(release):
            release(context)

    def _stuck_active_idle_eviction_cap(self, idle_timeout: float) -> Optional[float]:
        """Idle cap after which an *active* transport is force-evicted.

        Returns ``None`` when the backstop is disabled (multiplier <= 0), in
        which case an active turn remains an absolute veto. Otherwise a
        transport with an active turn is force-evicted once it has been idle for
        ``max(idle_timeout * multiplier, floor)`` — the floor keeps the window
        sane even when ``idle_timeout`` is configured very small.
        """
        multiplier = DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_MULTIPLIER
        if multiplier <= 0:
            return None
        floor = max(0.0, float(DEFAULT_CODEX_STUCK_ACTIVE_IDLE_EVICTION_FLOOR_SECONDS))
        return max(idle_timeout * multiplier, floor)

    def _is_transport_evictable(
        self,
        *,
        has_active: bool,
        idle_for: float,
        idle_timeout: float,
        stuck_active_cap: Optional[float],
    ) -> bool:
        """Decide whether an idle transport is eligible for eviction.

        Pure decision (no lookups), so callers evaluate the active-turn flag
        exactly once. An idle transport with no active turn is evictable once it
        crosses the normal ``idle_timeout``. A transport with an active turn is
        normally vetoed, but is force-evictable once it crosses
        ``stuck_active_cap`` (the absolute-time backstop) — the only path that
        reaps a wedged app-server whose ``turn/completed`` never arrived.
        """
        if has_active:
            if stuck_active_cap is None:
                return False
            return idle_for >= stuck_active_cap
        return idle_for >= idle_timeout

    # ------------------------------------------------------------------
    # Transport management
    # ------------------------------------------------------------------

    def _is_recoverable_transport_error(self, error: Exception) -> bool:
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True

        text = str(error).lower()
        return any(
            marker in text
            for marker in (
                "transport is not available",
                "stdout closed",
                "timed out after 120s",
                # codex resolves configuration against its process cwd at
                # thread/start; a cwd deleted out from under the app-server
                # surfaces as this RPC error (#561). A restart respawns the
                # process in the (re-created) directory.
                "failed to load configuration",
            )
        )

    async def _drop_transport_after_failure(
        self,
        cwd: str,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> None:
        """Remove a broken app-server and clear stale in-memory request state."""
        lock = self._transport_locks.setdefault(cwd, asyncio.Lock())
        async with lock:
            current = self._transports.get(cwd)
            should_invalidate_cwd_sessions = current is None or current is transport
            if current is transport:
                self._transports.pop(cwd, None)
                self._transport_last_activity.pop(cwd, None)
                self._cwd_inodes().pop(cwd, None)
            try:
                await transport.stop()
            except Exception as exc:
                logger.warning("Failed to stop broken Codex transport for cwd=%s: %s", cwd, exc)

            if should_invalidate_cwd_sessions:
                for base_session_id in list(self._session_mgr.sessions_for_cwd(cwd)):
                    if base_session_id == request.base_session_id:
                        continue
                    self._session_mgr.invalidate_thread(base_session_id)
                    self._clear_thread_developer_instructions(base_session_id)
                    self._turn_registry.clear_session(base_session_id)

        self._session_mgr.invalidate_thread(request.base_session_id)
        self._clear_thread_developer_instructions(request.base_session_id)
        self._turn_registry.clear_session(request.base_session_id)

    async def _get_or_create_transport(self, cwd: str) -> CodexTransport:
        """Return an initialized transport for the given working directory."""
        # Serialize creation per cwd
        if cwd not in self._transport_locks:
            self._transport_locks[cwd] = asyncio.Lock()

        async with self._transport_locks[cwd]:
            # Double-check after acquiring lock
            existing = self._transports.get(cwd)
            if existing and existing.is_initialized:
                # Reuse only while the directory the app-server was spawned in
                # is still the SAME directory (#561): after a delete (+ possible
                # re-create) the cached process sits in a dead inode and every
                # thread/start fails. Untracked legacy entries reuse as before.
                spawned_ino = self._cwd_inodes().get(cwd)
                stale_cwd = spawned_ino is not None and self._cwd_inode(cwd) != spawned_ino
                if not stale_cwd:
                    self._touch_transport_activity(cwd)
                    return existing
                logger.warning(
                    "Codex transport cwd was replaced under the cached app-server; restarting transport for cwd=%s",
                    cwd,
                )

            # Stop stale transport if any
            if existing:
                await existing.stop()
                # The new app-server process won't know about threads/turns
                # from the old process.  Invalidate only sessions bound to
                # this cwd so healthy sessions on other cwds are unaffected.
                affected = self._session_mgr.sessions_for_cwd(cwd)
                for bid in affected:
                    self._session_mgr.invalidate_thread(bid)
                    self._clear_thread_developer_instructions(bid)
                    self._turn_registry.clear_session(bid)
                if affected:
                    logger.info(
                        "Invalidated %d stale Codex session(s) after transport restart for cwd=%s",
                        len(affected),
                        cwd,
                    )

            transport = CodexTransport(
                binary=self.codex_config.binary,
                cwd=cwd,
                extra_args=list(self.codex_config.extra_args),
            )

            # Wire up callbacks
            transport.on_notification(self._on_notification)
            # Bind the cwd so any server request (e.g. an auto-approval) refreshes
            # this transport's activity: a server request IS app-server liveness,
            # and unlike notifications it isn't always tied to a resolvable
            # turn/thread in params. Without this a turn that thinks silently and
            # then asks for approval near the stuck-active cap could be wrongly
            # force-evicted by the next sweep.
            transport.on_server_request(
                lambda req_id, method, params, _cwd=cwd: self._on_server_request(_cwd, req_id, method, params)
            )

            await transport.start()
            self._transports[cwd] = transport
            self._cwd_inodes()[cwd] = self._cwd_inode(cwd)
            self._touch_transport_activity(cwd)
            return transport

    # ------------------------------------------------------------------
    # Thread management
    # ------------------------------------------------------------------

    async def _start_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> str:
        """Create a new Codex thread and return its threadId."""
        params: Dict[str, Any] = {
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        self.ensure_agent_session_id(request)
        developer_instructions = self._build_thread_developer_instructions(request)
        if developer_instructions:
            params["developerInstructions"] = developer_instructions

        resp = await transport.send_request("thread/start", params)
        # thread/start returns Thread directly OR may nest under "thread"
        thread_id = resp.get("id", "")
        if not thread_id:
            thread_obj = resp.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        if not thread_id:
            raise RuntimeError("Codex thread/start returned no thread id")

        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        # Also persist for resume support
        self.bind_agent_session_id(request, thread_id)
        self._remember_thread_developer_instructions(request.base_session_id, thread_id, developer_instructions)
        return thread_id

    async def _fork_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        fork: dict[str, Any],
    ) -> str:
        """Fork an existing Codex thread and bind the new thread id."""
        self.ensure_agent_session_id(request)
        _, effective_model, _, _ = self._resolve_codex_agent_settings(request)
        source_thread_id = str(fork.get("source_native_session_id") or "").strip()
        params: Dict[str, Any] = {
            "threadId": source_thread_id,
            "cwd": request.working_path,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        developer_instructions = self._build_thread_developer_instructions(request)
        if developer_instructions:
            params["developerInstructions"] = developer_instructions
        if effective_model:
            params["model"] = effective_model

        self._mark_fork_correction_pending(request.base_session_id)
        try:
            should_trim = await self._should_rollback_forked_running_turn(fork)
            resp = await transport.send_request("thread/fork", params)
            thread_id = resp.get("id", "")
            if not thread_id:
                thread_obj = resp.get("thread")
                if isinstance(thread_obj, dict):
                    thread_id = thread_obj.get("id", "")
            if not thread_id:
                raise RuntimeError("Codex thread/fork returned no thread id")

            if should_trim:
                await self._rollback_forked_running_turn(transport, thread_id)
            await self._inject_forked_session_correction(transport, request, thread_id)
        finally:
            self._clear_fork_correction_pending(request.base_session_id)
        self._session_mgr.set_thread_id(request.base_session_id, thread_id)
        self.bind_agent_session_id(request, thread_id)
        self._remember_thread_developer_instructions(request.base_session_id, thread_id, developer_instructions)
        logger.info("Forked Codex thread %s from %s for session %s", thread_id, source_thread_id, request.base_session_id)
        return thread_id

    async def _should_rollback_forked_running_turn(self, fork: dict[str, Any]) -> bool:
        """Rollback only when Codex's latest-turn rollback still targets the reserved turn."""

        if not bool(fork.get("trim_latest_running_turn")):
            return False
        source_state = fork_source_state(fork)
        if source_state.anchor_is_terminal_agent_output:
            return False
        anchor_is_running_user = (
            getattr(source_state, "anchor_author", None) == "user"
            and getattr(source_state, "anchor_type", None) == "user"
        )
        if getattr(source_state, "has_user_turn_after_anchor", False):
            return False
        if anchor_is_running_user:
            if source_state.has_messages_after_anchor:
                return True
            if bool(fork.get("native_turn_started")):
                return True
            return await self._fork_source_turn_now_started(fork)
        if source_state.has_messages_after_anchor:
            return not source_state.has_terminal_agent_output_after_anchor
        if bool(fork.get("native_turn_started")):
            return True
        return await self._fork_source_turn_now_started(fork)

    async def _fork_source_turn_now_started(self, fork: dict[str, Any]) -> bool:
        source_session_id = str(fork.get("source_session_id") or "").strip()
        if not source_session_id:
            return False
        from vibe import internal_client

        try:
            turn_result = await internal_client.turn_state(source_session_id)
        except (internal_client.InternalServerTimeout, internal_client.InternalServerUnavailable):
            return False
        body = turn_result.get("body") or {}
        return bool(body.get("in_flight") and body.get("native_turn_started"))

    async def _rollback_forked_running_turn(
        self,
        transport: CodexTransport,
        thread_id: str,
    ) -> None:
        """Remove the source's still-running latest turn from a forked thread."""

        await transport.send_request(
            "thread/rollback",
            {
                "threadId": thread_id,
                "numTurns": 1,
            },
        )

    def _resolve_codex_agent_settings(
        self,
        request: AgentRequest,
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        routing_agent, routing_model, routing_effort = self._get_codex_overrides(request)
        request_subagent = getattr(request, "subagent_name", None)
        request_model = getattr(request, "subagent_model", None)
        request_effort = getattr(request, "subagent_reasoning_effort", None)
        vibe_model = getattr(request, "vibe_agent_model", None)
        vibe_effort = getattr(request, "vibe_agent_reasoning_effort", None)
        vibe_instructions = getattr(request, "vibe_agent_system_prompt", None)

        effective_agent = request_subagent or routing_agent
        explicit_model = request_model or vibe_model or routing_model
        explicit_effort = request_effort or vibe_effort or routing_effort

        agent_definition: Optional[SubagentDefinition] = None
        if effective_agent:
            try:
                working_path = getattr(request, "working_path", None)
                project_root = Path(working_path) if working_path else None
                agent_definition = load_codex_subagent(effective_agent, project_root=project_root)
            except Exception as exc:
                logger.warning("Failed to load Codex subagent %s: %s", effective_agent, exc)

        effective_model = explicit_model or (agent_definition.model if agent_definition else None) or self.codex_config.default_model
        effective_effort = explicit_effort or (agent_definition.reasoning_effort if agent_definition else None)
        developer_instructions = vibe_instructions or (agent_definition.developer_instructions if agent_definition else None)

        return effective_agent, effective_model, effective_effort, developer_instructions

    def _get_codex_overrides(
        self,
        request: AgentRequest,
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve scope routing through the controller's shared routing API."""
        controller = getattr(self, "controller", None)
        request_context = getattr(request, "context", None)
        getter = getattr(controller, "get_codex_overrides", None)
        if request_context is None or not callable(getter):
            return None, None, None
        try:
            return getter(request_context)
        except Exception as exc:
            logger.warning("Failed to resolve Codex routing overrides: %s", exc)
            return None, None, None

    async def _start_or_resume_thread(
        self,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> str:
        """Try to resume a persisted thread, fall back to creating a new one."""
        # Resume the native thread bound to the RESERVED workbench row (by PK): the
        # bind WRITE is by-PK, so the resume READ must read it back from the row, not
        # the (session_key, anchor) projection which drifts for avibe and would fork
        # a fresh thread (context loss) after a restart. Skip it for ANY subagent —
        # explicit (its own thread, distinct base_session_id) OR a routing-default
        # subagent (the namespaced base also has its own thread) — else the first
        # subagent turn would resume the MAIN thread. Falls back to the projection
        # for IM/CLI turns (no reserved target).
        persisted = self.sessions.get_agent_session_id(
            request.session_key,
            request.base_session_id,
            self.name,
        )
        _ctx_spec = getattr(getattr(request, "context", None), "platform_specific", None) or {}
        if not getattr(request, "subagent_name", None) and not _ctx_spec.get("routing_subagent"):
            persisted = self._reserved_native_session_id(getattr(request, "context", None), self.name) or persisted
        if persisted:
            try:
                self.bind_agent_session_id(request, persisted)
                resume_params: Dict[str, Any] = {
                    "threadId": persisted,
                    "developerInstructions": self._build_thread_developer_instructions(request),
                }
                model_provider = await self._resolve_resume_model_provider_override(transport, request, persisted)
                if model_provider:
                    resume_params["modelProvider"] = model_provider
                resp = await transport.send_request(
                    "thread/resume",
                    resume_params,
                )
                # thread/resume returns Thread directly OR may nest under "thread"
                thread_id = resp.get("id", "")
                if not thread_id:
                    thread_obj = resp.get("thread")
                    if isinstance(thread_obj, dict):
                        thread_id = thread_obj.get("id", "")
            except Exception as e:
                if self._is_recoverable_transport_error(e):
                    # Transient: reconnect the SAME thread (handled by the outer
                    # retry) — not context loss, keep.
                    logger.warning("Failed to resume Codex thread %s due to transport failure: %s", persisted, e)
                    raise
                from core.agent_auth_service import classify_auth_error

                if classify_auth_error("codex", str(e)):
                    # Auth expired/invalid: preserve the ORIGINAL error so
                    # handle_message's auth-recovery classifier can surface the
                    # reset-OAuth button — don't mask it as a generic resume failure.
                    logger.warning("Codex auth error while resuming thread %s: %s", persisted, e)
                    raise
                # FAIL LOUD: an associated thread that won't resume (expired/gone) is
                # context loss — surface it rather than silently starting a fresh
                # thread (product decision: no silent fallbacks).
                logger.warning("Failed to resume Codex thread %s: %s", persisted, e)
                raise CodexResumeUnavailableError(persisted) from e
            if not thread_id:
                raise CodexResumeUnavailableError(persisted, detail="thread/resume returned no thread id")
            self._session_mgr.set_thread_id(request.base_session_id, thread_id)
            self._remember_thread_developer_instructions(
                request.base_session_id,
                thread_id,
                resume_params.get("developerInstructions"),
            )
            logger.info("Resumed Codex thread %s for session %s", thread_id, request.base_session_id)
            return thread_id

        fork = pending_native_fork(request.context, self.name)
        if fork:
            return await self._fork_thread(transport, request, fork)

        # No associated thread yet (genuinely first turn) — start fresh.
        return await self._start_thread(transport, request)

    async def _resolve_resume_model_provider_override(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> Optional[str]:
        """Return a provider override only when a persisted thread is stale.

        Codex preserves a thread's latest model / reasoning effort on resume
        unless the client sends a model/provider override. Vibe Remote only
        needs to override the provider after the user changes Codex auth mode
        between Vibe Remote-managed OAuth/API-key providers, so inspect the
        stored thread first and leave normal resumes on Codex's persisted
        fallback path.
        """
        current_provider = await self._read_effective_model_provider(transport, request)
        if not current_provider:
            return None

        try:
            resp = await transport.send_request(
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": False,
                },
            )
        except Exception as exc:
            logger.warning("Failed to read Codex thread %s provider before resume: %s", thread_id, exc)
            return None

        thread_obj = resp.get("thread") if isinstance(resp, dict) else None
        if not isinstance(thread_obj, dict) and isinstance(resp, dict) and resp.get("id") == thread_id:
            thread_obj = resp
        stored_provider = thread_obj.get("modelProvider") if isinstance(thread_obj, dict) else None
        if not isinstance(stored_provider, str) or not stored_provider.strip():
            return None

        stored_provider = stored_provider.strip()
        if stored_provider == current_provider:
            return None
        if not self._is_managed_provider_transition(stored_provider, current_provider):
            return None
        return current_provider

    @staticmethod
    def _is_managed_provider_transition(stored_provider: str, current_provider: str) -> bool:
        return {stored_provider, current_provider}.issubset(_CODEX_MANAGED_PROVIDER_IDS)

    async def _read_effective_model_provider(
        self,
        transport: CodexTransport,
        request: AgentRequest,
    ) -> Optional[str]:
        """Ask Codex app-server for the provider it resolves for this request."""
        params: Dict[str, Any] = {"includeLayers": False}
        working_path = getattr(request, "working_path", None)
        if working_path:
            params["cwd"] = working_path

        try:
            resp = await transport.send_request("config/read", params)
        except Exception as exc:
            logger.warning("Failed to read effective Codex model provider before resume: %s", exc)
            return None

        config_obj = resp.get("config") if isinstance(resp, dict) else None
        if not isinstance(config_obj, dict):
            return None
        model_provider = config_obj.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            return model_provider.strip()
        return None

    def _build_thread_developer_instructions(self, request: AgentRequest) -> Optional[str]:
        """Build Codex thread-level developer instructions for start/resume.

        Codex treats these as session configuration, not appended chat history.
        Passing the current value on resume refreshes stale Vibe Remote targeting
        instructions without growing the thread transcript.
        """
        _, _, _, agent_instructions = self._resolve_codex_agent_settings(request)
        platform = (
            request.context.platform
            or (request.context.platform_specific or {}).get("platform")
            or self.controller.config.platform
        )

        instruction_parts: list[str] = []
        if agent_instructions:
            instruction_parts.append(agent_instructions)

        instruction_parts.append(
            build_system_prompt_injection(
                include_quick_replies=getattr(self.controller.config, "reply_enhancements", True)
                and platform != "wechat",
                include_show_pages=getattr(self.controller.config, "show_pages_prompt", True),
                include_codex_generated_images=True,
                avibe_cloud_connected=avibe_cloud_url_available(self.controller.config),
                context=request.context,
                fallback_platform=platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                current_agent_backend="codex",
            )
        )

        return "\n\n".join(part for part in instruction_parts if part) or None

    async def _inject_forked_session_correction(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> None:
        """Append a fork correction as Codex model-visible developer history.

        Codex accepts ``developerInstructions`` on ``thread/fork``, but the fork
        also copies the source thread's previous developer messages. Appending a
        fresh developer item makes the target session id authoritative without
        creating a user turn.
        """
        correction = build_forked_session_correction_prompt(request.context)
        if not correction:
            return
        await transport.send_request(
            "thread/inject_items",
            {
                "threadId": thread_id,
                "items": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": correction}],
                    }
                ],
            },
        )

    async def _refresh_thread_developer_instructions_if_needed(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> None:
        """Refresh thread-level instructions for already-cached Codex threads."""
        self.ensure_agent_session_id(request)
        developer_instructions = self._build_thread_developer_instructions(request)
        if not developer_instructions:
            return

        if not hasattr(self, "_thread_developer_instructions"):
            self._thread_developer_instructions = {}

        cached = self._thread_developer_instructions.get(request.base_session_id)
        if cached == (thread_id, developer_instructions):
            return

        resume_params: Dict[str, Any] = {
            "threadId": thread_id,
            "developerInstructions": developer_instructions,
        }
        model_provider = await self._resolve_resume_model_provider_override(transport, request, thread_id)
        if model_provider:
            resume_params["modelProvider"] = model_provider

        await transport.send_request(
            "thread/resume",
            resume_params,
        )
        self._remember_thread_developer_instructions(
            request.base_session_id,
            thread_id,
            developer_instructions,
        )

    def _remember_thread_developer_instructions(
        self,
        base_session_id: str,
        thread_id: str,
        developer_instructions: Optional[str],
    ) -> None:
        if not developer_instructions:
            return
        if not hasattr(self, "_thread_developer_instructions"):
            self._thread_developer_instructions = {}
        self._thread_developer_instructions[base_session_id] = (thread_id, developer_instructions)

    def _clear_thread_developer_instructions(self, base_session_id: str) -> None:
        if hasattr(self, "_thread_developer_instructions"):
            self._thread_developer_instructions.pop(base_session_id, None)

    def _fork_correction_pending_sessions(self) -> set[str]:
        if not hasattr(self, "_fork_correction_pending_base_sessions"):
            self._fork_correction_pending_base_sessions = set()
        return self._fork_correction_pending_base_sessions

    def _mark_fork_correction_pending(self, base_session_id: str) -> None:
        self._fork_correction_pending_sessions().add(base_session_id)

    def _clear_fork_correction_pending(self, base_session_id: str) -> None:
        self._fork_correction_pending_sessions().discard(base_session_id)

    def is_fork_correction_pending(self, base_session_id: str) -> bool:
        return base_session_id in self._fork_correction_pending_sessions()

    async def _start_turn(
        self,
        transport: CodexTransport,
        request: AgentRequest,
        thread_id: str,
    ) -> str:
        """Build input, configure overrides, and send turn/start to Codex."""
        self.ensure_agent_session_id(request)
        input_items = self._build_input(request)
        _, effective_model, effective_effort, _ = self._resolve_codex_agent_settings(request)

        turn_params: Dict[str, Any] = {
            "threadId": thread_id,
            "input": input_items,
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        }
        if effective_model:
            turn_params["model"] = effective_model
        if effective_effort:
            turn_params["effort"] = effective_effort

        self._turn_registry.begin_turn_start(request, thread_id)
        event_handler = getattr(self, "_event_handler", None)
        snapshot_generated_images = getattr(
            event_handler,
            "snapshot_generated_images",
            None,
        )
        if callable(snapshot_generated_images):
            snapshot_generated_images(thread_id, request.base_session_id)
        resp = await transport.send_request("turn/start", turn_params)

        turn_id = resp.get("id", "")
        if not turn_id:
            turn_obj = resp.get("turn")
            if isinstance(turn_obj, dict):
                turn_id = turn_obj.get("id", "")
        if not turn_id:
            turn_id = self._turn_registry.get_bootstrapped_turn_id(request.base_session_id, request) or ""
        if not turn_id:
            raise RuntimeError("Codex turn/start returned no turn id")

        turn_state = self._turn_registry.finalize_turn_start_response(turn_id, request)
        self._mark_runtime_turn_started(getattr(request, "context", None))
        bind_generated_image_snapshot = getattr(event_handler, "bind_generated_image_snapshot", None)
        if callable(bind_generated_image_snapshot):
            bind_generated_image_snapshot(thread_id, turn_id, request.base_session_id)
        logger.info(
            "Codex turn started: thread=%s turn=%s session=%s state=%s",
            thread_id,
            turn_id,
            request.composite_session_id,
            "registered" if turn_state else "already-finished",
        )
        return thread_id

    def _mark_runtime_turn_started(self, context: Any) -> None:
        service = getattr(getattr(self, "controller", None), "agent_service", None)
        mark_started = getattr(service, "mark_runtime_turn_started", None)
        if callable(mark_started):
            mark_started(context)

    # ------------------------------------------------------------------
    # Input building
    # ------------------------------------------------------------------

    def _build_input(self, request: AgentRequest) -> list[Dict[str, Any]]:
        """Convert AgentRequest into Codex UserInput items."""
        items: list[Dict[str, Any]] = []

        # Text input
        message = request.message
        if request.files:
            # Append file info like Claude agent does
            file_lines = ["", "[User Attachments]"]
            for attachment in request.files:
                if not attachment.local_path:
                    continue
                is_image = (attachment.mimetype or "").startswith("image/")
                if is_image:
                    # Send as localImage input
                    items.append(
                        {
                            "type": "localImage",
                            "path": attachment.local_path,
                        }
                    )
                else:
                    size_str = f", {attachment.size} bytes" if attachment.size else ""
                    file_lines.append(f"- File: {attachment.local_path} ({attachment.mimetype}{size_str})")
            if len(file_lines) > 2:
                message = f"{message}\n" + "\n".join(file_lines)

        if message:
            items.insert(0, {"type": "text", "text": message})

        return items

    # ------------------------------------------------------------------
    # Callback handlers (wired to transport)
    # ------------------------------------------------------------------

    async def _on_notification(self, method: str, params: Dict[str, Any]) -> None:
        """Route a server notification to the event handler."""
        request = self._find_request_for_notification(method, params)
        if not request:
            thread_id = self._extract_thread_id(params)
            turn_id = self._extract_turn_id(params)
            logger.debug(
                "No active request for Codex notification %s (thread=%s turn=%s)",
                method,
                thread_id,
                turn_id,
            )
            return

        self._touch_transport_activity(request.working_path)
        await self._event_handler.handle_notification(method, params, request)

    async def _on_server_request(
        self,
        cwd: str,
        req_id: int | str,
        method: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle server requests — auto-approve all."""
        # A server request means this app-server is alive and actively driving a
        # turn, so count it as activity for the stuck-active idle backstop.
        self._touch_transport_activity(cwd)
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            logger.info("Auto-approving Codex %s (item=%s)", method, params.get("itemId"))
            return {"approved": True}

        logger.warning("Unknown Codex server request: %s", method)
        return {"approved": True}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_request_for_thread(self, thread_id: str) -> Optional[AgentRequest]:
        """Look up the active AgentRequest for a given Codex threadId."""
        base_session_id = self._session_mgr.find_base_session_id_for_thread(thread_id)
        if not base_session_id:
            return None
        return self._turn_registry.get_latest_request(base_session_id)

    def _find_request_for_notification(self, method: str, params: Dict[str, Any]) -> Optional[AgentRequest]:
        turn_id = self._extract_turn_id(params)
        if turn_id:
            request = self._turn_registry.get_request_for_turn(turn_id)
            if request:
                return request

            thread_id = self._extract_thread_id(params)
            if not thread_id:
                return None
            if method != "turn/started":
                return None
            base_session_id = self._session_mgr.find_base_session_id_for_thread(thread_id)
            if not base_session_id:
                return None

            bootstrap_state = self._turn_registry.bootstrap_turn(turn_id, base_session_id, thread_id)
            if bootstrap_state:
                logger.info(
                    "Bootstrapped Codex turn %s for notification %s on session %s",
                    turn_id,
                    method,
                    base_session_id,
                )
                return bootstrap_state.request
            return None

        thread_id = self._extract_thread_id(params)
        if thread_id:
            return self._find_request_for_thread(thread_id)
        return None

    def _extract_thread_id(self, params: Dict[str, Any]) -> str:
        thread_id = params.get("threadId", "")
        if not thread_id:
            thread_obj = params.get("thread")
            if isinstance(thread_obj, dict):
                thread_id = thread_obj.get("id", "")
        return thread_id

    def _extract_turn_id(self, params: Dict[str, Any]) -> str:
        turn_id = params.get("turnId", "")
        if not turn_id:
            turn_obj = params.get("turn")
            if isinstance(turn_obj, dict):
                turn_id = turn_obj.get("id", "")
        return turn_id

    async def _delete_ack(self, request: AgentRequest) -> None:
        service = getattr(self.controller, "processing_indicator", None)
        if service is not None:
            await service.delete_ack_message(request)
            return
        ack_id = request.ack_message_id
        if ack_id and hasattr(self.im_client, "delete_message"):
            try:
                await self.im_client.delete_message(request.context.channel_id, ack_id)
            except Exception as err:
                logger.debug("Could not delete ack message: %s", err)
            finally:
                request.ack_message_id = None

    def _cwd_inodes(self) -> Dict[str, Optional[int]]:
        if not hasattr(self, "_transport_cwd_inodes"):
            self._transport_cwd_inodes = {}
        return self._transport_cwd_inodes

    @staticmethod
    def _cwd_inode(cwd: str) -> Optional[int]:
        try:
            return os.stat(cwd).st_ino
        except OSError:
            return None

    def _touch_transport_activity(self, cwd: str) -> None:
        if not hasattr(self, "_transport_last_activity"):
            self._transport_last_activity = {}
        if cwd:
            self._transport_last_activity[cwd] = time.monotonic()

    def _has_active_turns_for_cwd(self, cwd: str) -> bool:
        for base_session_id in self._session_mgr.sessions_for_cwd(cwd):
            if self._turn_registry.get_active_turn(base_session_id):
                return True
            has_pending_turn_start = getattr(self._turn_registry, "has_pending_turn_start", None)
            if callable(has_pending_turn_start) and has_pending_turn_start(base_session_id):
                return True
        return False
