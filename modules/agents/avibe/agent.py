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
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Sequence

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from config import paths
from config.platform_registry import get_platform_descriptor
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
from core.reply_enhancer import process_reply, strip_silent_blocks
from modules.agents.avibe.errors import error_text
from modules.agents.avibe.media import MediaSnapshots
from modules.agents.avibe.models import HubModelRouter, ProviderFactory, registry_providers, selection_from_hop
from modules.agents.avibe.prompt import current_environment, system_prompt
from modules.agents.avibe.store import AdapterTranscriptStore
from modules.agents.avibe.tools import ToolSuite, local_tool_suite
from modules.agents.base import AgentRequest, BaseAgent
from modules.im.base import FileAttachment
from storage import message_deliveries as delivery_store
from storage import messages_service
from storage.agent_transcript import (
    FINAL_TYPES,
    INPUT_TYPES,
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
# When this process loaded the backend, before any of its runs: a steer attempt
# opened earlier belongs to a previous process, so no live call can settle it.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)
_JOB_PRUNE_INTERVAL_S = 3600.0


def _instant(value: Any) -> Optional[datetime]:
    """A stored UTC timestamp (``...Z``) as an aware ``datetime``, or ``None``."""
    text = str(value or "").strip()
    try:
        instant = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=timezone.utc)


def _microsecond_text(instant: datetime) -> str:
    """The fixed-width form ``agent_events.created_at`` is written in, so the two compare as text."""
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass
class _Run:
    agent: Agent
    router: HubModelRouter
    request: AgentRequest
    session_id: str
    turn_id: str
    cwd: str
    started_at: float = field(default_factory=time.monotonic)
    reason: Optional[str] = None
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
        return f"{BACKEND}:{self.turn_id}"


