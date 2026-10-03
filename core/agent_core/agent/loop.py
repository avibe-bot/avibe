"""C-3 in-process agent loop; persistence precedes every committed-row event.

Queue semantics informed by Pi (MIT), packages/agent/src/agent-loop.ts, and the
bare-loop control experiment. This is an Avibe implementation, not a port.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import replace
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Optional, Sequence, TypeVar

from core.agent_core.agent.events import (
    AgentError,
    AgentEvent,
    AssistantTextDelta,
    AssistantThinkingDelta,
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
from core.agent_core.agent.models import DEFAULT_MAX_OUTPUT_TOKENS, ModelRouter, ModelSelection, RetryPolicy
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
from core.agent_core.harness.projection import ProjectionError, project, validate_message_append
from core.agent_core.harness.store import ContextEntry, TranscriptStore
from core.agent_core.messages import AssistantMessage, Message, TextBlock, ToolCallBlock, ToolResultMessage, text
from core.agent_core.tools.base import JobHost, Tool, ToolContext, ToolResult

T = TypeVar("T")
logger = logging.getLogger(__name__)


class _Ended(Exception):
    pass


class ProviderProtocolViolation(ValueError):
    """A model response cannot be admitted to the canonical transcript."""


class UnsupportedModelRoute(ValueError):
    """The selected model cannot provide the tools required by the agent."""


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
        """Return accepted but unconsumed inputs to the adapter's P3 queue.

        Call after the run closes admission (normally after RunEnded). The
        adapter still has the original durable rows; this transfers admission
        ownership back in steer/follow-up priority order, exactly once.
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

    async def run(self, input: AgentInput, *, turn_id: str) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            if self._running:
                raise RuntimeError("this Agent already has an active run")
            if self._steers or self._follow_ups:
                raise RuntimeError("return unconsumed inputs with take_pending_inputs() before starting another run")
            self._running = self._open = True
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

        worker = asyncio.create_task(self._drive(deepcopy(input), emit))
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
        row = await self._scope.call(
            lambda: self.store.append_response(self.session_id, deepcopy(message), final=final),
            interruptible=False,
        )
        self._rows.append(row)
        return row

    async def _drive(self, input: AgentInput, emit: Callable[..., Awaitable[None]]) -> None:
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
            cleanup = asyncio.create_task(self._finish(loaded, emit))
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

    async def _finish(self, loaded: bool, emit: Callable[..., Awaitable[None]]) -> None:
        async with self._lock:
            self._open = False
        await self._scope.close()
        await self._flush_diagnostics(emit)
        try:
            if loaded:
                await self._cleanup(lambda: self._save_state(cleanup=True))
                outcome = RunOutcome(self._ctx.turn_id, self._outcome.reason, self.snapshot())
                for hook in self.hooks:
                    await self._cleanup(lambda: self._hook(lambda: hook.after_run(outcome, self._ctx), cleanup=True))
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

    async def _request(self, system: str, selected: ModelSelection) -> tuple[ModelRequest, dict[str, Tool]]:
        self._check_model_route(selected)
        capabilities = selected.capabilities
        tools = dict(self._run_tools if self._run_tools is not None else self._tools)
        context = project(
            self._rows,
            system=system,
            rehydrated=self.rehydrate(deepcopy(self._ctx.state)) if self.rehydrate else (),
        )
        request = ModelRequest(
            endpoint=deepcopy(selected.endpoint),
            system=context.system,
            messages=context.messages,
            tools=tuple(deepcopy(tool.spec) for tool in tools.values()),
            max_tokens=min(self.max_tokens, selected.max_output_tokens),
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
        self, system: str, emit: Callable[..., Awaitable[None]], selected: Optional[ModelSelection] = None
    ) -> AsyncIterator[tuple[Done | ProviderError, dict[str, Tool]]]:
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
            request, tools = await self._request(system, selected)
            selected = None
            if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                yield retry_error, tools
                return
            self._scope.check()
            stream = self.models.provider_for(request.endpoint.protocol).stream(request, self._ctx.cancel)
            streamed = False
            terminal = None
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
                if terminal is None:
                    terminal = ProviderError("unknown", "Provider stream ended without a terminal event.", False)
                delay = (
                    self.retry.delay(terminal, retries=retries, streamed=streamed, elapsed_s=time.monotonic() - started)
                    if isinstance(terminal, ProviderError)
                    else None
                )
                if delay is None:
                    # The caller admits and commits this terminal inside the
                    # scope, BEFORE aclose. Never retry an accepted terminal.
                    yield terminal, tools
                    return
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await self._cleanup(close, stream=True)
            retry_error = terminal
            retries += 1
            await self._scope.call(lambda: asyncio.sleep(delay))

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
                    await self._drain_steers(emit)
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
            for hook in self.hooks:
                decision = await self._hook(lambda: hook.before_tool(call, self._ctx))
                if isinstance(decision, Deny):
                    result = ToolResult((text(decision.reason),), is_error=True)
                    break
                if isinstance(decision, End):
                    result = ToolResult((text("[skipped by policy]"),), is_error=True)
                    end = True
                    break
                if isinstance(decision, AlterArgs):
                    call = replace(call, arguments=deepcopy(decision.arguments))
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
