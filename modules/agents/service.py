import logging
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.session_activities import SessionActivityRegistry
from core.message_output import terminal_output_for, terminal_turn_output

from .base import (
    AGENT_RUNTIME_TURN_KEY,
    AGENT_RUNTIME_TURN_TOKEN,
    AGENT_TURN_TOKEN,
    AgentRequest,
    BaseAgent,
)

logger = logging.getLogger(__name__)

STALE_STOP_REASONS = {"not_active", "runtime_unavailable"}


class AgentService:
    """Registry and dispatcher for agent implementations."""

    # These clocks never decide that a turn is old. They only pace a positive
    # backend-liveness probe and confirm one definitive False result across the
    # small process-exit/terminal-delivery race window.
    _liveness_probe_interval_seconds = 1.0
    _liveness_failure_grace_seconds = 0.5
    _backend_exit_terminal_error = "backend_runtime_exited_before_terminal"

    def __init__(
        self,
        controller,
        *,
        activities: SessionActivityRegistry | None = None,
    ):
        self.controller = controller
        self.agents: Dict[str, BaseAgent] = {}
        self.default_agent = "claude"
        self._turn_gates: dict[str, _RuntimeTurnGate] = {}
        self.activities = activities or SessionActivityRegistry()
        set_output_settled_callback = getattr(
            self.activities,
            "set_output_settled_callback",
            None,
        )
        if callable(set_output_settled_callback):
            set_output_settled_callback(self._on_activity_output_settled)
        # Strong refs to fire-and-forget tasks (e.g. the cancellation tidy) so the
        # event loop doesn't GC them before they run (asyncio only weak-refs tasks).
        self._background_tasks: set[asyncio.Task] = set()
        self._backend_ready: dict[str, asyncio.Event] = {}

    def _backend_ready_event(self, backend: str) -> asyncio.Event:
        event = self._backend_ready.get(backend)
        if event is None:
            event = asyncio.Event()
            event.set()
            self._backend_ready[backend] = event
        return event

    def begin_backend_drain(self, backend: str) -> None:
        self._backend_ready_event(backend).clear()

    def end_backend_drain(self, backend: str) -> None:
        self._backend_ready_event(backend).set()

    async def wait_backend_ready(self, backend: str) -> None:
        await self._backend_ready_event(backend).wait()

    async def prepare_backend_restart(self, backend: str) -> None:
        agent = self.agents.get(backend)
        prepare = getattr(agent, "prepare_runtime_restart", None)
        if callable(prepare):
            await prepare()

    def backend_runtime_active(self, backend: str) -> bool:
        if self.activities.has_backend_work(backend):
            return True
        agent = self.agents.get(backend)
        probe = getattr(agent, "runtime_has_active_turns", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            logger.debug("Backend active-runtime probe failed for %s", backend, exc_info=True)
            return True

    def force_end_backend_activities(self, backend: str) -> list[Any]:
        completed = self.activities.end_backend(backend, status="killed")
        for activity in completed:
            self.on_activity_terminal(activity)
        return completed

    def register(self, agent: BaseAgent):
        self.agents[agent.name] = agent
        logger.info(f"Registered agent backend: {agent.name}")

    def _on_activity_output_settled(self, activity: Any) -> None:
        agent = self.agents.get(str(getattr(activity, "backend", "") or ""))
        notify = getattr(agent, "on_activity_output_settled", None)
        if callable(notify):
            notify(str(getattr(activity, "runtime_key", "") or ""))

    def on_activity_terminal(self, activity: Any) -> bool:
        """Let the Run owner acknowledge one terminal Activity."""

        service = getattr(self.controller, "scheduled_task_service", None)
        settle = getattr(service, "settle_activity_runs", None)
        if not callable(settle):
            return False
        try:
            settle(activity)
            if str(getattr(activity, "status", "") or "") != "completed":
                self.activities.ack_recovered_terminal(activity)
            return True
        except Exception:
            logger.warning(
                "Failed to settle Runs for terminal Activity %s",
                getattr(activity, "id", ""),
                exc_info=True,
            )
            return False

    def end_activity_runtime(self, backend: str, runtime_key: str) -> list[Any]:
        """Terminate one backend connection's Activities and notify Run owners."""

        completed = self.activities.end_runtime(
            backend,
            runtime_key,
            retain_terminal_snapshots=True,
        )
        for activity in completed:
            self.on_activity_terminal(activity)
        return completed

    def get(self, agent_name: Optional[str]) -> BaseAgent:
        target = agent_name or self.default_agent
        if target in self.agents:
            return self.agents[target]
        raise KeyError(target)

    async def handle_message(self, agent_name: str, request: AgentRequest):
        agent = self.get(agent_name)
        runtime_key = self._runtime_turn_key(agent, request)
        gate = self._get_turn_gate(runtime_key)
        # Only a message that is ACTUALLY queued behind a running turn shows the
        # queued 👌. The gate is held for the whole turn (released only on the
        # terminal result via the outbound dispatcher), so gate.lock.locked() here
        # means another turn for this runtime key is in flight and this message will
        # block on acquire() below — surface that wait with 👌, then promote it to
        # the running 👀 once the gate is acquired. A non-contended message skips 👌
        # entirely and goes straight to 👀 at promote_reaction_to_running, so it does
        # NOT flash 👌→👀. Reaction add/remove is owned by the processing indicator
        # and is a no-op on platforms / modes without a reaction indicator (e.g.
        # WeChat, typing-only, avibe Web).
        indicator = getattr(self.controller, "processing_indicator", None)
        queued_reaction_task: Optional[asyncio.Task] = None
        if indicator is not None and gate.lock.locked():
            # Contended: show the queued 👌. Fire it as a CONCURRENT task instead of
            # awaiting it here — awaiting a network reaction call before acquire()
            # would leave this turn OUT of the lock's FIFO waiter queue, so a later
            # same-runtime message could reach acquire() first and reorder prompts
            # within the session (Codex P1). Calling acquire() immediately reserves
            # this turn's place in the queue; the task adds 👌 concurrently while we
            # block, and we join it after acquiring (before promoting to 👀).
            queued_reaction_task = asyncio.create_task(indicator.show_queued_reaction(request))
        try:
            await gate.lock.acquire()
        except BaseException:
            # Cancellation (e.g. SIGTERM / shutdown) while still waiting in the
            # queue is raised by acquire() OUTSIDE the main try block below, so its
            # CancelledError handler never runs. Let the queued-reaction task settle,
            # then clean up the 👌 (and any eager typing) so it does not leak.
            if queued_reaction_task is not None:
                try:
                    await queued_reaction_task
                except BaseException:
                    pass
                if indicator is not None:
                    try:
                        # Pass the request (not the handle) so finish() clears BOTH
                        # the handle and the flat request.ack_reaction_* fields.
                        await indicator.finish(request)
                    except Exception:
                        logger.debug("Failed to clean up queued reaction on cancel", exc_info=True)
            raise
        # A restart may have started while this turn waited behind the previous
        # owner of the runtime key. Keep the key lock, wait for cutover, then
        # resolve the current adapter so the queued turn cannot enter the old
        # generation.
        try:
            await self.wait_backend_ready(agent_name)
            agent = self.get(agent_name)
        except BaseException:
            if queued_reaction_task is not None:
                try:
                    await queued_reaction_task
                except BaseException:
                    pass
                if indicator is not None:
                    try:
                        await indicator.finish(request)
                    except Exception:
                        logger.debug("Failed to clean up queued reaction on restart-wait cancel", exc_info=True)
            if gate.lock.locked() and not gate.token:
                gate.lock.release()
            raise
        gate.token = uuid.uuid4().hex
        gate.backend = agent.name
        gate.agent = agent
        gate.runtime_started = False
        gate.task = asyncio.current_task()
        gate.context = request.context
        gate.request = request
        gate.cancel_tidy_task = None
        self._stamp_runtime_turn(request, runtime_key, gate.token)
        try:
            # Register the indicator handle for terminal/cancel cleanup BEFORE the
            # reaction awaits below. If the turn is cancelled (shutdown / supersede)
            # while promote is awaiting the reaction API, the scheduled terminal tidy
            # resolves the handle by turn token from this registry — tracking it first
            # ensures a queued 👌 or fallback typing/message indicator is still
            # finished instead of leaking on the message (Codex P2).
            self._track_processing_indicator_turn(request)
            # Settle the concurrently-added queued 👌 and promote it to the running
            # 👀 INSIDE this cancellation-managed try. A cancel (shutdown / supersede)
            # during these awaits must reach the CancelledError handler below so the
            # runtime turn is released; otherwise the gate token + lock would leak and
            # later prompts for this runtime key would block forever (Codex P1). Each
            # is individually guarded so a reaction failure can't break the turn.
            if queued_reaction_task is not None:
                try:
                    await queued_reaction_task
                except Exception:
                    logger.debug("Failed to settle queued reaction", exc_info=True)
            if indicator is not None:
                try:
                    await indicator.promote_reaction_to_running(request, agent_name=agent_name)
                except Exception:
                    logger.debug("Failed to promote reaction to running", exc_info=True)
            # INBOUND status chokepoint (one of exactly two — the other is the outbound
            # MessageDispatcher.emit_agent_message). Every turn, every source (chat /
            # scheduled / Show Page), every backend funnels through here, so this is the
            # single place that marks an avibe session "running". The matching idle /
            # failed is written by the outbound terminal result. Non-avibe turns carry
            # no workbench session id and are skipped.
            manager = getattr(self.controller, "session_turns", None)
            if manager is not None:
                manager.on_running(request.context)
            # Turn-start work runs HERE — after the gate/on_running confirm the
            # turn is actually running — not while it was queued behind another
            # turn. Claiming the shared per-turn trigger id and posting the
            # "starting" status bubble now means a queued turn never hijacks the
            # running turn's bubble key or posts a premature bubble. Both hooks
            # are optional and guarded so a missing hook or a bubble failure can
            # never break the turn.
            await self._begin_turn_status(request.context)
            await agent.handle_message(request)
        except asyncio.CancelledError:
            # Shutdown / SIGTERM / supersede cancels the turn mid-flight. Without a
            # terminal emit the concise status bubble stays stuck on its last
            # action. Best effort: SCHEDULE (don't await) a silent terminal result
            # so the outbound chokepoint collapses the bubble + settles the dot;
            # awaiting here would swallow the cancellation. (C3)
            #
            # The runtime turn must be released AFTER that emit runs, NOT before:
            # releasing clears the turn token, and the result branch drops any
            # emit whose turn is no longer current — so an early release would
            # make the scheduled tidy a no-op and leave the bubble stuck. Release
            # inside the scheduled task's finally; fall back to a synchronous
            # release if the emit can't be scheduled so the gate never leaks.
            emit = getattr(self.controller, "emit_agent_message", None)
            scheduled = False
            if callable(emit):
                try:

                    async def _tidy_on_cancel() -> None:
                        try:
                            await emit(
                                request.context,
                                "result",
                                "",
                                is_error=True,
                                level="silent",
                                output=terminal_output_for(request),
                            )
                        except Exception:
                            logger.debug("Failed to emit terminal tidy on cancellation", exc_info=True)
                        finally:
                            self.release_runtime_turn(request.context)

                    tidy_task = asyncio.create_task(_tidy_on_cancel())
                    gate.cancel_tidy_task = tidy_task
                    self._background_tasks.add(tidy_task)
                    tidy_task.add_done_callback(self._background_tasks.discard)
                    scheduled = True
                except Exception:
                    logger.debug("Failed to schedule terminal tidy on cancellation", exc_info=True)
            if not scheduled:
                self.release_runtime_turn(request.context)
            raise
        except Exception:
            # The message handler converts backend exceptions into a terminal
            # error result using the same context. Try that shared terminal path
            # here too; if delivery itself is broken, the finally below still
            # releases this turn's token so later prompts cannot hang forever.
            try:
                emit = getattr(self.controller, "emit_agent_message", None)
                if callable(emit):
                    try:
                        await emit(
                            request.context,
                            "result",
                            "",
                            is_error=True,
                            level="silent",
                            output=terminal_output_for(request),
                        )
                    except Exception:
                        logger.debug("Failed to emit terminal result for backend exception", exc_info=True)
            finally:
                self.release_runtime_turn(request.context)
            raise

    async def clear_sessions(self, session_key: str) -> Dict[str, int]:
        cleared: Dict[str, int] = {}
        for name in list(self.agents.keys()):
            count = await self.clear_backend_sessions(name, session_key)
            if count:
                cleared[name] = count
        return cleared

    async def clear_backend_sessions(self, agent_name: str, session_key: str) -> int:
        agent = self.get(agent_name)
        runtime_key_getter = getattr(agent, "runtime_turn_keys_for_session_key", None)
        runtime_keys = runtime_key_getter(session_key) if callable(runtime_key_getter) else set()
        runtime_tokens = self._runtime_turn_tokens(runtime_keys, backend=agent.name)
        count = await agent.clear_sessions(session_key)
        for runtime_key, runtime_token in runtime_tokens.items():
            self.release_runtime_turn_key(runtime_key, runtime_token)
        return count

    def release_runtime_turns_for_backend(self, agent_name: str) -> None:
        runtime_tokens = self.runtime_turn_tokens_for_backend(agent_name)
        self.release_runtime_turn_tokens(runtime_tokens)

    def runtime_turn_tokens_for_backend(self, agent_name: str) -> dict[str, str]:
        agent = self.agents.get(agent_name)
        backend = agent.name if agent is not None else agent_name
        return {
            runtime_key: gate.token
            for runtime_key, gate in self._turn_gates.items()
            if gate.backend == backend and gate.token
        }

    def release_runtime_turn_tokens(self, runtime_tokens: dict[str, str]) -> None:
        for runtime_key, runtime_token in runtime_tokens.items():
            self.release_runtime_turn_key(runtime_key, runtime_token)

    async def handle_stop(self, agent_name: str, request: AgentRequest) -> bool:
        current_agent = self.get(agent_name)
        payload = getattr(request.context, "platform_specific", None) or {}
        stamped_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_key = stamped_key or self._runtime_turn_key(current_agent, request)
        gate = self._turn_gates.get(runtime_key)
        agent = gate.agent if gate is not None and gate.agent is not None else current_agent
        if gate is not None and gate.token:
            self._stamp_runtime_turn(request, runtime_key, gate.token)
        handled = await agent.handle_stop(request)
        if (
            not handled
            and getattr(request, "stop_failure_reason", None) in STALE_STOP_REASONS
            and gate is not None
            and gate.token
            and gate.runtime_started
        ):
            self.release_runtime_turn(request.context)
        return handled

    def mark_runtime_turn_started(self, context: Any) -> None:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None or gate.token != runtime_token:
            return
        gate.runtime_started = True
        gate.liveness_probe = self._capture_backend_liveness(gate, context)
        self._start_runtime_liveness_monitor(runtime_key, gate, runtime_token)

    def _capture_backend_liveness(
        self,
        gate: "_RuntimeTurnGate",
        context: Any,
    ) -> Callable[[], Optional[bool]]:
        capture = getattr(gate.agent, "capture_backend_liveness", None)
        if callable(capture):
            try:
                probe = capture(context)
                if callable(probe):
                    return probe
            except Exception:
                logger.debug(
                    "Failed to capture backend liveness for %s",
                    gate.backend,
                    exc_info=True,
                )
        return lambda: self.backend_alive(context, use_captured=False)

    @staticmethod
    def _probe_backend_liveness(
        probe: Callable[[], Optional[bool]] | None,
    ) -> Optional[bool]:
        if probe is None:
            return None
        try:
            return probe()
        except Exception:
            logger.debug("Captured backend liveness probe raised", exc_info=True)
            return None

    def _start_runtime_liveness_monitor(
        self,
        runtime_key: str,
        gate: "_RuntimeTurnGate",
        runtime_token: str,
    ) -> None:
        existing = gate.liveness_task
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(
                self._monitor_runtime_liveness(runtime_key, runtime_token),
                name=f"agent-runtime-liveness:{gate.backend}:{runtime_key}",
            )
        except RuntimeError:
            # mark_runtime_turn_started is normally called on the controller loop.
            # A synchronous unit/stub caller has no runtime to supervise.
            return
        gate.liveness_task = task
        self._background_tasks.add(task)

        def _finished(finished: asyncio.Task) -> None:
            self._background_tasks.discard(finished)
            current = self._turn_gates.get(runtime_key)
            if current is not None and current.token == runtime_token and current.liveness_task is finished:
                current.liveness_task = None
            if finished.cancelled():
                return
            try:
                error = finished.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "Runtime liveness monitor crashed for backend=%s runtime=%s: %r",
                    gate.backend,
                    runtime_key,
                    error,
                    exc_info=error,
                )

        task.add_done_callback(_finished)

    async def _monitor_runtime_liveness(self, runtime_key: str, runtime_token: str) -> None:
        """Turn a definitive owned-backend death into the normal terminal path.

        Age is deliberately absent. A live or unknown backend may run forever.
        Recovery requires the same runtime-key/token owner and two definitive
        ``False`` probes around a short grace period; a terminal event or newer
        turn changes the token and makes this monitor a no-op.
        """

        while True:
            await asyncio.sleep(self._liveness_probe_interval_seconds)
            gate = self._turn_gates.get(runtime_key)
            if gate is None or gate.token != runtime_token or not gate.runtime_started:
                return
            if gate.context is None or self._probe_backend_liveness(gate.liveness_probe) is not False:
                continue

            await asyncio.sleep(self._liveness_failure_grace_seconds)
            gate = self._turn_gates.get(runtime_key)
            if gate is None or gate.token != runtime_token or not gate.runtime_started:
                return
            context = gate.context
            if context is None or self._probe_backend_liveness(gate.liveness_probe) is not False:
                continue

            logger.error(
                "Accepted Agent turn lost its owned backend before terminal delivery "
                "(backend=%s runtime=%s)",
                gate.backend,
                runtime_key,
            )
            emit = getattr(self.controller, "emit_agent_message", None)
            if not callable(emit):
                logger.error(
                    "Cannot recover dead backend turn because the terminal emitter is unavailable "
                    "(backend=%s runtime=%s)",
                    gate.backend,
                    runtime_key,
                )
                continue
            try:
                output = (
                    terminal_output_for(gate.request)
                    if gate.request is not None
                    else terminal_turn_output()
                )
                await emit(
                    context,
                    "result",
                    "",
                    is_error=True,
                    level="silent",
                    output=output,
                    terminal_error=self._backend_exit_terminal_error,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not release only half of the ownership graph. Retrying the
                # idempotent terminal chokepoint is safer than freeing the runtime
                # gate while the Session sink or Run row may still be active.
                logger.exception(
                    "Failed to converge dead backend turn through terminal delivery "
                    "(backend=%s runtime=%s)",
                    gate.backend,
                    runtime_key,
                )
                continue
            return

    def runtime_turn_started(self, context: Any) -> bool:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return False
        gate = self._turn_gates.get(runtime_key)
        return bool(gate is not None and gate.token == runtime_token and gate.runtime_started)

    def runtime_turn_active(self, runtime_key: str) -> bool:
        """Whether a foreground turn holds or is queued on this backend runtime."""

        gate = self._turn_gates.get(str(runtime_key or "").strip())
        return bool(
            gate is not None
            and (gate.lock.locked() or self._lock_has_live_waiters(gate.lock))
        )

    @staticmethod
    def _lock_has_live_waiters(lock: asyncio.Lock) -> bool:
        """True when ``lock`` has at least one not-yet-cancelled queued waiter.

        ``asyncio.Lock.locked()`` reports only whether the lock is HELD, not whether
        coroutines are queued waiting for it — but acquiring a lock that is free yet
        has waiters still suspends. ``begin_agent_initiated_turn`` needs this signal
        to stay strictly non-blocking. ``_waiters`` is a CPython asyncio internal (a
        deque or ``None``); guarded with ``getattr`` so a future runtime that drops
        it degrades to "no waiters" instead of raising.
        """
        waiters = getattr(lock, "_waiters", None)
        if not waiters:
            return False
        return any(not w.cancelled() for w in waiters)

    async def begin_agent_initiated_turn(
        self, agent_name: str, context: Any, runtime_key: str
    ) -> Optional[str]:
        """Open a runtime-gate turn for backend output Avibe did NOT initiate.

        Claude Code (and any future backend that re-invokes its agent loop inside
        a persistent process) can start a fresh turn when a background task
        finishes or a ScheduleWakeup fires — WITHOUT Avibe sending a query. That
        output reaches the long-lived receiver with no turn open, so the outbound
        active-turn guard (``emit_matches_runtime_turn``) would drop every
        assistant/tool/result emit as a stale straggler. Open a turn here so the
        reply rides the same INBOUND chokepoint (session → ``running``) and
        OUTBOUND chokepoint (terminal result → idle/failed + persist + deliver +
        notify + gate release) as a user turn.

        Returns the fresh turn token when the gate was free and a turn was opened,
        else ``None`` when the gate is contended (a user turn holds OR is queued on
        it) — let that turn own the output; the agent-initiated emit then adopts its
        token instead.

        STRICTLY NON-BLOCKING: this runs ON the long-lived Claude receiver, which is
        the only reader of the SDK stream. An ``asyncio.Lock`` can be momentarily
        unlocked while it still has QUEUED WAITERS (a user turn that blocked on this
        gate while the previous turn held it, between that turn's release and the
        waiter resuming). In that window ``locked()`` is False, yet
        ``await acquire()`` would SUSPEND the receiver behind the queued user turn —
        and the receiver is what reads that turn's terminal result to release the
        gate, so the session would deadlock. So bail unless the gate is free AND has
        no live waiters; the ``acquire()`` below then completes synchronously
        without yielding, so no concurrent turn can slip in (Codex P1).
        """
        runtime_key = str(runtime_key or "").strip()
        if not runtime_key:
            return None
        gate = self._get_turn_gate(runtime_key)
        if gate.lock.locked() or self._lock_has_live_waiters(gate.lock):
            return None
        await gate.lock.acquire()
        gate.token = uuid.uuid4().hex
        gate.backend = agent_name
        gate.agent = self.agents.get(agent_name)
        gate.context = context
        gate.request = None
        # The backend already produced output, so the turn is unambiguously
        # running — there is no queued-startup window to distinguish (unlike a
        # user turn waiting on the gate), so mark it started immediately.
        gate.runtime_started = True
        if context.platform_specific is None:
            context.platform_specific = {}
        context.platform_specific[AGENT_RUNTIME_TURN_KEY] = runtime_key
        context.platform_specific[AGENT_RUNTIME_TURN_TOKEN] = gate.token
        gate.liveness_probe = self._capture_backend_liveness(gate, context)
        self._start_runtime_liveness_monitor(runtime_key, gate, gate.token)
        # Fresh streaming token too: the previous turn's SSE sink is already gone,
        # and a leftover ``turn_token`` would only confuse ``mark_turn_complete``
        # (which no-ops anyway with no live sink for this session).
        context.platform_specific[AGENT_TURN_TOKEN] = gate.token
        # INBOUND status chokepoint (mirrors ``handle_message``): mark the avibe
        # session ``running`` so the sidebar dot reflects the agent-initiated work,
        # and register the turn with the FSM so the Workbench Stop button works and
        # the browser sees turn.start/turn.end. Non-avibe contexts resolve to no
        # session id and are skipped (no-op).
        manager = getattr(self.controller, "session_turns", None)
        if manager is not None:
            try:
                manager.on_running(context)
            except Exception:
                logger.debug("on_running failed for agent-initiated turn", exc_info=True)
            register = getattr(manager, "register_agent_initiated_turn", None)
            if callable(register):
                try:
                    register(context)
                except Exception:
                    logger.debug("register_agent_initiated_turn failed", exc_info=True)
        return gate.token

    def release_runtime_turn(self, context: Any) -> None:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None or gate.token != runtime_token:
            return
        self.release_runtime_turn_key(runtime_key, runtime_token)

    def release_runtime_turn_key(self, runtime_key: str, runtime_token: str | None = None) -> None:
        runtime_key = str(runtime_key or "").strip()
        if not runtime_key:
            return
        gate = self._turn_gates.get(runtime_key)
        if gate is None:
            return
        if runtime_token is not None and gate.token != runtime_token:
            return
        liveness_task = gate.liveness_task
        gate.liveness_task = None
        gate.token = ""
        gate.backend = ""
        gate.runtime_started = False
        gate.agent = None
        gate.task = None
        gate.context = None
        gate.request = None
        gate.liveness_probe = None
        if liveness_task is not None and not liveness_task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if liveness_task is not current:
                liveness_task.cancel()
        if gate.lock.locked():
            gate.lock.release()

    async def force_cancel_backend_turns(self, backend: str) -> None:
        """Cancel every foreground owner and emit a terminal outcome before cutover."""
        owned = [
            (runtime_key, gate, gate.token)
            for runtime_key, gate in self._turn_gates.items()
            if gate.backend == backend and gate.token
        ]
        tasks = {
            gate.task
            for _runtime_key, gate, _token in owned
            if gate.task is not None and not gate.task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=2.0)
        tidies = {
            gate.cancel_tidy_task
            for _runtime_key, gate, _token in owned
            if gate.cancel_tidy_task is not None and not gate.cancel_tidy_task.done()
        }
        if tidies:
            await asyncio.wait(tidies, timeout=2.0)
        emit = getattr(self.controller, "emit_agent_message", None)
        for runtime_key, gate, token in owned:
            if gate.token != token:
                continue
            context = gate.context
            if context is not None and callable(emit):
                try:
                    await emit(
                        context,
                        "result",
                        "",
                        is_error=True,
                        level="silent",
                        output=terminal_turn_output(),
                    )
                except Exception:
                    logger.debug("Failed to settle forced backend turn %s", runtime_key, exc_info=True)
            self.release_runtime_turn_key(runtime_key, token)

    def emit_matches_runtime_turn(self, context: Any) -> bool:
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if not runtime_key or not runtime_token:
            return True
        gate = self._turn_gates.get(runtime_key)
        return gate is not None and gate.token == runtime_token

    def backend_alive(
        self,
        context: Any,
        *,
        use_captured: bool = True,
    ) -> Optional[bool]:
        """Resolve the backend for this turn (via its runtime gate) and ask it
        whether it is still alive. Returns ``None`` when the backend is unknown
        or has no probe — callers treat ``None`` as alive (no false alarm)."""
        payload = getattr(context, "platform_specific", None) or {}
        runtime_key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip()
        gate = self._turn_gates.get(runtime_key) if runtime_key else None
        runtime_token = str(payload.get(AGENT_RUNTIME_TURN_TOKEN) or "").strip()
        if (
            use_captured
            and gate is not None
            and runtime_token
            and gate.token == runtime_token
            and getattr(gate, "runtime_started", False)
        ):
            return self._probe_backend_liveness(
                getattr(gate, "liveness_probe", None)
            )
        backend = gate.backend if gate else None
        agent = getattr(gate, "agent", None) if gate is not None else None
        if agent is None and backend:
            agent = self.agents.get(backend)
        if agent is None:
            return None
        probe = getattr(agent, "backend_alive", None)
        if not callable(probe):
            return None
        try:
            return probe(context)
        except Exception:
            logger.debug("backend_alive probe raised for %s", backend, exc_info=True)
            return None

    def _get_turn_gate(self, runtime_key: str) -> "_RuntimeTurnGate":
        if runtime_key not in self._turn_gates:
            self._turn_gates[runtime_key] = _RuntimeTurnGate()
        return self._turn_gates[runtime_key]

    @staticmethod
    def _runtime_turn_key(agent: BaseAgent, request: AgentRequest) -> str:
        runtime_key = getattr(agent, "runtime_turn_key", None)
        if callable(runtime_key):
            return runtime_key(request)
        return (
            str(getattr(request, "composite_session_id", "") or "").strip()
            or str(getattr(request, "base_session_id", "") or "").strip()
            or "default"
        )

    def _runtime_turn_tokens(self, runtime_keys: set[str], *, backend: str | None = None) -> dict[str, str]:
        tokens = {}
        for runtime_key in runtime_keys:
            gate = self._turn_gates.get(runtime_key)
            if gate is None or not gate.token:
                continue
            if backend is not None and gate.backend != backend:
                continue
            tokens[runtime_key] = gate.token
        return tokens

    @staticmethod
    def _stamp_runtime_turn(request: AgentRequest, runtime_key: str, runtime_token: str) -> None:
        if request.context.platform_specific is None:
            request.context.platform_specific = {}
        request.context.platform_specific[AGENT_RUNTIME_TURN_KEY] = runtime_key
        request.context.platform_specific[AGENT_RUNTIME_TURN_TOKEN] = runtime_token
        if request.context.platform_specific.get(AGENT_TURN_TOKEN):
            return
        request.context.platform_specific[AGENT_TURN_TOKEN] = runtime_token

    async def _begin_turn_status(self, context: Any) -> None:
        """Run turn-start status work at the real start of the turn.

        Claims the shared per-turn trigger id (so the dispatcher's consolidated
        bubble key points at THIS turn) and posts the initial "starting" status
        bubble. Both are best-effort: a controller without these hooks, or a
        bubble failure, must not break the turn. The bubble is awaited inline so
        it exists before the backend's first process emit can edit it.
        """
        update_thread_message_id = getattr(self.controller, "update_thread_message_id", None)
        if callable(update_thread_message_id):
            try:
                update_thread_message_id(context)
            except Exception:
                logger.debug("update_thread_message_id failed at turn start", exc_info=True)
        dispatcher = getattr(self.controller, "message_dispatcher", None)
        begin_status_bubble = getattr(dispatcher, "begin_status_bubble", None) if dispatcher else None
        if callable(begin_status_bubble):
            try:
                await begin_status_bubble(context)
            except Exception:
                logger.debug("begin_status_bubble failed at turn start", exc_info=True)

    def _track_processing_indicator_turn(self, request: AgentRequest) -> None:
        handle = getattr(request, "processing_indicator", None)
        if handle is None:
            return
        service = getattr(self.controller, "processing_indicator", None)
        track = getattr(service, "track_turn", None)
        if callable(track):
            track(request.context, request)

    async def refresh_runtime_config(self, agent_name: str, runtime_config: Any) -> bool:
        """Refresh a backend's live runtime state from the latest config.

        Backend adapters own their cached transports/sessions, so the service
        centralizes dispatch while adapters decide how to apply the new
        runtime config. Returns ``False`` when the backend is not registered or
        does not expose the refresh contract.
        """
        agent = self.agents.get(agent_name)
        if agent is None:
            return False
        refresh = getattr(agent, "refresh_runtime_config", None)
        if not callable(refresh):
            return False
        runtime_tokens = self.runtime_turn_tokens_for_backend(agent.name)
        try:
            await refresh(runtime_config)
            return True
        finally:
            self.release_runtime_turn_tokens(runtime_tokens)


@dataclass
class _RuntimeTurnGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    token: str = ""
    backend: str = ""
    runtime_started: bool = False
    agent: BaseAgent | None = None
    task: asyncio.Task | None = None
    context: Any = None
    request: AgentRequest | None = None
    liveness_probe: Callable[[], Optional[bool]] | None = None
    cancel_tidy_task: asyncio.Task | None = None
    liveness_task: asyncio.Task | None = None