@dataclass
class _SessionRuntime:
    """A Session's writer lock and run while some caller holds it; retired when idle."""

    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recovered: bool = False
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
        # J5 while the service runs: when the jobs directory was last pruned, and the pass in flight.
        self._pruned_at: Optional[float] = None
        self._prune_task: Optional[asyncio.Task] = None

    # --- BaseAgent -----------------------------------------------------------

    async def handle_message(self, request: AgentRequest) -> None:
        context = request.context
        payload = context.platform_specific or {}
        session_id = self.ensure_agent_session_id(request)
        turn_id = str(payload.get("turn_token") or "").strip()
        input_id = str(payload.get("delivery_id") or "").strip()
        if not session_id or not turn_id or not input_id:
            await self._fail(request, "generic", "Avibe Agent turn has no Session, Turn, or input identity.")
            return
        async with self._held(session_id) as runtime:
            try:
                await self._resume(runtime)
            except Exception as error:
                logger.exception("Avibe Agent resume failed for Session %s", session_id)
                await self._fail(request, "generic", f"resume failed: {error}", cause=error)
                return
            cwd = request.working_path or runtime.cwd
            try:
                router = await self._preflight(request, session_id)
            except Exception as error:
                await self._fail_preflight(request, error)
                return
            run = self._new_run(request, session_id, turn_id, cwd, router, await self._avibe_sections(request, cwd))
            runtime.cwd = cwd
            runtime.run = run
            try:
                # Native acceptance materializes the input row the loop consumes.
                self.bind_agent_session_id(request, session_id)
                mark_backend_dispatch_attempted(context)
                self.mark_runtime_turn_started(context)
                message = await self._render_input(
                    session_id, request.message, request.files, request.input_metadata
                )
                agent_input: Optional[AgentInput] = AgentInput(input_id, message)
                while agent_input is not None:
                    if run.stop_requested:
                        # A Stop acknowledged before the loop could abort, such as while an
                        # input was still being prepared: no run starts. An input left
                        # unconsumed is admitted at the Session's next resume (T3).
                        run.reason = "aborted"
                        break
                    async for event in run.agent.run(agent_input, turn_id=turn_id):
                        await self._on_event(run, event)
                    agent_input = await self._continuing_input(run)
                await self._settle(run)
            finally:
                runtime.run = None
                await self._admit_returned_inputs(run)
                await run.router.aclose()
                self._prune_jobs_soon()

    async def handle_stop(self, request: AgentRequest) -> bool:
        session_id = self._session_id(request.context)
        runtime = self._runtimes.get(session_id or "")
        run = runtime.run if runtime is not None else None
        if run is None:
            request.stop_failure_reason = "not_active"
            return False
        run.stop_requested = True
        run.agent.abort("stopped by user")
        return True

    async def clear_sessions(self, session_key: str) -> int:
        cleared = 0
        for runtime in list(self._runtimes.values()):
            run = runtime.run
            if run is not None and run.request.session_key == session_key:
                run.stop_requested = True
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

    async def recover_runtime_state(self) -> None:
        """At startup or live registration: settle open tool calls and prune settled jobs (T2, J5).

        Never calls the model. T3 stays with each Session's resume.
        """
        await self._recover_open_tool_calls()

    async def _recover_open_tool_calls(self) -> None:
        """At startup, settle every Session's open tool calls before any run (recovery.md T2).

        Eager, not at the Session's next message: a running foreground job is handed to
        its Watch now (J6), so no command outlives its Turn without an owner. Settlement
        is idempotent, so a pass that failed can simply run again. Never calls the
        model; T3 stays with each Session's resume.
        """
        for session_id in await asyncio.to_thread(self._sessions_with_open_tail):
            async with self._held(session_id, wait=False) as runtime:
                if runtime is None:
                    # A run holds the Session; its own resume settles open calls first.
                    continue
                try:
                    await self._settle_open_calls(runtime)
                except Exception:
                    logger.exception("Avibe Agent startup tool-call recovery failed for Session %s", session_id)
        await self._prune_settled_jobs()

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
        created = _instant(meta.get("created_at"))
        if not session_id or not call_id or created is None:
            return False
        with self._engine.connect() as conn:
            return (
                conn.execute(
                    select(agent_events.c.id)
                    .where(
                        agent_events.c.session_id == session_id,
                        agent_events.c.event_type == "tool_result",
                        agent_events.c.context_seq.is_not(None),
                        func.json_extract(agent_events.c.content_json, "$.message.tool_call_id") == call_id,
                        agent_events.c.created_at >= _microsecond_text(created),
                    )
                    .limit(1)
                ).first()
                is not None
            )

    async def shutdown_runtime(self) -> None:
        """Disabling the backend ends its runs; the rolling refresh drains Turns before this."""
        for runtime in list(self._runtimes.values()):
            if runtime.run is not None:
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
        if await run.agent.steer(AgentInput(message_id, message)):
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

        ACCEPTED when its row was consumed or the live run accepted it; otherwise
        UNKNOWN, because the live attempt in this process owns its definitive
        negative receipt. Only an attempt a previous process opened is NOT_ACTIVE:
        no call can still settle it.
        """
        leader = await asyncio.to_thread(self._attempt_leader, request.attempt_id)
        message_id, opened_at = leader if leader is not None else (None, None)
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
        if opened_at is not None and opened_at < _PROCESS_STARTED_AT:
            return steer_result(SteerOutcome.NOT_ACTIVE, reason="previous_process")
        return steer_result(SteerOutcome.UNKNOWN, reason="no_attempt_evidence")

    # --- one run ---------------------------------------------------------------

    async def _preflight(self, request: AgentRequest, session_id: str) -> HubModelRouter:
        """Resolve the route before the input is written; the run's first model call reuses it."""
        from modules.agents.model_hub import bind_launch, resolve_model_hub_launch

        model = request.subagent_model or request.vibe_agent_model
        if not model:
            raise ValueError("The Avibe Agent has no model selected.")
        context = request.context

        async def resolve() -> ModelSelection:
            launch = await resolve_model_hub_launch(
                self.controller, BACKEND, model, process_scope=f"{BACKEND}:{session_id}", context=context
            )
            bind_launch(context, launch)
            return selection_from_hop(launch.to_hop_resolution(), gateway_base_url=launch.gateway_base_url)

        return HubModelRouter(resolve, self._providers, first=await resolve())

    def _new_run(
        self,
        request: AgentRequest,
        session_id: str,
        turn_id: str,
        cwd: str,
        router: HubModelRouter,
        sections: str,
    ) -> _Run:
        suite = self._tools()
        agent = Agent(
            session_id=session_id,
            models=router,
            tools=(),
            hooks=(),
            store=self.store,
            jobs=suite.jobs,
            cwd=cwd,
            reasoning_effort=request.subagent_reasoning_effort or request.vibe_agent_reasoning_effort,
        )
        # Job-backed tools receive the loop's tracking wrapper, so Stop kills foreground commands.
        tools = tuple(suite.create_tools(agent.jobs, self.media.image_sink(session_id)))
        agent.set_tools(tools)
        agent.system = system_prompt([tool.spec.name for tool in tools], sections)
        return _Run(agent, router, request, session_id, turn_id, cwd)

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
            if body.strip():
                # The same result path as the other backends; the row is already the message.
                await self.emit_result_message(
                    context,
                    body,
                    subtype="error" if failed else "success",
                    started_at=run.started_at,
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
        await self._fail(request, kind, diagnostic, reason=reason)

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
        try:
            for item in await run.agent.take_pending_inputs():
                await self.store.consume_input(run.session_id, item.message_id, item.message)
        except Exception:
            logger.exception("Avibe Agent could not admit returned inputs for Session %s", run.session_id)

    # --- resume (recovery.md T2, T3) -------------------------------------------

    async def _resume(self, runtime: _SessionRuntime) -> None:
        session_id = runtime.session_id
        if not runtime.recovered:
            await self._settle_open_calls(runtime)
            runtime.cwd = runtime.cwd or await asyncio.to_thread(self._session_workdir, session_id)
            runtime.recovered = True
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
        return await owner([delivery]) if callable(owner) else None

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

    def _committed_at(self, row_ids: Sequence[str]) -> dict[str, datetime]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(messages.c.id, messages.c.created_at).where(messages.c.id.in_(set(row_ids))))
            return {row_id: instant for row_id, created in rows if (instant := _instant(created)) is not None}

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
                inputs.append(
                    (row["id"], row["content_text"] or "", list(file_attachments_from_specs(specs) or ()), delivery)
                )
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
                images.append(
                    await self.media.snapshot_file(
                        attachment.local_path, mime, session_id=session_id, name=attachment.name or None
                    )
                )
            else:
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

    async def _avibe_sections(self, request: AgentRequest, cwd: str) -> str:
        """Avibe's injected prompt sections, built off the event loop as the other backends build them.

        Skill resolution scans the filesystem and may run ``claude plugin list``.
        """
        from core.managed_skills import managed_skill_claude_cli_path, managed_skill_project_base
        from core.system_prompt_injection import build_system_prompt_injection, get_enabled_agents_for_prompt

        context = request.context
        return await asyncio.to_thread(
            build_system_prompt_injection,
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
        )

    def _environment(self, session_id: str) -> dict[str, str]:
        runtime = self._runtimes.get(session_id)
        cwd = runtime.run.cwd if runtime is not None and runtime.run is not None else (runtime.cwd if runtime else "")
        return current_environment(cwd, self._watch_lines(session_id))

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
            label = getattr(watch, "shell_command", None) or getattr(watch, "name", None) or watch.id
            lines.append(f'{watch.id} "{label}" running')
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
        try:
            await self.controller.emit_agent_message(
                run.request.context,
                "toolcall",
                formatter.format_toolcall(event.name, arguments),
                status_label=formatter.format_toolcall_label(event.name, arguments),
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
    ) -> None:
        await emit_backend_failure(
            self.controller,
            request.context,
            BACKEND,
            diagnostic or (kind or "error"),
            display_text=f"❌ {error_text(kind, self._language(), reason=reason)}",
            request=request,
            cause=cause,
        )

    async def _fail_preflight(self, request: AgentRequest, error: BaseException) -> None:
        from modules.agents.model_hub import launch_refusal_copy

        logger.warning("Avibe Agent could not resolve its model route: %s", error)
        refusal = launch_refusal_copy(self.controller, error)
        if refusal is not None:
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
    async def _held(self, session_id: str, *, wait: bool = True) -> AsyncIterator[Optional[_SessionRuntime]]:
        """Hold the Session's writer lock; the last holder retires its in-memory state.

        With ``wait=False`` a Session someone already holds yields ``None`` instead of
        waiting: startup recovery never blocks behind a run, which does the same work
        at its own resume.
        """
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            runtime = self._runtimes[session_id] = _SessionRuntime(session_id)
        if not wait and runtime.lock.locked():
            yield None
            return
        runtime.holders += 1
        try:
            async with runtime.lock:
                yield runtime
        finally:
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
        leader = self._attempt_leader(attempt_id)
        return leader[0] if leader is not None else None

    def _attempt_leader(self, attempt_id: str) -> Optional[tuple[str, Optional[datetime]]]:
        """The attempt's first Delivery id, and when the attempt was opened."""
        if not attempt_id:
            return None
        with self._engine.connect() as conn:
            rows = delivery_store.attempt_deliveries(conn, attempt_id)
        if not rows:
            return None
        return str(rows[0]["id"]), _instant(rows[0].get("current_attempt_opened_at"))

    def _consumed(self, message_id: str) -> bool:
        with self._engine.connect() as conn:
            seq = conn.execute(select(messages.c.context_seq).where(messages.c.id == message_id)).scalar()
        return seq is not None

    def _sessions_with_open_tail(self) -> list[str]:
        """Avibe Sessions whose context does not end with a final reply: only these can hold an open call.

        Every run settles earlier open calls before it starts and commits its own results
        before its next model call, so a context ending in a ``result`` row has none.
        """
        from sqlalchemy import func

        own = select(agent_sessions.c.id).where(agent_sessions.c.agent_backend == BACKEND).subquery()
        with self._engine.connect() as conn:
            last: dict[str, int] = {}
            for table in (messages, agent_events):
                for session_id, seq in conn.execute(
                    select(table.c.session_id, func.max(table.c.context_seq))
                    .where(table.c.session_id.in_(select(own.c.id)), table.c.context_seq.is_not(None))
                    .group_by(table.c.session_id)
                ):
                    last[session_id] = max(seq, last.get(session_id, 0))
            finals = dict(
                conn.execute(
                    select(messages.c.session_id, func.max(messages.c.context_seq))
                    .where(
                        messages.c.session_id.in_(select(own.c.id)),
                        messages.c.context_seq.is_not(None),
                        messages.c.type.in_(FINAL_TYPES),
                    )
                    .group_by(messages.c.session_id)
                ).all()
            )
        return sorted(session_id for session_id, seq in last.items() if finals.get(session_id) != seq)

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

