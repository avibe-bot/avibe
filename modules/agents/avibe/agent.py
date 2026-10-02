"""``AvibeAgent``: the Avibe Agent engine (``core.agent_core``) as an Avibe backend.

One Turn is one ``Agent.run`` (plan section 4). The adapter

* resolves the model through Model Hub (``resolve_hop``) before the input is
  written, so a refused route fails like any backend that never started;
* marks native acceptance, which materializes the input's ``messages`` row, and
  then runs the loop, whose rows *are* the transcript (C-5);
* delivers every committed response from its row (the dispatcher's committed
  path, part by part on IM) and settles the Turn from the run's outcome;
* steers the running loop with P1 deliveries and lets a refused steer fall back
  to the P3 queue; Stop aborts the run;
* before any run of a Session in this process, under the Session writer lock,
  settles open tool calls (T2), admits accepted inputs that were never consumed
  (T3), and re-delivers pending outbox rows (D1). The Turn a restart interrupted
  is settled by the Turn owner (T4; ``avibe`` is a process-bound runtime).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

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
from core.message_dispatcher import CommittedOutput
from core.message_output import MessageOutput, stop_output_for, terminal_output_for
from core.native_dispatch_phase import mark_backend_dispatch_attempted
from core.reply_enhancer import process_reply, strip_silent_blocks
from modules.agents.avibe.errors import error_text
from modules.agents.avibe.media import MediaSnapshots
from modules.agents.avibe.models import HubModelRouter, ProviderFactory, registry_providers, selection_from_hop
from modules.agents.avibe.prompt import current_environment, system_prompt
from modules.agents.avibe.store import AdapterTranscriptStore
from modules.agents.avibe.tools import ToolSuite, WatchJobs, local_tool_suite
from modules.agents.base import AgentRequest, BaseAgent
from modules.im.base import FileAttachment
from storage import message_deliveries as delivery_store
from storage import messages_service
from storage.agent_transcript import (
    INPUT_TYPES,
    RenderedDisplay,
    SQLiteTranscriptStore,
    render_text,
)
from storage.db import get_cached_sqlite_engine
from storage.models import agent_sessions, message_deliveries, messages, session_turns

logger = logging.getLogger(__name__)

BACKEND = "avibe"
# Final responses whose empty text is explained by the stop itself (loop-control.md section 2).
_EXPLAINED_STOPS = ("refusal", "safety")
_COMPLETED = ("completed", "ended_by_hook")


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
    stop_requested: bool = False

    @property
    def native_turn_id(self) -> str:
        return f"{BACKEND}:{self.turn_id}"


@dataclass
class _SessionRuntime:
    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recovered: bool = False
    run: Optional[_Run] = None
    cwd: str = ""


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
        watch_jobs: Optional[WatchJobs] = None,
        state_dir: Optional[Path] = None,
    ) -> None:
        super().__init__(controller)
        self._engine = engine or get_cached_sqlite_engine()
        state = Path(state_dir) if state_dir is not None else paths.get_state_dir() / "agent_core"
        self._state_dir = state
        self.media = MediaSnapshots(self._engine, state / "media")
        self.store = AdapterTranscriptStore(
            SQLiteTranscriptStore(self._engine, render=self._render_display),
            self._engine,
            environment=self._environment,
        )
        self._providers = providers or registry_providers(media_loader=self.media)
        self._tool_suite = tool_suite
        self._watch_jobs = watch_jobs
        self._runtimes: dict[str, _SessionRuntime] = {}

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
        runtime = self._runtime(session_id)
        async with runtime.lock:
            try:
                await self._resume(runtime)
            except Exception as error:
                logger.exception("Avibe Agent resume failed for Session %s", session_id)
                await self._fail(request, "generic", f"resume failed: {error}", cause=error)
                return
            cwd = request.working_path or runtime.cwd
            try:
                router, selection = await self._preflight(request, session_id)
            except Exception as error:
                await self._fail_preflight(request, error)
                return
            run = self._new_run(request, session_id, turn_id, cwd, router, selection)
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
                async for event in run.agent.run(AgentInput(input_id, message), turn_id=turn_id):
                    await self._on_event(run, event)
                await self._settle(run)
            finally:
                runtime.run = None
                await self._admit_returned_inputs(run)
                await run.router.aclose()

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
            elif run is None:
                self.store.forget(runtime.session_id)
        return cleared

    def runtime_turn_keys(self) -> set[str]:
        return {self.runtime_turn_key(rt.run.request) for rt in self._runtimes.values() if rt.run is not None}

    def runtime_turn_keys_for_session_key(self, session_key: str) -> set[str]:
        return {
            self.runtime_turn_key(rt.run.request)
            for rt in self._runtimes.values()
            if rt.run is not None and rt.run.request.session_key == session_key
        }

    async def restore_pending_deliveries(self, platforms: set[str]) -> int:
        """At startup, once ``platforms`` can deliver: resume every Session a crash left with pending rows.

        Resume settles open tool calls, admits unconsumed inputs, and re-delivers
        pending outbox rows (recovery.md T2, T3, D1); it never runs the model.
        """
        session_ids = await asyncio.to_thread(self._sessions_with_pending_rows, platforms)
        for session_id in session_ids:
            runtime = self._runtime(session_id)
            async with runtime.lock:
                try:
                    await self._resume(runtime)
                except Exception:
                    logger.exception("Avibe Agent startup resume failed for Session %s", session_id)
        return len(session_ids)

    async def shutdown_runtime(self) -> None:
        """Disabling the backend ends its runs; the rolling refresh drains Turns before this."""
        for runtime in list(self._runtimes.values()):
            if runtime.run is not None:
                runtime.run.agent.abort("backend disabled")
        self._runtimes.clear()

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
        """A steer is in-process: it is accepted only while its run is alive or once consumed."""
        run = self._run_for(request.target_session_id, request.expected_logical_turn_id)
        message_id = await asyncio.to_thread(self._attempt_leader_id, request.attempt_id)
        if message_id is not None and await asyncio.to_thread(self._consumed, message_id):
            return steer_result(SteerOutcome.ACCEPTED, turn_id=request.expected_logical_turn_id)
        if run is not None and run.native_turn_id == request.expected_native_turn_id:
            # The accepted steer is still queued in the live run that owns it.
            return steer_result(SteerOutcome.ACCEPTED, turn_id=run.turn_id)
        return steer_result(SteerOutcome.NOT_ACTIVE, reason="not_active")

    # --- one run ---------------------------------------------------------------

    async def _preflight(self, request: AgentRequest, session_id: str) -> tuple[HubModelRouter, ModelSelection]:
        from modules.agents.model_hub import bind_launch, resolve_model_hub_launch

        model = request.subagent_model or request.vibe_agent_model
        if not model:
            raise ValueError("The Avibe Agent has no model selected.")
        context = request.context

        async def resolve_hop() -> dict[str, Any]:
            launch = await resolve_model_hub_launch(
                self.controller, BACKEND, model, process_scope=f"{BACKEND}:{session_id}", context=context
            )
            bind_launch(context, launch)
            return launch.to_hop_resolution()

        selection = selection_from_hop(await resolve_hop())
        return HubModelRouter(resolve_hop, self._providers, first=selection), selection

    def _new_run(
        self,
        request: AgentRequest,
        session_id: str,
        turn_id: str,
        cwd: str,
        router: HubModelRouter,
        selection: ModelSelection,
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
        agent.system = system_prompt((tool.spec.name for tool in tools), self._avibe_sections(request, cwd))
        return _Run(agent, router, request, session_id, turn_id, cwd)

    async def _on_event(self, run: _Run, event: AgentEvent) -> None:
        if isinstance(event, MessageCommitted):
            message = self.store.response(event.message_id)
            if message is not None:
                run.tool_calls.update({call.id: call for call in message.tool_calls})
                self._note_tokens(run, message)
            if event.final:
                # Delivered at the end of the run, which decides how the Turn settles.
                run.final_row = event.message_id
            else:
                await self._deliver(run.request.context, run.session_id, event.message_id, final=False)
        elif isinstance(event, ToolStarted):
            await self._emit_tool_started(run, event)
        elif isinstance(event, AgentError):
            run.errors.append((event.kind, event.message))
        elif isinstance(event, RunEnded):
            run.reason = event.reason

    async def _settle(self, run: _Run) -> None:
        """Settle the Turn from the run's outcome (loop-control.md section 6)."""
        request, context = run.request, run.request.context
        reason = run.reason or "error"
        kind, diagnostic = run.errors[0] if run.errors else (None, reason)
        if run.stop_requested and reason == "aborted":
            await self.controller.emit_agent_message(
                context, "result", "", level="silent", output=stop_output_for(request)
            )
            return
        final = self.store.response(run.final_row) if run.final_row else None
        if final is not None and strip_silent_blocks(self._display_source(final, final=True)).strip():
            # A refusal or safety stop without text already shows its explanation as the reply.
            await self._deliver(
                context,
                run.session_id,
                run.final_row,
                final=True,
                is_error=reason not in _COMPLETED,
                result_footer=self._result_footer(run, is_error=reason not in _COMPLETED),
                output=terminal_output_for(request),
            )
            return
        if reason in _COMPLETED and kind is None:
            await self.controller.emit_agent_message(
                context, "result", "", level="silent", output=terminal_output_for(request)
            )
            return
        await self._fail(request, kind, diagnostic, reason=reason)

    async def _admit_returned_inputs(self, run: _Run) -> None:
        """Inputs the run accepted but did not consume enter the context (recovery.md T3).

        They already are accepted messages of the settled Turn, which has no way back
        to the P3 queue; the next Turn answers them with the full context.
        """
        try:
            for item in await run.agent.take_pending_inputs():
                await self.store.consume_input(run.session_id, item.message_id, item.message)
        except Exception:
            logger.exception("Avibe Agent could not admit returned inputs for Session %s", run.session_id)

    # --- delivery from committed rows ------------------------------------------

    async def _deliver(
        self,
        context: Any,
        session_id: str,
        row_id: str,
        *,
        final: bool,
        is_error: bool = False,
        result_footer: Optional[str] = None,
        output: Optional[MessageOutput] = None,
        message: Optional[AssistantMessage] = None,
        delivered_parts: tuple[bool, ...] = (),
    ) -> None:
        message = message or self.store.response(row_id)
        if message is None:
            return
        source = self._display_source(message, final=final)
        if not source.strip():
            return

        async def acknowledge(index: int, count: int, native_message_id: Optional[str]) -> bool:
            return await self.store.record_delivery_part(
                session_id, row_id, index=index, count=count, native_message_id=native_message_id
            )

        committed = CommittedOutput(row_id=row_id, acknowledge=acknowledge, delivered_parts=delivered_parts)
        dispatcher = self.controller.message_dispatcher
        try:
            await dispatcher.emit_agent_message(
                context=context,
                message_type="result" if final else "assistant",
                text=source,
                is_error=is_error,
                result_footer=result_footer,
                output=output or MessageOutput(completes_turn=False, completes_run=False),
                committed=committed,
            )
        except Exception:
            # The row stays pending and is re-delivered before the Session's next run.
            logger.exception("Avibe Agent delivery of %s failed", row_id)
            if output is not None and output.completes_turn and not output.detached:
                await self.controller.emit_agent_message(
                    context, "result", "", level="silent", is_error=True, output=output
                )
        finally:
            manager = getattr(self.controller, "session_turns", None)
            complete = getattr(manager, "on_terminal_delivery_complete", None)
            if callable(complete):
                complete(context)

    async def _redeliver_pending(self, session_id: str) -> None:
        """Deliver every committed row whose delivery a crash or failure left pending (D1/D2)."""
        for item in await self.store.pending_deliveries(session_id):
            context = await asyncio.to_thread(self._redelivery_context, session_id, item.row_id)
            if context is None:
                logger.warning("Avibe Agent cannot re-deliver %s: its channel is not this Session's", item.row_id)
                continue
            # Detached: a re-delivered row never settles a Turn, old or current.
            await self._deliver(
                context,
                session_id,
                item.row_id,
                final=True,
                output=MessageOutput(completes_turn=False, completes_run=False, detached=True),
                message=item.message,
                delivered_parts=tuple(part is not None for part in item.parts),
            )

    def _redelivery_context(self, session_id: str, row_id: str) -> Any:
        from modules.im import MessageContext

        with self._engine.connect() as conn:
            row = messages_service.get_message(conn, row_id, session_id=session_id)
        if row is None:
            return None
        platform = row.get("platform")
        if platform == "avibe":
            # The row is the Workbench message; announcing it needs only the Session.
            return MessageContext(
                user_id="workbench",
                channel_id=session_id,
                platform="avibe",
                platform_specific={
                    "agent_session_id": session_id,
                    "workbench_session_id": session_id,
                    "platform": "avibe",
                },
            )
        builder = getattr(getattr(self.controller, "session_turns", None), "_build_context", None)
        context = builder(session_id) if callable(builder) else None
        return context if context is not None and context.platform == platform else None

    # --- resume (recovery.md T2, T3, D1) ---------------------------------------

    async def _resume(self, runtime: _SessionRuntime) -> None:
        session_id = runtime.session_id
        if not runtime.recovered:
            rows = await self.store.load(session_id)
            open_calls = open_tool_calls(rows)
            if open_calls:
                suite = self._tools()
                job_ids = {}
                for owner, call in open_calls:
                    job_id = suite.find_job(owner.session_id, call.id)
                    if job_id is not None:
                        job_ids[(owner.session_id, call.id)] = job_id
                await settle_open_calls(
                    session_id=session_id,
                    store=self.store,
                    jobs=suite.jobs,
                    job_ids=job_ids,
                    render_result=suite.render_recovered,
                )
            runtime.cwd = runtime.cwd or await asyncio.to_thread(self._session_workdir, session_id)
            runtime.recovered = True
        for message_id, text_value, files, metadata in await asyncio.to_thread(self._unconsumed_inputs, session_id):
            message = await self._render_input(session_id, text_value, files, metadata)
            await self.store.consume_input(session_id, message_id, message)
        await self._redeliver_pending(session_id)

    def _unconsumed_inputs(self, session_id: str) -> list[tuple[str, str, list[FileAttachment], AgentInputMetadata]]:
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
                    messages.c.author_id,
                    messages.c.author_name,
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
                metadata = AgentInputMetadata(user_id=row["author_id"], user_name=row["author_name"])
                inputs.append(
                    (row["id"], row["content_text"] or "", list(file_attachments_from_specs(specs) or ()), metadata)
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
        """The text a surface renders a response from: its text blocks, verbatim.

        A final refusal or safety stop without text is explained by localized copy
        written with the row, so a re-delivery shows it too (loop-control.md section 2).
        """
        value = render_text(message)
        if final and not value.strip() and message.stop_reason in _EXPLAINED_STOPS:
            return error_text(message.stop_reason, self._language())
        return value

    def _render_display(
        self,
        message: AssistantMessage,
        *,
        final: bool,
        conn: Any,
        platform: str,
        scope_id: Optional[str],
        session_id: str,
    ) -> RenderedDisplay:
        """The row's display copy, as ``persist_agent_message`` would have written it, in its transaction."""
        source = self._display_source(message, final=final)
        if not final:
            return RenderedDisplay(strip_silent_blocks(source), {"kind": "assistant"})
        capabilities = get_platform_descriptor(platform).capabilities
        enhanced = process_reply(
            source,
            include_quick_replies=getattr(self.config, "reply_enhancements", True),
            allow_unseparated_quick_replies=capabilities.supports_quick_replies,
            keep_file_links=platform == "avibe",
        )
        display = enhanced.text if enhanced.text.strip() else strip_silent_blocks(source)
        content: dict[str, Any] = {"kind": "result"}
        if platform == "avibe":
            from core.workbench_media import rewrite_agent_media

            display = rewrite_agent_media(conn, scope_id=scope_id, session_id=session_id, text=display)
            if enhanced.buttons:
                content["quick_replies"] = [button.text for button in enhanced.buttons]
            run = self._run_for(session_id, None)
            footer = self._result_footer(run, is_error=False) if run is not None else None
            if footer:
                content["result_footer"] = footer
        return RenderedDisplay(display, content)

    def _avibe_sections(self, request: AgentRequest, cwd: str) -> str:
        from core.managed_skills import managed_skill_claude_cli_path, managed_skill_project_base
        from core.system_prompt_injection import build_system_prompt_injection, get_enabled_agents_for_prompt

        context = request.context
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

    def _result_footer(self, run: Optional[_Run], *, is_error: bool) -> Optional[str]:
        if run is None or not getattr(self.config, "show_duration", True):
            return None
        context = run.request.context
        token_field = ""
        field_for = getattr(self.controller, "session_token_field", None)
        if callable(field_for):
            try:
                token_field = field_for(context) or ""
            except Exception:
                token_field = ""
        duration_ms = max(0, int((time.monotonic() - run.started_at) * 1000))
        try:
            footer = self._get_formatter(context).format_result_footer(
                "error" if is_error else "success", duration_ms, token_field=token_field
            )
        except Exception:
            return None
        return footer or None

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
            self._tool_suite = local_tool_suite(str(self._state_dir / "jobs"), watches=self._watch_jobs)
        return self._tool_suite

    def _runtime(self, session_id: str) -> _SessionRuntime:
        runtime = self._runtimes.get(session_id)
        if runtime is None:
            runtime = self._runtimes[session_id] = _SessionRuntime(session_id)
        return runtime

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

    def _sessions_with_pending_rows(self, platforms: set[str]) -> list[str]:
        from sqlalchemy import func

        with self._engine.connect() as conn:
            return list(
                conn.execute(
                    select(messages.c.session_id)
                    .select_from(messages.join(agent_sessions, agent_sessions.c.id == messages.c.session_id))
                    .where(
                        agent_sessions.c.agent_backend == BACKEND,
                        messages.c.context_seq.is_not(None),
                        messages.c.platform.in_(sorted(platforms - {""})),
                        func.json_extract(messages.c.metadata_json, "$.delivery.state") == "pending",
                    )
                    .group_by(messages.c.session_id)
                ).scalars()
            )

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

