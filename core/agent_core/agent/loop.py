"""C-3 in-process agent loop; persistence precedes every committed-row event.

Queue semantics informed by Pi (MIT), packages/agent/src/agent-loop.ts, and the
bare-loop control experiment. This is an Avibe implementation, not a port.
With a ``ContextConfig`` the loop also applies C-9 before every model request:
clearing, the forked checkpoint turn, and the overflow ladder
(``agent-core-contracts/context.md``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Mapping, Optional, Sequence, TypeVar

from core.agent_core.agent.checkpoint import BUDGET_USED, CheckpointPolicy
from core.agent_core.agent.events import (
    AgentError,
    AgentEvent,
    AssistantTextDelta,
    AssistantThinkingDelta,
    CompactionFailed,
    CompactionFinished,
    CompactionPaused,
    CompactionSkipped,
    CompactionStarted,
    ContextExhausted,
    ContextPart,
    MessageCommitted,
    RunEnded,
    RunEndReason,
    RunStarted,
    SteerApplied,
    ToolFinished,
    ToolProgress,
    ToolStarted,
)
from core.agent_core.agent.hooks import (
    AgentInput,
    AlterArgs,
    AlterResult,
    Deny,
    End,
    Hooks,
    RunContext,
    RunOutcome,
    SkipTools,
    Snapshot,
)
from core.agent_core.agent.jobs import TrackingJobHost
from core.agent_core.agent.lifecycle import RunAborted as _Aborted, RunScope
from core.agent_core.agent.models import ModelRouter, ModelSelection, RetryPolicy
from core.agent_core.agent.outcome import OutcomeOwner
from core.agent_core.agent.state import HookStateError, state_representation
from core.agent_core.ai.provider import (
    Done,
    ModelRequest,
    ProviderError,
    TextDelta,
    ThinkingDelta,
)
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.context import (
    CHECKPOINT_TOOL_FLOOR,
    CHECKPOINT_TOOL_ROUNDS,
    CHECKPOINT_TOOL_SLACK,
    CLEAR_SOFT_RATIO,
    DEFAULT_MAX_OUTPUT_TOKENS,
    INEFFECTIVE_RATIO,
    MAX_OVERFLOWS,
    MAX_ROLLS,
    PAUSE_AFTER,
    PROMPT_VERSION,
    Budget,
    ContextConfig,
    StateRequest,
    add_usage,
    budget,
    carried_skills,
    checkpoint_max_tokens,
    checkpoint_request,
    checkpoint_text,
    clear_edit,
    clearable_results,
    compaction_payload,
    fit_result,
    half_cut,
    last_anchor,
    message_tokens,
    messages_tokens,
    normal_cut,
    output_tokens,
    request_facts,
    request_tokens,
    rolling_cut,
    sent_tokens,
    summarized_to_seq,
    text_tokens,
    unit_tokens,
)
from core.agent_core.harness.projection import (
    ContextView,
    ProjectionError,
    context_view,
    project,
    validate_message_append,
)
from core.agent_core.harness.store import ContextEntry, TranscriptStore
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    message_to_dict,
    text,
    usage_to_dict,
)
from core.agent_core.tools.base import JobHost, Tool, ToolContext, ToolResult

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _Ended(Exception):
    pass


class ProviderProtocolViolation(ValueError):
    """A model response cannot be admitted to the canonical transcript."""


class UnsupportedModelRoute(ValueError):
    """The selected model cannot provide the tools required by the agent."""


class _Exhausted(Exception):
    """C-9 section 8 (d): nothing more can move out and the request still does not fit."""

    def __init__(self, limit: int, parts: tuple[ContextPart, ...]) -> None:
        self.limit, self.parts = limit, parts
        sizes = ", ".join(f"{part.name} ~{part.tokens}" for part in parts)
        super().__init__(f"The context does not fit the model's input limit of {limit} tokens: {sizes}.")


#: C-9 guard state (``AgentState.context``) before anything happened.
_GUARD_DEFAULT: dict[str, Any] = {"failures": 0, "ineffective": 0, "paused": False}
#: The checkpoint request without a focus, as the fit checks measure it.
_CHECKPOINT_REQUEST = checkpoint_request()


async def _silent(*_: Any, **__: Any) -> None:
    """The checkpoint turn's emit: it shows nothing (C-9 section 6)."""


@dataclass
class _Ladder:
    """One model request's progress through C-9 sections 3 and 8."""

    compacted: bool = False
    summary_failed: bool = False
    rolls: int = 0
    overflows: int = 0
    #: The provider refused the request as overflow: the next stage takes a ladder step whatever it measures.
    refused: bool = False


@dataclass(frozen=True)
class _Attempt:
    """One model attempt of the run: the C-9 attempt ledger's entry (context.md section 10).

    ``message`` is the response, or the partial a failed attempt carried.
    """

    purpose: Literal["conversation", "checkpoint"]
    request: ModelRequest
    message: Optional[AssistantMessage]
    error: Optional[ProviderError] = None


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    overflow: bool = False


class _ForkTooLarge(Exception):
    """A checkpoint request that cannot fit the window; it is never sent."""


