"""``VibeyAgent``: the Avibe Agent engine (``core.agent_core``) as the ``vibey`` backend.

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
* runs every Turn with C-9 context management (``VibeyContextHost``), within
  the route's Model Hub limits;
* before any run of a Session in this process, under the Session writer lock,
  settles open tool calls (T2) and admits accepted inputs that were never
  consumed (T3); at startup it settles every Session's open calls eagerly,
  except a running command of a Turn not yet settled. The Turn owner then
  reruns the Turn a restart interrupted in place, as a transcript resume of
  itself (T4; ``vibey`` is a process-bound runtime), whose T2 waits for that
  command as its live call would, or settles the Turn after having it handed to
  a Watch; a run the service stop cut leaves its Turn open for that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Mapping, Optional, Sequence

import httpx
from sqlalchemy import and_, literal_column, or_, select, text as sql_text, union
from sqlalchemy.engine import Engine

from config import paths
from core.agent_core.agent.events import (
    NAMED_ERROR_KINDS,
    AgentError,
    AgentEvent,
    CompactionFailed,
    CompactionFinished,
    MessageCommitted,
    RunEnded,
    ToolFinished,
    ToolStarted,
)
from core.agent_core.agent.hooks import AgentInput
from core.agent_core.agent.loop import Agent
from core.agent_core.agent.preview import RequestPreview
from core.agent_core.agent.models import ModelSelection, RetryPolicy
from core.agent_core.agent.recovery import settle_open_calls
from core.agent_core.harness.context import ContextConfig, cache_cold
from core.agent_core.ai.provider import ModelRequest
from core.agent_core.harness.projection import open_tool_calls
from core.agent_core.hosted_continuations import pending_hosted
from core.agent_core.ai.request_artifacts import RequestArtifactStore, capture_model_requests
from core.agent_core.ai.transform import in_flight_position
from core.agent_core.tools.base import CallInstance
from core.agent_core.tools.paths import to_thread_joined
from core.agent_core.tools.jobs import call_instance
from core.agent_core.messages import (
    HOSTED_RENDER_EPOCH,
    HOSTED_RENDER_RULE,
    HOSTED_RESULT_TOOLS,
    HostedImage,
    IMAGE_MIME_TYPES,
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    UserMessage,
    has_reply,
    image_deliverable,
    message_from_dict,
    text as text_block,
)
from core.agent_input import AgentInputMetadata
from core.agent_session_context import session_model_settings, session_speed_for_context
from core.services.agent_steering import (
    ActiveSteerTarget,
    SteerOutcome,
    SteerReconcileRequest,
    SteerRequest,
    SteerResult,
    result as steer_result,
)
from core.backend_failure import emit_backend_failure
from core.agent_output import Narration, Reply, ToolActivity, TurnEnd, TurnOutcome, emit_output
from core.citations import CitationBundle, register_citations, register_text_citations
from core.hosted_activity import HostedActivity, hosted_line, native_activities, proxy_activity
from core.message_output import MessageOutput, terminal_output_for
from core.tool_activity import PROJECTION_KEY, PROJECTION_VERSION, SOURCE_KEY, relative_to as _relative_to, source_link
from core.native_dispatch_phase import mark_backend_dispatch_attempted
from core.processing_indicator import STOPPED_REACTION_EMOJI
from core.reply_enhancer import strip_silent_blocks
from core.skill_observability import accept_catalog
from modules.agents.vibey.context import (
    VibeyContextHost,
    budgeted,
    mark_skill_loads,
)
from modules.agents.vibey.errors import error_text
from core.prompt_registry import RenderedPromptBlock, join_prompt_blocks
from modules.agents.vibey.instructions import load_instruction_files, project_context
from modules.agents.vibey.prompt_snapshot import State as PromptState, SnapshotStore, candidate_prefix, decide, frozen_tools, route_identity
from modules.agents.vibey.media import MediaSnapshots
from modules.agents.vibey.models import HubModelRouter, ProviderFactory, registry_providers, selection_from_hop
from modules.agents.vibey.native_search_recovery import (
    SEARCH_TOOLS, interrupted_native_searches, response_recovery_metadata, should_switch, turn_recovery_facts,
)
from modules.agents.vibey.prompt import EnvironmentValue, current_environment, system_prompt
from modules.agents.vibey.store import AdapterTranscriptStore
from modules.agents.vibey.tools import ToolSuite, hosted_tools, local_tool_suite
from modules.agents.base import AGENT_RUNTIME_TURN_KEY, AgentRequest, BaseAgent
from modules.agents.catalog import display_name_for_backend
from modules.im.base import FileAttachment
from storage import message_deliveries as delivery_store
from storage.agent_session_rows import session_route_settings, session_speed
from storage.agent_transcript import (
    INPUT_TYPES,
    MESSAGE_TURN_ID_SQL,
    RESPONSE_TYPES,
    SQLiteTranscriptStore,
    call_instance_result,
    committed_final,
    final_outcome,
    render_text,
    text_parts,
)
from storage.db import get_cached_sqlite_engine
from storage.models import agent_events, agent_runs, agent_sessions, message_deliveries, messages, session_turns
from storage.vibey_recovery import VIBEY_RECOVERY_INPUT_PREDICATE
from vibe.i18n import t as i18n_t

logger = logging.getLogger(__name__)

BACKEND = "vibey"
# Final responses whose empty text is explained by the stop itself (loop-control.md section 2).
_EXPLAINED_STOPS = ("length", "refusal", "safety")
_COMPLETED = ("completed", "ended_by_hook")
# A transient source failure may use the existing bounded continuation policy; a silent normal stop gets one
# transcript-only continuation before the ordinary empty-response failure is delivered.
_MAX_AUTO_CONTINUATIONS = 5
_JOB_PRUNE_INTERVAL_S = 3600.0
_DELIVERY_TURN_RECOVERY_STATES = ("claimed", "interrupt_waiting")
_DELIVERY_TARGET_RECOVERY_STATES = ("steering", "reconciling_steer")
_NONTERMINAL_TURN_STATES = ("waiting", "starting", "active")


#: ``Agent.max_tokens`` for every Turn: the resolved hop's budgeted maximum decides the output (``budgeted``).
_NO_OUTPUT_CAP = 1 << 30
#: A failure's diagnostic in the service log: one redacted line, bounded, never a payload.
_LOGGED_DIAGNOSTIC_CHARS = 500
#: The served hop names what the gateway's ``x-avibe-served-hop`` header said, so it is bounded and kept on the line.
_LOGGED_HOP_CHARS = 200


def _loggable(text: str, limit: int = _LOGGED_DIAGNOSTIC_CHARS) -> str:
    """``text`` as one redacted log field: whitespace collapsed, so it cannot start a line of its own, and bounded."""
    from core.agent_core.ai.errors import redact_provider_text

    line = " ".join(redact_provider_text(text).split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _hub_error_kind(kind: Optional[str]) -> Optional[str]:
    """The failure's kind as Model Hub records it: a kind the engine names, never an unexpected exception's class."""
    return kind if kind in NAMED_ERROR_KINDS else None


@dataclass(frozen=True)
class _Route:
    """What a Turn's next model request asks for: the model, its reasoning effort, and the processing tier
    (``"fast"``, or None for standard)."""

    model: str
    reasoning_effort: Optional[str]
    speed: Optional[str]


@dataclass
class _Run:
    """One Turn's state, published before its first await (``handle_message``)."""

    request: AgentRequest
    session_id: str
    turn_id: str
    instance: str
    agent: Optional[Agent] = None
    router: Optional[HubModelRouter] = None
    # What dispatch resolved for the Turn, then what its Session asks for at each later request (``_follow_session``).
    route: Optional[_Route] = None
    cwd: str = ""
    instructions_notice: Optional[str] = None
    settled: bool = False
    reason: Optional[str] = None
    # The error that decided ``reason`` (``RunEnded.cause``): the failure's kind, text, and Model Hub attribution.
    cause: Optional[AgentError] = None
    # Model Hub's copy for a route it refused during the run (``_preflight``).
    refusal: Optional[str] = None
    errors: list[AgentError] = field(default_factory=list)
    final_row: Optional[str] = None
    tool_calls: dict[str, ToolCallBlock] = field(default_factory=dict)
    tool_responses: dict[str, str] = field(default_factory=dict)
    # This run's committed responses by row id, for delivering them; released with the run.
    responses: dict[str, AssistantMessage] = field(default_factory=dict)
    # The loop's decision for each response whether it replies (``has_reply``), committed with its row.
    replied: dict[str, bool] = field(default_factory=dict)
    # The images each final delivers with its reply, as committed with its row.
    reply_images: dict[str, tuple[HostedImage, ...]] = field(default_factory=dict)
    # A rerun's final as its row committed it, undelivered: that display carries the images
    # the run that died generated, which this run does not have.
    committed_display: Optional[str] = None
    # Steer attempts ``Agent.steer`` accepted into this run: reconcile's in-process evidence.
    accepted_steers: set[str] = field(default_factory=set)
    # Automatic continuations in this failure episode. Success resets both fields;
    # the initial call is not counted. One proxy reserve may spend the final slot
    # beyond the normal cap, but never reopens the native window.
    continuations: int = 0
    continuation_started_at: Optional[float] = None
    stop_requested: bool = False
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    # When each tool call started (monotonic), for a proxied hosted call's duration.
    tool_started: dict[str, float] = field(default_factory=dict)
    # Images the run's hosted calls generated, as (path, label), attached to its final reply.
    hosted_images: list[tuple[str, str]] = field(default_factory=list)

    @property
    def native_turn_id(self) -> str:
        """``vibey:<adapter instance>:<turn>``: the instance that runs the Turn, for steer reconcile."""
        return f"{BACKEND}:{self.instance}:{self.turn_id}"


@dataclass
class _SessionRuntime:
    """A Session's writer lock and run while some caller holds it; retired when idle."""

    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    run: Optional[_Run] = None
    cwd: str = ""
    holders: int = 0


