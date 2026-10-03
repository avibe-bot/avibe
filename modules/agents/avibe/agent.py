"""``AvibeAgent``: the Avibe Agent engine (``core.agent_core``) as an Avibe backend.

One Turn is one ``Agent.run`` (plan section 4). The adapter

* resolves the model through Model Hub (``resolve_hop``) before the input is
  written, so a refused route fails like any backend that never started;
* marks native acceptance, which materializes the input's ``messages`` row, and
  then runs the loop, whose rows *are* the transcript (C-5);
* delivers every committed response through the same emit path as the other
  backends; the dispatcher writes the committed row's display columns instead
  of persisting a second row (``MessageOutput.persisted_row_id``);
* steers the running loop with P1 deliveries and lets a refused steer fall back
  to the P3 queue; Stop aborts the run;
* before any run of a Session in this process, under the Session writer lock,
  settles open tool calls (T2) and admits accepted inputs that were never
  consumed (T3); at startup it settles every Session's open calls eagerly. The
  Turn a restart interrupted is settled by the Turn owner (T4; ``avibe`` is a
  process-bound runtime).
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Optional, Sequence

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from config import paths
from core.agent_core.agent.events import (
    AgentError,
    AgentEvent,
    MessageCommitted,
    RunEnded,
    ToolStarted,
)
from core.agent_core.agent.hooks import AgentInput
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.models import ModelSelection
from core.agent_core.agent.recovery import settle_open_calls
from core.agent_core.harness.projection import open_tool_calls
from core.agent_core.harness.store import ContextEntry
from core.agent_core.tools.jobs import instant
from core.agent_core.messages import (
    IMAGE_MIME_TYPES,
    AssistantMessage,
    ToolCallBlock,
    UserMessage,
    text as text_block,
)
from core.agent_input import AgentInputMetadata
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.backend_failure import emit_backend_failure
from core.message_output import MessageOutput, stop_output_for, terminal_output_for
from core.native_dispatch_phase import mark_backend_dispatch_attempted
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.reply_enhancer import strip_silent_blocks
from core.skill_observability import accept_catalog
from modules.agents.avibe.errors import error_text
from modules.agents.avibe.media import MediaSnapshots
from modules.agents.avibe.models import HubModelRouter, ProviderFactory, registry_providers, selection_from_hop
from modules.agents.avibe.prompt import current_environment, system_prompt
from modules.agents.avibe.store import AdapterTranscriptStore
from modules.agents.avibe.tools import ToolSuite, local_tool_suite
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest, BaseAgent
from modules.im.base import FileAttachment
from storage import message_deliveries as delivery_store
from storage.agent_transcript import (
    INPUT_TYPES,
    RESPONSE_TYPES,
    SQLiteTranscriptStore,
    final_outcome,
    render_text,
    source_tool_result,
)
from storage.db import get_cached_sqlite_engine
from storage.models import agent_events, agent_sessions, message_deliveries, messages, session_turns

logger = logging.getLogger(__name__)

BACKEND = "avibe"
# Final responses whose empty text is explained by the stop itself (loop-control.md section 2).
_EXPLAINED_STOPS = ("refusal", "safety")
_COMPLETED = ("completed", "ended_by_hook")
_JOB_PRUNE_INTERVAL_S = 3600.0
# Startup recovery retries a Session it could not settle: 5 s, 30 s, 2 min, then every 10 min.
_RECOVERY_RETRY_DELAYS_S = (5.0, 30.0, 120.0)
_RECOVERY_RETRY_PERIOD_S = 600.0


def _relative_to(cwd: str) -> Callable[[str], str]:
    """Tool-call paths relative to the run's cwd, as the Claude backend shows them."""

    def relative(path: str) -> str:
        # A relative argument names a path under the run's cwd, as the tool resolves it.
        absolute = os.path.abspath(os.path.join(cwd, os.path.expanduser(path)) if cwd else os.path.expanduser(path))
        if not cwd:
            return absolute
        shown = os.path.relpath(absolute, cwd)
        return absolute if shown.startswith("../..") else shown

    return relative


@dataclass
class _Run:
    """One Turn's state, published before its first await (``handle_message``)."""

    request: AgentRequest
    session_id: str
    turn_id: str
    instance: str
    agent: Optional[Agent] = None
    router: Optional[HubModelRouter] = None
    cwd: str = ""
    settled: bool = False
    reason: Optional[str] = None
    # Model Hub's copy for a route it refused during the run (``_preflight``).
    refusal: Optional[str] = None
    errors: list[tuple[str, str]] = field(default_factory=list)
    final_row: Optional[str] = None
    tool_calls: dict[str, ToolCallBlock] = field(default_factory=dict)
    # This run's committed responses by row id, for delivering them; released with the run.
    responses: dict[str, AssistantMessage] = field(default_factory=dict)
    # Steer attempts ``Agent.steer`` accepted into this run: reconcile's in-process evidence.
    accepted_steers: set[str] = field(default_factory=set)
    stop_requested: bool = False

    @property
    def native_turn_id(self) -> str:
        """``avibe:<adapter instance>:<turn>``: the instance that runs the Turn, for steer reconcile."""
        return f"{BACKEND}:{self.instance}:{self.turn_id}"


@dataclass
class _SessionRuntime:
    """A Session's writer lock and run while some caller holds it; retired when idle."""

    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run: Optional[_Run] = None
    cwd: str = ""
    holders: int = 0


