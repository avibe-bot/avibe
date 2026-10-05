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

from core.agent_core.agent.checkpoint import BUDGET_USED, DENIED, CheckpointPolicy, Decision
from core.agent_core.agent.events import (
    AgentError,
    AgentEvent,
    AssistantTextDelta,
    AssistantThinkingDelta,
    CompactionFailed,
    CompactionFinished,
    CompactionStarted,
    ContextExhausted,
    ContextPart,
    ErrorOrigin,
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
    UNPRODUCTIVE_CHECKPOINTS,
    MAX_OVERFLOWS,
    MAX_ROLLS,
    PROMPT_VERSION,
    Anchor,
    Budget,
    ContextConfig,
    StateRequest,
    add_usage,
    budget,
    checkpoint_max_tokens,
    checkpoint_request,
    checkpoint_text,
    clear_edit,
    clearable_results,
    compaction_payload,
    fit_batch,
    fit_edit,
    fit_result,
    half_cut,
    last_anchor,
    message_tokens,
    messages_tokens,
    normal_cut,
    notes_tokens,
    output_tokens,
    request_facts,
    request_tokens,
    rolling_cut,
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
from core.agent_core.harness.store import ContextEntry, EntryKind, TranscriptStore
from core.agent_core.messages import (
    AssistantMessage,
    Message,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    message_to_dict,
    text,
    usage_to_dict,
)
from core.agent_core.tools.base import JobHost, Tool, ToolContext, ToolResult
from core.agent_core.tools.paths import to_thread_joined

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _Ended(Exception):
    pass


class ProviderProtocolViolation(ValueError):
    """A model response cannot be admitted to the canonical transcript."""


class UnsupportedModelRoute(ValueError):
    """The selected model cannot provide the tools required by the agent."""


class _Exhausted(Exception):
    """C-9 section 8 (d): the run stops ``context_exhausted``, saying what fills the context.

    ``kind`` is the error's: ``context_exhausted`` when the conversation's length is the cause, or, when moving the
    conversation out cannot help, what the newest unit is (``tool_output_too_large`` or ``input_too_large``).
    """

    def __init__(self, limit: int, parts: tuple[ContextPart, ...], kind: str = "context_exhausted") -> None:
        self.limit, self.parts, self.kind = limit, parts, kind
        sizes = ", ".join(f"{part.name} ~{part.tokens}" for part in parts)
        super().__init__(f"The context does not fit the model's input limit of {limit} tokens: {sizes}.")


#: The checkpoint request (C-9 section 11); every checkpoint turn and the fit checks send exactly this.
_CHECKPOINT_REQUEST = checkpoint_request()


#: The most a checkpoint-turn call that does not run adds: one of the policy's two fixed texts (never cut).
_FIXED_RESULT_TOKENS = max(text_tokens(DENIED), text_tokens(BUDGET_USED))


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


@dataclass
class _Attempt:
    """One model attempt of the run: the C-9 attempt ledger's entry (context.md section 10).

    ``message`` is the response, or the partial a failed attempt carried. ``committed``: it became a response
    row; ``audited``: its usage went to a ``ModelAttempt`` audit row (context.md section 10, invariant 4).
    """

    purpose: Literal["conversation", "checkpoint"]
    request: ModelRequest
    message: Optional[AssistantMessage]
    error: Optional[ProviderError] = None
    committed: bool = False
    audited: bool = False


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
        # C-9's per-run bound (section 10): unproductive checkpoint attempts this run, in memory only.
        self._unproductive = 0
        # The ``context_seq`` of each input this run consumed, its first and every steer: a checkpoint inside the Turn
        # keeps them as they were (C-9 section 5).
        self._turn_inputs: set[int] = set()
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
        return self._pump(turn_id, lambda emit: self._drive(emit, input))

    async def _pump(
        self, turn_id: str, drive: Callable[[Callable[..., Awaitable[None]]], Awaitable[None]]
    ) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            if self._running:
                raise RuntimeError("this Agent already has an active run")
            if self._steers or self._follow_ups:
                raise RuntimeError("return unconsumed inputs with take_pending_inputs() before starting another run")
            self._running = True
            self._open = True
            self._consumer_closed = False
            self._ctx = RunContext(self.session_id, turn_id, CancelToken())
            self._scope = RunScope(self._ctx.cancel)
            self._outcome = OutcomeOwner()
            self._seq = 0

        # Bounded delivery applies backpressure to provider streaming. No emit
        # awaits this queue while holding the input-admission lock.
        events: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=64)

        async def emit(event_type: Any, **fields: Any) -> Optional[AgentEvent]:
            if self._consumer_closed:
                return None
            event = event_type(turn_id=turn_id, seq=self._seq, **fields)
            self._seq += 1
            await events.put(event)
            return event

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
        """Hook state at a commit point; a C-9 transition writes it with its own rows (``_commit_context``)."""
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
        self._turn_inputs.add(row.context_seq)

    def _validate_response(self, message: AssistantMessage, *, after: Sequence[Message] = ()) -> None:
        """A response is valid after the committed rows and then ``after`` (a checkpoint turn's own messages)."""
        entries = list(self._rows)
        seq = max((entry.context_seq for entry in entries), default=0)
        for item in after:
            seq += 1
            kind: EntryKind = (
                "input"
                if isinstance(item, UserMessage)
                else "response"
                if isinstance(item, AssistantMessage)
                else "tool_result"
            )
            entries.append(ContextEntry(self.session_id, seq, kind, f"uncommitted-{seq}", item))
        try:
            validate_message_append(entries, session_id=self.session_id, kind="response", message=message)
        except ProjectionError as error:
            raise ProviderProtocolViolation(f"Provider protocol violation: {error}") from error

    async def _response(self, message: AssistantMessage, *, final: bool) -> ContextEntry:
        self._validate_response(message)
        await self._save_state()
        # The attempt this response answered: the ledger's latest conversation attempt (the C-9 anchor).
        attempt = next((item for item in reversed(self._attempts) if item.purpose == "conversation"), None)
        facts = {"request": request_facts(attempt.request)} if self.context is not None and attempt else {}
        row = await self._scope.call(
            lambda: self.store.append_response(self.session_id, deepcopy(message), final=final, **facts),
            interruptible=False,
        )
        if attempt is not None:
            attempt.committed = True
        self._rows.append(row)
        return row

    async def _drive(self, emit: Callable[..., Awaitable[None]], input: AgentInput) -> None:
        """One run of ``input``."""
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
                self._unproductive = 0
                self._turn_inputs = set()
                self._attempts = []
                loaded = True
                system = self.system
                self._run_tools = dict(self._tools)
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
                decided = self._outcome.primary("context_exhausted")
                await emit(ContextExhausted, limit=error.limit, parts=error.parts)
                await self._error(emit, error.kind, str(error), "local", cause=decided)
            except _Aborted:
                self._outcome.primary("aborted")
            except asyncio.CancelledError:
                decided = self._outcome.primary("aborted")
                self._ctx.cancel.cancel("event consumer closed" if self._consumer_closed else "dependency cancelled")
                if not self._consumer_closed:
                    message = "An agent dependency was cancelled."
                    await self._error(emit, "dependency_cancelled", message, "local", cause=decided)
            except Exception as error:
                decided = self._outcome.primary("error")
                if isinstance(error, HookStateError):
                    self._ctx.state = deepcopy(self._committed_state)
                # A response the transcript cannot hold is the served model's; any other exception is the loop's own.
                origin = "source" if isinstance(error, ProviderProtocolViolation) else "local"
                await self._error(emit, type(error).__name__, str(error), origin, cause=decided)
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
        await emit(RunEnded, reason=self._outcome.reason, cause=self._outcome.cause)

    async def _error(
        self, emit: Callable[..., Awaitable[Any]], kind: str, message: str, origin: ErrorOrigin, *, cause: bool
    ) -> None:
        """The one place the loop announces an error: ``origin`` as its caller states it, where the error is raised.

        ``cause``: the error decided the run's outcome (``OutcomeOwner.primary``), so ``RunEnded`` names it; a
        diagnostic never does.
        """
        event = await emit(AgentError, kind=kind, message=message, origin=origin)
        if cause:
            self._outcome.cause = event

    async def _flush_diagnostics(self, emit: Callable[..., Awaitable[None]]) -> None:
        while self._outcome.diagnostics:
            kind, message = self._outcome.diagnostics[0]
            await self._error(emit, kind, message, "local", cause=False)
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
        request, tools = self._built(system, selected, messages=messages, max_tokens=max_tokens)
        for hook in self.hooks:
            decision = await self._hook(lambda: hook.before_model(request, self._ctx))
            if isinstance(decision, End):
                await self._save_state()
                raise _Ended()
            if decision is not None:
                request = decision
        allowed = {spec.name for spec in request.tools}
        return request, {name: tool for name, tool in tools.items() if name in allowed}

    def _built(
        self,
        system: str,
        selected: ModelSelection,
        *,
        messages: Sequence[Message],
        max_tokens: Optional[int] = None,
    ) -> tuple[ModelRequest, dict[str, Tool]]:
        """The request carrying ``messages`` on ``selected``, before any hook. C-9 composes its forks and the stop
        check's minimal request from this alone, which holds because v1 takes no user hooks with a ContextConfig."""
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
        return request, tools

    @asynccontextmanager
    async def _model(
        self,
        system: str,
        emit: Callable[..., Awaitable[None]],
        selected: Optional[ModelSelection] = None,
        *,
        compose: Optional[Callable[[ModelSelection], Awaitable[tuple[ModelRequest, dict[str, Tool]]]]] = None,
        purpose: Literal["conversation", "checkpoint"] = "conversation",
        after: Sequence[Message] = (),
    ) -> AsyncIterator[tuple[Done | ProviderError, dict[str, Tool]]]:
        """One model request with its transient retries.

        ``compose(selection)`` is the request pipeline: it returns the final request, which is sent unchanged.
        A conversation request's pipeline is ``_compose``. Every attempt enters the run's attempt ledger. A
        conversation request the provider rejects as overflow goes back through the pipeline with the ladder
        told to shrink (C-9 section 8). A checkpoint attempt is never context.

        Admission is here, for every purpose: the response the caller receives (or a partial with content) is
        valid after the committed rows and ``after``, the messages the request carried beyond them (a checkpoint
        turn's request and its own turn), or ``ProviderProtocolViolation`` is raised before anyone acts on it.
        """
        ladder = _Ladder()
        if compose is None:

            async def compose(selection: ModelSelection) -> tuple[ModelRequest, dict[str, Tool]]:
                return await self._compose(system, selection, emit, ladder)

        first = len(self._attempts)
        failure: Optional[BaseException] = None
        aborted = False
        try:
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
                        # Only an overflow before any output: once something was streamed, the user saw it.
                        and not streamed
                        and (terminal.partial is None or not terminal.partial.content)
                    )
                    delay = (
                        self.retry.delay(terminal, retries=retries, streamed=streamed, elapsed_s=time.monotonic() - started)
                        if isinstance(terminal, ProviderError) and not relieve
                        else None
                    )
                    if delay is None and not relieve:
                        # Admitted here; the caller commits it inside the scope,
                        # BEFORE aclose. Never retry an accepted terminal.
                        admitted = terminal.message if isinstance(terminal, Done) else terminal.partial
                        if isinstance(terminal, Done) or (admitted is not None and admitted.content):
                            self._validate_response(admitted, after=after)
                        yield terminal, tools
                        return
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        await self._cleanup(close, stream=True)
                # The attempt is not the run's answer and is never context (invariant 4): it stays in the ledger and
                # the audit. A retried request goes out again unchanged; one refused as overflow is rebuilt after a
                # ladder step (section 8).
                await self._settle_attempts(first)
                terminal = replace(terminal, partial=None)
                if relieve:
                    ladder.overflows += 1
                    ladder.refused = True
                    if ladder.overflows >= MAX_OVERFLOWS:
                        view = context_view(self._rows)
                        plan = budget(
                            request, route.capabilities, transcript=view.messages, anchor=last_anchor(self._rows, view)
                        )
                        newest = half_cut(view.units, turn_inputs=self._turn_inputs, pinned=view.pinned) is None
                        raise self._exhausted(request, view, plan, newest=newest)
                    retries, started, retry_error = 0, time.monotonic(), None
                    continue
                retry_error = terminal
                retries += 1
                await self._scope.call(lambda: asyncio.sleep(delay))
        except (_Aborted, asyncio.CancelledError, GeneratorExit):
            aborted = True  # the cancelled run scope admits no further store write
            raise
        except BaseException as error:
            failure = error
            raise
        finally:
            if not aborted:
                # Invariant 4: the ledger, not each exit path, keeps the usage of attempts that did not become a
                # response row: a retry, a relief, a usage-only terminal, a refusal at admission, a failed commit.
                try:
                    await self._settle_attempts(first, error=failure)
                except _Aborted:
                    if failure is None:
                        raise

    async def _settle_attempts(self, first: int, *, error: Optional[BaseException] = None) -> None:
        """Every conversation attempt since ``first`` that reported usage and did not become a response row:
        exactly one non-context ``ModelAttempt`` audit row, in every mode, with or without ``ContextConfig``.

        It is the one place that usage is kept. ``error`` is why the model call ended, for an attempt that
        carries no provider error of its own (a response refused at admission, a failed commit).
        """
        for attempt in self._attempts[first:]:
            message = attempt.message
            if attempt.purpose != "conversation" or attempt.committed or attempt.audited:
                continue
            if message is None or message.usage is None:
                continue
            attempt.audited = True
            cause = attempt.error
            payload: dict[str, Any] = {
                "version": 1,
                "usage": usage_to_dict(message.usage),
                "error": f"{cause.kind}: {cause.message}" if cause is not None else str(error) if error else None,
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
                        # A usage-only partial is attempt data, never context; the ledger audits it.
                        terminal = replace(terminal, partial=None)
                    reason = (
                        "context_exhausted"
                        if terminal.kind == "overflow"
                        else "aborted"
                        if terminal.kind == "aborted"
                        else "error"
                    )
                    decided = self._outcome.primary(reason)
                    # ``aborted`` is the loop's own cancellation and ``overflow`` says our request was too large;
                    # every other provider error is the source's.
                    origin = "local" if terminal.kind in {"aborted", "overflow"} else "source"
                    await self._error(emit, terminal.kind, terminal.message, origin, cause=decided)
                    if terminal.partial is not None:
                        row = await self._response(terminal.partial, final=False)
                        await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=False)
                    return reason
                message = terminal.message
                failed = message.stop_reason in {"error", "aborted"}
                if not (message.tool_calls and message.stop_reason == "length"):
                    length_tool_retries = 0
                if failed:
                    aborted = message.stop_reason == "aborted"
                    decided = self._outcome.primary("aborted" if aborted else "error")
                    await self._error(
                        emit,
                        message.stop_reason,
                        message.error_message or f"Model stopped with {message.stop_reason}.",
                        "local" if aborted else "source",
                        cause=decided,
                    )
                final = await self._commit_model_message(message, emit)
                empty_reply = final and not any(
                    isinstance(block, TextBlock) and block.text and block.text.strip() for block in message.content
                )
                if empty_reply:
                    decided = self._outcome.primary("error")
                    await self._error(
                        emit,
                        message.stop_reason if message.stop_reason in {"refusal", "safety"} else "empty_response",
                        message.error_message or f"Model stopped with {message.stop_reason} without a reply.",
                        "source",
                        cause=decided,
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
                        decided = self._outcome.primary("error")
                        await self._error(
                            emit,
                            "length",
                            "The model repeatedly exceeded its output limit while emitting a tool call.",
                            "source",
                            cause=decided,
                        )
                        return "error"
                    await self._drain_steers(emit)
                    continue
                self._open = False
                if not failed:
                    decided = self._outcome.primary("error")
                    message_text = message.error_message or f"Model stopped with {reason}."
                    await self._error(emit, reason, message_text, "source", cause=decided)
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

    async def _execute(self, tool: Tool, call: ToolCallBlock, emit: Callable[..., Awaitable[None]]) -> ToolResult:
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
    # The ordering and ownership invariants are context.md section 10 (1-6), the one statement of them. Their
    # owners here: ``_compose``, ``_fork``, and ``_minimal`` (1, requests composed and budgeted; ``_compaction`` is
    # the one builder of checkpoint rows), ``_checkpoint_tool`` (2), ``_commit_context`` (3), ``self._attempts``
    # with ``_settle_attempts`` and ``_checkpoint``'s one audit (4), ``harness.context.budget`` (5), and ``_model``
    # (6, admission).

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
            request, tools = await self._request(system, selected, messages=self._carried(view))
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
        """C-9 on the final request (sections 3 and 8); True when it changed the context.

        Whether the run still compacts is read from its live bound at every decision, never kept: a checkpoint
        attempt in this pass can use it up.
        """
        # The bound first: once the run has stopped compacting, nothing is built for a compaction either.
        if not plan.can_fit:
            if self._stopped:
                raise self._exhausted(request, view, plan, newest=False)  # (d); the provider never sees it
            _, minimal = await self._minimal(system, selected, request, view, plan, carry=False)
            if not minimal.can_fit:
                # Not even the request the drop would leave (its row and the last unit, without the previous
                # checkpoint's text) can fit: moving the conversation out cannot help. The newest tool batch is cut
                # to fit (section 3); anything else is (d), and the provider never sees it.
                rest = minimal.est - self._batch_tokens(view)
                room = minimal.input_limit - minimal.output - rest
                if await self._fit_batch(view, (room - minimal.margin, room)):
                    return True
                raise self._exhausted(request, view, plan, newest=True)
        refused, ladder.refused = ladder.refused, False
        shrink = refused
        if not shrink:
            if self.context.clear_tool_results and (
                plan.est >= CLEAR_SOFT_RATIO * plan.threshold or self._cache_cold()
            ):
                targets = clearable_results(view)
                if targets:
                    await self._commit_context([("context_edit", clear_edit(target)) for target in targets])
                    return True
            if plan.est >= plan.threshold and not ladder.compacted and not self._stopped:
                cut = normal_cut(view.units, plan.keep, turn_inputs=self._turn_inputs, pinned=view.pinned)
                if cut is not None:
                    ladder.compacted = True
                    if not self._fork_fits(system, selected, view, "normal", cut):
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
            if not shrink and (ladder.summary_failed or self._stopped) and plan.can_fit:
                return False
        return await self._shrink(system, selected, request, view, plan, emit, ladder, refused=refused)

    @property
    def _stopped(self) -> bool:
        """The run has made ``UNPRODUCTIVE_CHECKPOINTS`` failed or ineffective checkpoint attempts (section 10)."""
        return self._unproductive >= UNPRODUCTIVE_CHECKPOINTS

    def _hold(self, request: ModelRequest, view: ContextView, plan: Budget, *, refused: bool) -> bool:
        """The run's bound (section 10), read live before every step of the ladder.

        Not stopped: False, and the step may run. Stopped: nothing more is compacted this run; a request that can
        fit and the provider has not refused is sent as it is (True), and any other ends the run
        ``context_exhausted``.
        """
        if not self._stopped:
            return False
        if refused or not plan.can_fit:
            raise self._exhausted(request, view, plan, newest=False)
        return True

    async def _minimal(
        self,
        system: str,
        selected: ModelSelection,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        *,
        carry: bool,
    ) -> tuple[ModelRequest, Budget]:
        """Section 8 (d): the request that would remain with everything but the last unit moved out, and its budget.

        The drop's own row for that cut (its state included, the input of a turn it splits kept, and the previous
        checkpoint's text when ``carry``), uncommitted, projected and composed like the conversation's next request
        (invariant 1); budgeted with no anchor, whose history is gone.
        """
        if len(view.units) < 2:
            return request, plan  # nothing can move out
        cut = len(view.units) - 1
        hosted = await self._hosted(view, cut)
        dropped = self._dropped(view, carry=carry)
        payload, after = await self._compaction(request, view, plan, cut, **dropped, hosted=hosted)
        minimal, _ = self._built(system, selected, messages=self._carried(after))
        return minimal, budget(minimal, selected.capabilities, transcript=after.messages)

    def _carried(self, view: ContextView) -> tuple[Message, ...]:
        """What a conversation request carries for ``view``: the rehydrated state, then the projected context."""
        return (*self._rehydrated(), *view.messages)

    @staticmethod
    def _dropped(view: ContextView, *, carry: bool) -> dict[str, Any]:
        """A dropped row's fields (section 8 c): no model call; the previous checkpoint's text kept when ``carry``,
        else left out and said so."""
        previous = view.compaction.payload.get("checkpoint", "") if view.compaction is not None else ""
        return {
            "mode": "dropped",
            "reason": "overflow",
            "checkpoint": previous if carry else "",
            "checkpoint_omitted": bool(previous) and not carry,
            "summarizer": None,
            "usage": None,
        }

    async def _carries(
        self, system: str, selected: ModelSelection, request: ModelRequest, view: ContextView, plan: Budget
    ) -> bool:
        """Whether a drop keeps the previous checkpoint's text (section 8 c): only when the request the drop would
        leave, carrying it, can fit this route. A checkpoint written on a larger route may not."""
        if view.compaction is None or not view.compaction.payload.get("checkpoint"):
            return True  # nothing to carry
        return (await self._minimal(system, selected, request, view, plan, carry=True))[1].can_fit

    def _fork_base(self, view: ContextView, mode: str, cut: int) -> tuple[Message, ...]:
        """What a fork carries before the checkpoint request: the whole context, or for a rolling fork its prefix."""
        return tuple(view.messages) if mode == "normal" else view.prefix(cut)

    def _fork(
        self,
        system: str,
        route: ModelSelection,
        base: Sequence[Message],
        prompt: UserMessage,
        turn: Sequence[Message],
        anchor: Optional[Anchor],
    ) -> tuple[ModelRequest, dict[str, Tool], Budget]:
        """A checkpoint turn's request on ``route`` (section 6) and its budget: ``base``, the checkpoint request,
        and the turn so far. The stage's dry run and the turn itself compose it here, so they cannot differ."""
        transcript = (*base, prompt, *turn)
        request, tools = self._built(
            system,
            route,
            messages=(*self._rehydrated(), *transcript),
            max_tokens=checkpoint_max_tokens(route.capabilities, self.max_tokens),
        )
        return request, tools, budget(request, route.capabilities, transcript=transcript, anchor=anchor)

    def _fork_fits(self, system: str, selected: ModelSelection, view: ContextView, mode: str, cut: int) -> bool:
        """A dry compose of the first request the checkpoint turn would send, budgeted: a choice, not admission."""
        fork = self._fork(
            system, selected, self._fork_base(view, mode, cut), _CHECKPOINT_REQUEST, (), last_anchor(self._rows, view)
        )
        return fork[2].can_fit

    async def _shrink(
        self,
        system: str,
        selected: ModelSelection,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        emit: Callable[..., Awaitable[None]],
        ladder: _Ladder,
        *,
        refused: bool,
    ) -> bool:
        """The overflow ladder (section 8), one loop; True when a step changed the context, False to send as it is.

        Every step is preceded by the run's bound, read live (``_hold``), as the stage reads it first: a checkpoint
        attempt that uses the bound up ends the ladder there, never in a drop.
        """
        units = view.units
        while True:
            if self._hold(request, view, plan, refused=refused):
                return False
            if not ladder.summary_failed and not ladder.compacted:
                ladder.compacted = True
                cut = normal_cut(units, plan.keep, turn_inputs=self._turn_inputs, pinned=view.pinned)
                if cut is not None and self._fork_fits(system, selected, view, "normal", cut):
                    outcome = await self._checkpoint(
                        system, selected, request, view, plan, cut, mode="normal", reason="overflow", emit=emit
                    )
                    if outcome.ok:
                        return True
                    ladder.summary_failed = not outcome.overflow
                continue
            if not ladder.summary_failed and ladder.rolls < MAX_ROLLS:
                cut = rolling_cut(
                    units,
                    lambda cut: self._fork_fits(system, selected, view, "rolling", cut),
                    turn_inputs=self._turn_inputs,
                    pinned=view.pinned,
                )
                if cut is None:
                    ladder.rolls = MAX_ROLLS
                    continue
                ladder.rolls += 1
                outcome = await self._checkpoint(
                    system, selected, request, view, plan, cut, mode="rolling", reason="overflow", emit=emit
                )
                if outcome.ok:
                    return True
                ladder.summary_failed = True
                continue
            cut = half_cut(units, turn_inputs=self._turn_inputs, pinned=view.pinned)
            if cut is not None:
                carry = await self._carries(system, selected, request, view, plan)
                await self._drop(request, view, plan, cut, emit, carry=carry)
                return True
            # Nothing more can move out. A refused request's newest tool batch is cut to half of what the model read
            # (the provider counts more than the estimate), down to its notes; the refusals bound the halvings.
            if ladder.overflows and await self._fit_batch(view, (self._batch_tokens(view) // 2,), floor=True):
                return True
            if ladder.overflows or not plan.can_fit:
                raise self._exhausted(request, view, plan, newest=True)
            return False

    @staticmethod
    def _newest_batch(view: ContextView) -> list[tuple[ContextEntry, Message]]:
        """The committed results of the newest unit when it is a tool batch, as the model reads them."""
        last = view.units[-1] if view.units else None
        if last is None or last.lead.kind != "response":
            return []
        return [(entry, message) for entry, message in last.entries[1:] if entry is not None]

    def _batch_tokens(self, view: ContextView) -> int:
        return sum(message_tokens(message) for _, message in self._newest_batch(view))

    async def _fit_batch(self, view: ContextView, rooms: Sequence[int], *, floor: bool = False) -> bool:
        """Section 3: cut the newest tool batch, which nothing can move out, to the first of ``rooms`` (tokens) it can
        fit, or with ``floor`` to its notes when none can; True when it committed the cuts as ``fit_tool_result``
        edits.

        Water-filling over the batch's whole outputs (the rows keep them): the largest results are cut to one cap,
        each saying so. A batch that cannot fit any room is left as it is, for the stop.
        """
        results = self._newest_batch(view)
        if not results:
            return False
        originals = [entry.message for entry, _ in results]
        if floor:
            rooms = (*rooms, notes_tokens(originals))
        for room in rooms:
            texts = fit_batch(originals, room)
            if texts is not None:
                break
        else:
            return False
        # Only what changes what the model reads: a stage that cut nothing new leaves the stop to decide.
        edits = [
            ("context_edit", fit_edit(entry, cut))
            for (entry, message), cut in zip(results, texts)
            if cut is not None and message.content != (text(cut),)
        ]
        if not edits:
            return False
        await self._commit_context(edits)
        return True

    def _exhausted(self, request: ModelRequest, view: ContextView, plan: Budget, *, newest: bool) -> _Exhausted:
        """Section 8 (d): what fills the request, for the user's stop message; ``newest`` when moving the conversation
        out cannot help, so the newest unit is the cause."""
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
        kind = "context_exhausted"
        if newest and last is not None:
            kind = "input_too_large" if last.lead.kind == "input" else "tool_output_too_large"
        return _Exhausted(plan.input_limit, tuple(parts), kind)

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
    ) -> _Outcome:
        """Sections 6 and 7: a forked checkpoint turn; on success its row joins the context."""
        await emit(CompactionStarted, reason=reason)
        prompt = _CHECKPOINT_REQUEST
        base = self._fork_base(view, mode, cut)
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
            fork, tools, fork_plan = self._fork(system, route, base, prompt, turn, anchor)
            if not fork_plan.can_fit:
                raise _ForkTooLarge()
            measured[:] = [fork_plan]
            return fork, tools

        finished: dict[str, Any] = {"outcome": "failed", "error": None, "compaction_event_id": None}
        aborted = False
        try:
            try:
                while True:
                    try:
                        async with self._model(
                            system, _silent, selection, compose=compose, purpose="checkpoint", after=(prompt, *turn)
                        ) as (terminal, tools):
                            selection = None
                    except _ForkTooLarge:
                        overflow, error = True, "overflow: the checkpoint request does not fit the model's input limit."
                        break
                    except ProviderProtocolViolation as violation:
                        # Not admitted: a failed checkpoint, and none of its calls runs.
                        error = str(violation)
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
                    calls = message.tool_calls
                    for index, call in enumerate(calls):
                        self._scope.check()
                        # The bound is the window of the turn's route: a tool runs only while the next request can
                        # still grow by the floor, after the fixed-text results every later call of the batch may
                        # need are reserved (section 6).
                        reserved = (len(calls) - index - 1) * _FIXED_RESULT_TOKENS
                        room = sent.input_limit - grown - sent.output - reserved
                        policy.open = policy.open and rounds <= CHECKPOINT_TOOL_ROUNDS and room >= CHECKPOINT_TOOL_FLOOR
                        result = await self._checkpoint_tool(call, tools, policy, limit=room - CHECKPOINT_TOOL_SLACK)
                        turn.append(result)
                        produced.append(result)
                        grown += message_tokens(result)
            finally:
                policy.close()  # the scratch root's descriptor lives for this turn only
            payload = None
            if checkpoint:
                summarizer = {
                    "origin": {"provider": origin.provider, "api": origin.api, "model": origin.model},
                    "prompt_version": PROMPT_VERSION,
                    "rounds": rounds,
                }
                try:
                    hosted = await self._hosted(view, cut)
                except (_Aborted, asyncio.CancelledError):
                    raise
                except Exception as failure:
                    # Only the host's part after the model answered (state, lookup) is a failed checkpoint,
                    # counted; an error building the row itself ends the run, like any engine error.
                    error = f"The checkpoint row could not be built: the host failed: {type(failure).__name__}: {failure}"
                else:
                    payload, _ = await self._compaction(
                        request,
                        view,
                        plan,
                        cut,
                        mode=mode,
                        reason=reason,
                        checkpoint=checkpoint,
                        summarizer=summarizer,
                        usage=self._turn_usage(start),
                        hosted=hosted,
                    )
            if payload is None:
                finished["error"] = error
                self._unproductive += 1
                await emit(CompactionFailed, reason=reason, error=error)
                return _Outcome(False, overflow)
            (row,) = await self._commit_context([("compaction", payload)])
            if mode == "normal" and payload["tokens_after_estimate"] >= INEFFECTIVE_RATIO * payload["threshold"]:
                self._unproductive += 1  # it made too little room to count as progress
            finished.update(outcome="completed", compaction_event_id=row.row_id)
            await emit(
                CompactionFinished,
                event_id=row.row_id,
                reason=reason,
                mode=mode,
                tokens_before=row.payload["tokens_before"],
                tokens_after_estimate=row.payload["tokens_after_estimate"],
            )
            return _Outcome(True)
        except (_Aborted, asyncio.CancelledError):
            aborted = True  # the cancelled run scope admits no further store write
            raise
        except BaseException as failure:
            if finished["outcome"] != "completed" and finished["error"] is None:
                finished["error"] = f"{type(failure).__name__}: {failure}"  # a failed commit, or a failed compose
            raise
        finally:
            if not aborted:
                # Invariant 4 for checkpoint turns: one audit, from the ledger, on every exit but an abort.
                await self._record_turn(self._turn_record(reason, mode, produced, start, finished))

    def _turn_usage(self, start: int) -> Optional[Usage]:
        """The usage every attempt of a checkpoint turn reported, from the ledger."""
        usage = None
        for item in self._attempts[start:]:
            usage = add_usage(usage, item.message.usage if item.message is not None else None)
        return usage

    def _turn_record(
        self, reason: str, mode: str, produced: Sequence[Message], start: int, finished: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A checkpoint turn's audit (``CheckpointTurn``, section 6): its messages, usage, and outcome."""
        messages = []
        for message in produced:
            try:
                messages.append(message_to_dict(message))
            except (ValueError, RecursionError) as invalid:
                # Arguments a provider sent that are not JSON: refused at admission (the turn's error names
                # them), and no JSON row can hold them either.
                self._outcome.diagnostic(type(invalid).__name__, f"Checkpoint audit left out a message: {invalid}")
        record: dict[str, Any] = {"version": 1, "reason": reason, "mode": mode, "messages": messages}
        usage = self._turn_usage(start)
        if usage is not None:
            record["usage"] = usage_to_dict(usage)
        return {**record, **finished}

    async def _checkpoint_tool(
        self, original: ToolCallBlock, tools: Mapping[str, Tool], policy: CheckpointPolicy, *, limit: int
    ) -> ToolResultMessage:
        """The checkpoint turn's tool pipeline (invariant 2); nothing it produces is committed.

        The budget first, then the table, then execution (a scratch write or edit through the root's
        descriptor), then the bound on whatever result came out. No user hook runs under C-9 (v1).
        """
        call = deepcopy(original)
        decision = policy.decide(call) if policy.open else Decision(denial=BUDGET_USED)
        if decision.denial is not None:
            # The policy's fixed texts are short and never cut: a truncation note would invite another call.
            return ToolResultMessage(call.id, call.name, (text(decision.denial),), True)
        if decision.scratch is not None:
            # Relative to the scratch root's descriptor: no pathname is resolved again. Joined, never interrupted:
            # the worker thread outlives a cancelled await, and the root closes only after it is done (section 6).
            result = await self._scope.call(
                lambda: to_thread_joined(policy.run, call, decision.scratch), interruptible=False
            )
        elif (tool := tools.get(call.name)) is None:
            result = ToolResult((text(f"Tool {call.name} is not available."),), is_error=True)
        else:
            result = await self._execute(tool, call, _silent)
        # Every other result that enters the turn is bounded to the room the window leaves (section 6).
        return ToolResultMessage(call.id, call.name, fit_result(result.content, limit), result.is_error)

    async def _drop(
        self,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        cut: int,
        emit: Callable[..., Awaitable[None]],
        *,
        carry: bool,
    ) -> None:
        """Section 8 (c): the earliest part moves out with no model call; the previous checkpoint stays when it can
        fit (``_carries``)."""
        await emit(CompactionStarted, reason="overflow")
        hosted = await self._hosted(view, cut)
        payload, _ = await self._compaction(request, view, plan, cut, **self._dropped(view, carry=carry), hosted=hosted)
        (row,) = await self._commit_context([("compaction", payload)])
        await emit(
            CompactionFinished,
            event_id=row.row_id,
            reason="overflow",
            mode="dropped",
            tokens_before=row.payload["tokens_before"],
            tokens_after_estimate=row.payload["tokens_after_estimate"],
        )

    async def _compaction(
        self,
        request: ModelRequest,
        view: ContextView,
        plan: Budget,
        cut: int,
        *,
        mode: str,
        reason: str,
        checkpoint: str,
        summarizer: Optional[Mapping[str, Any]],
        usage: Optional[Usage],
        hosted: tuple[tuple[str, ...], Optional[str]],
        checkpoint_omitted: bool = False,
    ) -> tuple[dict[str, Any], ContextView]:
        """The checkpoint row for ``cut`` (section 7), uncommitted, and the context it would leave.

        The one builder of a ``Compaction`` row: the drop, the checkpoint turn, and the stop check's dry run.
        ``hosted`` is the host's part (``_hosted``).
        """
        state, earlier = hosted
        payload = compaction_payload(
            view,
            cut,
            mode=mode,
            reason=reason,
            checkpoint=checkpoint,
            state=state,
            earlier_record=earlier,
            tokens_before=plan.est,
            threshold=plan.threshold,
            window=plan.window,
            summarizer=summarizer,
            usage=usage,
            checkpoint_omitted=checkpoint_omitted,
            turn_inputs=self._turn_inputs,
        )
        seq = max((row.context_seq for row in self._rows), default=0) + 1
        candidate = ContextEntry(self.session_id, seq, "compaction", "uncommitted-compaction", payload=payload)
        after = context_view([*self._rows, candidate])
        # What the next request would carry; it is budgeted for real when it is built.
        payload["tokens_after_estimate"] = request_tokens(request.system, request.tools, self._carried(after))
        return payload, after

    async def _hosted(self, view: ContextView, cut: int) -> tuple[tuple[str, ...], Optional[str]]:
        """The host's part of a checkpoint row for ``cut``: the rendered ``state`` and the earlier-record lookup."""
        host = self.context.host
        if host is None:
            return (), None
        needed = StateRequest(self.session_id)
        state = tuple(await self._scope.call(lambda: host.render_state(needed)))
        if not all(isinstance(item, str) for item in state):
            raise TypeError("ContextHost.render_state must return strings")
        return state, host.earlier_record(self.session_id, summarized_to_seq(view, cut))

    async def _commit_context(
        self, rows: Sequence[tuple[Literal["compaction", "context_edit"], Mapping[str, Any]]]
    ) -> tuple[ContextEntry, ...]:
        """The one writer of C-9 state (invariant 3).

        Edits or a checkpoint, with the hook state of this commit point, go in one transaction, before any event
        announces the transition. A failed commit changes nothing, in memory or on disk.
        """
        entries: list[tuple[str, Mapping[str, Any]]] = []
        representation = state_representation(self._ctx.state)
        state = deepcopy(self._ctx.state)
        if representation != self._committed_state_json:
            entries.append(("agent_state", {"version": 1, "state": state}))
        entries.extend(rows)
        committed = await self._scope.call(
            lambda: self.store.append_payloads(self.session_id, deepcopy(entries)), interruptible=False
        )
        self._rows.extend(committed)
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
            self._outcome.diagnostic(type(error).__name__, f"Audit write failed ({kind}): {error}")
