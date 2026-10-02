"""C-3 in-process agent loop; persistence precedes every committed-row event.

Queue semantics informed by Pi (MIT), packages/agent/src/agent-loop.ts, and the
bare-loop control experiment. This is an Avibe implementation, not a port.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import suppress
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
from core.agent_core.agent.models import ModelRouter, RetryPolicy
from core.agent_core.ai.provider import (
    Done,
    ModelRequest,
    ProviderError,
    TextDelta,
    ThinkingDelta,
)
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.projection import OrphanSettler, project
from core.agent_core.harness.store import ContextEntry, TranscriptStore
from core.agent_core.messages import AssistantMessage, Message, ToolCallBlock, ToolResultMessage, text
from core.agent_core.tools.base import JobHost, Tool, ToolContext, ToolResult

T = TypeVar("T")


class _Aborted(Exception):
    pass


class _Ended(Exception):
    pass


class _ProviderFailed(Exception):
    def __init__(self, error: ProviderError) -> None:
        self.error = error
        super().__init__(error.message)


class Agent:
    """One Session's single writer. All methods run on the same asyncio loop.

    ``run_id`` is the Avibe Turn id. ``steer``/``follow_up`` return False after
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
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        retry: RetryPolicy = RetryPolicy(),
        settle_orphan: Optional[OrphanSettler] = None,
        rehydrate: Optional[Callable[[Mapping[str, Any]], Sequence[Message]]] = None,
    ) -> None:
        self.session_id = session_id
        self.models = models
        self.store = store
        self.jobs = jobs if isinstance(jobs, TrackingJobHost) else TrackingJobHost(jobs)
        self.hooks = tuple(hooks)
        self.cwd, self.env = cwd, dict(env or {})
        self.system = system
        self.max_tokens, self.reasoning_effort = max_tokens, reasoning_effort
        self.retry = retry
        self.settle_orphan, self.rehydrate = settle_orphan, rehydrate
        self._tools: dict[str, Tool] = {}
        self._run_tools: Optional[dict[str, Tool]] = None
        self.set_tools(tools)
        self._lock = asyncio.Lock()
        self._steers: deque[AgentInput] = deque()
        self._follow_ups: deque[AgentInput] = deque()
        self._running = False
        self._open = False
        self._ctx = RunContext(session_id, "", CancelToken())
        self._rows: list[ContextEntry] = []
        self._committed_state: dict[str, Any] = {}
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

    def abort(self, reason: str = "aborted") -> None:
        if self._running:
            self._ctx.cancel.cancel(reason)

    def snapshot(self) -> Snapshot:
        """A committed cut, including only state that can be restored there."""
        seq = self._rows[-1].context_seq if self._rows else 0
        return Snapshot(self.session_id, seq, deepcopy(self._committed_state))

    async def run(self, input: AgentInput, *, run_id: str) -> AsyncIterator[AgentEvent]:
        async with self._lock:
            if self._running:
                raise RuntimeError("this Agent already has an active run")
            self._running = self._open = True
            self._steers.clear()
            self._follow_ups.clear()
            self._ctx = RunContext(self.session_id, run_id, CancelToken())
            self._seq = 0

        # Bounded delivery applies backpressure to provider streaming. No emit
        # awaits this queue while holding the input-admission lock.
        events: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=64)

        async def emit(event_type: Any, **fields: Any) -> None:
            event = event_type(run_id=run_id, seq=self._seq, **fields)
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
                self.abort("event consumer closed")
                worker.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await worker
            finally:
                async with self._lock:
                    self._running = self._open = False
                    self._run_tools = None
                    self._steers.clear()
                    self._follow_ups.clear()

    async def _cancellable(self, awaitable: Awaitable[T]) -> T:
        operation = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(self._ctx.cancel.wait())
        try:
            ready, _ = await asyncio.wait({operation, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in ready:
                raise _Aborted()
            return await operation
        finally:
            for task in (operation, cancelled):
                if not task.done():
                    task.cancel()
            # Await cleanup: a cancelled job start may still need to record its
            # handle before the run can kill it.
            await asyncio.gather(operation, cancelled, return_exceptions=True)

    def _check_abort(self) -> None:
        if self._ctx.cancel.cancelled:
            raise _Aborted()

    async def _save_state(self) -> None:
        if self._ctx.state == self._committed_state:
            return
        # Reject non-JSON state at the writer boundary, before it reaches a store.
        state = json.loads(json.dumps(self._ctx.state, allow_nan=False))
        row = await self.store.append_payload(self.session_id, "agent_state", {"version": 1, "state": state})
        self._rows.append(row)
        self._committed_state = deepcopy(state)

    async def _consume(self, input: AgentInput) -> None:
        row = await self.store.consume_input(self.session_id, input.message_id, deepcopy(input.message))
        self._rows.append(row)
        await self._save_state()

    async def _response(self, message: AssistantMessage, *, final: bool) -> ContextEntry:
        row = await self.store.append_response(self.session_id, deepcopy(message), final=final)
        self._rows.append(row)
        await self._save_state()
        return row

    async def _drive(self, input: AgentInput, emit: Callable[..., Awaitable[None]]) -> None:
        reason: RunEndReason = "error"
        loaded = False
        consumer_closed = False
        try:
            await emit(RunStarted)
            self._rows = list(await self.store.load(self.session_id))
            projection = project(self._rows, settle_orphan=self.settle_orphan)
            self._ctx.state = deepcopy(dict(projection.state))
            self._committed_state = deepcopy(self._ctx.state)
            loaded = True
            system = self.system
            self._run_tools = dict(self._tools)
            for hook in self.hooks:
                setup = await self._cancellable(hook.before_run(deepcopy(input), self._ctx))
                if setup is not None:
                    if setup.system is not None:
                        system = setup.system
                    if setup.tools is not None:
                        names = [tool.spec.name for tool in setup.tools]
                        if len(names) != len(set(names)):
                            raise ValueError("tool names must be unique")
                        self._run_tools = dict(zip(names, setup.tools))
            self._check_abort()
            await self._consume(input)
            reason = await self._loop(system, emit)
        except _Ended:
            reason = "ended_by_hook"
        except _Aborted:
            reason = "aborted"
        except _ProviderFailed as failure:
            error = failure.error
            reason = (
                "context_exhausted" if error.kind == "overflow" else ("aborted" if error.kind == "aborted" else "error")
            )
            if error.partial is not None:
                row = await self._response(error.partial, final=False)
                await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=False)
            await emit(AgentError, kind=error.kind, message=error.message)
        except asyncio.CancelledError:
            self._ctx.cancel.cancel("event consumer closed")
            reason = "aborted"
            consumer_closed = True
        except Exception as error:
            await emit(AgentError, kind=type(error).__name__, message=str(error))
        finally:
            async with self._lock:
                self._open = False

        try:
            if self._ctx.cancel.cancelled or reason == "aborted":
                await self.jobs.kill_foreground(self.session_id)
            if loaded:
                await self._save_state()
                outcome = RunOutcome(self._ctx.run_id, reason, self.snapshot())
                for hook in self.hooks:
                    await hook.after_run(outcome, self._ctx)
                await self._save_state()
        except Exception as error:
            if consumer_closed:
                raise
            await emit(AgentError, kind=type(error).__name__, message=str(error))
            if reason != "aborted":
                reason = "error"
        if consumer_closed:
            raise asyncio.CancelledError()
        await emit(RunEnded, reason=reason)

    async def _request(self, system: str) -> tuple[ModelRequest, dict[str, Tool]]:
        selected = await self._cancellable(self.models.resolve())
        capabilities = selected.capabilities
        tools = dict(self._run_tools if self._run_tools is not None else self._tools)
        context = project(
            self._rows,
            system=system,
            rehydrated=self.rehydrate(deepcopy(self._ctx.state)) if self.rehydrate else (),
            settle_orphan=self.settle_orphan,
        )
        request = ModelRequest(
            endpoint=selected.endpoint,
            system=context.system,
            messages=context.messages,
            tools=tuple(deepcopy(tool.spec) for tool in tools.values()) if capabilities.supports_tools else (),
            max_tokens=min(self.max_tokens or capabilities.max_output_tokens, capabilities.max_output_tokens),
            reasoning_effort=self.reasoning_effort,
            supports_images=capabilities.supports_images,
        )
        for hook in self.hooks:
            decision = await self._cancellable(hook.before_model(request, self._ctx))
            if isinstance(decision, End):
                await self._save_state()
                raise _Ended()
            if decision is not None:
                request = decision
        allowed = {spec.name for spec in request.tools}
        return request, {name: tool for name, tool in tools.items() if name in allowed}

    async def _model(
        self, system: str, emit: Callable[..., Awaitable[None]]
    ) -> tuple[AssistantMessage, dict[str, Tool]]:
        retries = 0
        while True:
            self._check_abort()
            request, tools = await self._request(system)
            stream = self.models.provider_for(request.endpoint.protocol).stream(request, self._ctx.cancel)
            streamed = False
            terminal = None
            try:
                while True:
                    try:
                        event = await self._cancellable(anext(stream))
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
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            if isinstance(terminal, Done):
                return terminal.message, tools
            if terminal is None:
                terminal = ProviderError("unknown", "Provider stream ended without a terminal event.", False)
            delay = self.retry.delay(terminal, retries=retries, streamed=streamed)
            if delay is None:
                raise _ProviderFailed(terminal)
            retries += 1
            await self._cancellable(asyncio.sleep(delay))

    async def _loop(self, system: str, emit: Callable[..., Awaitable[None]]) -> RunEndReason:
        while True:
            message, tools = await self._model(system, emit)
            self._check_abort()
            pending: list[tuple[AgentInput, bool]] = []
            failed = message.stop_reason in {"error", "aborted"}
            async with self._lock:
                if not message.tool_calls and not failed:
                    pending = [(item, True) for item in self._steers]
                    self._steers.clear()
                    if not pending:
                        pending = [(item, False) for item in self._follow_ups]
                        self._follow_ups.clear()
                final = not message.tool_calls and not pending and not failed
                if final or failed:
                    self._open = False
                row = await self._response(message, final=final)
                for item, _ in pending:
                    await self._consume(item)
            await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=final)
            for item, is_steer in pending:
                if is_steer:
                    await emit(SteerApplied, message_id=item.message_id)

            end, skip = False, False
            for hook in self.hooks:
                decision = await self._cancellable(hook.after_model(deepcopy(message), self._ctx))
                if isinstance(decision, End):
                    end = True
                    break
                skip = skip or isinstance(decision, SkipTools)
            if failed:
                await emit(
                    AgentError,
                    kind=message.stop_reason,
                    message=message.error_message or f"Model stopped with {message.stop_reason}.",
                )
                return "aborted" if message.stop_reason == "aborted" else "error"
            if not message.tool_calls:
                await self._save_state()
                if end:
                    return "ended_by_hook"
                if final:
                    return "completed"
                continue

            terminate = False
            for call in message.tool_calls:
                self._check_abort()
                result, step_end = await self._tool(call, tools, skip or end, emit)
                terminate = terminate or result.terminate
                end = end or step_end
            if end:
                return "ended_by_hook"
            if terminate:
                return "completed"
            async with self._lock:
                steers = list(self._steers)
                self._steers.clear()
                for item in steers:
                    await self._consume(item)
            for item in steers:
                await emit(SteerApplied, message_id=item.message_id)

    async def _tool(
        self,
        original: ToolCallBlock,
        tools: Mapping[str, Tool],
        skip: bool,
        emit: Callable[..., Awaitable[None]],
    ) -> tuple[ToolResult, bool]:
        call = deepcopy(original)
        end = False
        result = None
        if skip:
            result = ToolResult((text("[skipped by policy]"),), is_error=True)
        else:
            for hook in self.hooks:
                decision = await self._cancellable(hook.before_tool(call, self._ctx))
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
                decision = await self._cancellable(hook.after_tool(call, result, self._ctx))
                if isinstance(decision, End):
                    end = True
                    break
                if isinstance(decision, AlterResult):
                    result = decision.result
        message = ToolResultMessage(call.id, call.name, result.content, result.is_error)
        row = await self.store.append_tool_result(self.session_id, deepcopy(message), details=deepcopy(result.details))
        self._rows.append(row)
        await self._save_state()
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
        execution = asyncio.create_task(self._cancellable(tool.execute(call.arguments, ctx)))
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
            return ToolResult((text(str(error)),), is_error=True)
        finally:
            for task in (update, execution):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (update, execution) if task is not None), return_exceptions=True)