class AvibeAgent(BaseAgent):
    """Backend ``avibe``: the first-party agent loop over Avibe's own transcript rows."""

    name = BACKEND

    def __init__(
        self,
        controller: Any,
        *,
        engine: Optional[Engine] = None,
        providers: Optional[ProviderFactory] = None,
        tool_suite: Optional[ToolSuite] = None,
        state_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(controller)
        self._engine = engine or get_cached_sqlite_engine()
        state = Path(state_dir) if state_dir is not None else paths.get_state_dir() / "agent_core"
        self._state_dir = state
        self.media = MediaSnapshots(self._engine, state / "media")
        self.store = AdapterTranscriptStore(
            SQLiteTranscriptStore(self._engine, render=self._display_source),
            self._engine,
            environment=self._environment,
            on_response=self._on_response,
        )
        self._providers = providers or registry_providers(media_loader=self.media)
        self._tool_suite = tool_suite
        # Per-Session state lives only while a caller holds the Session (``_held``);
        # the last holder retires it, together with the store's per-Session state.
        self._runtimes: dict[str, _SessionRuntime] = {}
        # This adapter instance, named in every native turn id it reports: a steer attempt
        # whose Turn another instance ran (a retired adapter or a previous process) has no
        # live call here that could settle it.
        self._instance = secrets.token_hex(4)
        # Steer attempts whose live call is in flight in this instance: until it returns,
        # that call owns the attempt's receipt.
        self._steering: set[str] = set()
        # J5 while the service runs: when the jobs directory was last pruned, and the pass in flight.
        self._pruned_at: Optional[float] = None
        self._prune_task: Optional[asyncio.Task] = None
        # Startup recovery's retry while some Session has not settled (``recover_runtime_state``).
        self._recovery_task: Optional[asyncio.Task] = None

    # --- BaseAgent -----------------------------------------------------------

    async def handle_message(self, request: AgentRequest) -> None:
        """One Turn, with a single entry and a single exit.

        The Turn's state (its Stop intent, router, and failure route) is published
        before anything is awaited, so a Stop in any phase is honored: before the
        dispatch marks it settles the Turn as stopped with nothing dispatched. Every
        exception raised before settlement goes to the Hub-aware ``_fail``; one raised during
        or after it propagates to ``AgentService``, whose fallback releases the runtime gate;
        every exit closes the router.
        """
        context = request.context
        payload = context.platform_specific or {}
        session_id = self.ensure_agent_session_id(request)
        turn_id = str(payload.get("turn_token") or "").strip()
        input_id = str(payload.get("delivery_id") or "").strip()
        if not session_id or not turn_id or not input_id:
            await self._fail(request, "generic", "Avibe Agent turn has no Session, Turn, or input identity.")
            return
        turn = _Run(request, session_id, turn_id, self._instance)
        async with self._held(session_id, run=turn) as runtime:
            try:
                await self._run_turn(runtime, turn, input_id)
            except Exception as error:
                if turn.settled:
                    # Raised during or after settlement (its emits, or a failure notice): the
                    # shared owner's fallback releases the runtime gate, so it must see it.
                    raise
                logger.exception("Avibe Agent turn failed for Session %s", session_id)
                turn.settled = True
                await self._fail(request, "generic", f"turn failed: {error}", cause=error)
            finally:
                await self._admit_returned_inputs(turn)
                self.store.bind_agent(session_id, None)
                if turn.router is not None:
                    await turn.router.aclose()
                self._prune_jobs_soon()

    async def _run_turn(self, runtime: _SessionRuntime, turn: _Run, input_id: str) -> None:
        request, session_id = turn.request, turn.session_id
        context = request.context
        # Rows this Turn writes are the Turn's Agent's, from its request snapshot, even if the
        # Session's selected Agent changes while it runs.
        self.store.bind_agent(session_id, request.vibe_agent_name)
        await self._resume(runtime)
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        cwd = request.working_path or runtime.cwd
        try:
            turn.router = await self._preflight(turn)
        except Exception as error:
            turn.settled = True
            await self._fail_preflight(request, error)
            return
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        sections, skill_catalog = await self._avibe_sections(request, cwd)
        environment = await asyncio.to_thread(self._turn_environment, request, cwd)
        self._start_agent(turn, cwd, sections, environment)
        runtime.cwd = cwd
        # Prepared before anything is dispatched (``core.native_dispatch_phase``).
        message = await self._render_input(session_id, request.message, request.files, request.input_metadata)
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        # Native acceptance materializes the input row the loop consumes.
        self.bind_agent_session_id(request, session_id)
        mark_backend_dispatch_attempted(context)
        self.mark_runtime_turn_started(context)
        indicator = getattr(self.controller, "processing_indicator", None)
        if indicator is not None:
            # The ack message goes once the native write is accepted, as for the other backends.
            await indicator.delete_ack_message(request)
        accept_catalog(self.controller, context, skill_catalog, backend=BACKEND)
        agent_input: Optional[AgentInput] = AgentInput(input_id, message)
        while agent_input is not None:
            if turn.stop_requested:
                # A Stop acknowledged before this run could abort: no run starts. An input
                # left unconsumed is admitted at the Session's next resume (T3).
                turn.reason = "aborted"
                break
            async for event in turn.agent.run(agent_input, turn_id=turn.turn_id):
                await self._on_event(turn, event)
            agent_input = await self._continuing_input(turn)
        await self._settle(turn)
        self._maybe_backfill_session_title(request, session_id)

    async def _settle_stopped(self, turn: _Run) -> None:
        """A Stop before the loop started: the Turn settles as stopped, nothing dispatched."""
        turn.reason = "aborted"
        await self._settle(turn)

    async def handle_stop(self, request: AgentRequest) -> bool:
        run = self._stop_target(request)
        if run is None:
            request.stop_failure_reason = "not_active"
            return False
        run.stop_requested = True
        if run.agent is not None:
            run.agent.abort("stopped by user")
        return True

    def _stop_target(self, request: AgentRequest) -> Optional[_Run]:
        """The Turn a Stop addresses: by its Session, or by the runtime key the shared layer gates on.

        An IM ``/stop`` names no Session; like the other backends, it finds the Turn by
        the runtime identity ``AgentService`` stamped on the request.
        """
        runtime = self._runtimes.get(self._session_id(request.context) or "")
        if runtime is not None and runtime.run is not None:
            return runtime.run
        payload = getattr(request.context, "platform_specific", None) or {}
        key = str(payload.get(AGENT_RUNTIME_TURN_KEY) or "").strip() or self.runtime_turn_key(request)
        return next(
            (rt.run for rt in self._runtimes.values() if rt.run is not None and self.runtime_turn_key(rt.run.request) == key),
            None,
        )

    async def clear_sessions(self, session_key: str) -> int:
        cleared = 0
        for runtime in list(self._runtimes.values()):
            run = runtime.run
            if run is not None and run.request.session_key == session_key:
                run.stop_requested = True
                if run.agent is not None:
                    run.agent.abort("session cleared")
                cleared += 1
        return cleared

    def runtime_turn_keys(self) -> set[str]:
        return {self.runtime_turn_key(rt.run.request) for rt in self._runtimes.values() if rt.run is not None}

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        return {
            self.runtime_turn_key(rt.run.request)
            for rt in self._runtimes.values()
            if rt.run is not None and rt.run.request.session_key == session_key
        }

    async def recover_runtime_state(self) -> list[str]:
        """At startup or live registration: settle open tool calls, then prune settled jobs (T2, J5).

        Returns the Sessions it could not settle, never success over a failure. While
        any remain, one retry task keeps trying until every candidate has settled
        (``_retry_recovery``). Never calls the model; T3 stays with each Session's resume.
        """
        try:
            failed = await self._recover_round()
        except Exception:
            # The candidate scan itself failed: nothing is known to have settled.
            self._ensure_recovery_retry()
            raise
        if failed:
            self._ensure_recovery_retry()
        return failed

    @property
    def recovering(self) -> bool:
        """Whether recovery is still retrying a Session it could not settle."""
        return self._recovery_task is not None and not self._recovery_task.done()

    async def _recover_round(self) -> list[str]:
        """One recovery pass; J5 pruning follows a pass that settled every candidate."""
        failed = await self._recover_open_tool_calls()
        if not failed:
            await self._prune_settled_jobs()
        return failed

    def _ensure_recovery_retry(self) -> None:
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = asyncio.create_task(self._retry_recovery(), name="avibe-agent-recovery-retry")

    async def _retry_recovery(self) -> None:
        """Retry startup recovery with backoff until every candidate Session has settled.

        No kill fail-safe: a transient DB or Watch failure must not kill a user's command,
        whose own wrapper deadline still bounds it. ``stop_recovery`` ends the retries.
        """
        for attempt in itertools.count():
            delay = _RECOVERY_RETRY_DELAYS_S[attempt] if attempt < len(_RECOVERY_RETRY_DELAYS_S) else (
                _RECOVERY_RETRY_PERIOD_S
            )
            await asyncio.sleep(delay)
            try:
                failed = await self._recover_round()
            except Exception:
                logger.exception("Avibe Agent recovery pass failed; retrying")
                continue
            if not failed:
                logger.info("Avibe Agent recovery settled every Session")
                return
            logger.error("Avibe Agent recovery could not settle Sessions %s; retrying", ", ".join(failed))

    def stop_recovery(self) -> None:
        """Stop retrying recovery (service stop, or the backend being disabled)."""
        if self._recovery_task is not None and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._recovery_task = None

    async def _recover_open_tool_calls(self) -> list[str]:
        """Settle every candidate Session's open tool calls (recovery.md T2); return those that failed.

        Eager, not at the Session's next message: a running foreground job is handed to
        its Watch now (J6), so no command outlives its Turn without an owner. A Session a
        run holds is skipped: its own resume settles open calls first. Settlement is
        idempotent, so a Session that failed is simply tried again.
        """
        failed: list[str] = []
        for session_id in await asyncio.to_thread(self._sessions_with_open_tail):
            async with self._held(session_id, wait=False) as runtime:
                if runtime is None:
                    continue
                try:
                    await self._settle_open_calls(runtime)
                except Exception:
                    logger.exception("Avibe Agent tool-call recovery failed for Session %s", session_id)
                    failed.append(session_id)
        return failed

    def _prune_jobs_soon(self) -> None:
        """J5 while the service runs: runs create jobs, so a run's end prunes, at most hourly, off the Turn."""
        if self._pruned_at is not None and time.monotonic() - self._pruned_at < _JOB_PRUNE_INTERVAL_S:
            return
        if self._prune_task is not None and not self._prune_task.done():
            return
        self._prune_task = asyncio.create_task(self._prune_settled_jobs(), name="avibe-agent-job-prune")

    async def _prune_settled_jobs(self) -> None:
        """J5: remove finished jobs whose call has a durable result and whose Watch settled."""
        self._pruned_at = time.monotonic()
        try:
            suite = self._tools()
            if suite.prune is not None:
                removed = await asyncio.to_thread(suite.prune, self._job_call_settled)
                if removed:
                    logger.info("Avibe Agent pruned %d settled job(s)", len(removed))
        except Exception:
            logger.exception("Avibe Agent job pruning failed")

    def _job_call_settled(self, meta: Any) -> bool:
        """Whether the job's tool call has a durable ``tool_result`` row (the adapter's half of J5).

        Only a result committed after the job was created counts: a provider may reuse
        a tool-call id, and an earlier call's result does not settle a later one. The
        same rule as T2's job lookup (``_settle_open_calls``).
        """
        from sqlalchemy import func

        session_id, call_id = str(meta.get("session_id") or ""), str(meta.get("tool_call_id") or "")
        created = instant(meta.get("created_at"))
        if not session_id or not call_id or created is None:
            return False
        with self._engine.connect() as conn:
            committed = conn.execute(
                select(agent_events.c.created_at).where(
                    agent_events.c.session_id == session_id,
                    agent_events.c.event_type == "tool_result",
                    agent_events.c.context_seq.is_not(None),
                    func.json_extract(agent_events.c.content_json, "$.message.tool_call_id") == call_id,
                )
            ).scalars()
            return any((moment := instant(value)) is not None and moment >= created for value in committed)

    async def shutdown_runtime(self) -> None:
        """Disabling the backend ends its runs; the rolling refresh drains Turns before this."""
        self.stop_recovery()
        for runtime in list(self._runtimes.values()):
            if runtime.run is not None:
                runtime.run.stop_requested = True
                if runtime.run.agent is not None:
                    runtime.run.agent.abort("backend disabled")

    async def prepare_resume_binding(self, *, base_session_id: str, session_key: str, working_path: str) -> None:
        """Nothing to prepare: the transcript is the Session's own rows, read at the next run."""
        return None

    def backend_alive(self, context: Any) -> Optional[bool]:
        runtime = self._runtimes.get(self._session_id(context) or "")
        return True if runtime is not None and runtime.run is not None else None

    # --- steering (core.agent_steering) --------------------------------------

    def steering_native_turn_id(self, target: ActiveSteerTarget) -> Optional[str]:
        run = self._run_for(self._session_id(target.context), target.logical_turn_id)
        return run.native_turn_id if run is not None else None

    async def steer_active_turn(self, request: SteerRequest, target: ActiveSteerTarget) -> SteerResult:
        run = self._run_for(request.target_session_id, request.expected_logical_turn_id)
        if run is None or request.expected_native_turn_id != run.native_turn_id:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="not_active")
        self._steering.add(request.attempt_id)
        try:
            return await self._steer(run, request)
        finally:
            self._steering.discard(request.attempt_id)

    async def _steer(self, run: _Run, request: SteerRequest) -> SteerResult:
        try:
            message_id = await asyncio.to_thread(self._attempt_leader_id, request.attempt_id)
            if message_id is None:
                return steer_result(SteerOutcome.REFUSED, reason="attempt_unknown")
            message = await self._render_input(
                request.target_session_id, request.text, request.files, request.input_metadata
            )
        except Exception:
            logger.exception("Avibe Agent could not prepare a steer for Session %s", request.target_session_id)
            return steer_result(SteerOutcome.REFUSED, reason="preparation_failed")
        if run.agent is not None and await run.agent.steer(AgentInput(message_id, message)):
            run.accepted_steers.add(request.attempt_id)
            return steer_result(SteerOutcome.ACCEPTED, turn_id=run.turn_id)
        return steer_result(SteerOutcome.REFUSED, reason="run_closed")

    def reconciliation_steer_target(self, request: SteerReconcileRequest) -> ActiveSteerTarget:
        return ActiveSteerTarget(
            runtime_key="",
            logical_turn_id=request.expected_logical_turn_id,
            context=None,
            agent_request=None,
            agent=self,
        )

    async def reconcile_steer_attempt(self, request: SteerReconcileRequest, target: Any) -> SteerResult:
        """Evidence about a prior steer attempt, as the Codex and OpenCode reconcilers give it.

        ACCEPTED when its row was consumed or the live run accepted it; UNKNOWN only
        while this instance has the attempt's live call in flight (that call owns the
        receipt); otherwise NOT_ACTIVE. A live call that starts after a NOT_ACTIVE finds
        its attempt settled and refuses (``_steer``), and the Turn owner fences any late
        receipt for an attempt it no longer has, so a second negative changes nothing.
        """
        message_id = await asyncio.to_thread(self._attempt_leader_id, request.attempt_id)
        if message_id is not None and await asyncio.to_thread(self._consumed, message_id):
            return steer_result(SteerOutcome.ACCEPTED, turn_id=request.expected_logical_turn_id)
        run = self._run_for(request.target_session_id, request.expected_logical_turn_id)
        if (
            run is not None
            and run.native_turn_id == request.expected_native_turn_id
            and request.attempt_id in run.accepted_steers
        ):
            # The accepted steer is still queued in the live run that owns it.
            return steer_result(SteerOutcome.ACCEPTED, turn_id=run.turn_id)
        if request.attempt_id in self._steering:
            return steer_result(SteerOutcome.UNKNOWN, reason="in_progress")
        return steer_result(SteerOutcome.NOT_ACTIVE, reason="no_live_call")

    # --- one run ---------------------------------------------------------------

    async def _preflight(self, turn: _Run) -> HubModelRouter:
        """Resolve the route before the input is written; the run's first model call reuses it.

        A later attempt resolves again (C-6 item 3); a Model Hub refusal there is kept on
        the Turn, so its failure shows the Hub's copy, as a refusal at preflight does.
        """
        from modules.agents.model_hub import bind_launch, launch_refusal_copy, resolve_model_hub_launch

        request, session_id = turn.request, turn.session_id

        model = request.subagent_model or request.vibe_agent_model
        if not model:
            raise ValueError("The Avibe Agent has no model selected.")
        context = request.context

        async def resolve() -> ModelSelection:
            try:
                launch = await resolve_model_hub_launch(
                    self.controller, BACKEND, model, process_scope=f"{BACKEND}:{session_id}", context=context
                )
            except Exception as error:
                turn.refusal = launch_refusal_copy(self.controller, error)
                raise
            bind_launch(context, launch)
            return selection_from_hop(launch.to_hop_resolution(), gateway_base_url=launch.gateway_base_url)

        return HubModelRouter(resolve, self._providers, first=await resolve())

    def _start_agent(self, turn: _Run, cwd: str, sections: str, environment: Mapping[str, str]) -> None:
        """The Turn's loop, over its router, tools, system prompt, and the environment its commands run in."""
        request, session_id = turn.request, turn.session_id
        suite = self._tools()
        agent = Agent(
            session_id=session_id,
            models=turn.router,
            tools=(),
            hooks=(),
            store=self.store,
            jobs=suite.jobs,
            cwd=cwd,
            env=environment,
            reasoning_effort=request.subagent_reasoning_effort or request.vibe_agent_reasoning_effort,
        )
        # Job-backed tools receive the loop's tracking wrapper, so Stop kills foreground commands.
        tools = tuple(suite.create_tools(agent.jobs, self.media.image_sink(session_id)))
        agent.set_tools(tools)
        agent.system = system_prompt([tool.spec.name for tool in tools], sections)
        turn.agent, turn.cwd = agent, cwd

    async def _on_event(self, run: _Run, event: AgentEvent) -> None:
        if isinstance(event, MessageCommitted):
            if event.final:
                # Delivered at the end of the run, which decides how the Turn settles.
                run.final_row = event.message_id
            else:
                await self._emit_narration(run, event.message_id)
        elif isinstance(event, ToolStarted):
            await self._emit_tool_started(run, event)
        elif isinstance(event, AgentError):
            run.errors.append((event.kind, event.message))
        elif isinstance(event, RunEnded):
            run.reason = event.reason

    async def _settle(self, run: _Run) -> None:
        """Settle the Turn from the run's outcome (loop-control.md section 6).

        A committed final row is the Turn's outcome and result text, and it always
        settles through the result path, which writes that row's display and type as
        for the other backends; there is no separate failure notice for it. The row's
        own outcome is ``final_outcome`` (the rule its type was committed by); the live
        run adds only the failures it alone can observe after the commit. A Stop that
        arrives after the final row committed loses the race, as it does for the Codex
        backend.
        """
        run.settled = True
        request, context = run.request, run.request.context
        reason = run.reason or "error"
        kind, diagnostic = run.errors[0] if run.errors else (None, reason)
        final = run.responses.get(run.final_row) if run.final_row else None
        if final is not None:
            if run.stop_requested and reason == "aborted":
                reason = "completed"
            run_failed_after_commit = reason not in _COMPLETED
            failed = final_outcome(final) == "failed" or run_failed_after_commit
            body = self._display_source(final, final=True)
            if failed and not body.strip():
                # A silent final whose run failed after the commit: like a final without text
                # of its own (``_display_source``), its row carries the explanation.
                body = error_text(kind or "empty_response", self._language(), reason=reason)
            if failed:
                await self.record_model_hub_native_failure(context, diagnostic or (kind or "failed final"))
            if body.strip():
                # The same result path as the other backends; the row is already the message.
                await self.emit_result_message(
                    context,
                    body,
                    subtype="error" if failed else "success",
                    started_at=request.started_at,
                    request=request,
                    output=replace(terminal_output_for(request), persisted_row_id=run.final_row),
                )
            else:
                # A reply the model chose to keep silent.
                await self.controller.emit_agent_message(
                    context, "result", "", level="silent", output=terminal_output_for(request)
                )
            return
        if run.stop_requested and reason == "aborted":
            # The stop receipt IM shows, as for the other backends.
            await self._remove_ack_reaction(request, terminal_emoji=STOPPED_REACTION_EMOJI)
            await self.controller.emit_agent_message(
                context, "result", "", level="silent", output=stop_output_for(request)
            )
            return
        if reason in _COMPLETED and kind is None:
            # The run ended without a final reply by design (a terminating tool, a hook end).
            await self.controller.emit_agent_message(
                context, "result", "", level="silent", output=terminal_output_for(request)
            )
            return
        await self._fail(request, kind, diagnostic, reason=reason, refusal=run.refusal)

    async def _continuing_input(self, run: _Run) -> Optional[AgentInput]:
        """The input that continues a Turn whose run ended by design with inputs it accepted.

        An accepted steer belongs to the Turn that accepted it. A run that a terminating
        tool or a hook ``end`` closed before draining its queue therefore runs again for
        them: the earlier ones enter the context, the last starts the next run. A
        stopped or failed run continues nothing (``_admit_returned_inputs``).
        """
        if run.stop_requested or run.reason not in _COMPLETED:
            return None
        pending = await run.agent.take_pending_inputs()
        if not pending:
            return None
        for item in pending[:-1]:
            await self.store.consume_input(run.session_id, item.message_id, item.message)
        run.reason = None
        return pending[-1]

    async def _admit_returned_inputs(self, run: _Run) -> None:
        """Inputs a stopped or failed run accepted but did not consume enter the context (T3).

        They belong to the Turn that accepted them and share its outcome, as a message
        merged into another backend's stopped or failed turn does; the next Turn
        answers them with the full context.
        """
        if run.agent is None:
            return
        try:
            for item in await run.agent.take_pending_inputs():
                await self.store.consume_input(run.session_id, item.message_id, item.message)
        except Exception:
            logger.exception("Avibe Agent could not admit returned inputs for Session %s", run.session_id)

    # --- resume (recovery.md T2, T3) -------------------------------------------

    async def _resume(self, runtime: _SessionRuntime) -> None:
        session_id = runtime.session_id
        # T2 before every run: idempotent, and a Turn that waited behind an aborted one
        # finds the call that abort left open on the same, still-held runtime.
        await self._settle_open_calls(runtime)
        if not runtime.cwd:
            runtime.cwd = await asyncio.to_thread(self._session_workdir, session_id)
        for message_id, text_value, files, delivery in await asyncio.to_thread(self._unconsumed_inputs, session_id):
            message = await self._render_input(session_id, text_value, files, await self._input_metadata(delivery))
            await self.store.consume_input(session_id, message_id, message)

    async def _input_metadata(self, delivery: dict[str, Any]) -> Optional[AgentInputMetadata]:
        """A recovered input's sender facts, from the owner the live steer path uses (T3).

        ``SessionTurnManager`` rebuilds the Delivery's context, including a scheduled
        or agent-authored input's provenance, and asks the message handler, so the
        input renders after a restart exactly as it would have live.
        """
        owner = getattr(getattr(self.controller, "session_turns", None), "_steer_input_metadata", None)
        if not callable(owner):
            return None
        try:
            return await owner([delivery])
        except Exception:
            # The input is admitted regardless (T3 never drops one): a recovered input whose
            # sender facts cannot be rebuilt must not stop every later Turn of the Session.
            logger.exception("Avibe Agent could not rebuild sender facts for input %s", delivery.get("id"))
            return None

    async def _settle_open_calls(self, runtime: _SessionRuntime) -> None:
        """T2: one committed result for every open tool call, chosen from its job's state."""
        session_id = runtime.session_id
        open_calls = open_tool_calls(await self.store.load(session_id))
        if not open_calls:
            return
        suite = self._tools()

        def evidence() -> tuple[list[ContextEntry], dict[tuple[str, str], str]]:
            # A call is its instance, the owning response plus its id (providers reuse ids).
            # A call this Session inherited open takes the result its owner committed, matched
            # in context order. Only a job file falls back to the clock: its job started after
            # the owning response committed (one rule with J5).
            committed = self._committed_at([owner.row_id for owner, _ in open_calls])
            inherited, found = [], {}
            with self._engine.connect() as conn:
                for owner, call in open_calls:
                    if owner.session_id != session_id:
                        result = source_tool_result(conn, owner.session_id, owner.context_seq, call.id)
                        if result is not None:
                            inherited.append(result)
                            continue
                    since = committed.get(owner.row_id)
                    job_id = suite.find_job(owner.session_id, call.id, created_since=since)
                    if job_id is not None:
                        found[(owner.session_id, call.id)] = job_id
            return inherited, found

        # Each job lookup lists the jobs directory: one pass, off the event loop.
        inherited, job_ids = await asyncio.to_thread(evidence)
        # A tool result is the Agent's that made the call: its owning response's author, not
        # the Turn writing it now (or none, at startup) or the Session's current selection.
        # Only the last response can hold open calls, so they share one owner.
        owner_agent = await asyncio.to_thread(self._author_of, open_calls[0][0].row_id)
        with self.store.writing_as(session_id, owner_agent):
            for result in inherited:
                await self.store.append_tool_result(
                    session_id, result.message, details=dict(result.payload.get("details") or {})
                )
            await settle_open_calls(
                session_id=session_id,
                store=self.store,
                jobs=suite.jobs,
                job_ids=job_ids,
                render_result=suite.render_recovered,
            )

    def _author_of(self, row_id: str) -> Optional[str]:
        with self._engine.connect() as conn:
            return conn.execute(select(messages.c.author_name).where(messages.c.id == row_id)).scalar()

    def _committed_at(self, row_ids: Sequence[str]) -> dict[str, datetime]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(messages.c.id, messages.c.created_at).where(messages.c.id.in_(set(row_ids))))
            return {row_id: moment for row_id, created in rows if (moment := instant(created)) is not None}

    def _unconsumed_inputs(self, session_id: str) -> list[tuple[str, str, list[FileAttachment], dict[str, Any]]]:
        """Inputs accepted into an ``avibe`` Turn but never consumed, in acceptance order.

        Only Turns this backend ran count, so history from an earlier backend of the
        same Session never enters the context.
        """
        from core.workbench_media import file_attachments_from_specs, resolve_attachment_specs

        with self._engine.connect() as conn:
            rows = conn.execute(
                select(
                    messages.c.id,
                    messages.c.content_text,
                    messages.c.content_json,
                    message_deliveries.c.id.label("delivery_id"),
                    message_deliveries.c.turn_role,
                    session_turns.c.dispatch_text.label("turn_dispatch_text"),
                )
                .select_from(
                    messages.join(message_deliveries, message_deliveries.c.message_id == messages.c.id).join(
                        session_turns, session_turns.c.id == message_deliveries.c.turn_id
                    )
                )
                .where(
                    and_(
                        messages.c.session_id == session_id,
                        messages.c.context_seq.is_(None),
                        messages.c.type.in_(INPUT_TYPES),
                        message_deliveries.c.state == "accepted",
                        session_turns.c.backend == BACKEND,
                    )
                )
                .order_by(message_deliveries.c.materialized_at, message_deliveries.c.turn_position, messages.c.id)
            ).mappings().all()
            inputs = []
            seen: set[str] = set()
            for row in rows:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                content = _json_object(row["content_json"])
                specs = resolve_attachment_specs(
                    conn, session_id=session_id, attachments=list(content.get("attachments") or [])
                )
                delivery = delivery_store.get_delivery(conn, row["delivery_id"])
                # A Turn's initial input is sent as the Turn's dispatch text, which the Turn owner
                # derived from its Delivery batch (attachment notes, merged scheduled inputs) and
                # keeps after acceptance; the row's display text omits that.
                text_value = (
                    row["turn_dispatch_text"]
                    if row["turn_role"] == "initial" and row["turn_dispatch_text"]
                    else row["content_text"] or ""
                )
                inputs.append((row["id"], text_value, list(file_attachments_from_specs(specs) or ()), delivery))
        return inputs

    # --- rendering ---------------------------------------------------------------

    async def _render_input(
        self,
        session_id: str,
        message: str,
        files: Optional[Sequence[FileAttachment]],
        metadata: Optional[AgentInputMetadata],
    ) -> UserMessage:
        """The input as the model sees it: identity and time prefix, attachments, images.

        Images become context images through immutable snapshots; other files are
        listed by path, as the native backends list them. The environment block is
        added when the loop consumes the input (``AdapterTranscriptStore``).
        """
        images = []
        listed = []
        for attachment in files or ():
            if not attachment.local_path:
                continue
            mime = (attachment.mimetype or "").split(";", 1)[0].strip().lower()
            if mime in IMAGE_MIME_TYPES:
                try:
                    images.append(
                        await self.media.snapshot_file(
                            attachment.local_path, mime, session_id=session_id, name=attachment.name or None
                        )
                    )
                    continue
                except OSError:
                    # Unreadable now: listed by path, as the native backends pass every attachment.
                    logger.warning("Avibe Agent lists an image it could not snapshot: %s", attachment.local_path)
            size = f", {attachment.size} bytes" if attachment.size else ""
            listed.append(f"- File: {attachment.local_path} ({attachment.mimetype}{size})")
        body = message or ""
        if listed:
            body = "\n".join([body, "", "[User Attachments]", *listed]) if body.strip() else "\n".join(
                ["[User Attachments]", *listed]
            )
        rendered = self.render_input(body, metadata)
        content = ([text_block(rendered)] if rendered or not images else []) + images
        return UserMessage(content=tuple(content))

    def _display_source(self, message: AssistantMessage, *, final: bool) -> str:
        """The text a surface renders a response from: its text blocks without silent blocks.

        Also the row's commit-time display (the store's renderer). A final without
        text of its own (``final_outcome`` failed) is explained by localized copy:
        the refusal or safety explanation, else the empty-reply one, so the row
        never shows nothing for a failed Turn (loop-control.md section 2).
        """
        value = render_text(message)
        if final and not value.strip():
            kind = message.stop_reason if message.stop_reason in _EXPLAINED_STOPS else "empty_response"
            return error_text(kind, self._language())
        return strip_silent_blocks(value)

    async def _avibe_sections(self, request: AgentRequest, cwd: str) -> tuple[str, Optional[dict]]:
        """Avibe's injected prompt sections and the skill catalog they offered.

        Built off the event loop as the other backends build them: skill resolution
        scans the filesystem and may run ``claude plugin list``.
        """
        from core.managed_skills import managed_skill_claude_cli_path, managed_skill_project_base
        from core.system_prompt_injection import build_system_prompt_injection, get_enabled_agents_for_prompt

        context = request.context
        skill_catalog_sink: list[dict] = []

        def build() -> str:
            # Every lookup runs here, off the loop: the enabled-Agent list reads SQLite.
            return build_system_prompt_injection(
                agent_instructions=request.vibe_agent_system_prompt or "",
                backend=BACKEND,
                include_quick_replies=getattr(self.config, "reply_enhancements", True)
                and context.platform != "wechat",
                context=context,
                fallback_platform=context.platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                skills_cwd=cwd or None,
                skills_project_base=managed_skill_project_base(context),
                skills_claude_cli_path=managed_skill_claude_cli_path(self.config),
                skill_catalog_sink=skill_catalog_sink,
            )

        sections = await asyncio.to_thread(build)
        return sections, (skill_catalog_sink[0] if skill_catalog_sink else None)

    def _turn_environment(self, request: AgentRequest, cwd: str) -> dict[str, str]:
        """The environment a Turn's commands run in, from the owners the other backends' shells use.

        The service's environment without its own caller provenance, then this Turn's
        caller context (``AVIBE_SESSION_ID``, ``AVIBE_CALLER_*``, so a ``vibe`` call in a
        command records where it came from), the managed-skill bindings, and verified
        Git on ``PATH``, composed as the Codex backend composes its shell environment.
        The Model Hub gateway token stays in the adapter.
        """
        from core.caller_context import caller_env_for_platform_payload, environment_without_caller_context
        from core.git_runtime import prepend_vendored_git_to_path
        from core.managed_skills import (
            managed_skill_claude_cli_path,
            managed_skill_environment,
            managed_skill_project_base,
        )

        context = request.context
        environment = environment_without_caller_context()
        environment.update(
            caller_env_for_platform_payload(
                getattr(context, "platform_specific", None),
                message=context,
                fallback_platform=getattr(self.config, "platform", None),
            )
        )
        environment.update(
            managed_skill_environment(
                cwd or None,
                project_base=managed_skill_project_base(context),
                claude_cli_path=managed_skill_claude_cli_path(self.config),
            )
        )
        prepend_vendored_git_to_path(environment, base_env=environment, working_dir=cwd or None)
        return environment

    def _environment(self, session_id: str) -> dict[str, str]:
        runtime = self._runtimes.get(session_id)
        cwd = ((runtime.run.cwd if runtime.run is not None else "") or runtime.cwd) if runtime is not None else ""
        return current_environment(
            cwd, self._watch_lines(session_id), include_time=getattr(self.config, "include_time_info", True)
        )

    def _watch_lines(self, session_id: str) -> list[str]:
        service = getattr(self.controller, "watch_service", None)
        store = getattr(service, "store", None)
        list_watches = getattr(store, "list_watches", None)
        if not callable(list_watches):
            return []
        try:
            watches = list_watches()
        except Exception:
            logger.debug("Avibe Agent could not list Watches", exc_info=True)
            return []
        lines = []
        for watch in watches:
            if getattr(watch, "session_id", None) != session_id or not getattr(watch, "enabled", False):
                continue
            # Model context: a Watch's id, name and kind only. Its command can carry a
            # credential, and the block is persisted and sent to the provider.
            kind = "job" if getattr(watch, "job_target", None) else "command"
            name = " ".join(str(getattr(watch, "name", None) or "").split())
            lines.append(f'{watch.id} "{name}" {kind} running' if name else f"{watch.id} {kind} running")
        return lines

    # --- output helpers ------------------------------------------------------

    async def _emit_narration(self, run: _Run, row_id: str) -> None:
        """A non-final response through the same process path as the other backends' narration."""
        message = run.responses.get(row_id)
        body = self._display_source(message, final=False) if message is not None else ""
        if not body.strip():
            return
        try:
            await self.controller.emit_agent_message(
                run.request.context,
                "assistant",
                body,
                output=MessageOutput(completes_turn=False, completes_run=False, persisted_row_id=row_id),
            )
        except Exception:
            logger.exception("Avibe Agent could not show narration %s", row_id)

    async def _emit_tool_started(self, run: _Run, event: ToolStarted) -> None:
        call = run.tool_calls.get(event.tool_call_id)
        arguments = dict(call.arguments) if call is not None else {}
        formatter = self._get_formatter(run.request.context)
        relative = _relative_to(run.cwd)
        try:
            await self.controller.emit_agent_message(
                run.request.context,
                "toolcall",
                formatter.format_toolcall(event.name, arguments, get_relative_path=relative),
                status_label=formatter.format_toolcall_label(event.name, arguments, get_relative_path=relative),
            )
        except Exception:
            logger.exception("Avibe Agent could not show tool %s", event.name)

    def _on_response(self, session_id: str, row_id: str, message: AssistantMessage) -> None:
        """A response committed for the Session's run: keep what its delivery and tool events need."""
        run = self._run_for(session_id, None)
        if run is None:
            return
        run.responses[row_id] = message
        run.tool_calls.update({call.id: call for call in message.tool_calls})
        self._note_tokens(run, message)

    def _note_tokens(self, run: _Run, message: AssistantMessage) -> None:
        usage = message.usage
        note = getattr(self.controller, "note_session_tokens", None)
        if usage is None or not callable(note):
            return
        total = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.output_tokens
        try:
            note(run.request.context, total=total)
        except Exception:
            logger.debug("Avibe Agent token note failed", exc_info=True)

    async def _fail(
        self,
        request: AgentRequest,
        kind: Optional[str],
        diagnostic: str,
        *,
        reason: Optional[str] = None,
        cause: Optional[BaseException] = None,
        refusal: Optional[str] = None,
    ) -> None:
        """A failed Turn's notice; a Model Hub ``refusal`` copy takes precedence, as at preflight."""
        # A failure after Model Hub served the route replaces that served attempt, as for the other backends.
        await self.record_model_hub_native_failure(request.context, diagnostic or (kind or "error"))
        await emit_backend_failure(
            self.controller,
            request.context,
            BACKEND,
            diagnostic or (kind or "error"),
            display_text=f"❌ {refusal or error_text(kind, self._language(), reason=reason)}",
            request=request,
            cause=cause,
        )

    async def _fail_preflight(self, request: AgentRequest, error: BaseException) -> None:
        from modules.agents.model_hub import launch_refusal_copy

        logger.warning("Avibe Agent could not resolve its model route: %s", error)
        refusal = launch_refusal_copy(self.controller, error)
        # Recorded on every start failure, as the other backends do: a no-op unless a launch is bound.
        await self.record_model_hub_native_failure(request.context, str(error))
        display = f"❌ {refusal}" if refusal is not None else f"❌ {error_text('generic', self._language())}"
        await emit_backend_failure(
            self.controller,
            request.context,
            BACKEND,
            str(error),
            display_text=display,
            request=request,
            cause=error,
        )

    # --- lookups ---------------------------------------------------------------

    def _tools(self) -> ToolSuite:
        if self._tool_suite is None:
            # Jobs live in Watch's agent_jobs_dir(), where vibe stop and the job-Watch sweep look.
            self._tool_suite = local_tool_suite()
        return self._tool_suite

    @asynccontextmanager
    async def _held(
        self, session_id: str, *, wait: bool = True, run: Optional[_Run] = None
    ) -> AsyncIterator[Optional[_SessionRuntime]]:
        """Hold the Session's writer lock; the last holder retires its in-memory state.

        With ``wait=False`` a Session someone already holds yields ``None`` instead of
        waiting: startup recovery never blocks behind a run, which does the same work
        at its own resume. A Turn's ``run`` is published before the lock is awaited,
        so a Stop is honored even while the Turn waits for it.
        """
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            runtime = self._runtimes[session_id] = _SessionRuntime(session_id)
        if not wait and runtime.lock.locked():
            yield None
            return
        runtime.holders += 1
        if run is not None:
            runtime.run = run
        try:
            async with runtime.lock:
                yield runtime
        finally:
            if run is not None and runtime.run is run:
                runtime.run = None
            runtime.holders -= 1
            if runtime.holders == 0 and runtime.run is None and self._runtimes.get(session_id) is runtime:
                del self._runtimes[session_id]
                self.store.forget(session_id)

    def _run_for(self, session_id: Optional[str], turn_id: Optional[str]) -> Optional[_Run]:
        runtime = self._runtimes.get(session_id or "")
        run = runtime.run if runtime is not None else None
        if run is None or (turn_id is not None and run.turn_id != turn_id):
            return None
        return run

    @staticmethod
    def _session_id(context: Any) -> Optional[str]:
        return BaseAgent._reserved_agent_session_id(context) or BaseAgent._session_id_from_context(context)

    def _language(self) -> str:
        return str(getattr(self.config, "language", "en") or "en")

    def _attempt_leader_id(self, attempt_id: str) -> Optional[str]:
        """The ``messages.id`` a steer attempt materializes as: its first Delivery."""
        if not attempt_id:
            return None
        with self._engine.connect() as conn:
            rows = delivery_store.attempt_deliveries(conn, attempt_id)
        return str(rows[0]["id"]) if rows else None

    def _consumed(self, message_id: str) -> bool:
        with self._engine.connect() as conn:
            seq = conn.execute(select(messages.c.context_seq).where(messages.c.id == message_id)).scalar()
        return seq is not None

    def _sessions_with_open_tail(self) -> list[str]:
        """Sessions whose context can hold an open tool call: startup T2's candidates.

        Every run settles earlier open calls before its next model call, so only a
        Session's last response can have one: it has tool calls and fewer results after
        it. Candidates come from Avibe context rows (only this backend writes
        ``context_seq``), whatever Agent the Session is routed to now: a Session switched
        away mid-run still owns its jobs.
        """
        from sqlalchemy import func

        with self._engine.connect() as conn:
            last_response = (
                select(messages.c.session_id, func.max(messages.c.context_seq).label("seq"))
                .where(
                    messages.c.session_id.is_not(None),
                    messages.c.context_seq.is_not(None),
                    messages.c.type.in_(RESPONSE_TYPES),
                )
                .group_by(messages.c.session_id)
                .subquery()
            )
            responses = conn.execute(
                select(messages.c.session_id, messages.c.context_seq, messages.c.content_json).join(
                    last_response,
                    and_(
                        messages.c.session_id == last_response.c.session_id,
                        messages.c.context_seq == last_response.c.seq,
                    ),
                )
            ).all()
            candidates = []
            for session_id, seq, raw in responses:
                message = (_json_object(raw).get("model") or {}).get("message") or {}
                calls = [block for block in message.get("content") or () if isinstance(block, dict)
                         and block.get("type") == "tool_call"]
                if not calls:
                    continue
                settled = conn.execute(
                    select(func.count()).select_from(agent_events).where(
                        agent_events.c.session_id == session_id,
                        agent_events.c.event_type == "tool_result",
                        agent_events.c.context_seq > seq,
                    )
                ).scalar()
                if settled < len(calls):
                    candidates.append(session_id)
        return sorted(candidates)

    def _session_workdir(self, session_id: str) -> str:
        with self._engine.connect() as conn:
            workdir = conn.execute(
                select(agent_sessions.c.workdir).where(agent_sessions.c.id == session_id)
            ).scalar_one_or_none()
        return str(workdir or "")


def _json_object(raw: Any) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