class VibeyAgent(BaseAgent):
    """Backend ``vibey``: the first-party agent loop over Avibe's own transcript rows."""

    name = BACKEND

    def __init__(
        self,
        controller: Any,
        *,
        engine: Optional[Engine] = None,
        providers: Optional[ProviderFactory] = None,
        tool_suite: Optional[ToolSuite] = None,
        state_dir: Optional[Path] = None,
        gateway_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(controller)
        self._engine = engine or get_cached_sqlite_engine()
        state = Path(state_dir) if state_dir is not None else paths.get_state_dir() / "agent_core"
        self._state_dir = state
        self.prompt_snapshots = SnapshotStore(state)
        self.media = MediaSnapshots(self._engine, state / "media")
        # The exact bytes of every model request a Turn sends, kept when it does not succeed (``request_artifacts``).
        self.request_artifacts = RequestArtifactStore(state / "model-requests")
        from vibe import __version__

        self._version = __version__
        self.store = AdapterTranscriptStore(
            SQLiteTranscriptStore(self._engine, render=self._display_source),
            self._engine,
            environment=self._environment,
            on_response=self._on_response,
            response_metadata=self._response_metadata,
            file_write=self._record_instruction_write,
        )
        self._providers = providers or registry_providers(media_loader=self.media)
        # The client of the Turn's other gateway calls (the hosted-tool executor); None opens one per call.
        self._gateway_client = gateway_client
        # C-9: the Session parts a checkpoint carries (context.md section 9).
        self.context_host = VibeyContextHost(self._engine, environment=self._environment)
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
        self._media_sweep: Optional[asyncio.Task] = None

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
            await self._fail(request, None, f"{display_name_for_backend(BACKEND)} turn has no Session, Turn, or input identity.")
            return
        turn = _Run(request, session_id, turn_id, self._instance)
        async with self._held(session_id, run=turn) as runtime:
            try:
                with capture_model_requests(
                    self.request_artifacts, session_id=session_id, turn_id=turn_id, version=self._version
                ):
                    await self._run_turn(runtime, turn, input_id)
            except Exception as error:
                if turn.settled:
                    # Raised during or after settlement (its emits, or a failure notice): the
                    # shared owner's fallback releases the runtime gate, so it must see it.
                    raise
                turn.settled = True
                self._log_failure(turn, "error", type(error).__name__, "local", f"turn failed: {error}")
                # Where it was raised, for debugging only: the WARNING above is the failure's one line.
                logger.debug(
                    "Vibey turn %s raised in:\n%s", turn_id, "".join(traceback.format_tb(error.__traceback__))
                )
                await self._fail(request, type(error).__name__, f"turn failed: {error}", cause=error)
            finally:
                await self._admit_returned_inputs(turn, defer_continuation=self._service_stopping(turn))
                if (
                    (context.platform_specific or {}).get("vibey_resume")
                    and turn.reason not in _COMPLETED
                    and not self._service_stopping(turn)
                ):
                    # Preflight/Stop can end a rerun before its Agent took the durable inputs.
                    self._schedule_recovery()
                self.store.bind_agent(session_id, None)
                if turn.router is not None:
                    await turn.router.aclose()
                self._prune_jobs_soon()

    async def _run_turn(self, runtime: _SessionRuntime, turn: _Run, input_id: str) -> None:
        request, session_id = turn.request, turn.session_id
        context = request.context
        # Rows this Turn writes are the Turn's Agent's, from its request snapshot, even if the
        # Session's selected Agent changes while it runs.
        self.store.bind_agent(session_id, request.vibe_agent_name, turn_id=turn.turn_id)
        # A transcript resume (a Turn rerun after a restart) gives a job that survived the restart
        # the foreground wait its live call would have, so its real output becomes the call's result.
        resuming = (context.platform_specific or {}).get("vibey_resume") is True
        try:
            await self._resume(runtime, stop=turn.stop_event if resuming else None)
        except asyncio.CancelledError:
            if resuming and getattr(self.controller, "shutdown_requested", False) is not True:
                # Cancelled outside a shutdown (the backend disabled or refreshed): the work
                # stops, so the command the wait was for ends with it instead of running on
                # unowned. A shutdown leaves it for the next start's rerun.
                turn.stop_event.set()
                await self._settle_open_calls(runtime, stop=turn.stop_event)
            raise
        if resuming and await self._take_committed_final(turn):
            # The run that died committed the Turn's final: this one ends where that one
            # would have, with no model call. A Stop now loses to it, as it does live.
            self._accept_native_start(request, session_id)
            indicator = getattr(self.controller, "processing_indicator", None)
            if indicator is not None:
                await indicator.delete_ack_message(request)
            await self._settle(turn)
            self._maybe_backfill_session_title(request, session_id)
            return
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        cwd = request.working_path or runtime.cwd
        try:
            turn.router = await self._preflight(turn)
        except Exception as error:
            if self._service_stopping(turn):
                # Stopping the service (its Model Hub gateway first) failed the route, not
                # the Turn: leave it open, as a run cut below is, for the restart.
                await asyncio.Event().wait()
            turn.settled = True
            await self._fail_preflight(request, error)
            return
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        sections, skill_catalog = await self._avibe_sections(request, cwd, model=turn.router.preflight.endpoint.model_id)
        builtin_agent = await asyncio.to_thread(self._is_builtin_agent, request)
        environment = await asyncio.to_thread(self._turn_environment, request, cwd)
        files = await asyncio.to_thread(load_instruction_files, cwd, paths.get_global_instructions_path(), include_empty=True)
        self._start_agent(turn, cwd, join_prompt_blocks(sections), environment,
                          builtin_agent=builtin_agent, instructions=project_context(files))
        prompt_state = await self._prepare_prompt(
            turn.agent, request, turn.router, sections, files, environment, builtin_agent=builtin_agent, catalog=skill_catalog,
        )
        turn.instructions_notice = prompt_state.notice
        runtime.cwd = cwd
        # Prepared before anything is dispatched (``core.native_dispatch_phase``).
        message = await self._render_input(session_id, request.message, request.files, request.input_metadata)
        if turn.stop_requested:
            return await self._settle_stopped(turn)
        # Native acceptance materializes the input row the loop consumes, and binds this
        # process's generation: steers held for the Turn are dispatched once its Agent exists.
        self._accept_native_start(request, session_id)
        indicator = getattr(self.controller, "processing_indicator", None)
        if indicator is not None:
            # The ack message goes once the native write is accepted, as for the other backends.
            await indicator.delete_ack_message(request)
        accept_catalog(self.controller, context, prompt_state.snapshot.prefix.catalog, backend=BACKEND)
        agent_input: Optional[AgentInput] = AgentInput(input_id, message)
        platform_specific = context.platform_specific or {}
        transcript_resume = platform_specific.get("vibey_resume") is True
        resume_turn_inputs = (
            await asyncio.to_thread(
                self._resume_turn_inputs, session_id, platform_specific.get("vibey_source_turn_id")
            )
            if transcript_resume
            else None
        )
        while agent_input is not None:
            if turn.stop_requested:
                # A Stop acknowledged before this run could abort: no run starts. An input
                # left unconsumed is admitted at the Session's next resume (T3).
                turn.reason = "aborted"
                break
            stream = (
                turn.agent.resume(
                    agent_input,
                    turn_id=turn.turn_id,
                    turn_inputs=resume_turn_inputs,
                    pending_inputs=await self._recovered_inputs(session_id),
                )
                if transcript_resume
                else turn.agent.run(agent_input, turn_id=turn.turn_id)
            )
            transcript_resume = False
            resume_turn_inputs = None
            async for event in stream:
                await self._on_event(turn, event)
            if self._can_auto_continue(turn) and (
                turn.continuations < _MAX_AUTO_CONTINUATIONS or await self._native_search_switch_available(turn)
            ):
                if turn.continuation_started_at is None:
                    turn.continuation_started_at = time.monotonic()
                # Finish T2 before asking the same Agent to build its next request.
                # Accepted P1 inputs enter context unless the native continuation still
                # requires a tool-result-only user message; those restore to its queue.
                await self._settle_left_open(runtime)
                resume_turn_inputs = await self._admit_returned_inputs(turn, defer_continuation=True)
                if await self._wait_auto_continue(turn):
                    turn.continuations += 1
                    turn.reason = None
                    turn.cause = None
                    turn.final_row = None
                    transcript_resume = True
                    continue
                # Stop/backoff exhaustion ended the continuation we deferred T3 for.
                await self._resume(runtime)
            agent_input = await self._continuing_input(turn)
        if self._cut_by_service_stop(turn):
            # Stopping the service (its Model Hub gateway first) ended this run, not
            # anything the Turn did. Leave the Turn open: startup recovery runs T2 and
            # continues it from the committed transcript (vibey-restart-auto-resume.md).
            await asyncio.Event().wait()
        if turn.reason not in _COMPLETED:
            await self._settle_left_open(runtime)
        await self._settle(turn)
        self._maybe_backfill_session_title(request, session_id)

    async def _take_committed_final(self, run: _Run) -> bool:
        """Whether the Turn's transcript already holds its final response, which the run then takes.

        Its replied decision is the one its row's type was committed by (``final_outcome``).
        """
        found = await asyncio.to_thread(self._committed_final, run.session_id, run.turn_id)
        if found is None:
            return False
        row_id, message_type, message, display = found
        run.final_row, run.reason = row_id, "completed"
        run.responses[row_id] = message
        run.replied[row_id] = message_type != "error"
        run.committed_display = display
        return True

    def _committed_final(
        self, session_id: str, turn_id: str
    ) -> Optional[tuple[str, str, AssistantMessage, Optional[str]]]:
        """``(row_id, type, message, committed display or None once delivered)`` of the Turn's final."""
        with self._engine.connect() as conn:
            final = committed_final(conn, session_id, turn_id)
            row = (
                conn.execute(
                    select(messages.c.content_json, messages.c.content_text).where(messages.c.id == final.row_id)
                ).first()
                if final is not None
                else None
            )
        if final is None or row is None:
            return None
        try:
            content = json.loads(row.content_json or "{}")
            message = message_from_dict(content["model"]["message"])
        except (TypeError, ValueError, KeyError):
            logger.warning("Vibey could not read the committed final %s of Turn %s", final.row_id, turn_id)
            return None
        if not isinstance(message, AssistantMessage):
            return None
        return final.row_id, final.message_type, message, None if "kind" in content else str(row.content_text or "")

    def _cut_by_service_stop(self, run: _Run) -> bool:
        """Whether a run that did not complete ended while the service was stopping."""
        return bool(run.reason not in _COMPLETED and not run.final_row and self._service_stopping(run))

    def _service_stopping(self, run: _Run) -> bool:
        """Whether the service is stopping under a run nobody asked to stop."""
        return not run.stop_requested and getattr(self.controller, "shutdown_requested", False) is True

    def _resume_turn_inputs(self, session_id: str, source_turn_id: Any) -> tuple[int, ...]:
        """Committed inputs of the failed task, including steers across its retry ancestry."""
        if not isinstance(source_turn_id, str) or not source_turn_id:
            return ()
        pins: set[int] = set()
        visited: set[str] = set()
        with self._engine.connect() as conn:
            while source_turn_id and source_turn_id not in visited:
                visited.add(source_turn_id)
                turn = delivery_store.get_turn(conn, source_turn_id)
                if turn is None or turn["session_id"] != session_id or turn["backend"] != BACKEND:
                    break
                pins.update(conn.execute(
                    select(messages.c.context_seq)
                    .select_from(message_deliveries.join(messages, message_deliveries.c.message_id == messages.c.id))
                    .where(
                        message_deliveries.c.turn_id == source_turn_id,
                        messages.c.session_id == session_id,
                        messages.c.context_seq.is_not(None),
                        messages.c.type.in_(INPUT_TYPES),
                    )
                ).scalars())
                initial = delivery_store.get_delivery(conn, turn["initial_delivery_id"])
                payload = delivery_store.execution_delivery_payload(conn, initial) if initial else {}
                if not delivery_store.is_vibey_resume_payload(payload):
                    break
                source_turn_id = payload["metadata"].get(delivery_store.VIBEY_SOURCE_TURN_METADATA_KEY)
                if not isinstance(source_turn_id, str):
                    break
        return tuple(sorted(pins))

    @staticmethod
    def _can_auto_continue(run: _Run) -> bool:
        """Whether the failed request can safely be retried from committed context."""

        cause = run.cause
        return bool(
            not run.stop_requested
            and run.reason == "error"
            and not run.final_row
            and cause is not None
            and cause.origin == "source"
            and cause.continuable
        )

    async def _wait_auto_continue(self, run: _Run) -> bool:
        """Honor Retry-After and one failure episode's budget, with Stop owning the wait.

        Success ends the episode. Consecutive failures spend one window, including
        request duration and recovery work, not just backoff. Expiry never interrupts
        a running request; a qualifying cut-off may spend one final proxy reserve.
        """
        if run.stop_requested:
            run.reason = "aborted"
            return False
        if not self._can_auto_continue(run):
            return False
        policy = run.agent.retry
        elapsed = time.monotonic() - run.continuation_started_at
        backoff = min(policy.max_delay_s, policy.initial_delay_s * 2**run.continuations)
        delay = max(backoff, run.cause.retry_after_s or 0.0)
        if elapsed + delay >= policy.max_elapsed_s:
            # A streamed native request can itself consume the remaining window. Reserve one final
            # proxy attempt rather than ending before the alternate path was tried; its normal delay
            # (including Retry-After) still holds, and a proxy failure gets no renewed window.
            if not await self._native_search_switch_available(run):
                return False
        try:
            await asyncio.wait_for(run.stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        if run.stop_requested:
            run.reason = "aborted"
            return False
        if time.monotonic() - run.continuation_started_at >= policy.max_elapsed_s:
            if not await self._native_search_switch_available(run):
                return False
            # Spend the episode's final reserve slot, even if time expired before
            # the ordinary attempt cap. A second tool cannot obtain another reserve.
            run.continuations = _MAX_AUTO_CONTINUATIONS
        return True

    def _accept_native_start(self, request: AgentRequest, session_id: str) -> None:
        # The native start records the request this Turn runs with, which a restart rerun keeps.
        if request.context.platform_specific is None:
            request.context.platform_specific = {}
        request.context.platform_specific["vibey_execution"] = {
            "agent_id": request.vibe_agent_id,
            "agent_name": request.vibe_agent_name,
            "model": request.vibe_agent_model,
            "reasoning_effort": request.vibe_agent_reasoning_effort,
            "system_prompt": request.vibe_agent_system_prompt,
        }
        self.bind_agent_session_id(request, session_id)
        mark_backend_dispatch_attempted(request.context)
        self.mark_runtime_turn_started(request.context)

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
        run.stop_event.set()
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
                run.stop_event.set()
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

    async def recover_runtime_state(self, session_id: Optional[str] = None) -> list[str]:
        """One settlement pass outside a Turn: T2 and T3 admission; return the unsettled Sessions.

        Candidates come from the rows: Sessions whose context can hold an open call, and
        Sessions with accepted inputs no run consumed. Each runs the same resume a Turn
        runs (T2 and T3, no model call). A Session a run holds is skipped: its own resume
        settles it. So is a Session whose open call has a running job and an unsettled
        Turn: the Turn owner either continues that Turn, whose own resume waits for the
        job, or settles it after asking for this Session alone (``session_id``), which
        hands the job over as before (``vibey-restart-auto-resume.md``). Settled-job
        bookkeeping is scheduled separately after readiness; it is not recovery evidence.
        Never reports success over a failure; ``VibeyRecovery`` owns the retries. The first
        pass of the process also starts the media sweep.
        """
        if self._media_sweep is None:
            self._media_sweep = asyncio.create_task(self._sweep_media(), name="vibey-agent-media-sweep")
        explicit = session_id is not None
        candidates_started_at = time.monotonic()
        candidates = [session_id] if session_id else await asyncio.to_thread(self._recovery_candidates)
        logger.info(
            "Startup phase=vibey_recovery_candidates duration_ms=%d candidates=%d",
            round((time.monotonic() - candidates_started_at) * 1000),
            len(candidates),
        )
        unsettled: list[str] = []
        for candidate in candidates:
            async with self._held(candidate, wait=False) as runtime:
                if runtime is None:
                    continue
                try:
                    await self._resume(runtime, defer_continued=not explicit)
                except Exception:
                    logger.exception("Vibey could not settle Session %s outside a Turn", candidate)
                    unsettled.append(candidate)
        logger.info(
            "Startup phase=vibey_recovery_prune duration_ms=0 unsettled=%d deferred=true",
            len(unsettled),
        )
        return unsettled

    def _recovery_candidates(self) -> list[str]:
        return sorted({*self._sessions_with_open_tail(), *self._sessions_with_unconsumed_inputs()})

    def _durable_recovery_turn_query(self, *, include_id: bool = False):
        columns = (session_turns.c.session_id, session_turns.c.id) if include_id else (session_turns.c.session_id,)
        return select(*columns).where(
            session_turns.c.backend == BACKEND,
            session_turns.c.state.in_(_NONTERMINAL_TURN_STATES),
        )

    def _durable_recovery_query(self):
        unfinished_run_statuses = ("pending", "queued", "processing", "running")
        def delivery_source(states: tuple[str, ...], alias: str):
            values = ", ".join(f"'{state}'" for state in states)
            return sql_text(
                "select distinct state, session_id, turn_id, current_target_turn_id "
                "from message_deliveries indexed by ix_message_deliveries_recovery_owner "
                f"where state in ({values})"
            ).columns(
                state=message_deliveries.c.state.type,
                session_id=message_deliveries.c.session_id.type,
                turn_id=message_deliveries.c.turn_id.type,
                current_target_turn_id=message_deliveries.c.current_target_turn_id.type,
            ).subquery(alias)

        delivery_by_turn_rows = delivery_source(_DELIVERY_TURN_RECOVERY_STATES, "recovery_turn_deliveries")
        delivery_by_target_rows = delivery_source(_DELIVERY_TARGET_RECOVERY_STATES, "recovery_target_deliveries")
        delivery_by_turn = select(delivery_by_turn_rows.c.session_id).select_from(
            delivery_by_turn_rows.join(
                session_turns,
                delivery_by_turn_rows.c.turn_id == session_turns.c.id,
            )
        ).where(session_turns.c.backend == BACKEND)
        delivery_by_target = select(delivery_by_target_rows.c.session_id).select_from(
            delivery_by_target_rows.join(
                session_turns,
                delivery_by_target_rows.c.current_target_turn_id == session_turns.c.id,
            )
        ).where(session_turns.c.backend == BACKEND)

        return union(
            self._durable_recovery_turn_query(),
            delivery_by_turn,
            delivery_by_target,
            select(agent_runs.c.session_id).where(
                agent_runs.c.run_type == "agent_run",
                or_(agent_runs.c.agent_backend == BACKEND, agent_runs.c.agent_name == BACKEND),
                agent_runs.c.session_id.is_not(None),
                agent_runs.c.status.in_(unfinished_run_statuses),
            ),
        )

    def _unsettled_interrupted_sessions_query(self):
        """Find only the latest transcript tail of an interrupted Vibey Session.

        Terminal outcome is ownership evidence, not recovery evidence. The latest
        response must still contain more tool calls than durable results after it;
        normal stopped/canceled history therefore contributes no candidate.
        """
        return sql_text(
            f"""
            WITH interrupted_sessions AS (
                SELECT DISTINCT session_id
                FROM session_turns
                WHERE backend = 'vibey'
                  AND state = 'terminal'
                  AND terminal_outcome IN ('failed', 'canceled')
            ), latest AS (
                SELECT interrupted_sessions.session_id AS session_id,
                       (
                           SELECT max(messages.context_seq)
                           FROM messages
                           WHERE messages.session_id = interrupted_sessions.session_id
                             AND messages.context_seq IS NOT NULL
                             AND messages.type IN ('assistant', 'output', 'result', 'error')
                       ) AS seq
                FROM interrupted_sessions
            )
            SELECT latest.session_id
            FROM latest
            JOIN messages
              ON messages.session_id = latest.session_id
             AND messages.context_seq = latest.seq
            WHERE (
                SELECT count(*)
                FROM json_each(
                    CASE WHEN json_valid(messages.content_json) = 1
                         THEN messages.content_json ELSE '{{}}' END,
                    '$.model.message.content'
                ) AS blocks
                WHERE json_extract(blocks.value, '$.type') = 'tool_call'
            ) > (
                SELECT count(*)
                FROM agent_events
                WHERE agent_events.session_id = messages.session_id
                  AND agent_events.event_type = 'tool_result'
                  AND agent_events.context_seq > messages.context_seq
            )
            """
        )

    def _durable_recovery_sessions(self) -> set[str]:
        """Return Sessions whose durable work can own a restart-open Vibey tail.

        The transcript check is exact, but it must not begin by walking all
        historical responses. A live/non-terminal Vibey Turn is the durable owner
        for a crash, Stop, backend switch, or fork boundary; an unfinished Vibey
        Agent Run is the legacy/Workbench owner for work that had not settled.
        Terminal interrupted Turns are considered only through the latest Session
        tail and only while that tail still has an unmatched tool call.
        """
        with self._engine.connect() as conn:
            candidates = set(conn.execute(self._durable_recovery_query()).scalars())
            candidates.update(conn.execute(self._unsettled_interrupted_sessions_query()).scalars())
        return {str(session_id) for session_id in candidates if session_id is not None}

    def _durable_recovery_turns(self) -> dict[str, set[str]]:
        with self._engine.connect() as conn:
            rows = conn.execute(self._durable_recovery_turn_query(include_id=True)).all()
        turns: dict[str, set[str]] = {}
        for session_id, turn_id in rows:
            turns.setdefault(str(session_id), set()).add(str(turn_id))
        return turns

    def _schedule_recovery(self) -> None:
        """Hand what this Turn could not settle to the process's recovery owner."""
        recovery = getattr(self.controller, "vibey_recovery", None)
        if recovery is not None:
            recovery.ensure()

    def schedule_post_readiness_maintenance(self) -> None:
        """Start settled-job bookkeeping after the controller has published readiness."""
        self._prune_jobs_soon()

    def _prune_jobs_soon(self) -> None:
        """J5 while the service runs: runs create jobs, so a run's end prunes, at most hourly, off the Turn."""
        if self._pruned_at is not None and time.monotonic() - self._pruned_at < _JOB_PRUNE_INTERVAL_S:
            return
        if self._prune_task is not None and not self._prune_task.done():
            return
        self._prune_task = asyncio.create_task(self._prune_settled_jobs(), name="vibey-agent-job-prune")

    async def _prune_settled_jobs(self) -> None:
        """J5: remove finished jobs whose call has a durable result and whose Watch settled."""
        self._pruned_at = time.monotonic()
        try:
            suite = self._tools()
            if suite.prune is not None:
                removed = await asyncio.to_thread(suite.prune, self._job_call_settled)
                if removed:
                    logger.info("Vibey pruned %d settled job(s)", len(removed))
        except Exception:
            logger.exception("Vibey job pruning failed")

    async def _sweep_media(self) -> None:
        """Remove the media files no row registers, once per process, off the loop (``MediaSnapshots.sweep``)."""
        try:
            removed, freed = await asyncio.to_thread(self.media.sweep)
        except Exception:
            logger.exception("Vibey media sweep failed")
            return
        if removed:
            logger.info("Vibey removed %d unregistered media file(s), %d bytes", removed, freed)

    def _job_call_settled(self, meta: Any) -> bool:
        """Whether the job's call instance has a durable ``tool_result`` row (the adapter's half of J5).

        Matched in context order, never by time: a provider may reuse a tool-call id,
        and an earlier call's result does not settle a later one.
        """
        call = call_instance(meta)
        if call is None:
            return False
        with self._engine.connect() as conn:
            return call_instance_result(conn, call.session_id, call.response_seq, call.tool_call_id) is not None

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
            logger.exception("Vibey could not prepare a steer for Session %s", request.target_session_id)
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

        A later attempt resolves again (C-6 item 3), for the model and processing tier the
        Session asks for by then (``_follow_session``); a Model Hub refusal there is kept on
        the Turn, so its failure shows the Hub's copy, as a refusal at preflight does.
        """
        from modules.agents.model_hub import bind_launch, launch_refusal_copy, resolve_model_hub_launch

        request, session_id = turn.request, turn.session_id

        model = request.subagent_model or request.vibe_agent_model
        if not model:
            raise ValueError(f"{display_name_for_backend(BACKEND)} has no model selected.")
        context = request.context
        turn.route = _Route(
            model,
            request.subagent_reasoning_effort or request.vibe_agent_reasoning_effort,
            session_speed_for_context(context),
        )

        async def resolve() -> ModelSelection:
            route = turn.route
            try:
                # Every resolution names the Turn: the loop's later model calls run in their own tasks.
                launch = await resolve_model_hub_launch(
                    self.controller, BACKEND, route.model, process_scope=f"{BACKEND}:{session_id}", context=context,
                    turn_id=turn.turn_id, speed=route.speed,
                )
            except Exception as error:
                turn.refusal = launch_refusal_copy(self.controller, error)
                raise
            bind_launch(context, launch)
            # Every resolved hop, the first and each retry's, carries the output the Agent asks for on it.
            selection = budgeted(selection_from_hop(launch.to_hop_resolution()))
            return await self._native_search_recovery_route(turn, selection)

        first = await resolve()

        async def resolve_next() -> ModelSelection:
            await self._follow_session(turn)
            return await resolve()

        return HubModelRouter(resolve_next, self._providers, first=first)

    async def _follow_session(self, turn: _Run) -> None:
        """Before each model request after the Turn's first: take what the Session asks for now.

        A Session's model, reasoning effort, and speed can change while its Turn runs (the Web
        picker, ``vibe session update``), and the next request goes out on them. Only a Turn
        that dispatch resolved from the Session's row follows it, by dispatch's own rule over
        the row as it is now: a setting the row does not name is the one the Session inherits,
        and an explicit subagent model or effort is the request's own. The tool list, the
        hosted-tool declarations, and the system prompt stay the Turn's (``_build_agent``).
        """
        request, route = turn.request, turn.route
        target = (request.context.platform_specific or {}).get("agent_session_target")
        if route is None or not isinstance(target, dict) or str(target.get("id") or "") != turn.session_id:
            return
        try:
            row = await asyncio.to_thread(self._session_route_settings, turn.session_id)
        except Exception:
            # Following is an addition to the Turn, never a reason to fail it: it keeps what it has.
            logger.warning("Vibey could not read the settings of Session %s", turn.session_id, exc_info=True)
            return
        if row is None:
            return
        model, reasoning_effort, _ = session_model_settings(
            row,
            fallback_model=request.vibe_agent_inherited_model,
            fallback_reasoning_effort=request.vibe_agent_inherited_reasoning_effort,
        )
        current = _Route(
            # A Session that names no model cannot run; the Turn keeps the one it has.
            route.model if request.subagent_model else str(model or "").strip() or route.model,
            route.reasoning_effort if request.subagent_reasoning_effort else reasoning_effort,
            session_speed(row["metadata"]),
        )
        if current == route:
            return
        logger.info(
            "Vibey turn %s follows its Session: model %s -> %s, effort %s -> %s, speed %s -> %s",
            turn.turn_id, route.model, current.model, route.reasoning_effort, current.reasoning_effort,
            route.speed or "standard", current.speed or "standard",
        )
        turn.route = current
        if turn.agent is not None:
            # The request's capability filter still decides whether the hop takes it (``Agent._built``).
            turn.agent.reasoning_effort = current.reasoning_effort

    def _session_route_settings(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            return session_route_settings(conn, session_id)

    async def _native_search_recovery_route(self, turn: _Run, selection: ModelSelection) -> ModelSelection:
        """Derive a per-tool route from this Turn's immutable committed response facts."""
        native = {
            tool for tool, route in selection.endpoint.hosted_tools.items()
            if tool in SEARCH_TOOLS and route.mode == "native"
        }
        runtime = getattr(self.controller, "model_hub_runtime", None)
        candidate = getattr(runtime, "has_proxied_hosted_candidate", None)
        if not native or not callable(candidate):
            return selection
        rows = await self.store.load_turn_response_metadata(turn.session_id, turn.turn_id)
        counts, previously_proxied = turn_recovery_facts(rows)
        # The current immutable hop also covers retries before a response commits
        # (503/no output, pin re-resolution, overflow relief). Do not lose an early
        # switch when the loop replaces its latest attempt with an uncommitted one.
        current = getattr(turn.router, "current", None)
        previously_proxied.update(
            tool for tool, route in getattr(getattr(current, "endpoint", None), "hosted_tools", {}).items()
            if route.mode == "proxy" and route.route_switch == "native_interrupted"
        )
        just_interrupted = interrupted_native_searches(getattr(turn.agent, "last_attempt_response", None))
        last_retry = self._last_permitted_auto_retry(turn)
        switched = {
            tool for tool in native
            if candidate(turn.turn_id, tool) and (
                tool in previously_proxied
                or should_switch(counts[tool], last_permitted_retry=last_retry and tool in just_interrupted)
            )
        }
        return self._with_native_search_proxy_routes(selection, switched) if switched else selection

    async def _native_search_switch_available(self, turn: _Run) -> bool:
        """A reserve is for this just-failed native call, at most once per episode."""
        if turn.continuations > _MAX_AUTO_CONTINUATIONS:
            return False
        just_interrupted = interrupted_native_searches(getattr(turn.agent, "last_attempt_response", None))
        if not just_interrupted:
            return False
        runtime = getattr(self.controller, "model_hub_runtime", None)
        candidate = getattr(runtime, "has_proxied_hosted_candidate", None)
        current = getattr(getattr(turn, "router", None), "current", None)
        routes = getattr(getattr(current, "endpoint", None), "hosted_tools", {})
        return bool(callable(candidate) and any(
            tool in routes and routes[tool].mode == "native" and candidate(turn.turn_id, tool)
            for tool in just_interrupted
        ))

    @staticmethod
    def _with_native_search_proxy_routes(selection: ModelSelection, switched: set[str]) -> ModelSelection:
        hosted = {
            tool: replace(
                route,
                mode="proxy",
                replay=False,
                declaration=None,
                include=(),
                route_switch="native_interrupted",
            ) if tool in switched else route
            for tool, route in selection.endpoint.hosted_tools.items()
        }
        return replace(selection, endpoint=replace(selection.endpoint, hosted_tools=hosted))

    @staticmethod
    def _last_permitted_auto_retry(turn: _Run) -> bool:
        """Whether this continuation is the last retry the existing budget admits."""

        if turn.continuation_started_at is None:
            return False
        policy = turn.agent.retry if turn.agent is not None else RetryPolicy()
        if turn.continuations >= _MAX_AUTO_CONTINUATIONS:
            return True
        elapsed = time.monotonic() - turn.continuation_started_at
        backoff = min(policy.max_delay_s, policy.initial_delay_s * 2**turn.continuations)
        return elapsed + backoff >= policy.max_elapsed_s

    def _start_agent(
        self,
        turn: _Run,
        cwd: str,
        sections: str,
        environment: Mapping[str, str],
        *,
        builtin_agent: bool,
        instructions: str = "",
    ) -> None:
        turn.agent = self._build_agent(
            turn.request, turn.session_id, turn.router, cwd, sections, environment,
            builtin_agent=builtin_agent, instructions=instructions,
        )
        turn.cwd = cwd

    def _build_agent(
        self,
        request: AgentRequest,
        session_id: str,
        router: HubModelRouter,
        cwd: str,
        sections: str,
        environment: Mapping[str, str],
        *,
        builtin_agent: bool,
        instructions: str = "",
    ) -> Agent:
        """Production construction shared with detached previews; this does not register or start a Turn.

        The bash rule names ``rg`` only when the command environment's PATH finds it.
        """
        suite = self._tools()
        agent = Agent(
            session_id=session_id,
            models=router,
            tools=(),
            hooks=(),
            store=self.store,
            jobs=suite.jobs,
            cwd=cwd,
            env=environment,
            reasoning_effort=request.subagent_reasoning_effort or request.vibe_agent_reasoning_effort,
            # The route decides the output (``budgeted``): no Agent-wide cap below it.
            max_tokens=_NO_OUTPUT_CAP,
            context=ContextConfig(
                host=self.context_host,
                scratch_dir=str(self._state_dir / "scratch" / session_id),
                checkpoint_dir=str(self._state_dir / "checkpoints" / session_id),
            ),
        )
        # Job-backed tools receive the loop's tracking wrapper, so Stop kills foreground commands.
        tools = mark_skill_loads(
            (
                *hosted_tools(router, self._gateway_client, self.media, language=self._language),
                *suite.create_tools(agent.jobs, self.media.image_sink(session_id)),
            )
        )
        agent.set_tools(tools)
        finds_rg = shutil.which("rg", path=environment.get("PATH", "")) is not None
        agent.system = system_prompt(
            [tool.spec.name for tool in tools],
            sections,
            builtin_agent=builtin_agent,
            rg=finds_rg,
            project_context=instructions,
        )
        return agent

    async def _prepare_prompt(
        self, agent: Agent, request: AgentRequest, router: HubModelRouter,
        blocks: Sequence[RenderedPromptBlock], files: Sequence[Any], environment: Mapping[str, str], *,
        builtin_agent: bool, persist: bool = True, catalog: Optional[dict] = None,
    ) -> PromptState:
        """Read, compare and publish optional prefix state off the loop, once per Turn."""
        entries = await self.store.load(agent.session_id)
        endpoint = router.preflight.endpoint

        def prepare():
            candidate = candidate_prefix(
                agent.system, agent.tools, endpoint, blocks, files, builtin_agent=builtin_agent,
                rg=shutil.which("rg", path=environment.get("PATH", "")) is not None, catalog=catalog,
            )
            previous = self.prompt_snapshots.load(agent.session_id)
            prefix = previous.snapshot.prefix if previous is not None else candidate
            probe = ModelRequest(endpoint=endpoint, system=prefix.system, tools=prefix.tools, messages=(), max_tokens=1)
            now = agent.context.clock()
            decision = decide(
                previous, candidate, now=now,
                route=route_identity(endpoint, agent=request.vibe_agent_id or "", model=request.subagent_model or request.vibe_agent_model or "",
                                     effort=request.subagent_reasoning_effort or request.vibe_agent_reasoning_effort or ""),
                checkpoint_seq=max((entry.context_seq for entry in entries if entry.kind == "compaction"), default=0),
                cold=cache_cold(probe, entries, now=now),
            )
            if persist and decision.state != previous:
                if not self.prompt_snapshots.save(agent.session_id, decision.state, snapshot=decision.reason is not None):
                    # Without a durable first-seen time, retaining a warm prefix could
                    # reset a tier-2 deadline forever. Lose the optimization, not freshness.
                    failed = decision
                    decision = decide(None, candidate, now=now, route=failed.state.snapshot.route,
                                      checkpoint_seq=failed.state.observed_checkpoint_seq, cold=False)
                    decision = replace(decision, reason="persistence_failure", detected=failed.detected,
                                       applied=failed.applied or failed.state.pending)
            if persist and (decision.detected or decision.reason):
                self._record_prompt_events(request, agent.session_id, decision, now)
            return decision

        decision = await to_thread_joined(prepare)
        state = decision.state
        agent.cache_causes = (({"kind": "prompt_applied", "reason": decision.reason},) if decision.reason else ())
        agent.prompt_pending = bool(state.pending)
        agent.prompt_notice = state.notice != "current"
        agent.system = state.snapshot.prefix.system
        agent.set_tools(frozen_tools(state.snapshot.prefix.tools, agent.tools))
        router.prefix_declarations = (route_identity(endpoint), state.snapshot.prefix.hosted)
        return state

    def _record_prompt_events(self, request: AgentRequest, session_id: str, decision, now: float) -> None:
        """Worker-only diagnostics: a failed metrics write cannot fail a Turn."""
        from dataclasses import asdict
        from storage import agent_events_service

        events = []
        if decision.detected:
            events.append(("prompt.pending", {"components": [asdict(item) for item in decision.detected]}))
        if decision.reason:
            events.append(("prompt.applied", {
                "reason": decision.reason,
                "components": [{**asdict(item), "wait_s": max(0, now - item.pending_since)} for item in decision.applied],
            }))
        try:
            with self._engine.begin() as conn:
                scope_id = conn.execute(select(agent_sessions.c.scope_id).where(agent_sessions.c.id == session_id)).scalar()
                for event_type, data in events:
                    agent_events_service.append(
                        conn, scope_id=scope_id, session_id=session_id, platform=request.context.platform or "avibe",
                        event_type=event_type, content={"version": 1, "at": now, **data}, visibility="trace",
                        backend=BACKEND, agent_name=request.vibe_agent_name,
                        turn_id=(request.context.platform_specific or {}).get("turn_token"),
                    )
        except Exception:
            logger.warning("Could not record optional prompt metrics for Session %s", session_id)

    async def preview_request(
        self, session_id: str, request: AgentRequest, *, context_seq: int | None = None,
    ) -> RequestPreview:
        """Reconstruct from current configuration without binding a runtime, consuming input, or activating supply."""
        model = request.subagent_model or request.vibe_agent_model
        if not model:
            raise ValueError("request_preview_model_unset")
        launch = await self.controller.model_hub_runtime.describe_vibey_request(model)
        selected = budgeted(selection_from_hop(launch.to_hop_resolution()))

        async def no_resolve() -> ModelSelection:
            raise RuntimeError("A preview must not resolve a model route")

        router = HubModelRouter(no_resolve, self._providers, first=selected)
        cwd = request.working_path
        sections, _ = await self._avibe_sections(request, cwd, model=selected.endpoint.model_id)
        builtin_agent = await asyncio.to_thread(self._is_builtin_agent, request)
        environment = await asyncio.to_thread(self._turn_environment, request, cwd)
        files = await asyncio.to_thread(load_instruction_files, cwd, paths.get_global_instructions_path(), include_empty=True)
        agent = self._build_agent(
            request, session_id, router, cwd, join_prompt_blocks(sections), environment,
            builtin_agent=builtin_agent, instructions=project_context(files),
        )
        await self._prepare_prompt(agent, request, router, sections, files, environment,
                                   builtin_agent=builtin_agent, persist=False)
        return await agent.dry_tick(
            selected=router.prompt_route(selected), context_seq=context_seq, gateway=True, media_loader=self.media,
        )

    async def _on_event(self, run: _Run, event: AgentEvent) -> None:
        if isinstance(event, MessageCommitted):
            # Store callbacks run in the producer, potentially several responses
            # ahead of this bounded event consumer. Bind tool ids here, in event
            # order, so a later response reusing an id cannot steal an earlier
            # ToolStarted/ToolFinished (or its compatibility trace).
            message = run.responses.get(event.message_id)
            if message is not None:
                run.tool_calls.update({call.id: call for call in message.tool_calls})
                run.tool_responses.update({call.id: event.message_id for call in message.tool_calls})
            # A response's native hosted calls show before the text that reads their results.
            await self._emit_native_hosted(run, event.message_id)
            await self.controller.message_dispatcher.publish_tool_projection(run.request.context, event.message_id, unhosted_only=True)
            if event.final:
                # Delivered at the end of the run, which decides how the Turn settles.
                run.final_row = event.message_id
            elif event.superseded:
                await self._emit_reply(run, event.message_id)
            else:
                await self._emit_narration(run, event.message_id)
        elif isinstance(event, ToolStarted):
            run.tool_started[event.tool_call_id] = time.monotonic()
            response_id = run.tool_responses.get(event.tool_call_id)
            if response_id:
                await self.controller.message_dispatcher.publish_tool_projection(
                    run.request.context, response_id, call_id=event.tool_call_id,
                    facts={"started_at": datetime.now(timezone.utc).isoformat()}, unhosted_only=True,
                )
            await self._emit_tool_started(run, event)
        elif isinstance(event, ToolFinished):
            started = run.tool_started.pop(event.tool_call_id, None)
            duration = int((time.monotonic() - started) * 1000) if started is not None else None
            response_id = run.tool_responses.get(event.tool_call_id)
            if response_id:
                await self.controller.message_dispatcher.publish_tool_projection(
                    run.request.context, response_id, call_id=event.tool_call_id,
                    facts={"duration_ms": duration, "ended_at": datetime.now(timezone.utc).isoformat()}, publish=False,
                )
            await self._emit_proxy_hosted(run, event, duration=duration)
            call = run.tool_calls.get(event.tool_call_id)
            if response_id and (call is None or call.name not in HOSTED_RESULT_TOOLS):
                await self.controller.message_dispatcher.publish_tool_projection(run.request.context, response_id, call_id=event.tool_call_id)
        elif isinstance(event, AgentError):
            run.errors.append(event)
        elif isinstance(event, RunEnded):
            run.reason, run.cause = event.reason, event.cause
        elif isinstance(event, CompactionFinished):
            # Silent (C-9 section 10); the session's occupancy snapshot drops with the context.
            self._note_total(run, event.tokens_after_estimate)
        elif isinstance(event, CompactionFailed):
            # Silent to the user too (C-9 section 10); the log keeps why, a refusal's category included.
            logger.warning(
                "Vibey compaction failed: session=%s turn=%s reason=%s error=%s",
                run.session_id,
                run.turn_id,
                event.reason,
                _loggable(event.error),
            )

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
        # The failure is the error that decided the outcome, never a diagnostic, whose text is only a fallback
        # detail. Model Hub records it against the route only when the served source produced it (C-6).
        cause = run.cause
        kind = cause.kind if cause is not None else None
        diagnostic = cause.message if cause is not None else run.errors[0].message if run.errors else reason
        origin = cause.origin if cause is not None else None
        source = cause is not None and cause.origin == "source"
        final = run.responses.get(run.final_row) if run.final_row else None
        if final is not None:
            if run.stop_requested and reason == "aborted":
                reason = "completed"
            run_failed_after_commit = reason not in _COMPLETED
            # Whether the final replies is the run's decision, committed with its row (``final_outcome``).
            # Whether the final replies, and the images it delivers, are the run's, committed with its row.
            replied = run.replied.get(run.final_row)
            failed = final_outcome(final, replied=replied) == "failed" or run_failed_after_commit
            if failed:
                self._log_failure(run, reason, kind, origin, diagnostic)
            body, citations = self._display_with_citations(
                final, final=True, replied=bool(replied), images=run.reply_images.get(run.final_row, ())
            )
            if run.committed_display is not None and citations is None:
                body = run.committed_display
            if failed and not body.strip():
                # A silent final whose run failed after the commit: like a final without text
                # of its own (``_display_source``), its row carries the explanation.
                body = error_text(kind or "empty_response", self._language(), reason=reason)
            if failed and source:
                await self.record_model_hub_native_failure(
                    context, diagnostic or (kind or "failed final"), error_kind=_hub_error_kind(kind)
                )
            if body.strip():
                # The same result path as the other backends; the row is already the message.
                await self.emit_result_message(
                    context,
                    body,
                    subtype="error" if failed else "success",
                    started_at=request.started_at,
                    request=request,
                    output=replace(terminal_output_for(request), persisted_row_id=run.final_row),
                    citations=citations,
                )
            else:
                # A reply the model chose to keep silent: the Turn's end with no reply of
                # its own; any earlier reply in the Turn was already delivered as one.
                await emit_output(self.controller, context, TurnEnd(output=terminal_output_for(request)))
            return
        # Generated images are paid for and already announced: they reach the user
        # whether or not a final reply follows.
        await self._emit_orphan_images(run)
        if run.stop_requested and reason == "aborted":
            # The stop receipt IM shows, as for the other backends.
            await self._remove_ack_reaction(request, terminal_emoji=STOPPED_REACTION_EMOJI)
            await emit_output(
                self.controller,
                context,
                TurnEnd(outcome=TurnOutcome.STOPPED, silent=True, output=terminal_output_for(request)),
            )
            return
        if reason in _COMPLETED and kind is None:
            # The run ended without a final reply by design (a terminating tool, a hook end).
            await emit_output(self.controller, context, TurnEnd(silent=True, output=terminal_output_for(request)))
            return
        self._log_failure(run, reason, kind, origin, diagnostic)
        await self._fail(request, kind, diagnostic, reason=reason, refusal=run.refusal, source=source)

    @staticmethod
    def _log_failure(
        run: _Run, reason: str, kind: Optional[str], origin: Optional[str], diagnostic: str
    ) -> None:
        """One WARNING for a failed run, so a failed Turn can be diagnosed from the log: its kind, who produced it,
        the hop that served the last response, and the diagnostic, which is never display copy (``errors.py``)."""
        served = next((message.origin for message in reversed(run.responses.values())), None)
        logger.warning(
            "Vibey run failed: session=%s turn=%s reason=%s kind=%s origin=%s served=%s diagnostic=%s",
            run.session_id,
            run.turn_id,
            reason,
            kind or "-",
            origin or "-",
            _loggable(f"{served.provider}/{served.api}/{served.model}", _LOGGED_HOP_CHARS) if served else "-",
            _loggable(diagnostic or ""),
        )

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

    async def _admit_returned_inputs(self, run: _Run, *, defer_continuation: bool = False) -> tuple[int, ...]:
        """Inputs a stopped or failed run accepted but did not consume enter the context (T3).

        They belong to the Turn that accepted them and share its outcome, as a message
        merged into another backend's stopped or failed turn does; the next Turn
        answers them with the full context.
        """
        consumed: list[int] = []
        if run.agent is None:
            return ()
        try:
            pending = await run.agent.take_pending_inputs()
            if pending and defer_continuation and await self._deferred_continuation_open(run.session_id):
                # Durable accepted rows restore the queue at resume, including after shutdown.
                return ()
            for item in pending:
                entry = await self.store.consume_input(run.session_id, item.message_id, item.message)
                consumed.append(entry.context_seq)
        except Exception:
            # The rows stay accepted and unconsumed: the recovery owner admits them (T3)
            # without waiting for the Session's next message.
            logger.exception("Vibey could not admit returned inputs for Session %s", run.session_id)
            self._schedule_recovery()
        return tuple(consumed)

    # --- resume (recovery.md T2, T3) -------------------------------------------

    async def _resume(
        self,
        runtime: _SessionRuntime,
        *,
        stop: Optional[asyncio.Event] = None,
        defer_continued: bool = False,
    ) -> None:
        """T2, then T3. ``stop``: the continuing Turn's own resume, which waits for a running job
        (``_settle_open_calls``). ``defer_continued``: recovery leaves alone a Session the Turn owner
        has yet to continue or settle."""
        session_id = runtime.session_id
        # T2 before every run: idempotent, and a Turn that waited behind an aborted one
        # finds the call that abort left open on the same, still-held runtime.
        if not await self._settle_open_calls(runtime, stop=stop, defer_continued=defer_continued):
            return
        if not runtime.cwd:
            runtime.cwd = await asyncio.to_thread(self._session_workdir, session_id)
        if (defer_continued or stop is not None) and await self._deferred_continuation_open(session_id):
            # T3 cannot close an Anthropic server-tool turn. Startup leaves the accepted
            # rows untouched; Agent.resume restores them to its queue before admission opens.
            return
        for item in await self._recovered_inputs(session_id):
            await self.store.consume_input(session_id, item.message_id, item.message)

    async def _deferred_continuation_open(self, session_id: str) -> bool:
        turn = await asyncio.to_thread(self._active_turn, session_id)
        if turn is None:
            return False
        entries = [row for row in await self.store.load_turn(session_id, str(turn["id"])) if row.message is not None]
        position = in_flight_position([row.message for row in entries])
        return (
            position is not None
            and bool(pending_hosted(entries[position].message))
            and await asyncio.to_thread(self._work_state, str(turn["id"])) == "open"
        )

    def _active_turn(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            return delivery_store.active_turn(conn, session_id)

    async def _recovered_inputs(self, session_id: str) -> tuple[AgentInput, ...]:
        inputs = []
        for message_id, text_value, files, delivery in await asyncio.to_thread(self._unconsumed_inputs, session_id):
            message = await self._render_input(session_id, text_value, files, await self._input_metadata(delivery))
            inputs.append(AgentInput(message_id, message))
        return tuple(inputs)

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
            logger.exception("Vibey could not rebuild sender facts for input %s", delivery.get("id"))
            return None

    async def _settle_left_open(self, runtime: _SessionRuntime) -> None:
        """T2 as soon as a run ends without completing (a Stop, a failure), not at the Session's next resume.

        The calls the run was executing then carry what their jobs recorded, such as a
        Stop's reason, while the Turn's own outcome is still being settled.
        """
        try:
            await self._settle_open_calls(runtime)
        except Exception:
            # The calls stay open, and the recovery owner settles them once this Turn releases the Session.
            logger.exception("Vibey could not settle the calls a run left open in Session %s", runtime.session_id)
            self._schedule_recovery()

    async def _settle_open_calls(
        self,
        runtime: _SessionRuntime,
        *,
        stop: Optional[asyncio.Event] = None,
        defer_continued: bool = False,
    ) -> bool:
        """T2: one committed result for every open tool call, chosen from its job's state.

        With ``stop``, a job still running is first waited for as its live call would wait
        (its foreground window from the job's launch, or until ``stop``), so only a job still
        running then is handed to a Watch. With ``defer_continued``, a running job whose Turn
        is not yet settled is left to that Turn's owner and nothing is written: ``False``.
        """
        session_id = runtime.session_id
        open_calls = open_tool_calls(await self.store.load(session_id))
        if not open_calls:
            return True
        suite = self._tools()

        def evidence() -> dict[CallInstance, str]:
            # A call is its instance, the owning response plus its id (providers reuse ids). A fork's
            # point is settled (C-10 fork.md section 2), so every open call is this Session's own.
            found = {}
            for owner, call in open_calls:
                instance = CallInstance(owner.session_id, owner.row_id, owner.context_seq, call.id)
                job_id = suite.find_job(instance)
                if job_id is not None:
                    found[instance] = job_id
            return found

        # Each job lookup lists the jobs directory: one pass, off the event loop.
        job_ids = await asyncio.to_thread(evidence)
        # A tool result is the Agent's that made the call: its owning response's author, not
        # the Turn writing it now (or none, at startup) or the Session's current selection.
        # Only the last response can hold open calls, so they share one owner.
        owner_agent, owner_turn = await asyncio.to_thread(self._response_owner, open_calls[0][0].row_id)
        work = await asyncio.to_thread(self._work_state, owner_turn)
        if (
            defer_continued
            and work == "open"
            and any(suite.jobs.status(job_id).state == "running" for job_id in job_ids.values())
        ):
            return False
        if work == "stopped" and stop is None:
            # A Stop settled the Turn while its command outlived the run: it ends the command,
            # as it ends a live foreground command, rather than leaving it to a Watch.
            stop = asyncio.Event()
            stop.set()
        wait_running = None
        if stop is not None and suite.wait_recovered is not None:
            wait_recovered = suite.wait_recovered

            async def wait_running(call: Any, job_id: str) -> Any:
                return await wait_recovered(call, job_id, stop)

        with self.store.writing_as(session_id, owner_agent, turn_id=owner_turn):
            await settle_open_calls(
                session_id=session_id,
                store=self.store,
                jobs=suite.jobs,
                job_ids=job_ids,
                render_result=suite.render_recovered,
                wait_running=wait_running,
            )
        # Recovery commits results without ToolFinished events. For the live
        # owner, publish their settled projection now, before a terminal (or an
        # automatic continuation) can clear/reuse the browser's activity buffer.
        # Startup recovery with no live owner remains a durable read-only update.
        run = self._run_for(session_id, owner_turn)
        if run is not None:
            for owner, call in open_calls:
                await self.controller.message_dispatcher.publish_tool_projection(
                    run.request.context, owner.row_id, call_id=call.id,
                )
        return True

    def _work_state(self, turn_id: Optional[str]) -> str:
        """Where the Turn owning an open call stands: ``open``, ``stopped``, or ``settled``.

        A Turn a restart cut stays open and reruns in place, so its call's Turn is the one
        that continues it. ``stopped``: a Stop or a replacement canceled that Turn, or a stop
        asked of it is not yet settled (``stop_requested``; it never reruns).
        """
        from core.restart_resume import stop_requested

        if not turn_id:
            return "settled"
        with self._engine.connect() as conn:
            turn = delivery_store.get_turn(conn, turn_id)
            if turn is not None and turn["state"] in delivery_store.TURN_OWNER_STATES:
                return "stopped" if stop_requested(conn, turn) else "open"
        if turn is None:
            return "settled"
        return "stopped" if turn.get("terminal_outcome") == "canceled" else "settled"

    def _response_owner(self, row_id: str) -> tuple[Optional[str], Optional[str]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(messages.c.author_name, messages.c.metadata_json).where(messages.c.id == row_id)
            ).first()
        if row is None:
            return None, None
        return row.author_name, _json_object(row.metadata_json).get("turn_id")

    def _job_route(self, meta: Mapping[str, Any]) -> dict[str, Any]:
        """A job Watch's Agent and authorization, from the job's owning Turn (fail closed).

        The same rule for every hand-over, in a Turn or at startup: the owning response
        is the one the job's call instance names; its Turn is the one whose input the
        response answers, and that Turn's initial input row carries the principal and
        its persisted resource snapshot.
        A remote Turn without a usable snapshot, or a job whose Turn cannot be found, is
        handed over as unverifiable: owned, but never followed up.
        """
        from core.caller_context import caller_context_from_platform_payload
        from storage.resource_access_service import (
            RESOURCE_USER_CONTEXT_METADATA_KEY,
            ResourceAccessError,
            resource_user_context_from_metadata,
        )

        call = call_instance(meta)
        if call is None:
            return {"unverifiable_remote": True}
        session_id = call.session_id
        with self._engine.connect() as conn:
            owner = conn.execute(
                select(messages.c.author_name, messages.c.metadata_json).where(
                    messages.c.id == call.response_id,
                    messages.c.session_id == session_id,
                    messages.c.context_seq == call.response_seq,
                )
            ).first()
            origin = self._turn_initial_input(
                conn, session_id, call.response_seq, turn_id=_json_object(owner.metadata_json).get("turn_id")
            ) if owner else None
        if owner is None or origin is None:
            return {"unverifiable_remote": True}
        route: dict[str, Any] = {"agent_name": owner.author_name} if owner.author_name else {}
        metadata = _json_object(origin["metadata_json"])
        caller = caller_context_from_platform_payload(
            {"agent_session_id": session_id, "platform": origin["platform"], "message_metadata": metadata},
            message=SimpleNamespace(
                platform=origin["platform"], user_id=origin["author_id"] or "", channel_id=None,
                message_id=None, thread_id=None,
            ),
        )
        if caller is None or not caller.is_remote:
            return route
        try:
            context = (
                resource_user_context_from_metadata({RESOURCE_USER_CONTEXT_METADATA_KEY: caller.resource_user_context})
                if caller.resource_user_context
                else None
            )
        except ResourceAccessError:
            context = None
        if context is None:
            return {**route, "unverifiable_remote": True}
        return {**route, "user_context": context}

    @staticmethod
    def _turn_initial_input(
        conn: Any, session_id: str, before_seq: int, *, turn_id: Optional[str] = None
    ) -> Optional[dict]:
        """The response's durable Turn input; legacy responses use context order."""
        if turn_id is not None:
            row = conn.execute(
                select(messages.c.platform, messages.c.author_id, messages.c.metadata_json)
                .select_from(
                    session_turns.join(message_deliveries, message_deliveries.c.id == session_turns.c.initial_delivery_id)
                    .join(messages, messages.c.id == message_deliveries.c.message_id)
                )
                .where(session_turns.c.id == turn_id, session_turns.c.session_id == session_id)
            ).mappings().first()
            return dict(row) if row is not None else None
        latest = conn.execute(
            select(messages.c.id)
            .where(
                messages.c.session_id == session_id,
                messages.c.context_seq.is_not(None),
                messages.c.context_seq < before_seq,
                messages.c.type.in_(INPUT_TYPES),
            )
            .order_by(messages.c.context_seq.desc())
            .limit(1)
        ).scalar()
        if latest is None:
            return None
        # The Turn that wrote it, in any state: the hand-over may come after that Turn settled
        # (T2 at a later resume, or a retried recovery pass after T4).
        turn_id = conn.execute(
            select(message_deliveries.c.turn_id)
            .where(message_deliveries.c.message_id == latest, message_deliveries.c.turn_id.is_not(None))
            .limit(1)
        ).scalar()
        initial_id = latest
        if turn_id is not None:
            initial_id = conn.execute(
                select(message_deliveries.c.message_id)
                .select_from(
                    session_turns.join(message_deliveries, message_deliveries.c.id == session_turns.c.initial_delivery_id)
                )
                .where(session_turns.c.id == turn_id)
            ).scalar() or latest
        row = conn.execute(
            select(messages.c.platform, messages.c.author_id, messages.c.metadata_json).where(messages.c.id == initial_id)
        ).mappings().first()
        return dict(row) if row is not None else None

    def _unconsumed_inputs(self, session_id: str) -> list[tuple[str, str, list[FileAttachment], dict[str, Any]]]:
        """Inputs accepted into a ``vibey`` Turn but never consumed, in acceptance order.

        Only Turns this backend ran count, so history from an earlier backend of the
        same Session never enters the context.
        """
        from core.workbench_media import file_attachments_from_specs, resolve_attachment_specs

        with self._engine.connect() as conn:
            # CROSS JOIN fixes SQLite's loop order: start with indexed live
            # inputs, then seek their Deliveries/Turns. A materialized CTE alone
            # still lets the planner scan all historical accepted Deliveries.
            rows = conn.execute(
                sql_text(
                    "select messages.id, messages.content_text, messages.content_json, "
                    "message_deliveries.id as delivery_id, message_deliveries.turn_role, "
                    "session_turns.dispatch_text as turn_dispatch_text "
                    "from messages cross join message_deliveries "
                    "on message_deliveries.message_id = messages.id "
                    "cross join session_turns on session_turns.id = message_deliveries.turn_id "
                    f"where {VIBEY_RECOVERY_INPUT_PREDICATE} "
                    "and messages.session_id = :session_id "
                    "and message_deliveries.state = 'accepted' and session_turns.backend = :backend "
                    "order by message_deliveries.materialized_at, message_deliveries.turn_position, messages.id"
                ),
                {"session_id": session_id, "backend": BACKEND},
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
                if delivery is None:
                    continue
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
                    logger.warning("Vibey lists an image it could not snapshot: %s", attachment.local_path)
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

    def _display_source(
        self, message: AssistantMessage, *, final: bool, replied: bool = False, images: Sequence[HostedImage] = ()
    ) -> str:
        """The text a surface renders a response from: its text blocks without silent blocks.

        Also the row's commit-time display (the store's renderer). A final without
        text of its own (``final_outcome`` failed) is explained by localized copy:
        the refusal or safety explanation, else the empty-reply one, so the row
        never shows nothing for a failed Turn (loop-control.md section 2). A final
        carries the images its Turn generated (``images``), so its committed row is
        the complete reply even if delivery never runs.
        """
        value = render_text(message)
        # A final that replies without text replies with its Turn's images, so there is nothing to explain.
        if final and not value.strip() and not replied:
            kind = message.stop_reason if message.stop_reason in _EXPLAINED_STOPS else "empty_response"
            return self._with_images(error_text(kind, self._language()), images)
        body = strip_silent_blocks(value)
        return self._with_images(body, images) if final else body

    async def _avibe_sections(
        self, request: AgentRequest, cwd: str, *, model: str,
    ) -> tuple[list[RenderedPromptBlock], Optional[dict]]:
        """Avibe's injected prompt sections, for the model the Turn's route resolved, and the skill catalog they offered.

        Built off the event loop as the other backends build them: skill resolution
        scans the ordinary static Skill directories.
        """
        from core.managed_skills import managed_skill_project_base
        from core.system_prompt_injection import (
            bot_for_turn,
            build_system_prompt_blocks,
            get_enabled_agents_for_prompt,
            get_enabled_projects_for_prompt,
        )

        context = request.context
        skill_catalog_sink: list[dict] = []

        def build() -> list[RenderedPromptBlock]:
            # Every lookup runs here, off the loop: the enabled-Agent list and the Bot read SQLite.
            bot = bot_for_turn(
                getattr(self.controller, "vibe_agent_store", None), agent_id=request.vibe_agent_id, cwd=cwd
            )
            return build_system_prompt_blocks(
                bot=bot,
                agent_instructions=request.vibe_agent_system_prompt or "",
                backend=BACKEND,
                include_quick_replies=getattr(self.config, "reply_enhancements", True)
                and context.platform != "wechat",
                context=context,
                fallback_platform=context.platform,
                enabled_agents=get_enabled_agents_for_prompt(self.controller),
                projects=get_enabled_projects_for_prompt() if bot is not None else None,
                skills_cwd=cwd or None,
                skills_project_base=managed_skill_project_base(context),
                skill_catalog_sink=skill_catalog_sink,
                model=model,
            )

        sections = await asyncio.to_thread(build)
        return sections, (skill_catalog_sink[0] if skill_catalog_sink else None)

    def _is_builtin_agent(self, request: AgentRequest) -> bool:
        """Whether the Turn runs as the backend's built-in Agent, the one the system prompt names Vibey.

        Read from the Agent record (the store's one owner of built-in identity), never from a name, so every Turn a
        Session runs as that Agent gets the same preamble and no other Agent is told it is Vibey. A Turn with no
        Agent record gets the neutral preamble.
        """
        store = getattr(self.controller, "vibe_agent_store", None)
        if store is None or not request.vibe_agent_id:
            return False
        builtin = store.get_builtin_default_agent_for_backend(BACKEND, enabled_only=False)
        return builtin is not None and builtin.id == request.vibe_agent_id

    def _turn_environment(self, request: AgentRequest, cwd: str) -> dict[str, str]:
        """The environment a Turn's commands run in, from the owners the other backends' shells use.

        The service's environment without its own caller provenance, then this Turn's
        caller context (``AVIBE_SESSION_ID``, ``AVIBE_CALLER_*``, so a ``vibe`` call in a
        command records where it came from), the managed-skill bindings, and verified
        Git and ripgrep on ``PATH``, composed as the Codex backend composes its shell environment.
        The Model Hub gateway token stays in the adapter.
        """
        from core.caller_context import caller_env_for_platform_payload, environment_without_caller_context
        from core.agent_path import prepend_managed_tools_to_path
        from core.managed_skills import (
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
            )
        )
        prepend_managed_tools_to_path(environment, base_env=environment, working_dir=cwd or None)
        return environment

    def _record_instruction_write(self, session_id: str, path: str, revision: str) -> None:
        """Only the instruction chain can affect this prefix; never accumulate unrelated file history."""
        runtime = self._runtimes.get(session_id)
        cwd = runtime.run.cwd if runtime is not None and runtime.run is not None else ""
        target = Path(path)
        directory = Path(cwd).absolute() if cwd else None
        if target == paths.get_global_instructions_path() or (
            directory is not None and target.parent in (directory, *directory.parents)
        ):
            self.prompt_snapshots.record_write(session_id, path, revision)

    def _environment(self, session_id: str) -> dict[str, EnvironmentValue]:
        runtime = self._runtimes.get(session_id)
        cwd = ((runtime.run.cwd if runtime.run is not None else "") or runtime.cwd) if runtime is not None else ""
        fields = {"session": session_id, **current_environment(cwd, self._watch_lines(session_id), shell=self._tools().shell)}
        if runtime is not None and runtime.run is not None and runtime.run.instructions_notice is not None:
            fields["instructions"] = runtime.run.instructions_notice
        return fields

    def _watch_lines(self, session_id: str) -> list[str]:
        service = getattr(self.controller, "watch_service", None)
        store = getattr(service, "store", None)
        list_watches = getattr(store, "list_watches", None)
        if not callable(list_watches):
            return []
        try:
            watches = list_watches()
        except Exception:
            logger.debug("Vibey could not list Watches", exc_info=True)
            return []
        lines = []
        for watch in watches:
            if getattr(watch, "session_id", None) != session_id or not getattr(watch, "enabled", False):
                continue
            # Model context: a Watch's id, name and kind only. Its command can carry a
            # credential, and the block is persisted and sent to the provider.
            # Raw here; the block displays each line (``render_environment``).
            kind = "job" if getattr(watch, "job_target", None) else "command"
            name = str(getattr(watch, "name", None) or "").strip()
            lines.append(f'{watch.id} "{name}" {kind} running' if name else f"{watch.id} {kind} running")
        return lines

    # --- output helpers ------------------------------------------------------

    async def _emit_narration(self, run: _Run, row_id: str) -> None:
        """A non-final response through the same process path as the other backends' narration."""
        await self._emit_non_final(run, row_id, reply=False)

    async def _emit_reply(self, run: _Run, row_id: str) -> None:
        """A completed reply later input took the final word from: a ``result`` that does not end the Turn.

        What a reply is depends on the reply alone (``docs/plans/reply-and-turn-end.md``):
        it is green, notifies, and counts as unread like the final, whatever its
        length; the steered input's answer is the Turn's next reply.
        """
        await self._emit_non_final(run, row_id, reply=True)

    async def _emit_non_final(self, run: _Run, row_id: str, *, reply: bool) -> None:
        message = run.responses.get(row_id)
        body, citations = self._display_with_citations(message, final=False) if message is not None else ("", None)
        if not body.strip():
            return
        event: Reply | Narration
        if reply:
            # A reply is identified by its committed row: two replies with the same
            # text stay two, and a replay of one stays one.
            event = Reply(
                body,
                citations=citations,
                output=MessageOutput(
                    completes_turn=False, completes_run=False, persisted_row_id=row_id, idempotency_key=row_id
                ),
            )
        else:
            event = Narration(
                body,
                citations=citations,
                output=MessageOutput(completes_turn=False, completes_run=False, persisted_row_id=row_id),
            )
        try:
            await emit_output(self.controller, run.request.context, event)
        except Exception:
            logger.exception("Vibey could not show response %s", row_id)

    def _display_with_citations(
        self, message: AssistantMessage, *, final: bool, replied: bool = False, images: Sequence[HostedImage] = ()
    ) -> tuple[str, Optional[CitationBundle]]:
        """The display text and citation bundle for provider citations or raw citation markers.

        The same text ``_display_source`` gives, with each cited span carrying a
        token that delivery writes out as a Markdown link (IM) and a badge (Web),
        through ``core.citations`` like every other backend. A marker without
        trustworthy structured source evidence is registered with an empty source
        map and becomes the localized source-unavailable fallback instead.
        """
        blocks = [(separator, block.text, block.citations or ()) for separator, block in text_parts(message)]
        if not any(citations for _, _, citations in blocks):
            body = self._display_source(message, final=final, replied=replied, images=images)
            return register_citations(
                body,
                {},
                unresolved_label=i18n_t("message.citationUnresolved", self._language()),
            )
        body, bundle = register_text_citations(
            blocks,
            unresolved_label=i18n_t("message.citationUnresolved", self._language()),
            transform=strip_silent_blocks,
        )
        if final and not body.strip():
            return self._display_source(message, final=True, replied=replied, images=images), None
        return (self._with_images(body, images) if final else body), bundle

    def _with_images(self, body: str, images: Sequence[HostedImage]) -> str:
        """A final reply with its Turn's generated images attached, as file links.

        A file link is how every surface delivers a local file: IM uploads it as an
        attachment, and the Web shows it inline from the same media token its card
        registered. An image the reply already links is not attached twice.
        """
        links = self._image_links([(image.path, Path(image.path).name) for image in images], body)
        if not links:
            return body
        # A reply that is only images still says what it carries, as the Codex backend's does.
        lead = body.rstrip() or i18n_t("info.generatedImagesFallback", self._language())
        return f"{lead}\n\n{links}"

    @staticmethod
    def _image_links(images: Sequence[tuple[str, str]], body: str) -> str:
        """File links for the ``(path, label)`` images that ``body`` does not already link.

        Only a link counts: the model is given each image's path as text, and a
        reply that mentions it in prose has still not delivered the file. A path
        that is not absolute names no file a surface could send, so it is skipped.
        """
        links = []
        for path, label in images:
            if not os.path.isabs(path):
                logger.warning("Vibey skipped a hosted image with a relative path: %s", path)
                continue
            uri = Path(path).as_uri()
            if f"]({uri})" in body:
                continue
            safe = label.replace("[", "(").replace("]", ")")
            links.append(f"![{safe}]({uri})")
        return "\n\n".join(links)

    async def _emit_orphan_images(self, run: _Run) -> None:
        """A run's generated images when it ends with no final reply to carry them."""
        links = self._image_links(run.hosted_images, "")
        if not links:
            return
        try:
            # The released ``output`` type stays until the presentation decision point
            # (docs/plans/reply-and-turn-end.md, PR 3): not a reply, not a notice.
            await self.controller.emit_agent_message(
                run.request.context,
                "output",
                f"{i18n_t('info.generatedImagesFallback', self._language())}\n\n{links}",
                output=MessageOutput(completes_turn=False, completes_run=False, records_run_output=False),
            )
        except Exception:
            logger.exception("Vibey could not deliver %d generated image(s)", len(run.hosted_images))

    async def _source_display(self, tool: str = "web_search") -> tuple[Mapping[str, str], Optional[Mapping[str, Mapping[str, Any]]]]:
        """Each Model Hub Source's display name and current state by id, as Model Hub holds them now.

        Read through the Model Hub runtime, the one owner of its current config, off the event loop: ``self.config``
        is the controller's app config, which carries no Model Hub state, and a Source renamed, repaired, turned off
        for search or removed since must show as it is now (``core.hosted_activity.SourceState``). Without a read
        the states are None, so a card offers no repair it could not check.
        """
        runtime = getattr(self.controller, "model_hub_runtime", None)
        if runtime is None:
            return {}, None

        def read() -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
            from config.v2_config import hosted_tool_setting, model_hub_builtin_sources
            from core.handlers.model_hub.hosted_tools import search_service_settings
            from core.handlers.model_hub.fetch_readers import fetch_service_settings

            hub = runtime.snapshot()
            setting = hosted_tool_setting(hub, tool)
            display_sources = [*hub.sources, *model_hub_builtin_sources()]
            names = {source.id: source.display_name for source in display_sources if source.display_name}
            states: dict[str, dict[str, Any]] = {}
            for source in display_sources:
                state: dict[str, Any] = {"protocol": source.protocol, "vendor": source.vendor}
                if source.state.status in ("needs_action", "error"):
                    state["needs_action"] = source.state.detail_key
                service = fetch_service_settings(setting, source) if tool == "web_fetch" else search_service_settings(setting, source)
                if service is not None:
                    state["enabled"] = service["enabled"]
                states[source.id] = state
            return names, states

        try:
            return await asyncio.to_thread(read)
        except Exception:
            # Display only: the card and the line still show each Source by its id or the label it was recorded with.
            logger.warning("Vibey could not read Model Hub's Sources for display", exc_info=True)
            return {}, None

    async def _emit_hosted(self, run: _Run, activity: HostedActivity) -> None:
        """One hosted call: its line for IM and its card for the Web (``core.hosted_activity``)."""
        # Only a completed call delivers what it produced, as only its image counts as a reply (``has_reply``).
        if activity.completed:
            run.hosted_images.extend(
                (image.path, Path(image.path).name) for image in activity.images if image_deliverable(image)
            )
        formatter = self._get_formatter(run.request.context)
        line = hosted_line(activity, self._language(), escape=formatter.escape_special_chars)
        try:
            await emit_output(
                self.controller,
                run.request.context,
                ToolActivity(
                    line, status_label=line, hosted=activity.to_payload(),
                    output=self._tool_output(run, activity.call_id, response_id=activity.response_id),
                ),
            )
        except Exception:
            logger.exception("Vibey could not show hosted call %s", activity.call_id)

    async def _emit_native_hosted(self, run: _Run, row_id: str) -> None:
        message = run.responses.get(row_id)
        if message is None:
            return
        names, _states = await self._source_display()
        activities = native_activities(message, source_names=names.get, response_id=row_id)
        source_ids = {activity.source_id for activity in activities if activity.source_id}
        if source_ids:
            await self.controller.message_dispatcher.publish_tool_projection(run.request.context, row_id, facts={
                "source_names": {key: names[key] for key in source_ids if key in names},
            }, publish=False)
        for activity in activities:
            await self._emit_hosted(run, activity)

    async def _emit_proxy_hosted(self, run: _Run, event: ToolFinished, *, duration: int | None = None) -> None:
        """A proxied hosted call, once its result is committed: the record its executor returned."""
        call = run.tool_calls.get(event.tool_call_id)
        if call is None or call.name not in HOSTED_RESULT_TOOLS:
            return
        try:
            result = await asyncio.to_thread(self._committed_tool_result, event.event_id)
        except Exception:
            logger.exception("Vibey could not read hosted result %s", event.event_id)
            result = None
        if result is None or result.hosted is None:
            # A hosted-named call that ended without a record (a plain error result):
            # it still shows, as the tool line its start would have drawn.
            await self._emit_tool_line(run, call.name, dict(call.arguments), call_id=call.id)
            return
        names, states = await self._source_display(call.name)
        activity = proxy_activity(
            call, result.hosted, source_names=names.get, source_state=states.get if states is not None else None,
            duration_ms=duration,
        )
        response_id = run.tool_responses.get(call.id)
        if response_id:
            source_ids = {
                activity.source_id, activity.fallback_source_id,
                *(attempt.source_id for attempt in activity.attempts or ()),
            } - {None}
            await self.controller.message_dispatcher.publish_tool_projection(run.request.context, response_id, call_id=call.id, facts={
                "source_names": {key: names[key] for key in source_ids if key in names},
                "source_states": {key: states[key] for key in source_ids if key in states} if states is not None else None,
            }, publish=False)
        await self._emit_hosted(run, activity)

    def _committed_tool_result(self, event_id: str) -> Optional[ToolResultMessage]:
        """The tool result a ``ToolFinished`` names: one row, by primary key."""
        with self._engine.connect() as conn:
            raw = conn.execute(select(agent_events.c.content_json).where(agent_events.c.id == event_id)).scalar()
        if not raw:
            return None
        payload = json.loads(raw) or {}
        message = message_from_dict(payload.get("message") or {})
        if isinstance(message, ToolResultMessage) and message.hosted is not None:
            details = payload.get("details")
            if isinstance(details, Mapping) and details.get("hosted_route_switch") == "native_interrupted":
                message = replace(message, hosted=replace(message.hosted, route_switch="native_interrupted"))
        return message if isinstance(message, ToolResultMessage) else None

    async def _emit_tool_started(self, run: _Run, event: ToolStarted) -> None:
        if event.name in HOSTED_RESULT_TOOLS:
            # A proxied hosted call shows once, as its card, when its result lands.
            return
        call = run.tool_calls.get(event.tool_call_id)
        await self._emit_tool_line(run, event.name, dict(call.arguments) if call is not None else {}, call_id=event.tool_call_id)

    @staticmethod
    def _tool_output(run: _Run, call_id: str, *, response_id: str | None = None) -> MessageOutput | None:
        owner = response_id or run.tool_responses.get(call_id)
        return MessageOutput(
            completes_run=False, records_run_output=False, metadata={SOURCE_KEY: source_link(owner, call_id)},
        ) if owner else None

    async def _emit_tool_line(self, run: _Run, name: str, arguments: dict, *, call_id: str | None = None) -> None:
        formatter = self._get_formatter(run.request.context)
        relative = _relative_to(run.cwd)
        try:
            await emit_output(
                self.controller,
                run.request.context,
                ToolActivity(
                    formatter.format_toolcall(name, arguments, get_relative_path=relative),
                    status_label=formatter.format_toolcall_label(name, arguments, get_relative_path=relative),
                    output=self._tool_output(run, call_id) if call_id else None,
                ),
            )
        except Exception:
            logger.exception("Vibey could not show tool %s", name)

    def _response_metadata(self, session_id: str, message: AssistantMessage) -> dict[str, Any]:
        run = self._run_for(session_id, None)
        current = getattr(getattr(run, "router", None), "current", None)
        routes = getattr(getattr(current, "endpoint", None), "hosted_tools", {})
        return {
            **({PROJECTION_KEY: {"version": PROJECTION_VERSION, "backend": BACKEND, "cwd": run.cwd,
                                 "platform": run.request.context.platform, "language": self._language()}} if run else {}),
            **response_recovery_metadata(message, (
                tool for tool, route in routes.items()
                if route.mode == "proxy" and route.route_switch == "native_interrupted"
            )),
            "hosted_render_epoch": HOSTED_RENDER_EPOCH,
            **({
                "hosted_render_rule": HOSTED_RENDER_RULE,
                "hosted_render_omitted": list(message.hosted_render_omitted),
            } if message.hosted_render_rule == HOSTED_RENDER_RULE and message.hosted_render_omitted else {}),
        }

    def _on_response(
        self,
        session_id: str,
        row_id: str,
        message: AssistantMessage,
        replied: Optional[bool],
        images: tuple[HostedImage, ...] = (),
    ) -> None:
        """A response committed for the Session's run: keep what its delivery and tool events need."""
        run = self._run_for(session_id, None)
        if run is None:
            return
        run.responses[row_id] = message
        if message.stop_reason not in {"error", "aborted"}:
            run.continuations = 0
            run.continuation_started_at = None
        if replied is not None:
            run.replied[row_id] = replied
        if images:
            run.reply_images[row_id] = images
        self._note_tokens(run, message)

    def _note_tokens(self, run: _Run, message: AssistantMessage) -> None:
        usage = message.usage
        note = getattr(self.controller, "note_session_tokens", None)
        if usage is None or not callable(note):
            return
        self._note_total(run, usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens + usage.output_tokens)

    def _note_total(self, run: _Run, total: int) -> None:
        note = getattr(self.controller, "note_session_tokens", None)
        if not callable(note):
            return
        try:
            note(run.request.context, total=total)
        except Exception:
            logger.debug("Vibey token note failed", exc_info=True)

    async def _fail(
        self,
        request: AgentRequest,
        kind: Optional[str],
        diagnostic: str,
        *,
        reason: Optional[str] = None,
        cause: Optional[BaseException] = None,
        refusal: Optional[str] = None,
        source: bool = False,
    ) -> None:
        """A failed Turn's notice; a Model Hub ``refusal`` copy takes precedence, as at preflight.

        A failure the served ``source`` produced (``AgentError.origin``) replaces that served attempt, as for the
        other backends; any other (a context that cannot fit, a Stop, a local error) leaves Model Hub's record as it is.
        """
        if source:
            await self.record_model_hub_native_failure(
                request.context, diagnostic or (kind or "error"), error_kind=_hub_error_kind(kind)
            )
        output = terminal_output_for(request)
        if kind:
            # The kind, never the diagnostic, so a failed Turn can be diagnosed from its notice row.
            output = replace(output, metadata={**output.metadata, "error_kind": kind})
        await emit_backend_failure(
            self.controller,
            request.context,
            BACKEND,
            diagnostic or (kind or "error"),
            display_text=f"❌ {refusal or error_text(kind, self._language(), reason=reason)}",
            request=request,
            output=output,
            cause=cause,
        )

    async def _fail_preflight(self, request: AgentRequest, error: BaseException) -> None:
        from modules.agents.model_hub import hold_refused_input, hub_supply_refusal, launch_refusal_copy

        logger.warning("Vibey could not resolve its model route: %s", error)
        language = self._language()
        # Every route runs through the Model Hub: when it leaves Vibey no model (disabled, its gateway
        # off, no source), that explains the failure, and the supply refusal is the failure's cause.
        supply = await asyncio.to_thread(hub_supply_refusal, self.controller, BACKEND, language)
        if supply is not None:
            hold_refused_input(request.context)
        refusal = str(supply) if supply is not None else (
            launch_refusal_copy(self.controller, error) or error_text(None, language)
        )
        # Recorded on every start failure, as the other backends do: a no-op unless a launch is bound.
        await self.record_model_hub_native_failure(request.context, str(error))
        display = f"❌ {refusal}"
        await emit_backend_failure(
            self.controller,
            request.context,
            BACKEND,
            str(error),
            display_text=display,
            request=request,
            cause=supply or error,
        )

    # --- lookups ---------------------------------------------------------------

    def _tools(self) -> ToolSuite:
        if self._tool_suite is None:
            # Jobs live in Watch's agent_jobs_dir(), where vibe stop and the job-Watch sweep look;
            # a job's Watch takes its Agent and authorization from the job's owning Turn.
            self._tool_suite = local_tool_suite(route=self._job_route)
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
        """Find Sessions whose current transcript tail still owns an open call.

        The exact transcript check is intentionally downstream of durable-owner
        discovery. A nonterminal Turn, unsettled Delivery, unfinished Agent Run,
        or terminal interrupted Session tail may put a Session into the candidate
        set; the terminal tail still needs an unmatched tool call.
        """
        from sqlalchemy import func

        durable_turns = self._durable_recovery_turns()
        candidate_sessions = self._durable_recovery_sessions()
        turn_ids = {turn_id for ids in durable_turns.values() for turn_id in ids}
        responses = []
        if turn_ids:
            # Keep this expression byte-identical to ix_messages_turn_id; SQLite
            # will otherwise fall back to the all-history transcript index.
            turn_id_expr = literal_column(f"({MESSAGE_TURN_ID_SQL})")
            with self._engine.connect() as conn:
                last_response = (
                    select(turn_id_expr.label("turn_id"), func.max(messages.c.context_seq).label("seq"))
                    .where(
                        turn_id_expr.in_(turn_ids),
                        messages.c.context_seq.is_not(None),
                        messages.c.type.in_(RESPONSE_TYPES),
                    )
                    .group_by(turn_id_expr)
                    .subquery()
                )
                responses.extend(
                    conn.execute(
                        select(messages.c.session_id, messages.c.context_seq, messages.c.content_json).join(
                            last_response,
                            and_(
                                turn_id_expr == last_response.c.turn_id,
                                messages.c.context_seq == last_response.c.seq,
                            ),
                        )
                    ).all()
                )

        # Legacy rows predating the per-response Turn metadata still need the
        # session-based check. Restrict that fallback to durable Sessions for
        # which the indexed Turn lookup found no response at all.
        seen_sessions = {str(session_id) for session_id, _seq, _raw in responses}
        legacy_sessions = candidate_sessions - seen_sessions
        if legacy_sessions:
            with self._engine.connect() as conn:
                last_response = (
                    select(messages.c.session_id, func.max(messages.c.context_seq).label("seq"))
                    .where(
                        messages.c.session_id.in_(legacy_sessions),
                        messages.c.context_seq.is_not(None),
                        messages.c.type.in_(RESPONSE_TYPES),
                    )
                    .group_by(messages.c.session_id)
                    .subquery()
                )
                responses.extend(
                    conn.execute(
                        select(messages.c.session_id, messages.c.context_seq, messages.c.content_json).join(
                            last_response,
                            and_(
                                messages.c.session_id == last_response.c.session_id,
                                messages.c.context_seq == last_response.c.seq,
                            ),
                        )
                    ).all()
                )

        candidates = []
        for session_id, seq, raw in responses:
            message = (_json_object(raw).get("model") or {}).get("message") or {}
            calls = [
                block for block in message.get("content") or ()
                if isinstance(block, dict) and block.get("type") == "tool_call"
            ]
            if not calls:
                continue
            with self._engine.connect() as conn:
                settled = conn.execute(
                    select(func.count()).select_from(agent_events).where(
                        agent_events.c.session_id == session_id,
                        agent_events.c.event_type == "tool_result",
                        agent_events.c.context_seq > seq,
                    )
                ).scalar()
            if settled < len(calls):
                candidates.append(str(session_id))

        return sorted(candidates)

    def _sessions_with_unconsumed_inputs(self) -> list[str]:
        """Sessions with inputs accepted into a ``vibey`` Turn that no run consumed (T3)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sql_text(
                    "select messages.session_id from messages "
                    "cross join message_deliveries on message_deliveries.message_id = messages.id "
                    "cross join session_turns on session_turns.id = message_deliveries.turn_id "
                    f"where {VIBEY_RECOVERY_INPUT_PREDICATE} "
                    "and messages.session_id is not null "
                    "and message_deliveries.state = 'accepted' and session_turns.backend = :backend"
                ),
                {"backend": BACKEND},
            ).scalars()
            return sorted({str(session_id) for session_id in rows if session_id is not None})

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