class Agent:
    """One Session's single writer. All methods run on the same asyncio loop.

    ``turn_id`` is the Avibe Turn id. ``steer``/``follow_up`` return False after
    finality closes admission, allowing the adapter to retain the input in P3.
    Tools that launch jobs must use ``agent.jobs`` (or pass one TrackingJobHost
    both here and to the tools) so abort owns every foreground handle.
    """

    def __init__(
        self,
        *,
        session_id: str,
        models: ModelRouter,
        tools: Sequence[Tool],
        hooks: Sequence[Hooks],
        store: TranscriptStore,
        jobs: JobHost,
        cwd: str,
        env: Optional[Mapping[str, str]] = None,
        system: str = "",
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        reasoning_effort: Optional[str] = None,
        retry: RetryPolicy = RetryPolicy(),
        rehydrate: Optional[Callable[[Mapping[str, Any]], Sequence[Message]]] = None,
        context: Optional[ContextConfig] = None,
    ) -> None:
        self.session_id = session_id
        self.models = models
        self.store = store
        self.jobs = jobs if isinstance(jobs, TrackingJobHost) else TrackingJobHost(jobs)
        self.hooks = tuple(hooks)
        self.cwd, self.env = cwd, dict(env or {})
        self.system = system
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self.max_tokens, self.reasoning_effort = max_tokens, reasoning_effort
        self.retry = retry
        self.rehydrate = rehydrate
        if context is not None and self.hooks:
            # Hooks with context management are a post-v1 design item (plan section 10).
            raise ValueError("ContextConfig and user hooks are mutually exclusive in v1")
        self.context = context
        self._tools: dict[str, Tool] = {}
        self._run_tools: Optional[dict[str, Tool]] = None
        self.set_tools(tools)
        self._lock = asyncio.Lock()
        self._steers: deque[AgentInput] = deque()
        self._follow_ups: deque[AgentInput] = deque()
        self._running = False
        self._open = False
        self._consumer_closed = False
        self._ctx = RunContext(session_id, "", CancelToken())
        self._scope = RunScope(self._ctx.cancel)
        self._outcome = OutcomeOwner()
        self._rows: list[ContextEntry] = []
        self._committed_state: dict[str, Any] = {}
        self._committed_state_json = "{}"
        self._guard: dict[str, Any] = dict(_GUARD_DEFAULT)
        self._committed_guard: dict[str, Any] = dict(_GUARD_DEFAULT)
        # The run's attempt ledger (C-9): every model attempt, in order.
        self._attempts: list[_Attempt] = []
        self._last_model_at: Optional[float] = None
        self._seq = 0

    def set_tools(self, tools: Sequence[Tool]) -> None:
        names = [tool.spec.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        self._tools = dict(zip(names, tools))
        if self._run_tools is not None:
            self._run_tools = dict(self._tools)

    async def steer(self, input: AgentInput) -> bool:
        async with self._lock:
            if not self._open or self._ctx.cancel.cancelled:
                return False
            self._steers.append(deepcopy(input))
            return True

    async def follow_up(self, input: AgentInput) -> bool:
        async with self._lock:
            if not self._open or self._ctx.cancel.cancelled:
                return False
            self._follow_ups.append(deepcopy(input))
            return True

    async def take_pending_inputs(self) -> tuple[AgentInput, ...]:
        """Return accepted but unconsumed inputs to the adapter.

        Call after the run closes admission (normally after RunEnded). The
        adapter still has the original durable rows; this transfers admission
        ownership back in steer/follow-up priority order, exactly once. The
        inputs still belong to the Turn that accepted them, never to a new P3
        Turn (``recovery.md`` T3).
        """
        async with self._lock:
            if self._open:
                raise RuntimeError("cannot return pending inputs while admission is open")
            pending = tuple(deepcopy([*self._steers, *self._follow_ups]))
            self._steers.clear()
            self._follow_ups.clear()
            return pending

    def abort(self, reason: str = "aborted") -> None:
        if self._running:
            self._outcome.primary("aborted")
            self._ctx.cancel.cancel(reason)

    def snapshot(self) -> Snapshot:
        """A committed cut, including only state that can be restored there."""
        seq = self._rows[-1].context_seq if self._rows else 0
        return Snapshot(self.session_id, seq, deepcopy(self._committed_state))

    def run(self, input: AgentInput, *, turn_id: str) -> AsyncIterator[AgentEvent]:
        input = deepcopy(input)
        return self._pump(turn_id, lambda emit: self._drive(emit, input=input), admit=True)

    def compact(self, *, turn_id: str, focus: Optional[str] = None) -> AsyncIterator[AgentEvent]:
        """Manual ``/compact [focus]`` (C-9 section 10): clear the pause, then write a normal checkpoint.

        Runs like a run without an input: it refuses while a run (or another
        compaction) is active, admits no steer, and ends ``completed`` when the
        checkpoint was written.
        """
        if self.context is None:
            raise RuntimeError("context management is not configured for this Agent")
        return self._pump(turn_id, lambda emit: self._drive(emit, focus=focus), admit=False)

    async def _pump(
        self, turn_id: str, drive: Callable[[Callable[..., Awaitable[None]]], Awaitable[None]], *, admit: bool
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            if self._running:
                raise RuntimeError("this Agent already has an active run")
            if self._steers or self._follow_ups:
                raise RuntimeError("return unconsumed inputs with take_pending_inputs() before starting another run")
            self._running = True
            self._open = admit
            self._consumer_closed = False
            self._ctx = RunContext(self.session_id, turn_id, CancelToken())
            self._scope = RunScope(self._ctx.cancel)
            self._outcome = OutcomeOwner()
            self._seq = 0

        # Bounded delivery applies backpressure to provider streaming. No emit
        # awaits this queue while holding the input-admission lock.
        events: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=64)

        async def emit(event_type: Any, **fields: Any) -> None:
            if self._consumer_closed:
                return
            event = event_type(turn_id=turn_id, seq=self._seq, **fields)
            self._seq += 1
            await events.put(event)

        worker = asyncio.create_task(drive(emit))
        receive: Optional[asyncio.Task] = None
        try:
            while True:
                if worker.done() and events.empty():
                    await worker
                    break
                receive = asyncio.create_task(events.get())
                ready, _ = await asyncio.wait({worker, receive}, return_when=asyncio.FIRST_COMPLETED)
                if receive in ready:
                    yield receive.result()
                    receive = None
                elif worker.done() and events.empty():
                    receive.cancel()
                    with suppress(asyncio.CancelledError):
                        await receive
                    receive = None
                    await worker
                    break
                else:
                    yield await receive
                    receive = None
        finally:
            if receive is not None:
                receive.cancel()
                with suppress(asyncio.CancelledError):
                    await receive
            if not worker.done():
                self._consumer_closed = True
                self.abort("event consumer closed")
                worker.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await worker
            finally:
                async with self._lock:
                    self._running = self._open = False
                    self._run_tools = None

    async def _save_state(self, *, cleanup: bool = False) -> None:
        """Hook state at a commit point. C-9 guard state is written only by ``_commit_context``."""
        representation = state_representation(self._ctx.state)
        if representation == self._committed_state_json:
            return
        state = deepcopy(self._ctx.state)

        async def persist() -> ContextEntry:
            return await self.store.append_payload(self.session_id, "agent_state", {"version": 1, "state": state})

        row = await persist() if cleanup else await self._scope.call(persist, interruptible=False)
        self._rows.append(row)
        self._committed_state = deepcopy(state)
        self._committed_state_json = representation

    async def _hook(self, factory: Callable[[], Awaitable[T]], *, cleanup: bool = False) -> T:
        result = await factory() if cleanup else await self._scope.call(factory)
        state_representation(self._ctx.state)
        # End is a directive; its owning stage selects the outcome only after
        # the required state and step commits have succeeded.
        return result

    async def _consume(self, input: AgentInput) -> None:
        validate_message_append(self._rows, session_id=self.session_id, kind="input", message=input.message)
        await self._save_state()
        row = await self._scope.call(
            lambda: self.store.consume_input(self.session_id, input.message_id, deepcopy(input.message)),
            interruptible=False,
        )
        self._rows.append(row)

    def _validate_response(self, message: AssistantMessage) -> None:
        try:
            validate_message_append(self._rows, session_id=self.session_id, kind="response", message=message)
        except ProjectionError as error:
            raise ProviderProtocolViolation(f"Provider protocol violation: {error}") from error

    async def _response(self, message: AssistantMessage, *, final: bool) -> ContextEntry:
        self._validate_response(message)
        await self._save_state()
        facts = {}
        if self.context is not None:
            # The request this response answered: the ledger's latest conversation attempt (the C-9 anchor).
            attempt = next((item for item in reversed(self._attempts) if item.purpose == "conversation"), None)
            if attempt is not None:
                facts = {"request": request_facts(attempt.request)}
        row = await self._scope.call(
            lambda: self.store.append_response(self.session_id, deepcopy(message), final=final, **facts),
            interruptible=False,
        )
        self._rows.append(row)
        return row

    async def _drive(
        self,
        emit: Callable[..., Awaitable[None]],
        *,
        input: Optional[AgentInput] = None,
        focus: Optional[str] = None,
    ) -> None:
        """One run of ``input``, or, without one, a manual compaction with ``focus``."""
        loaded = False
        try:
            try:
                await emit(RunStarted)
                # Preflight before hooks or input consumption. Reuse the selection
                # for the first request; later attempts resolve the route afresh.
                selected = await self._scope.call(self.models.resolve)
                self._check_model_route(selected)
                self._rows = list(await self._scope.call(lambda: self.store.load(self.session_id)))
                projection = project(self._rows)
                self._ctx.state = deepcopy(dict(projection.state))
                self._committed_state_json = state_representation(self._ctx.state)
                self._committed_state = deepcopy(self._ctx.state)
                self._guard = {**_GUARD_DEFAULT, **deepcopy(dict(projection.context_state))}
                self._committed_guard = dict(self._guard)
                self._attempts = []
                loaded = True
                system = self.system
                self._run_tools = dict(self._tools)
                if input is None:
                    self._outcome.primary(await self._manual_compaction(system, selected, focus, emit))
                else:
                    for hook in self.hooks:
                        setup = await self._hook(lambda: hook.before_run(deepcopy(input), self._ctx))
                        if setup is not None:
                            if setup.system is not None:
                                system = setup.system
                            if setup.tools is not None:
                                names = [tool.spec.name for tool in setup.tools]
                                if len(names) != len(set(names)):
                                    raise ValueError("tool names must be unique")
                                self._run_tools = dict(zip(names, setup.tools))
                    await self._consume(input)
                    self._outcome.primary(await self._loop(system, emit, selected))
            except _Ended:
                self._outcome.primary("ended_by_hook")
            except _Exhausted as error:
                self._outcome.primary("context_exhausted")
                await emit(ContextExhausted, limit=error.limit, parts=error.parts)
                await emit(AgentError, kind="context_exhausted", message=str(error))
            except _Aborted:
                self._outcome.primary("aborted")
            except asyncio.CancelledError:
                self._outcome.primary("aborted")
                self._ctx.cancel.cancel("event consumer closed" if self._consumer_closed else "dependency cancelled")
                if not self._consumer_closed:
                    await emit(AgentError, kind="dependency_cancelled", message="An agent dependency was cancelled.")
            except Exception as error:
                self._outcome.primary("error")
                if isinstance(error, HookStateError):
                    self._ctx.state = deepcopy(self._committed_state)
                await emit(AgentError, kind=type(error).__name__, message=str(error))
        finally:
            # If an external cancellation interrupted an exception handler,
            # admission still records its first cause before cleanup starts.
            self._outcome.primary("aborted" if self._ctx.cancel.cancelled else "error")
            self._scope.stop()
            # Cleanup is a separate joined task, outside cancelled admission.
            # Consumer closure cannot interrupt commit/job ownership release.
            cleanup = asyncio.create_task(self._finish(loaded, emit, run_hooks=input is not None))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
        if self._consumer_closed:
            raise asyncio.CancelledError()
        await emit(RunEnded, reason=self._outcome.reason)

    async def _flush_diagnostics(self, emit: Callable[..., Awaitable[None]]) -> None:
        while self._outcome.diagnostics:
            kind, message = self._outcome.diagnostics[0]
            await emit(AgentError, kind=kind, message=message)
            self._outcome.diagnostics.popleft()

    async def _cleanup(self, factory: Callable[[], Awaitable[Any]], *, stream: bool = False) -> None:
        try:
            await factory()
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, HookStateError):
                self._ctx.state = deepcopy(self._committed_state)
            kind = (
                "stream_cleanup"
                if stream
                else ("dependency_cancelled" if isinstance(error, asyncio.CancelledError) else type(error).__name__)
            )
            self._outcome.diagnostic(kind, f"Cleanup failed: {type(error).__name__}: {error}")

    async def _finish(self, loaded: bool, emit: Callable[..., Awaitable[None]], *, run_hooks: bool) -> None:
        async with self._lock:
            self._open = False
        await self._scope.close()
        await self._flush_diagnostics(emit)
        try:
            if loaded:
                await self._cleanup(lambda: self._save_state(cleanup=True))
                if run_hooks:
                    outcome = RunOutcome(self._ctx.turn_id, self._outcome.reason, self.snapshot())
                    for hook in self.hooks:
                        await self._cleanup(
                            lambda: self._hook(lambda: hook.after_run(outcome, self._ctx), cleanup=True)
                        )
                    await self._cleanup(lambda: self._save_state(cleanup=True))
        finally:
            # Sweep on every terminal path, including failed tool cleanup and
            # cleanup hooks. Handed-over handles are no longer foreground.
            try:
                reason = "aborted" if self._outcome.reason == "aborted" or self._ctx.cancel.cancelled else "killed"
                await self.jobs.kill_foreground(self.session_id, reason=reason)
            except Exception as error:
                self._outcome.foreground_leaked()
                if self._consumer_closed:
                    raise
                self._outcome.diagnostic(type(error).__name__, str(error))
            await self._flush_diagnostics(emit)

    @staticmethod
    def _check_model_route(selected: ModelSelection) -> None:
        if selected.capabilities.supports_tools is False:
            raise UnsupportedModelRoute("The selected model does not support tools; the agent requires tool support.")

    def _rehydrated(self) -> tuple[Message, ...]:
        return tuple(self.rehydrate(deepcopy(self._ctx.state))) if self.rehydrate else ()

    async def _request(
        self,
        system: str,
        selected: ModelSelection,
        *,
        messages: Sequence[Message],
        max_tokens: Optional[int] = None,
    ) -> tuple[ModelRequest, dict[str, Tool]]:
        """A request carrying ``messages``, after the user's ``before_model`` hooks: the first pipeline stage."""
        self._check_model_route(selected)
        capabilities = selected.capabilities
        tools = dict(self._run_tools if self._run_tools is not None else self._tools)
        request = ModelRequest(
            endpoint=deepcopy(selected.endpoint),
            system=system,
            messages=tuple(deepcopy(messages)),
            tools=tuple(deepcopy(tool.spec) for tool in tools.values()),
            max_tokens=output_tokens(capabilities, self.max_tokens) if max_tokens is None else max_tokens,
            reasoning_effort=(
                self.reasoning_effort
                if capabilities.supports_reasoning is True and self.reasoning_effort in capabilities.reasoning_efforts
                else None
            ),
            supports_images=capabilities.supports_images is True,
        )
        for hook in self.hooks:
            decision = await self._hook(lambda: hook.before_model(request, self._ctx))
            if isinstance(decision, End):
                await self._save_state()
                raise _Ended()
            if decision is not None:
                request = decision
        allowed = {spec.name for spec in request.tools}
        return request, {name: tool for name, tool in tools.items() if name in allowed}

    @asynccontextmanager
    async def _model(
        self,
        system: str,
        emit: Callable[..., Awaitable[None]],
        selected: Optional[ModelSelection] = None,
        *,
        compose: Optional[Callable[[ModelSelection], Awaitable[tuple[ModelRequest, dict[str, Tool]]]]] = None,
        purpose: Literal["conversation", "checkpoint"] = "conversation",
    ) -> AsyncIterator[tuple[Done | ProviderError, dict[str, Tool]]]:
        """One model request with its transient retries.

        ``compose(selection)`` is the request pipeline: it returns the final request, which is sent unchanged.
        A conversation request's pipeline is ``_compose``. Every attempt enters the run's attempt ledger. A
        conversation request the provider rejects as overflow goes back through the pipeline with the ladder
        told to shrink (C-9 section 8). A checkpoint attempt is never context.
        """
        ladder = _Ladder()
        if compose is None:

            async def compose(selection: ModelSelection) -> tuple[ModelRequest, dict[str, Tool]]:
                return await self._compose(system, selection, emit, ladder)

        retries = 0
        started = time.monotonic()
        retry_error: Optional[ProviderError] = None
        while True:
            self._scope.check()
            if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                yield retry_error, {}
                return
            if selected is None:
                selected = await self._scope.call(self.models.resolve)
            # Resolution is the last async stage before request admission.
            # Expiry wins before route validation, projection, rehydration or
            # hooks can replace the original error or persist new side effects.
            if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                yield retry_error, {}
                return
            route = selected
            request, tools = await compose(selected)
            selected = None
            if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                yield retry_error, tools
                return
            self._scope.check()
            stream = self.models.provider_for(request.endpoint.protocol).stream(request, self._ctx.cancel)
            streamed = False
            terminal = None
            relieve = False
            try:
                while True:
                    try:
                        event = await self._scope.call(lambda: anext(stream))
                    except StopAsyncIteration:
                        break
                    if isinstance(event, (Done, ProviderError)):
                        terminal = event
                        break
                    streamed = True
                    if isinstance(event, TextDelta):
                        await emit(AssistantTextDelta, delta=event.delta)
                    elif isinstance(event, ThinkingDelta):
                        await emit(AssistantThinkingDelta, delta=event.delta)
                if self.context is not None:
                    self._last_model_at = self.context.clock()
                if terminal is None:
                    terminal = ProviderError("unknown", "Provider stream ended without a terminal event.", False)
                self._attempts.append(
                    _Attempt(
                        purpose,
                        request,
                        terminal.message if isinstance(terminal, Done) else terminal.partial,
                        None if isinstance(terminal, Done) else terminal,
                    )
                )
                relieve = (
                    purpose == "conversation"
                    and self.context is not None
                    and isinstance(terminal, ProviderError)
                    and terminal.kind == "overflow"
                    and (terminal.partial is None or not terminal.partial.content)
                )
                delay = (
                    self.retry.delay(terminal, retries=retries, streamed=streamed, elapsed_s=time.monotonic() - started)
                    if isinstance(terminal, ProviderError) and not relieve
                    else None
                )
                if delay is None and not relieve:
                    # The caller admits and commits this terminal inside the
                    # scope, BEFORE aclose. Never retry an accepted terminal.
                    yield terminal, tools
                    return
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await self._cleanup(close, stream=True)
            # The attempt is not the run's answer: its partial stays in the ledger, and a conversation
            # attempt's billed usage is kept as a non-final row before the request is tried again.
            # A retried or relieved attempt is never context: it stays in the ledger (and the audit), and the
            # request goes out again unchanged.
            await self._record_attempt(self._attempts[-1])
            terminal = replace(terminal, partial=None)
            if relieve:
                ladder.overflows += 1
                ladder.refused = True
                if ladder.overflows >= MAX_OVERFLOWS:
                    view = context_view(self._rows)
                    raise self._exhausted(request, view, budget(request, route.capabilities, transcript=view.messages))
                retries, started, retry_error = 0, time.monotonic(), None
                continue
            retry_error = terminal
            retries += 1
            await self._scope.call(lambda: asyncio.sleep(delay))

    async def _record_attempt(self, attempt: _Attempt) -> None:
        """A conversation attempt that never became context: its billed usage as a non-context audit row."""
        partial = attempt.message
        if self.context is None or attempt.purpose != "conversation" or partial is None or partial.usage is None:
            return
        payload: dict[str, Any] = {
            "version": 1,
            "usage": usage_to_dict(partial.usage),
            "error": f"{attempt.error.kind}: {attempt.error.message}" if attempt.error is not None else None,
        }
        await self._audit("attempt", payload)

    async def _commit_model_message(self, message: AssistantMessage, emit: Callable[..., Awaitable[None]]) -> bool:
        """Finality and queued-input admission share one lock with the commit."""
        pending: list[tuple[AgentInput, bool]] = []
        consumed: list[tuple[AgentInput, bool]] = []
        failed = message.stop_reason in {"error", "aborted"}
        row = None
        try:
            async with self._lock:
                if not message.tool_calls and not failed:
                    pending = [(item, True) for item in self._steers]
                    if not pending:
                        pending = [(item, False) for item in self._follow_ups]
                final = not message.tool_calls and not pending and not failed
                if final or failed:
                    self._open = False
                row = await self._response(message, final=final)
                for item, is_steer in pending:
                    await self._consume(item)
                    (self._steers if is_steer else self._follow_ups).popleft()
                    consumed.append((item, is_steer))
        finally:
            if row is not None and not self._consumer_closed:
                await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=final)
                for item, is_steer in consumed:
                    if is_steer:
                        await emit(SteerApplied, message_id=item.message_id)
        return final

    async def _loop(self, system: str, emit: Callable[..., Awaitable[None]], selected: ModelSelection) -> RunEndReason:
        first_selection: Optional[ModelSelection] = selected
        length_tool_retries = 0
        while True:
            async with self._model(system, emit, first_selection) as (terminal, tools):
                first_selection = None
                if isinstance(terminal, ProviderError):
                    if terminal.partial is not None and not terminal.partial.content:
                        # A usage-only partial is attempt data, never context.
                        await self._record_attempt(self._attempts[-1])
                        terminal = replace(terminal, partial=None)
                    if terminal.partial is not None:
                        self._validate_response(terminal.partial)
                    reason = (
                        "context_exhausted"
                        if terminal.kind == "overflow"
                        else "aborted"
                        if terminal.kind == "aborted"
                        else "error"
                    )
                    self._outcome.primary(reason)
                    await emit(AgentError, kind=terminal.kind, message=terminal.message)
                    if terminal.partial is not None:
                        row = await self._response(terminal.partial, final=False)
                        await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=False)
                    return reason
                message = terminal.message
                self._validate_response(message)
                failed = message.stop_reason in {"error", "aborted"}
                if not (message.tool_calls and message.stop_reason == "length"):
                    length_tool_retries = 0
                if failed:
                    self._outcome.primary("aborted" if message.stop_reason == "aborted" else "error")
                    await emit(
                        AgentError,
                        kind=message.stop_reason,
                        message=message.error_message or f"Model stopped with {message.stop_reason}.",
                    )
                final = await self._commit_model_message(message, emit)
                empty_reply = final and not any(
                    isinstance(block, TextBlock) and block.text and block.text.strip() for block in message.content
                )
                if empty_reply:
                    self._outcome.primary("error")
                    await emit(
                        AgentError,
                        kind=message.stop_reason if message.stop_reason in {"refusal", "safety"} else "empty_response",
                        message=message.error_message or f"Model stopped with {message.stop_reason} without a reply.",
                    )
            # aclose failures are diagnostics, never a reason to discard the
            # committed response or skip its tools. Primary errors precede them.
            await self._flush_diagnostics(emit)
            end, skip = False, False
            for hook in self.hooks:
                decision = await self._hook(lambda: hook.after_model(deepcopy(message), self._ctx))
                if isinstance(decision, End):
                    end = True
                    break
                skip = skip or isinstance(decision, SkipTools)
            if message.tool_calls and message.stop_reason != "tool_use":
                reason = message.stop_reason
                skip_reason = (
                    "Tool call was truncated by the output limit; re-issue it with complete arguments."
                    if reason == "length"
                    else f"Tool call not executed because the model stopped with {reason}."
                )
                for call in message.tool_calls:
                    self._scope.check()
                    await self._tool(
                        call,
                        tools,
                        True,
                        emit,
                        skip_reason=skip_reason,
                    )
                if reason == "length":
                    if end:
                        return "ended_by_hook"
                    length_tool_retries += 1
                    # Reuse the bounded loop retry budget so an impossible
                    # tool call cannot spin forever after repeated truncation.
                    if length_tool_retries > self.retry.max_retries:
                        self._open = False
                        self._outcome.primary("error")
                        await emit(
                            AgentError,
                            kind="length",
                            message="The model repeatedly exceeded its output limit while emitting a tool call.",
                        )
                        return "error"
                    await self._drain_steers(emit)
                    continue
                self._open = False
                if not failed:
                    self._outcome.primary("error")
                    await emit(
                        AgentError,
                        kind=reason,
                        message=message.error_message or f"Model stopped with {reason}.",
                    )
                return "aborted" if message.stop_reason == "aborted" else "error"
            if failed:
                return "aborted" if message.stop_reason == "aborted" else "error"
            if not message.tool_calls:
                await self._save_state()
                if empty_reply:
                    return "error"
                if end:
                    return "ended_by_hook"
                if final:
                    return "completed"
                continue

            terminate = False
            for call in message.tool_calls:
                self._scope.check()
                result, step_end = await self._tool(call, tools, skip or end, emit)
                terminate = terminate or result.terminate
                end = end or step_end
            if end:
                return "ended_by_hook"
            if terminate:
                return "completed"
            await self._drain_steers(emit)

    async def _drain_steers(self, emit: Callable[..., Awaitable[None]]) -> None:
        applied = []
        try:
            async with self._lock:
                while self._steers:
                    item = self._steers[0]
                    await self._consume(item)
                    self._steers.popleft()
                    applied.append(item)
        finally:
            if not self._consumer_closed:
                for item in applied:
                    await emit(SteerApplied, message_id=item.message_id)

    async def _gate(
        self, call: ToolCallBlock, hooks: Sequence[Hooks]
    ) -> tuple[ToolCallBlock, Optional[ToolResult], bool]:
        """``before_tool``: the call as altered, the result when a hook denied or ended it, and whether one ended."""
        for hook in hooks:
            decision = await self._hook(lambda: hook.before_tool(call, self._ctx))
            if isinstance(decision, Deny):
                return call, ToolResult((text(decision.reason),), is_error=True), False
            if isinstance(decision, End):
                return call, ToolResult((text("[skipped by policy]"),), is_error=True), True
            if isinstance(decision, AlterArgs):
                call = replace(call, arguments=deepcopy(decision.arguments))
        return call, None, False

    async def _tool(
        self,
        original: ToolCallBlock,
        tools: Mapping[str, Tool],
        skip: bool,
        emit: Callable[..., Awaitable[None]],
        *,
        skip_reason: str | None = None,
    ) -> tuple[ToolResult, bool]:
        call = deepcopy(original)
        end = False
        result = None
        if skip:
            result = ToolResult((text(skip_reason or "[skipped by policy]"),), is_error=True)
        else:
            call, result, end = await self._gate(call, self.hooks)
        await emit(
            ToolStarted,
            tool_call_id=call.id,
            name=call.name,
            preview=json.dumps(call.arguments, ensure_ascii=False)[:500],
        )
        if result is None:
            tool = tools.get(call.name)
            if tool is None:
                result = ToolResult((text(f"Tool {call.name} is not available."),), is_error=True)
            else:
                result = await self._execute(tool, call, emit)
        if not end and not skip:
            for hook in self.hooks:
                decision = await self._hook(lambda: hook.after_tool(call, result, self._ctx))
                if isinstance(decision, End):
                    end = True
                    break
                if isinstance(decision, AlterResult):
                    result = decision.result
        message = ToolResultMessage(call.id, call.name, result.content, result.is_error)
        validate_message_append(self._rows, session_id=self.session_id, kind="tool_result", message=message)
        await self._save_state()
        row = await self._scope.call(
            lambda: self.store.append_tool_result(self.session_id, deepcopy(message), details=deepcopy(result.details)),
            interruptible=False,
        )
        self._rows.append(row)
        await emit(
            ToolFinished,
            tool_call_id=call.id,
            event_id=row.row_id,
            is_error=result.is_error,
            watch_id=result.details.get("watch_id"),
        )
        return result, end

    async def _execute(
        self, tool: Tool, call: ToolCallBlock, emit: Callable[..., Awaitable[None]], *, pinned: Optional[str] = None
    ) -> ToolResult:
        # Progress is a latest-tail value, not an unbounded buffer of tool output.
        updates: asyncio.Queue[str] = asyncio.Queue(maxsize=1)

        def progress(tail: str) -> None:
            if updates.full():
                updates.get_nowait()
            updates.put_nowait(tail)

        ctx = ToolContext(
            self.session_id,
            call.id,
            self.cwd,
            dict(self.env),
            self._ctx.cancel,
            on_progress=progress,
            pinned_target=pinned,
        )
        execution = asyncio.create_task(self._scope.call(lambda: tool.execute(call.arguments, ctx)))
        update = None
        try:
            while not execution.done() or not updates.empty():
                update = asyncio.create_task(updates.get())
                ready, _ = await asyncio.wait({execution, update}, return_when=asyncio.FIRST_COMPLETED)
                if update in ready:
                    await emit(ToolProgress, tool_call_id=call.id, tail=update.result())
                    update = None
                elif execution.done():
                    break
            return await execution
        except (_Aborted, asyncio.CancelledError):
            raise
        except Exception as error:
            # Tools are an explicit failure boundary; preserve the error as a
            # model-visible result so the next call can correct its arguments.
            logger.exception("Tool execution failed: %s", call.name)
            message = str(error) or f"{type(error).__name__}: tool execution failed."
            return ToolResult((text(message[:500]),), is_error=True)
        finally:
            for task in (update, execution):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (update, execution) if task is not None), return_exceptions=True)
            # Release failure is supplementary to a tool's cancellation or
            # result. The final sweep alone arbitrates a still-leaked process.
            # A user abort is recorded as such, so bash and recovery report "Command aborted".
            release_reason = "aborted" if self._ctx.cancel.cancelled else "killed"
            await self._cleanup(
                lambda: self.jobs.kill_foreground(self.session_id, tool_call_id=call.id, reason=release_reason)
            )

    # --- C-9 context management (agent-core-contracts/context.md) ---------------
    #
    # Invariants (context.md section 10):
    # 1. Request pipeline: projection -> user before_model -> budget() on that final request -> C-9 stage
    #    -> send. A stage that changes the context rebuilds the request from the top; nothing changes a
    #    request after it is budgeted.
    # 2. Tool pipeline: user before_tool -> checkpoint policy on the final arguments -> execute -> bound
    #    every result; artifacts are recorded from the final arguments after execution.
    # 3. ``_commit_context`` is the one writer of C-9 state: one transaction per transition, before any event.
    # 4. ``self._attempts`` is the one record of model attempts; the audit and the anchor read from it.
    # 5. An anchor holds only for the route that answered it (``harness.context.budget``).

    def _cache_cold(self) -> bool:
        """No model request for longer than the provider cache TTL (section 3)."""
        last = self._last_model_at
        for row in reversed(self._rows):
            if row.kind == "response" and row.created_at is not None:
                last = row.created_at if last is None else max(last, row.created_at)
                break
        return last is not None and self.context.clock() - last > self.context.cache_ttl_s

    async def _compose(
        self, system: str, selected: ModelSelection, emit: Callable[..., Awaitable[None]], ladder: _Ladder
    ) -> tuple[ModelRequest, dict[str, Tool]]:
        """The conversation request pipeline (invariant 1)."""
        while True:
            view = context_view(self._rows)
            request, tools = await self._request(system, selected, messages=(*self._rehydrated(), *view.messages))
            if self.context is None:
                return request, tools
            plan = budget(request, selected.capabilities, transcript=view.messages, anchor=last_anchor(self._rows, view))
            if not await self._stage(system, selected, request, view, plan, emit, ladder):
                return request, tools

    async def _stage(
        self,
        system: str,
        selected: ModelSelection,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        emit: Callable[..., Awaitable[None]],
        ladder: _Ladder,
    ) -> bool:
        """C-9 on the final request (sections 3 and 8); True when it changed the context."""
        head = [message for unit in view.units[:-1] for message in unit.messages]
        if max(0, plan.est - messages_tokens(head)) + plan.output > plan.input_limit:
            # (d): even the checkpoint and the last unit alone cannot fit. The provider never sees it.
            raise self._exhausted(request, view, plan)
        shrink, ladder.refused = ladder.refused, False
        if not shrink:
            if self.context.clear_tool_results and (
                plan.est >= CLEAR_SOFT_RATIO * plan.threshold or self._cache_cold()
            ):
                targets = clearable_results(view)
                if targets:
                    await self._commit_context([("context_edit", clear_edit(target)) for target in targets], self._guard)
                    return True
            if plan.est >= plan.threshold and not ladder.compacted and not self._guard["paused"]:
                cut = normal_cut(view.units, plan.keep)
                if cut is not None:
                    ladder.compacted = True
                    if not self._fork_can_fit(selected, plan, plan.est):
                        shrink = True  # no fork can take the whole context: the ladder from (b)
                    else:
                        outcome = await self._checkpoint(
                            system, selected, request, view, plan, cut, mode="normal", reason="threshold", emit=emit
                        )
                        if outcome.ok:
                            return True
                        # A checkpoint request that overflowed means the whole context cannot fit a fork (b).
                        shrink, ladder.summary_failed = outcome.overflow, not outcome.overflow
            if not shrink and plan.fits:
                return False
            # With no checkpoint request left to try, a request that can fit at all is sent; the provider judges.
            if not shrink and (ladder.summary_failed or self._guard["paused"]) and plan.can_fit:
                return False
        return await self._shrink(system, selected, request, view, plan, emit, ladder)

    def _fork_can_fit(self, selected: ModelSelection, plan: Budget, est: int) -> bool:
        """Whether a fork carrying ``est`` tokens and the checkpoint request can fit: a choice, not admission.

        The fork itself goes through the request pipeline and is budgeted there.
        """
        cap = checkpoint_max_tokens(selected.capabilities, self.max_tokens)
        return est + message_tokens(_CHECKPOINT_REQUEST) + cap <= plan.input_limit

    async def _shrink(
        self,
        system: str,
        selected: ModelSelection,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        emit: Callable[..., Awaitable[None]],
        ladder: _Ladder,
    ) -> bool:
        """One step of the overflow ladder (section 8); False when nothing can move and the provider judges."""
        units = view.units
        if not ladder.summary_failed and not self._guard["paused"] and not ladder.compacted:
            ladder.compacted = True
            cut = normal_cut(units, plan.keep)
            if cut is not None and self._fork_can_fit(selected, plan, plan.est):
                outcome = await self._checkpoint(
                    system, selected, request, view, plan, cut, mode="normal", reason="overflow", emit=emit
                )
                if outcome.ok:
                    return True
                ladder.summary_failed = not outcome.overflow
        if not ladder.summary_failed and not self._guard["paused"] and ladder.rolls < MAX_ROLLS:
            tails = [0] * (len(units) + 1)
            for index in range(len(units) - 1, -1, -1):
                tails[index] = tails[index + 1] + unit_tokens(units[index])
            whole = sent_tokens(request)
            cut = rolling_cut(units, lambda cut: self._fork_can_fit(selected, plan, whole - tails[cut]))
            if cut is not None:
                ladder.rolls += 1
                outcome = await self._checkpoint(
                    system, selected, request, view, plan, cut, mode="rolling", reason="overflow", emit=emit
                )
                if outcome.ok:
                    return True
                ladder.summary_failed = True
            else:
                ladder.rolls = MAX_ROLLS
        cut = half_cut(units)
        if cut is not None:
            await self._drop(request, view, plan, cut, emit)
            return True
        if ladder.overflows or not plan.can_fit:
            raise self._exhausted(request, view, plan)
        return False

    def _exhausted(self, request: ModelRequest, view: ContextView, plan: Budget) -> _Exhausted:
        """Section 8 (d): what fills the request, for the user's stop message."""
        last = view.units[-1] if view.units else None
        last_tokens = unit_tokens(last) if last is not None else 0
        parts = [
            ContextPart("system", text_tokens(request.system)),
            ContextPart("tools", request_tokens("", request.tools, ())),
            ContextPart("history", max(0, messages_tokens(request.messages) - last_tokens)),
        ]
        if last is not None:
            name = "current_request" if last.lead.kind == "input" else "latest_tool_batch"
            parts.append(ContextPart(name, last_tokens))
        parts.append(ContextPart("output", plan.output + plan.margin))
        return _Exhausted(plan.input_limit, tuple(parts))

    async def _manual_compaction(
        self, system: str, selected: ModelSelection, focus: Optional[str], emit: Callable[..., Awaitable[None]]
    ) -> RunEndReason:
        """Section 10: clear the pause and both counters, then a normal checkpoint between runs."""
        view = context_view(self._rows)
        # Measured for the cut only; it is never sent.
        request, _ = await self._request(system, selected, messages=(*self._rehydrated(), *view.messages))
        plan = budget(request, selected.capabilities, transcript=view.messages, anchor=last_anchor(self._rows, view))
        cut = normal_cut(view.units, plan.keep)
        if cut is None:
            # Everything is within the kept tail: the adapter tells the user there is nothing to compact yet.
            await self._commit_context([], dict(_GUARD_DEFAULT))
            await emit(CompactionSkipped, reason="manual")
            return "completed"
        self._guard = dict(_GUARD_DEFAULT)  # the checkpoint's transition starts from a cleared pause
        outcome = await self._checkpoint(
            system, selected, request, view, plan, cut, mode="normal", reason="manual", emit=emit, focus=focus,
            mid_turn=False,
        )
        return "completed" if outcome.ok else "error"

    async def _checkpoint(
        self,
        system: str,
        selected: ModelSelection,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        cut: int,
        *,
        mode: str,
        reason: str,
        emit: Callable[..., Awaitable[None]],
        focus: Optional[str] = None,
        mid_turn: bool = True,
    ) -> _Outcome:
        """Sections 6 and 7: a forked checkpoint turn; on success its row joins the context."""
        await emit(CompactionStarted, reason=reason)
        prompt = checkpoint_request(focus)
        rehydrated = self._rehydrated()
        head = (view.checkpoint,) if view.checkpoint is not None else ()
        base = view.messages if mode == "normal" else (*head, *(m for unit in view.units[:cut] for m in unit.messages))
        anchor = last_anchor(self._rows, view)
        policy = CheckpointPolicy(cwd=self.cwd, scratch_dir=self.context.scratch_dir)
        turn: list[Message] = []  # what the turn's next request carries after the checkpoint request
        produced: list[Message] = []  # the audit: every attempt's message from the ledger, and the tool results
        start = mark = len(self._attempts)
        measured: list[Budget] = []  # the budget of the turn's latest request, as sent
        rounds = 0
        error: Optional[str] = None
        overflow = False
        checkpoint = ""
        origin = None
        selection: Optional[ModelSelection] = selected

        async def compose(route: ModelSelection) -> tuple[ModelRequest, dict[str, Tool]]:
            transcript = (*base, prompt, *turn)
            fork, tools = await self._request(
                system,
                route,
                messages=(*rehydrated, *transcript),
                max_tokens=checkpoint_max_tokens(route.capabilities, self.max_tokens),
            )
            fork_plan = budget(fork, route.capabilities, transcript=transcript, anchor=anchor)
            if not fork_plan.can_fit:
                raise _ForkTooLarge()
            measured[:] = [fork_plan]
            return fork, tools

        while True:
            try:
                async with self._model(system, _silent, selection, compose=compose, purpose="checkpoint") as (
                    terminal,
                    tools,
                ):
                    selection = None
            except _ForkTooLarge:
                overflow, error = True, "overflow: the checkpoint request does not fit the model's input limit."
                break
            finally:
                produced.extend(item.message for item in self._attempts[mark:] if item.message is not None)
                mark = len(self._attempts)
            if isinstance(terminal, ProviderError):
                overflow = terminal.kind == "overflow"
                error = f"{terminal.kind}: {terminal.message}"
                break
            message = terminal.message
            self._scope.check()
            turn.append(message)
            origin = message.origin
            if message.stop_reason == "stop" and not message.tool_calls:
                checkpoint = checkpoint_text(message)
                if not checkpoint:
                    error = "The checkpoint turn ended without checkpoint text."
                break
            if message.stop_reason != "tool_use" or not message.tool_calls:
                # Only a stop with text and no call is a checkpoint, and only a tool-use stop carries calls.
                error = f"The checkpoint turn stopped with {message.stop_reason} and no usable checkpoint."
                break
            if not policy.open:
                # Its earlier calls were already answered with BUDGET_USED.
                error = "The checkpoint turn kept calling tools after its tool budget was used up."
                break
            rounds += 1
            sent = measured[0]
            grown = sent.est + message_tokens(message)
            for call in message.tool_calls:
                self._scope.check()
                # The bound is the window of the turn's route: a tool runs only while the next request can
                # still grow by the floor (section 6).
                room = sent.input_limit - grown - sent.output
                policy.open = policy.open and rounds <= CHECKPOINT_TOOL_ROUNDS and room >= CHECKPOINT_TOOL_FLOOR
                result = await self._checkpoint_tool(call, tools, policy, limit=room - CHECKPOINT_TOOL_SLACK)
                turn.append(result)
                produced.append(result)
                grown += message_tokens(result)
        usage = None
        for item in self._attempts[start:]:
            usage = add_usage(usage, item.message.usage if item.message is not None else None)
        record: dict[str, Any] = {
            "version": 1,
            "reason": reason,
            "mode": mode,
            "messages": [message_to_dict(message) for message in produced],
        }
        if usage is not None:
            record["usage"] = usage_to_dict(usage)
        if not checkpoint:
            guard, paused = self._counted(ok=False)
            await self._commit_context([], guard)
            await self._record_turn({**record, "outcome": "failed", "error": error, "compaction_event_id": None})
            await emit(CompactionFailed, reason=reason, error=error)
            if paused:
                await emit(CompactionPaused, cause=paused)
            return _Outcome(False, overflow)
        summarizer = {
            "origin": {"provider": origin.provider, "api": origin.api, "model": origin.model},
            "prompt_version": PROMPT_VERSION,
            "rounds": rounds,
        }
        row, paused = await self._commit_compaction(
            request,
            view,
            plan,
            cut,
            mode=mode,
            reason=reason,
            focus=focus,
            checkpoint=checkpoint,
            summarizer=summarizer,
            usage=usage,
            mid_turn=mid_turn,
        )
        await self._record_turn({**record, "outcome": "completed", "error": None, "compaction_event_id": row.row_id})
        await emit(
            CompactionFinished,
            event_id=row.row_id,
            reason=reason,
            mode=mode,
            tokens_before=row.payload["tokens_before"],
            tokens_after_estimate=row.payload["tokens_after_estimate"],
        )
        if paused:
            await emit(CompactionPaused, cause=paused)
        return _Outcome(True)

    def _counted(self, *, ok: bool, ineffective: Optional[bool] = None) -> tuple[dict[str, Any], Optional[str]]:
        """The guard after one checkpoint attempt (section 10), and the pause cause when it pauses now."""
        guard = dict(self._guard)
        cause = None
        if not ok:
            guard["failures"] += 1
            cause = "failures" if guard["failures"] >= PAUSE_AFTER else None
        else:
            guard["failures"] = 0
            if ineffective is not None:
                guard["ineffective"] = guard["ineffective"] + 1 if ineffective else 0
                cause = "ineffective" if guard["ineffective"] >= PAUSE_AFTER else None
        if cause is None or guard["paused"]:
            return guard, None
        guard["paused"] = True
        return guard, cause

    async def _checkpoint_tool(
        self, original: ToolCallBlock, tools: Mapping[str, Tool], policy: CheckpointPolicy, *, limit: int
    ) -> ToolResultMessage:
        """The checkpoint turn's tool pipeline (invariant 2); nothing it produces is committed.

        The budget first, then the table, then execution pinned to the path the table authorized, then the
        bound on whatever result came out. No user hook runs under C-9 (v1: ``ContextConfig`` excludes hooks).
        """
        call = deepcopy(original)
        result: Optional[ToolResult] = None
        if not policy.open:
            result = ToolResult((text(BUDGET_USED),), is_error=True)
        pinned = None
        if result is None:
            decision = policy.decide(call)
            if decision.denial is not None:
                result = ToolResult((text(decision.denial),), is_error=True)
            elif decision.pinned is not None:
                # The tool runs against the real path authorized, and publishes only while it still resolves there.
                pinned = decision.pinned
                call = replace(call, arguments={**call.arguments, "path": pinned})
        if result is None:
            tool = tools.get(call.name)
            if tool is None:
                result = ToolResult((text(f"Tool {call.name} is not available."),), is_error=True)
            else:
                result = await self._execute(tool, call, _silent, pinned=pinned)
        # Every result that enters the turn is bounded, a hook's denial included.
        return ToolResultMessage(call.id, call.name, fit_result(result.content, limit), result.is_error)

    async def _drop(
        self, request: ModelRequest, view: ContextView, plan: Budget, cut: int, emit: Callable[..., Awaitable[None]]
    ) -> None:
        """Section 8 (c): the earliest part moves out with no model call; the previous checkpoint stays."""
        await emit(CompactionStarted, reason="overflow")
        previous = view.compaction
        row, _ = await self._commit_compaction(
            request,
            view,
            plan,
            cut,
            mode="dropped",
            reason="overflow",
            focus=None,
            checkpoint=previous.payload.get("checkpoint", "") if previous is not None else "",
            summarizer=None,
            usage=None,
            mid_turn=True,
        )
        await emit(
            CompactionFinished,
            event_id=row.row_id,
            reason="overflow",
            mode="dropped",
            tokens_before=row.payload["tokens_before"],
            tokens_after_estimate=row.payload["tokens_after_estimate"],
        )

    async def _commit_compaction(
        self,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        cut: int,
        *,
        mode: str,
        reason: str,
        focus: Optional[str],
        checkpoint: str,
        summarizer: Optional[Mapping[str, Any]],
        usage: Optional[Usage],
        mid_turn: bool,
    ) -> tuple[ContextEntry, Optional[str]]:
        """The checkpoint row and, for a checkpoint request, the guard it moves, in one ``_commit_context``."""
        host = self.context.host
        skills = carried_skills(view, cut)
        state: tuple[str, ...] = ()
        earlier = None
        if host is not None:
            needed = StateRequest(self.session_id, skills, mid_turn)
            state = tuple(await self._scope.call(lambda: host.render_state(needed)))
            if not all(isinstance(item, str) for item in state):
                raise TypeError("ContextHost.render_state must return strings")
            earlier = host.earlier_record(self.session_id, summarized_to_seq(view, cut))
        payload = compaction_payload(
            view,
            cut,
            mode=mode,
            reason=reason,
            focus=focus,
            checkpoint=checkpoint,
            skills=skills,
            state=state,
            earlier_record=earlier,
            tokens_before=plan.est,
            threshold=plan.threshold,
            summarizer=summarizer,
            usage=usage,
        )
        seq = max((row.context_seq for row in self._rows), default=0) + 1
        candidate = ContextEntry(self.session_id, seq, "compaction", "uncommitted-compaction", payload=payload)
        after = context_view([*self._rows, candidate])
        # What the next request would carry; it is budgeted for real when it is built.
        payload["tokens_after_estimate"] = request_tokens(
            request.system, request.tools, (*self._rehydrated(), *after.messages)
        )
        guard, paused = self._guard, None
        if summarizer is not None:
            ineffective = payload["tokens_after_estimate"] >= INEFFECTIVE_RATIO * payload["threshold"]
            guard, paused = self._counted(ok=True, ineffective=ineffective if mode == "normal" else None)
        (row,) = await self._commit_context([("compaction", payload)], guard)
        return row, paused

    async def _commit_context(
        self, rows: Sequence[tuple[Literal["compaction", "context_edit"], Mapping[str, Any]]], guard: Mapping[str, Any]
    ) -> tuple[ContextEntry, ...]:
        """The one writer of C-9 state (invariant 3).

        Edits, a checkpoint, and the guard, with the hook state of this commit point, go in one transaction,
        before any event announces the transition. A failed commit changes nothing, in memory or on disk.
        """
        entries: list[tuple[str, Mapping[str, Any]]] = []
        representation = state_representation(self._ctx.state)
        state = deepcopy(self._ctx.state)
        if dict(guard) != self._committed_guard or representation != self._committed_state_json:
            entries.append(("agent_state", {"version": 1, "state": state, "context": dict(guard)}))
        entries.extend(rows)
        if not entries:
            return ()
        committed = await self._scope.call(
            lambda: self.store.append_payloads(self.session_id, deepcopy(entries)), interruptible=False
        )
        self._rows.extend(committed)
        self._guard, self._committed_guard = dict(guard), dict(guard)
        self._committed_state, self._committed_state_json = state, representation
        return tuple(row for row in committed if row.kind != "agent_state")

    async def _record_turn(self, payload: Mapping[str, Any]) -> None:
        """The checkpoint turn's audit row (section 6)."""
        await self._audit("checkpoint_turn", payload)

    async def _audit(self, kind: Literal["checkpoint_turn", "attempt"], payload: Mapping[str, Any]) -> None:
        """A non-context audit row; never context, so losing it never fails the run."""
        try:
            await self._scope.call(
                lambda: self.store.append_audit(self.session_id, kind, deepcopy(dict(payload))),
                interruptible=False,
            )
        except _Aborted:
            raise
        except Exception as error:
            self._outcome.diagnostic(type(error).__name__, f"Checkpoint audit failed: {error}")
