"""C-3 in-process agent loop; persistence precedes every committed-row event.

Queue semantics informed by Pi (MIT), packages/agent/src/agent-loop.ts, and the
bare-loop control experiment. This is an Avibe implementation, not a port.
With a ``ContextConfig`` the loop also applies C-9 before every model request:
clearing, the checkpoint turn, and the overflow ladder
(``agent-core-contracts/context.md``). The checkpoint turn is a side turn
(``agent-core-contracts/fork.md`` section 7), which ``Agent.side_turn`` runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Literal, Mapping, Optional, Sequence, TypeVar

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
from core.agent_core.agent.fork import (
    DREAMING,
    CheckpointDetail,
    Decision,
    Files,
    Folded,
    PolicyGate,
    SideReply,
    SideTurn,
    SideTurnResult,
)
from core.agent_core.agent.jobs import TrackingJobHost
from core.agent_core.agent.lifecycle import RunAborted as _Aborted, RunScope
from core.agent_core.agent.models import ModelRouter, ModelSelection, RetryPolicy
from core.agent_core.agent.outcome import OutcomeOwner
from core.agent_core.agent.state import HookStateError, state_representation
from core.agent_core.ai._common import endpoint_origin, native_hosted_tools, portable_endpoint
from core.agent_core.ai.provider import (
    Done,
    ModelEndpoint,
    ModelRequest,
    ProviderError,
    Refusal,
    TextDelta,
    ThinkingDelta,
)
from core.agent_core.cancel import CancelToken
from core.agent_core.harness.context import (
    CLEAR_SOFT_RATIO,
    DEFAULT_MAX_OUTPUT_TOKENS,
    INEFFECTIVE_RATIO,
    UNPRODUCTIVE_CHECKPOINTS,
    MAX_OVERFLOWS,
    MAX_ROLLS,
    PROMPT_VERSION,
    MEMO_FILE,
    MIND_CUT,
    MIND_CUT_UNSTORED,
    MIND_FILE,
    MIND_TEMPLATE,
    Anchor,
    Budget,
    CarriedSkill,
    ContextConfig,
    MemoItem,
    MemoList,
    StateRequest,
    add_usage,
    budget,
    cache_ttl_s,
    checkpoint_block,
    checkpoint_max_tokens,
    checkpoint_repair,
    checkpoint_request,
    checkpoint_text,
    clear_edit,
    carry_skills,
    clearable_results,
    compaction_payload,
    fit_batch,
    fit_edit,
    fit_memo,
    fit_mind,
    fit_result,
    half_cut,
    last_anchor,
    memo_budget,
    memo_item,
    memo_problems,
    with_placeholders,
    mind_revert,
    message_tokens,
    messages_tokens,
    normal_cut,
    notes_tokens,
    output_tokens,
    request_facts,
    request_tokens,
    review_mind,
    rolling_cut,
    skill_candidates,
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
from core.agent_core.hosted_continuations import pending_hosted
from core.agent_core.messages import (
    AssistantMessage,
    HostedImage,
    HostedRecord,
    Message,
    Origin,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
    has_reply,
    image_deliverable,
    message_to_dict,
    text,
    usage_to_dict,
)
from core.agent_core.tools.base import CallInstance, JobHost, Tool, ToolContext, ToolResult
from core.agent_core.tools.paths import to_thread_joined

T = TypeVar("T")
logger = logging.getLogger(__name__)

#: The result of a call a hook's ``end`` or ``skip_tools`` settled without running it (C-3 section 3).
_SKIPPED = "[skipped by policy]"
#: Safe model-visible result for a provider tool call whose arguments failed strict JSON validation.
_MALFORMED_TOOL_CALL = "[tool call skipped: provider returned malformed JSON arguments; no tool was executed]"


class _Ended(Exception):
    pass


class ProviderProtocolViolation(ValueError):
    """A model response cannot be admitted to the canonical transcript."""


class UnsupportedModelRoute(ValueError):
    """The selected model cannot provide the tools required by the agent."""


class _Exhausted(Exception):
    """C-9 section 8 (d): the run stops ``context_exhausted``, saying what fills the context.

    ``kind`` is the error's (``_cause``): the part that is too large when measurement shows it (``input_too_large``,
    ``tool_output_too_large``, ``step_too_large``), else ``context_exhausted``, the conversation's length.
    """

    def __init__(self, limit: int, parts: tuple[ContextPart, ...], kind: str = "context_exhausted") -> None:
        self.limit, self.parts, self.kind = limit, parts, kind
        sizes = ", ".join(f"{part.name} ~{part.tokens}" for part in parts)
        super().__init__(f"The context does not fit the model's input limit of {limit} tokens: {sizes}.")


def _mind_text(files: Mapping[str, Optional[str]]) -> Optional[str]:
    """The mind a checkpoint turn left in mind.md, without the file's final newlines (C-9 section 6)."""
    text = files.get(MIND_FILE)
    return None if text is None else text.rstrip("\n")


def _cache_of(origin: Origin) -> tuple[str, str]:
    """The upstream prompt cache an origin's requests use: provider and model.

    Canonical response rows and served-hop metadata carry the credential-free Source identity, but this C-9 cache
    clock intentionally keys by provider/model. Same-provider/model Sources therefore share one clock; Model Hub cache
    overrides are route-wide rather than a claim of per-Source clocks.
    """
    return origin.provider, origin.model


async def _silent(*_: Any, **__: Any) -> None:
    """A side turn's emit: it shows nothing (C-10 section 7)."""


#: The fresh hops a run resolves to answer refusals before any output (``vibey-hosted-tools.md`` §3.5).
MAX_REFUSAL_RESOLVES = 2


@dataclass
class _Refusals:
    """A run's refusals before any output, and how the requests after them render (``vibey-hosted-tools.md`` §3.5).

    A refusal is answered by a fresh hop, rendered normally: at most once per (Source, tool, aspect) a narrow hosted
    rule names, and at most ``MAX_REFUSAL_RESOLVES`` times in the run. Past a bound, or when a gateway hand-back's
    fresh hop is the refused one again, the request goes portable instead.

    A Source identity whose provider refused an opaque payload of a kind sends none of that kind again in this Session:
    ``opaque`` starts with the Session's earlier refusals (``replay_refusal`` rows), and each new one is answered once,
    then kept (``unsaved`` until the loop persists it). A refusal of a kind already demoted stands as the error.
    """

    resolves: int = 0
    rules: set[tuple[Optional[str], str, Optional[str]]] = field(default_factory=set)
    opaque: set[tuple[Optional[str], str]] = field(default_factory=set)
    unsaved: dict[Optional[str], list[str]] = field(default_factory=dict)

    def answer(self, refusal: Refusal) -> Optional[Literal["resolve", "portable"]]:
        """How the next attempt answers ``refusal``, counted; None when the refusal stands as the error."""
        if refusal.kind == "opaque_refused":
            fresh = [kind for kind in refusal.payloads if (refusal.source_identity, kind) not in self.opaque]
            if not fresh:
                return None
            self.opaque.update((refusal.source_identity, kind) for kind in fresh)
            self.unsaved.setdefault(refusal.source_identity, []).extend(fresh)
        rule = (refusal.source_id, refusal.tool, refusal.aspect) if refusal.tool is not None else None
        if self.resolves >= MAX_REFUSAL_RESOLVES or rule in self.rules:
            return "portable"
        self.resolves += 1
        if rule is not None:
            self.rules.add(rule)
        return "resolve"

    def render(self, selection: ModelSelection, *, portable: bool) -> ModelSelection:
        """``selection`` as the next request goes out: without the refused identities' opaque payloads, and portable
        when the refusal it answers asked for that."""
        endpoint = selection.endpoint
        if self.opaque:
            endpoint = replace(endpoint, opaque_refused=frozenset(self.opaque))
        if portable:
            endpoint = portable_endpoint(endpoint)
        return selection if endpoint is selection.endpoint else replace(selection, endpoint=endpoint)


def _same_hop(fresh: ModelEndpoint, refused: ModelEndpoint) -> bool:
    """A fresh hop that would send the refused request again: the same Source, account and model, with the same
    hosted-tool facts (§3.5)."""
    return (fresh.source_id, fresh.source_identity, fresh.model_id, fresh.hosted_tools) == (
        refused.source_id,
        refused.source_identity,
        refused.model_id,
        refused.hosted_tools,
    )


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

    purpose: Literal["conversation", "side_turn"]
    request: ModelRequest
    message: Optional[AssistantMessage]
    error: Optional[ProviderError] = None
    committed: bool = False
    audited: bool = False


@dataclass(frozen=True)
class _Outcome:
    ok: bool
    overflow: bool = False


class _SideTurnTooLarge(Exception):
    """A side turn's request that cannot fit the window; it is never sent."""


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
        # What a person typed, by input row id, as the host answered (None: not a person's message); a message never
        # changes, so the answer holds for the Agent's life (C-9 section 7, the memo).
        self._typed: dict[str, Optional[str]] = {}
        self._typed_failure: Optional[Exception] = None
        # The run's attempt ledger (C-9): every model attempt, in order.
        self._attempts: list[_Attempt] = []
        self._refusals = _Refusals()
        # The images the run's committed tool results generated, which its final delivers: a reply even without text
        # (``has_reply``). Read from the committed rows, so a result another writer settled counts as it committed.
        self._generated_images: list[HostedImage] = []
        # The C-9 cache clock deliberately keys the served provider/model boundary;
        # canonical rows also retain Source identity, but it is not a separate clock.
        self._last_model_at: dict[tuple[str, str], float] = {}
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
        return self._pump(turn_id, lambda emit: self._drive(emit, input, consume=True))

    def resume(
        self,
        input: AgentInput,
        *,
        turn_id: str,
        turn_inputs: Iterable[int] | None = None,
        pending_inputs: Iterable[AgentInput] = (),
    ) -> AsyncIterator[AgentEvent]:
        """Continue from the committed transcript without consuming another input row.

        The input is still passed to ``before_run`` so existing hook setup remains
        unchanged; it is a control value, not a second transcript message. A fresh
        Agent can provide the source Turn's input sequence numbers so compaction
        pins only that Turn's inputs. Within the same Turn, newly admitted inputs
        extend the existing pins rather than replacing them. ``pending_inputs`` restores
        accepted but unconsumed steers behind the next response, not into its request.
        """
        input = deepcopy(input)
        resume_turn_inputs = None if turn_inputs is None else set(turn_inputs)
        return self._pump(
            turn_id,
            lambda emit: self._drive(emit, input, consume=False),
            preserve_turn_inputs=True,
            resume_turn_inputs=resume_turn_inputs,
            pending_inputs=deepcopy(tuple(pending_inputs)),
        )

    async def _pump(
        self,
        turn_id: str,
        drive: Callable[[Callable[..., Awaitable[None]]], Awaitable[None]],
        *,
        preserve_turn_inputs: bool = False,
        resume_turn_inputs: set[int] | None = None,
        pending_inputs: tuple[AgentInput, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        previous_turn_inputs = set(self._turn_inputs)
        previous_seq = self._seq
        async with self._lock:
            if self._running:
                raise RuntimeError("this Agent already has an active run")
            if self._steers or self._follow_ups:
                raise RuntimeError("return unconsumed inputs with take_pending_inputs() before starting another run")
            self._steers.extend(pending_inputs)
            continuing_turn = preserve_turn_inputs and self._ctx.turn_id == turn_id
            self._running = True
            self._open = True
            self._consumer_closed = False
            self._ctx = RunContext(self.session_id, turn_id, CancelToken())
            self._scope = RunScope(self._ctx.cancel)
            self._outcome = OutcomeOwner()
            self._seq = previous_seq if continuing_turn else 0
            if preserve_turn_inputs:
                self._turn_inputs = (previous_turn_inputs if continuing_turn else set()) | set(resume_turn_inputs or ())

        # Bounded delivery applies backpressure to provider streaming. No emit
        # awaits this queue while holding the input-admission lock.
        events: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=64)
        # Concurrent tool calls emit too, and a put can overtake one that waits for room in the full queue: events
        # enter it one at a time, in ``seq`` order.
        ordered = asyncio.Lock()

        async def emit(event_type: Any, **fields: Any) -> Optional[AgentEvent]:
            async with ordered:
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

    async def _save_refusals(self) -> None:
        """Keep the run's new opaque refusals in the Session, so its later turns never send what was refused."""
        unsaved, self._refusals.unsaved = self._refusals.unsaved, {}
        for identity, payloads in unsaved.items():
            entry = {"version": 1, "source_identity": identity, "payloads": payloads}
            row = await self._scope.call(
                lambda entry=entry: self.store.append_payload(self.session_id, "replay_refusal", entry),
                interruptible=False,
            )
            self._rows.append(row)

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
        """A response is valid after the committed rows and then ``after`` (a side turn's own messages)."""
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

    async def _response(
        self, message: AssistantMessage, *, final: bool, replied: Optional[bool] = None, superseded: bool = False
    ) -> ContextEntry:
        self._validate_response(message)
        await self._save_state()
        # The attempt this response answered: the ledger's latest conversation attempt (the C-9 anchor).
        attempt = next((item for item in reversed(self._attempts) if item.purpose == "conversation"), None)
        facts = {"request": request_facts(attempt.request)} if self.context is not None and attempt else {}
        row = await self._scope.call(
            lambda: self.store.append_response(
                self.session_id,
                deepcopy(message),
                final=final,
                replied=replied,
                images=tuple(self._generated_images) if final else (),
                superseded=superseded,
                **facts,
            ),
            interruptible=False,
        )
        if attempt is not None:
            attempt.committed = True
        self._rows.append(row)
        return row

    async def _drive(
        self,
        emit: Callable[..., Awaitable[None]],
        input: AgentInput,
        *,
        consume: bool,
    ) -> None:
        """One run of ``input`` or one transcript-only continuation."""
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
                if consume:
                    self._turn_inputs = set()
                self._attempts = []
                # The Session's earlier opaque refusals hold from this run's first request.
                self._refusals = _Refusals(opaque=set(projection.opaque_refused))
                if consume:
                    self._generated_images = []
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
                if consume:
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
        self, emit: Callable[..., Awaitable[Any]], kind: str, message: str, origin: ErrorOrigin, *, cause: bool,
        provider_error: Optional[ProviderError] = None,
    ) -> None:
        """The one place the loop announces an error: ``origin`` as its caller states it, where the error is raised.

        ``cause``: the error decided the run's outcome (``OutcomeOwner.primary``), so ``RunEnded`` names it; a
        diagnostic never does.
        """
        event = await emit(
            AgentError, kind=kind, message=message, origin=origin,
            retryable=provider_error.retryable if provider_error is not None else False,
            continuable=provider_error.continuable if provider_error is not None else False,
            retry_after_s=provider_error.retry_after_s if provider_error is not None else None,
        )
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
                decided = self._outcome.foreground_leaked()
                if self._consumer_closed:
                    raise
                if decided:
                    # It decided the outcome, so it is the cause ``RunEnded`` names (C-3 section 6).
                    await self._error(emit, type(error).__name__, str(error), "local", cause=True)
                else:
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
        purpose: Literal["conversation", "side_turn"] = "conversation",
        after: Sequence[Message] = (),
    ) -> AsyncIterator[tuple[Done | ProviderError, dict[str, Tool]]]:
        """One model request with its transient retries.

        ``compose(selection)`` is the request pipeline: it returns the final request, which is sent unchanged.
        A conversation request's pipeline is ``_compose``. Every attempt enters the run's attempt ledger. A
        conversation request the provider rejects as overflow goes back through the pipeline with the ladder
        told to shrink (C-9 section 8). A side turn's attempt is never context.

        Admission is here, for every purpose: the response the caller receives (or a partial with content) is
        valid after the committed rows and ``after``, the messages the request carried beyond them (a side turn's
        prompt and its own turn), or ``ProviderProtocolViolation`` is raised before anyone acts on it.
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
            # A response that dropped after thinking alone is retried once per request (C-2 ``provider.md``): some
            # prompts drop at the same point every time.
            thinking_retried = False
            # A refusal before any output is answered by rendering the request again (§3.5, ``_Refusals``): the hop a
            # gateway hand-back refused, and whether the next request goes portable.
            refused: Optional[ModelEndpoint] = None
            portable = False
            while True:
                self._scope.check()
                if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                    yield retry_error, {}
                    return
                if selected is None:
                    selected = await self._scope.call(self.models.resolve)
                    portable = portable or (refused is not None and _same_hop(selected.endpoint, refused))
                # Resolution is the last async stage before request admission.
                # Expiry wins before route validation, projection, rehydration or
                # hooks can replace the original error or persist new side effects.
                if retry_error is not None and time.monotonic() - started >= self.retry.max_elapsed_s:
                    yield retry_error, {}
                    return
                route = selected
                request, tools = await compose(self._refusals.render(selected, portable=portable))
                selected, refused, portable = None, None, False
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
                    if terminal is None:
                        terminal = ProviderError("unknown", "Provider stream ended without a terminal event.", False)
                    if self.context is not None:
                        served = terminal.message if isinstance(terminal, Done) else terminal.partial
                        upstream = served.origin if served is not None else endpoint_origin(request.endpoint)
                        self._last_model_at[_cache_of(upstream)] = self.context.clock()
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
                    rerender = (
                        self._refusals.answer(terminal.refusal)
                        if isinstance(terminal, ProviderError)
                        and terminal.refusal is not None
                        and not streamed
                        and (terminal.partial is None or not terminal.partial.content)
                        else None
                    )
                    delay = (
                        self.retry.delay(terminal, retries=retries, streamed=streamed, elapsed_s=time.monotonic() - started)
                        if isinstance(terminal, ProviderError) and not relieve and rerender is None
                        else None
                    )
                    if delay is not None and terminal.after_thinking:
                        if thinking_retried:
                            delay = None
                        thinking_retried = True
                    if delay is None and not relieve and rerender is None:
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
                if rerender is not None:
                    await self._save_refusals()
                if rerender == "portable":
                    selected, portable = route, True
                    continue
                if rerender == "resolve":
                    if terminal.refusal.kind == "pin_unavailable":
                        refused = route.endpoint
                    continue
                if relieve:
                    ladder.overflows += 1
                    ladder.refused = True
                    if ladder.overflows >= MAX_OVERFLOWS:
                        view = context_view(self._rows)
                        plan = budget(
                            request, route.capabilities, transcript=view.messages, anchor=last_anchor(self._rows, view)
                        )
                        movable = half_cut(view.units, turn_inputs=self._turn_inputs, pinned=view.pinned) is not None
                        kind = "context_exhausted" if movable else self._cause(view, None)
                        raise self._exhausted(request, view, plan, kind=kind)
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

    async def _commit_model_message(
        self,
        message: AssistantMessage,
        emit: Callable[..., Awaitable[None]],
        *,
        replied: bool,
        keep_open: bool = False,
    ) -> tuple[ContextEntry, bool]:
        """Commit the response: its row, and whether it is final. Finality and queued-input admission share one lock
        with the commit. ``replied`` is the run's decision whether it replies, which the store takes as given."""
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
                # A completed reply the pending input took the final word from: one cut off at its output limit
                # never completed, as ``final_outcome`` holds for a final.
                superseded = bool(pending) and replied and message.stop_reason != "length"
                if (final or failed) and not keep_open:
                    self._open = False
                row = await self._response(message, final=final, replied=replied, superseded=superseded)
                for item, is_steer in pending:
                    await self._consume(item)
                    (self._steers if is_steer else self._follow_ups).popleft()
                    consumed.append((item, is_steer))
        finally:
            if row is not None and not self._consumer_closed:
                await emit(
                    MessageCommitted,
                    message_id=row.row_id,
                    context_seq=row.context_seq,
                    final=final,
                    superseded=superseded,
                )
                for item, is_steer in consumed:
                    if is_steer:
                        await emit(SteerApplied, message_id=item.message_id)
        return row, final

    async def _loop(self, system: str, emit: Callable[..., Awaitable[None]], selected: ModelSelection) -> RunEndReason:
        first_selection: Optional[ModelSelection] = selected
        length_tool_retries = 0
        while True:
            malformed_recovery: Optional[tuple[ProviderError, dict[str, Tool], ContextEntry]] = None
            async with self._model(system, emit, first_selection) as (terminal, tools):
                first_selection = None
                if isinstance(terminal, ProviderError):
                    malformed = terminal.malformed_tool_calls
                    if terminal.partial is not None and malformed:
                        calls_by_id = {call.id: call for call in terminal.partial.tool_calls}
                        malformed_by_id = {item.id: item for item in malformed}
                        if (
                            terminal.partial.stop_reason == "error"
                            and len(malformed_by_id) == len(malformed)
                            and all(
                                item.id in calls_by_id and calls_by_id[item.id].name == item.name
                                for item in malformed
                            )
                        ):
                            replied = has_reply(terminal.partial, generated_images=bool(self._generated_images))
                            response, _ = await self._commit_model_message(
                                terminal.partial, emit, replied=replied, keep_open=True
                            )
                            malformed_recovery = (terminal, tools, response)
                    if malformed_recovery is None:
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
                        await self._error(
                            emit, terminal.kind, terminal.message, origin, cause=decided, provider_error=terminal
                        )
                        if terminal.partial is not None:
                            row = await self._response(terminal.partial, final=False)
                            await emit(MessageCommitted, message_id=row.row_id, context_seq=row.context_seq, final=False)
                        return reason
                if malformed_recovery is None:
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
                    # Decided once: the empty-reply test, the row's type and settlement all take it (``has_reply``).
                    replied = has_reply(message, generated_images=bool(self._generated_images))
                    response, final = await self._commit_model_message(message, emit, replied=replied)
                    empty_reply = final and not replied
                    truncated_reply = final and message.stop_reason == "length"
                    if empty_reply or truncated_reply:
                        decided = self._outcome.primary("error")
                        await self._error(
                            emit,
                            message.stop_reason if message.stop_reason in {"length", "refusal", "safety"} else "empty_response",
                            message.error_message or (
                                "The model reached its output limit before completing the reply."
                                if truncated_reply else f"Model stopped with {message.stop_reason} without a reply."
                            ),
                            "source",
                            cause=decided,
                        )
            if malformed_recovery is not None:
                recovery_error, tools, response = malformed_recovery
                message = recovery_error.partial
                assert message is not None
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
            if malformed_recovery is not None:
                recovery_message = message
                malformed_ids = {call.id for call in recovery_error.malformed_tool_calls}
                skip_calls = {call_id: _MALFORMED_TOOL_CALL for call_id in malformed_ids}
                terminate, step_end = await self._calls(
                    recovery_message.tool_calls,
                    response,
                    tools,
                    emit,
                    skip=_SKIPPED if skip or end else None,
                    skip_calls=skip_calls,
                )
                if end or step_end:
                    self._open = False
                    await self._error(
                        emit,
                        recovery_error.kind,
                        recovery_error.message,
                        "source",
                        cause=False,
                        provider_error=recovery_error,
                    )
                    return "ended_by_hook"
                if terminate:
                    self._open = False
                    await self._error(
                        emit,
                        recovery_error.kind,
                        recovery_error.message,
                        "source",
                        cause=False,
                        provider_error=recovery_error,
                    )
                    return "completed"
                await self._error(
                    emit,
                    recovery_error.kind,
                    recovery_error.message,
                    "source",
                    cause=True,
                    provider_error=recovery_error,
                )
                await self._drain_steers(emit)
                return "error"
            if message.tool_calls and message.stop_reason != "tool_use":
                reason = message.stop_reason
                skip_reason = (
                    "Tool call was truncated by the output limit; re-issue it with complete arguments."
                    if reason == "length"
                    else f"Tool call not executed because the model stopped with {reason}."
                )
                await self._calls(message.tool_calls, response, tools, emit, skip=skip_reason)
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
                if empty_reply or truncated_reply:
                    return "error"
                if end:
                    return "ended_by_hook"
                if final:
                    return "completed"
                continue

            terminate, step_end = await self._calls(
                message.tool_calls, response, tools, emit, skip=_SKIPPED if skip or end else None
            )
            end = end or step_end
            if end:
                return "ended_by_hook"
            if terminate:
                return "completed"
            # Anthropic has not run these server tools yet. A steer after the client results would end its open
            # turn (and produce a 400), so admit it after the deferred result, not inside this continuation.
            if not pending_hosted(message):
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
                return call, ToolResult((text(_SKIPPED),), is_error=True), True
            if isinstance(decision, AlterArgs):
                call = replace(call, arguments=deepcopy(decision.arguments))
        return call, None, False

    @staticmethod
    def _groups(calls: Sequence[ToolCallBlock], tools: Mapping[str, Tool]) -> list[list[ToolCallBlock]]:
        """C-3 section 2: each maximal run of consecutive concurrent calls is one group, and every other call is a
        group of its own. A call to a tool that is not available runs nothing, so it never splits a group."""
        groups: list[list[ToolCallBlock]] = []
        joins = False
        for call in calls:
            tool = tools.get(call.name)
            concurrent = tool is None or getattr(tool, "concurrent", False) is True
            if concurrent and joins:
                groups[-1].append(call)
            else:
                groups.append([call])
            joins = concurrent
        return groups

    async def _calls(
        self,
        calls: Sequence[ToolCallBlock],
        response: ContextEntry,
        tools: Mapping[str, Tool],
        emit: Callable[..., Awaitable[None]],
        *,
        skip: Optional[str] = None,
        skip_calls: Optional[Mapping[str, str]] = None,
    ) -> tuple[bool, bool]:
        """A response's calls, group after group (C-3 sections 2 and 3): whether a result terminates the run, and
        whether a hook ended it. ``skip`` settles every call with that text instead, one at a time, running none;
        ``skip_calls`` settles only the named calls and does not expose them as started work."""
        terminate = end = False
        for group in [[call] for call in calls] if skip is not None else self._groups(calls, tools):
            self._scope.check()
            # Every gate of the group runs before any of its calls starts: (call, result, whether after_tool runs).
            gated: list[tuple[ToolCallBlock, Optional[ToolResult], bool]] = []
            for original in group:
                call = deepcopy(original)
                call_skip = (skip_calls or {}).get(call.id)
                if skip is not None or call_skip is not None or end:
                    gated.append((call, ToolResult((text(call_skip or skip or _SKIPPED),), is_error=True), False))
                    continue
                call, result, ended = await self._gate(call, self.hooks)
                if result is None and call.name not in tools:
                    result = ToolResult((text(f"Tool {call.name} is not available."),), is_error=True)
                gated.append((call, result, not ended))
                end = end or ended
            running: dict[int, asyncio.Task[ToolResult]] = {}
            try:
                for index, (call, result, _) in enumerate(gated):
                    if call.id not in (skip_calls or {}):
                        await emit(
                            ToolStarted,
                            tool_call_id=call.id,
                            name=call.name,
                            preview=json.dumps(call.arguments, ensure_ascii=False)[:500],
                        )
                    if result is None:
                        instance = CallInstance(response.session_id, response.row_id, response.context_seq, call.id)
                        running[index] = asyncio.create_task(self._execute(tools[call.name], call, instance, emit))
                # Results commit in call order. After an after_tool ``end``, the group's later calls have started
                # already: they still pass after_tool and commit their real results.
                for index, (call, result, after) in enumerate(gated):
                    if index in running:
                        result = await running[index]
                    if after:
                        for hook in self.hooks:
                            decision = await self._hook(lambda: hook.after_tool(call, result, self._ctx))
                            if isinstance(decision, End):
                                end = True
                                break
                            if isinstance(decision, AlterResult):
                                result = decision.result
                    await self._commit_result(
                        call,
                        result,
                        emit,
                        emit_finished=call.id not in (skip_calls or {}),
                    )
                    terminate = terminate or result.terminate
            finally:
                await self._join(running.values())
        return terminate, end

    @staticmethod
    async def _join(calls: Iterable[asyncio.Task]) -> None:
        """No call outlives its batch: the calls still running are cancelled, and every one is awaited."""
        calls = tuple(calls)
        for task in calls:
            if not task.done():
                task.cancel()
        joined = asyncio.gather(*calls, return_exceptions=True)
        try:
            await asyncio.shield(joined)
        except asyncio.CancelledError:
            await joined
            raise

    async def _commit_result(
        self,
        call: ToolCallBlock,
        result: ToolResult,
        emit: Callable[..., Awaitable[None]],
        *,
        emit_finished: bool = True,
    ) -> None:
        message = ToolResultMessage(call.id, call.name, result.content, result.is_error, hosted=result.hosted)
        validate_message_append(self._rows, session_id=self.session_id, kind="tool_result", message=message)
        await self._save_state()
        row = await self._scope.call(
            lambda: self.store.append_tool_result(self.session_id, deepcopy(message), details=deepcopy(result.details)),
            interruptible=False,
        )
        self._rows.append(row)
        # The committed result, which a settlement race may have taken from another writer, not this execution's.
        committed = row.message.hosted if isinstance(row.message, ToolResultMessage) else None
        if (committed is not None and isinstance(committed.record, HostedRecord)
                and committed.record.status == "completed" and call.name == "image_generate"):
            # Only an image a surface can deliver counts toward the reply, and is carried with it.
            self._generated_images.extend(image for image in committed.record.images or () if image_deliverable(image))
        if emit_finished:
            await emit(
                ToolFinished,
                tool_call_id=call.id,
                event_id=row.row_id,
                is_error=result.is_error,
                watch_id=result.details.get("watch_id"),
            )

    async def _execute(
        self,
        tool: Tool,
        call: ToolCallBlock,
        instance: Optional[CallInstance],
        emit: Callable[..., Awaitable[None]],
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
            instance,
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
    # owners here: ``_compose``, ``_side_request``, and ``_minimal`` (1, requests composed and budgeted;
    # ``_compaction`` is the one builder of checkpoint rows), ``_side_tool`` (2, the side turn's tool pipeline,
    # C-10 section 7), ``_commit_context`` (3), ``self._attempts`` with ``_settle_attempts`` and ``side_turn``'s
    # one audit (4), ``harness.context.budget`` (5), and ``_model`` (6, admission).

    def _cache_cold(self, endpoint: ModelEndpoint) -> bool:
        """The provider cache of the request's route has gone unused longer than it stays warm (section 3).

        Its use is the latest model call's end or response row's commit served by the route's upstream cache
        (``_cache_of``), so a long response counts as use until it ends, and a restarted Agent reads the rows. A route
        with no use in the Session yet takes the latest use of any route: every error leans toward a warm cache. The
        row also retains the served Source identity for canonical replay and scrubbing, but this C-9 clock intentionally
        keys by provider/model.
        """
        used = dict(self._last_model_at)
        for row in self._rows:
            if row.kind == "response" and row.created_at is not None:
                cache = _cache_of(row.message.origin)
                used[cache] = max(used.get(cache, row.created_at), row.created_at)
        last = used.get(_cache_of(endpoint_origin(endpoint)), max(used.values(), default=None))
        return last is not None and self.context.clock() - last > cache_ttl_s(endpoint)

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

        Whether the run still takes checkpoints is read from its live bound at every decision, never kept: a
        checkpoint attempt in this pass can use it up.
        """
        # Once the run takes no more checkpoints, only a drop can follow, and only for a request that cannot fit or
        # that the provider refused; for one that will be sent as it is, nothing is built for a compaction.
        compacting = not plan.can_fit or ladder.refused
        if not self._stopped:
            compacting = compacting or plan.est >= plan.threshold or not plan.fits
        if compacting:
            # A compaction may follow: what a person typed, for its memo, read off the event loop first.
            await self._prefetch_typed(view)
        if not plan.can_fit:
            _, minimal = await self._minimal(system, selected, request, view, plan, carry=False)
            if not minimal.can_fit:
                # Not even the request the drop would leave (its row and the last unit, without the previous
                # checkpoint's text) can fit: moving the conversation out cannot help. The newest tool batch is cut
                # to fit (section 3); anything else is (d), and the provider never sees it.
                rest = minimal.est - self._batch_tokens(view)
                room = minimal.input_limit - minimal.output - rest
                if await self._fit_batch(view, (room - minimal.margin, room)):
                    return True
                raise self._exhausted(request, view, plan, kind=self._cause(view, minimal))
        refused, ladder.refused = ladder.refused, False
        shrink = refused
        if not shrink:
            if self.context.clear_tool_results and (
                plan.est >= CLEAR_SOFT_RATIO * plan.threshold or self._cache_cold(request.endpoint)
            ):
                targets = clearable_results(view)
                if targets:
                    await self._commit_context([("context_edit", clear_edit(target)) for target in targets])
                    return True
            if plan.est >= plan.threshold and not ladder.compacted and not self._stopped:
                cut = normal_cut(view.units, plan.keep, turn_inputs=self._turn_inputs, pinned=view.pinned)
                if cut is not None:
                    ladder.compacted = True
                    turn = self._checkpoint_turn(request, view, plan, cut, mode="normal", reason="threshold")
                    if not self.side_turn_fits(turn, selected, system=system):
                        shrink = True  # no fork can take the whole context: the ladder from (b)
                    else:
                        outcome = await self._checkpoint(system, selected, turn, emit)
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
        """The run has made ``UNPRODUCTIVE_CHECKPOINTS`` failed or ineffective checkpoint attempts (section 10): it
        takes no more checkpoints. The drop, which calls no model, still makes room."""
        return self._unproductive >= UNPRODUCTIVE_CHECKPOINTS

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
        dropped = await self._dropped(view, cut, carry=carry, window=plan.window)
        payload, after = await self._compaction(request, view, plan, cut, **dropped, hosted=hosted)
        minimal, _ = self._built(system, selected, messages=self._carried(after))
        return minimal, budget(minimal, selected.capabilities, transcript=after.messages)

    def _carried(self, view: ContextView) -> tuple[Message, ...]:
        """What a conversation request carries for ``view``: the rehydrated state, then the projected context."""
        return (*self._rehydrated(), *view.messages)

    async def _dropped(self, view: ContextView, cut: int, *, carry: bool, window: int) -> dict[str, Any]:
        """A dropped row's fields (section 8 c): no model call. When ``carry``, the previous checkpoint as it was (its
        now, mind, and skills) and a memo that keeps every message, the new ones included, no one choosing among
        them; otherwise all of it left out, and said so."""
        previous = view.compaction.payload if view.compaction is not None else {}
        text = previous.get("checkpoint", "")
        fields: dict[str, Any] = {
            "mode": "dropped",
            "reason": "overflow",
            "checkpoint": text if carry else "",
            "checkpoint_omitted": self._carryable(view) and not carry,
            "summarizer": None,
            "usage": None,
        }
        if carry:
            mind = previous.get("mind") or ""
            fields.update(
                mind=mind,
                mind_shown=self._mind_shown(mind, window),
                memo=fit_memo(self._memo_list(view, cut).select(None), memo_budget(window)),
                skills=await self._kept_skills(view, window),
            )
        return fields

    async def _kept_skills(self, view: ContextView, window: int) -> tuple[CarriedSkill, ...]:
        """The skills a drop keeps (section 8 c): the latest checkpoint's, as it carried them. A ``checkpoint-v4`` row
        names them only, so the host renders them now, as a checkpoint would carry them (section 7)."""
        row = view.compaction
        previous = row.payload if row is not None else {}
        if "skills_carried" in previous:
            return tuple(CarriedSkill(**skill) for skill in previous["skills_carried"])
        host = self.context.host if self.context is not None else None
        names = [skill["name"] for skill in previous.get("skills", ())]
        if host is None or not names:
            return ()
        written = (row.created_at or 0.0) if row is not None else 0.0
        rendered = await self._scope.call(lambda: asyncio.to_thread(host.skill_contents, self.session_id, names))
        return carry_skills([(name, written) for name in names], rendered.get, window)

    @staticmethod
    def _carryable(view: ContextView) -> bool:
        """Whether the latest checkpoint has anything a drop could carry: its now, mind, memo, or skills."""
        previous = view.compaction.payload if view.compaction is not None else {}
        return any(previous.get(key) for key in ("checkpoint", "mind", "memo", "skills_carried"))

    async def _carries(
        self, system: str, selected: ModelSelection, request: ModelRequest, view: ContextView, plan: Budget
    ) -> bool:
        """Whether a drop keeps the previous checkpoint and the user's messages it moves out (section 8 c): only when
        the request the drop would leave, carrying them, can fit this route. A checkpoint written on a larger route
        may not. When what it carries cannot be gathered (the host could not say which inputs a person typed), the
        drop leaves it out rather than stop the run: the ladder must still free the room."""
        if len(view.units) < 2:
            return True  # nothing moves out
        try:
            return (await self._minimal(system, selected, request, view, plan, carry=True))[1].can_fit
        except (_Aborted, asyncio.CancelledError):
            raise
        except Exception:
            logger.warning("Cannot gather what a drop would carry; dropping without it", exc_info=True)
            return False

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

        Every step reads the run's bound live (section 10), as the stage does, so a checkpoint attempt here can use it
        up: from then on no checkpoint is tried, a request that can fit and the provider has not refused is sent as it
        is, and any other moves on to the drop.
        """
        units = view.units
        while True:
            stopped = self._stopped
            if stopped and not refused and plan.can_fit:
                return False
            if not stopped and not ladder.summary_failed and not ladder.compacted:
                ladder.compacted = True
                cut = normal_cut(units, plan.keep, turn_inputs=self._turn_inputs, pinned=view.pinned)
                turn = (
                    None
                    if cut is None
                    else self._checkpoint_turn(request, view, plan, cut, mode="normal", reason="overflow")
                )
                if turn is not None and self.side_turn_fits(turn, selected, system=system):
                    outcome = await self._checkpoint(system, selected, turn, emit)
                    if outcome.ok:
                        return True
                    ladder.summary_failed = not outcome.overflow
                continue
            if not stopped and not ladder.summary_failed and ladder.rolls < MAX_ROLLS:
                def rolling(cut: int) -> SideTurn:
                    return self._checkpoint_turn(request, view, plan, cut, mode="rolling", reason="overflow")

                cut = rolling_cut(
                    units,
                    lambda cut: self.side_turn_fits(rolling(cut), selected, system=system),
                    turn_inputs=self._turn_inputs,
                    pinned=view.pinned,
                )
                if cut is None:
                    ladder.rolls = MAX_ROLLS
                    continue
                ladder.rolls += 1
                outcome = await self._checkpoint(system, selected, rolling(cut), emit)
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
                kind = self._cause(view, None if ladder.overflows else plan)
                raise self._exhausted(request, view, plan, kind=kind)
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

    def _cause(self, view: ContextView, measured: Optional[Budget]) -> str:
        """What a stop names as too large when nothing more can move out (section 8 d), decided by measurement.

        ``measured`` is the request the stop judged (the minimal request, or the request itself), whose estimate is
        over its limit: a part of the newest unit is named only when the request without it fits. A request the
        provider refused (None) fit our estimate, so its excess is unknown: the results are named while the cuts left
        them above their notes, the step once they are at their notes.
        """
        last = view.units[-1] if view.units else None
        if last is None:
            return "context_exhausted"
        if last.lead.kind == "input":
            if measured is None:
                return "context_exhausted"
            without = measured.est - message_tokens(last.lead.message) + measured.output
            return "input_too_large" if without <= measured.input_limit else "context_exhausted"
        results = self._batch_tokens(view)
        at_notes = results <= notes_tokens([entry.message for entry, _ in self._newest_batch(view)])
        if measured is None:
            return "step_too_large" if at_notes else "tool_output_too_large"
        rest = measured.est - results + measured.output
        if rest <= measured.input_limit:
            return "tool_output_too_large"  # the results, even at their notes, are the excess
        if rest - message_tokens(last.lead.message) <= measured.input_limit:
            return "step_too_large"  # the step itself: its tool-call arguments or text
        return "context_exhausted"

    def _exhausted(
        self, request: ModelRequest, view: ContextView, plan: Budget, *, kind: str = "context_exhausted"
    ) -> _Exhausted:
        """Section 8 (d): what fills the request, for the user's stop message, and the error's ``kind`` (``_cause``)."""
        last = view.units[-1] if view.units else None
        last_tokens = unit_tokens(last) if last is not None else 0
        parts = [
            ContextPart("system", text_tokens(request.system)),
            ContextPart("tools", request_tokens("", request.tools, (), native_hosted_tools(request))),
            ContextPart("history", max(0, messages_tokens(request.messages) - last_tokens)),
        ]
        if last is not None:
            name = "current_request" if last.lead.kind == "input" else "latest_tool_batch"
            parts.append(ContextPart(name, last_tokens))
        parts.append(ContextPart("output", plan.output + plan.margin))
        return _Exhausted(plan.input_limit, tuple(parts), kind)

    async def _checkpoint(
        self, system: str, selected: ModelSelection, turn: SideTurn, emit: Callable[..., Awaitable[None]]
    ) -> _Outcome:
        """Sections 6 and 7: a checkpoint turn, the side turn ``_checkpoint_turn`` built; on success its row joins
        the context. The events and the run's bound are the checkpoint's; the turn is ``side_turn``'s."""
        reason, mode = turn.detail.reason, turn.detail.mode
        await emit(CompactionStarted, reason=reason)
        result = await self.side_turn(turn, selected, system=system)
        if result.outcome == "failed":
            self._unproductive += 1
            await emit(CompactionFailed, reason=reason, error=result.error)
            return _Outcome(False, result.overflow)
        row = result.folded
        assert row is not None  # a checkpoint's fold commits its row or fails
        if mode == "normal" and row.payload["tokens_after_estimate"] >= INEFFECTIVE_RATIO * row.payload["threshold"]:
            self._unproductive += 1  # it made too little room to count as progress
        await emit(
            CompactionFinished,
            event_id=row.row_id,
            reason=reason,
            mode=mode,
            tokens_before=row.payload["tokens_before"],
            tokens_after_estimate=row.payload["tokens_after_estimate"],
        )
        return _Outcome(True)

    def _checkpoint_turn(
        self, request: ModelRequest, view: ContextView, plan: Budget, cut: int, *, mode: str, reason: str
    ) -> SideTurn:
        """The checkpoint turn for ``cut`` as a side turn (C-10 section 12): the whole view, or for a rolling one its
        prefix, then the checkpoint request, under ``DREAMING``. It starts with the previous mind (the template at a
        first v5 checkpoint) and this cut's memo list in its scratch root (section 6). Its check accepts a final reply
        holding one checkpoint block above the floor of the route it was planned on, and asks one repair for a mind or
        memo the validators reject (section 10); its fold commits the checkpoint row."""
        previous = view.compaction.payload if view.compaction is not None else {}
        # Every empty section of the stored mind holds its placeholder (section 7): one stored before the placeholders,
        # or one whose turn left a section blank, so the section can be filled without a heading in oldText.
        prior: str = with_placeholders(previous.get("mind") or "")
        start = prior or MIND_TEMPLATE
        window = plan.window
        refused = None
        try:
            memo = self._memo_list(view, cut)
        except (_Aborted, asyncio.CancelledError):
            raise
        except Exception as failure:
            # A host that cannot say which inputs a person typed fails the checkpoint before any model call.
            memo = MemoList()
            refused = f"The checkpoint could not be prepared: the host failed: {type(failure).__name__}: {failure}"
        lines = memo.lines()
        listed = "".join(f"{line}\n" for line in lines)  # memo.md as the turn starts
        prompt = checkpoint_request(
            lines,
            mind=prior,
            scratch=os.path.abspath(self.context.scratch_dir) if self.context and self.context.scratch_dir else None,
            window=window,
        )

        def check(message: AssistantMessage, files: Files, final: bool) -> Optional[str]:
            problem = checkpoint_block(checkpoint_text(message), plan.window)[1]
            found = [problem] if problem is not None else []
            if final or files is None:
                # With no repair left, only a reply without a checkpoint fails the turn; the files fall back (fold).
                return found[0] if found else None
            found += review_mind(_mind_text(files), start, window).problems()
            found += memo_problems(memo, files.get(MEMO_FILE), window)
            return "; ".join(found) or None

        async def fold(reply: SideReply) -> Folded:
            # The side turn folds only a reply its check accepted: the block's text is the now.
            checkpoint = checkpoint_block(checkpoint_text(reply.message), plan.window)[0]
            origin = reply.message.origin
            summarizer = {
                "origin": {"provider": origin.provider, "api": origin.api, "model": origin.model},
                "prompt_version": PROMPT_VERSION,
                "rounds": reply.rounds,
            }
            left = reply.files
            try:
                hosted = await self._hosted(view, cut)
                mind, shown = await self._settled_mind(left, start, prior, window)
                skills = await self._carried_skills(view, cut, plan.window)
            except (_Aborted, asyncio.CancelledError):
                raise
            except Exception as failure:
                # Only the host's part after the model answered (state, lookup, skills, the mind's copy) is a failed
                # checkpoint, counted; an error building the row itself ends the run, like any engine error.
                return Folded(
                    None, f"The checkpoint row could not be built: the host failed: {type(failure).__name__}: {failure}"
                )
            # A memo.md that cannot be read keeps every item: more of the user's words, never fewer (section 10). So
            # does one the turn left as written after its tool budget closed: it may never have had room to check it.
            untouched = left is not None and left.get(MEMO_FILE) == listed
            checked = None if left is None or (reply.closed and untouched) else memo.read(left.get(MEMO_FILE))
            payload, _ = await self._compaction(
                request,
                view,
                plan,
                cut,
                mode=mode,
                reason=reason,
                checkpoint=checkpoint,
                summarizer=summarizer,
                usage=reply.usage,
                hosted=hosted,
                mind=mind,
                mind_shown=shown,
                memo=fit_memo(memo.select(checked), memo_budget(window)),
                skills=skills,
            )
            (row,) = await self._commit_context([("compaction", payload)])
            return Folded(row)

        return SideTurn(
            purpose="checkpoint",
            detail=CheckpointDetail(reason=reason, mode=mode),
            units=None if mode == "normal" else cut,
            prompt=prompt,
            policy=DREAMING,
            max_tokens=lambda capabilities: checkpoint_max_tokens(capabilities, self.max_tokens),
            fold=fold,
            check=check,
            repair=checkpoint_repair,
            # Each file ends with a newline, as a text file does: an oldText written that way still matches.
            files={MIND_FILE: f"{start}\n", MEMO_FILE: listed},
            refused=refused,
        )

    def _memo_bounds(self, view: ContextView) -> tuple[int, set[int], int]:
        """What a memo may offer (section 7): inputs after ``boundary`` or among ``released`` (the inputs the latest
        checkpoint kept as they were), and the latest checkpoint's ``summarized_to_seq``."""
        row = view.compaction
        previous = row.payload if row is not None else {}
        summarized = previous.get("summarized_to_seq", 0) if row is not None else 0
        boundary = 0 if row is not None and "memo" not in previous else summarized
        return boundary, set(previous.get("kept_inputs", ())), summarized

    async def _prefetch_typed(self, view: ContextView) -> None:
        """Ask the host, off the event loop, which inputs any cut of ``view`` could move out a person typed; a failure
        is kept for the memo to raise (``_typed_by``)."""
        host = self.context.host if self.context is not None else None
        if host is None:
            return
        boundary, released, _ = self._memo_bounds(view)
        last = view.units[-1].seq if view.units else 0
        missing = [
            entry.row_id
            for entry in self._rows
            if entry.kind == "input"
            and (boundary < entry.context_seq <= last or entry.context_seq in released)
            and entry.row_id not in self._typed
        ]
        if not missing:
            return
        try:
            found = await self._scope.call(lambda: asyncio.to_thread(host.user_messages, missing))
        except (_Aborted, asyncio.CancelledError):
            raise
        except Exception as failure:
            self._typed_failure = failure
            return
        self._typed_failure = None
        for row_id in missing:
            value = found.get(row_id)
            self._typed[row_id] = value if isinstance(value, str) else None

    def _memo_list(self, view: ContextView, cut: int) -> MemoList:
        """memo.md for a checkpoint at ``cut`` (C-9 section 7): what the previous checkpoint's memo carries, checked,
        then every input a person typed that this cut takes out of the context and no memo offered before,
        unchecked. The in-flight Turn's inputs the cut keeps are not offered: they stay as they were. A
        ``checkpoint-v4`` row has no memo, so the inputs it summarized are offered too, previewed longer: the
        conversation the turn reads no longer shows them."""
        previous = view.compaction.payload if view.compaction is not None else {}
        carried = tuple(MemoItem(**item) for item in previous.get("memo", ()))
        boundary, released, summarized = self._memo_bounds(view)
        through = summarized_to_seq(view, cut)
        kept = {unit.seq for unit in view.units[:cut] if unit.lead.kind == "input" and unit.seq in self._turn_inputs}
        known = {item.seq for item in carried}
        offered = [
            entry
            for entry in self._rows
            if entry.kind == "input"
            and (boundary < entry.context_seq <= through or entry.context_seq in released)
            and entry.context_seq not in kept
            and entry.context_seq not in known
        ]
        typed = self._typed_by(offered)
        new = tuple(memo_item(entry.context_seq, typed[entry.row_id]) for entry in offered if entry.row_id in typed)
        hidden = frozenset(item.seq for item in new if item.seq <= summarized and item.seq not in released)
        return MemoList(tuple(sorted((*carried, *new), key=lambda item: item.seq)), frozenset(known), hidden)

    def _typed_by(self, entries: Sequence[ContextEntry]) -> dict[str, str]:
        """What a person typed for each of ``entries`` that is a person's message, by row id, from the host's answers
        the stage prefetched (``_prefetch_typed``); its failure is raised here."""
        host = self.context.host if self.context is not None else None
        if host is None:
            return {}
        if any(entry.row_id not in self._typed for entry in entries):
            if self._typed_failure is not None:
                raise self._typed_failure
            raise RuntimeError("the memo asked for inputs the stage did not prefetch")
        return {entry.row_id: typed for entry in entries if (typed := self._typed.get(entry.row_id)) is not None}

    async def _settled_mind(self, files: Files, start: str, prior: str, window: int) -> tuple[str, str]:
        """The mind a checkpoint keeps, and what it shows of it (section 10), from the mind.md the turn left: with
        its structure lost, or past ``MIND_REVERT``, the previous mind; still the template, none; past a section's
        limit, the whole kept and the section shown cut to it, pointing to a read-only copy of the whole. With no
        scratch root, the mind could not be revised: the previous one stays."""
        if files is None:
            return prior, await self._kept_mind(prior, window)
        left = _mind_text(files)
        review = review_mind(left, start, window)
        if left is None or review.structure is not None or review.tokens > mind_revert(window):
            return prior, await self._kept_mind(prior, window)
        if review.empty:
            return "", ""
        if not review.over:
            return left, left
        marker = await self._mind_copy(left)
        return left, fit_mind(left, marker, window)[0]

    async def _kept_mind(self, mind: str, window: int) -> str:
        """What a checkpoint shows of the previous mind it keeps; its copy is written when this route cuts it."""
        shown = self._mind_shown(mind, window)
        if shown != mind:
            await self._mind_copy(mind)
        return shown

    def _mind_shown(self, mind: str, window: int) -> str:
        """What a checkpoint shows of a kept mind on a route of ``window`` tokens: cut to its sections' limits there.
        A mind kept whole on a large route is cut here on a small one; its copy was written when it was first cut,
        or is written now (``_mind_copy`` is called by the fold; a drop only points)."""
        if not mind or not review_mind(mind, mind, window).over:
            return mind
        return fit_mind(mind, self._mind_marker(mind), window)[0]

    def _mind_path(self, mind: str) -> Optional[str]:
        """Where the read-only copy of ``mind`` is, named by the SHA-256 of its text; None without ``checkpoint_dir``."""
        directory = self.context.checkpoint_dir if self.context is not None else None
        if directory is None:
            return None
        digest = hashlib.sha256(mind.encode("utf-8", "surrogatepass")).hexdigest()[:16]
        return os.path.join(directory, f"mind-{digest}.md")

    def _mind_marker(self, mind: str) -> str:
        """The note a cut mind section ends with: where the read-only copy of the whole mind is (section 10)."""
        path = self._mind_path(mind)
        return MIND_CUT_UNSTORED if path is None else MIND_CUT.format(path=path)

    async def _mind_copy(self, mind: str) -> str:
        """Write the read-only copy of ``mind`` a cut section points to, once per content; its marker."""
        path = self._mind_path(mind)
        if path is None:
            return MIND_CUT_UNSTORED

        def write() -> None:
            if os.path.isfile(path):
                return  # the same content, by its name: it is published only whole
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            # Whole or not at all: a private temporary file, synced, then renamed onto the name.
            tmp = os.path.join(directory, f".mind-{secrets.token_hex(6)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o444)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(mind.encode("utf-8", "surrogatepass"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
            except BaseException:
                with suppress(OSError):
                    os.unlink(tmp)
                raise

        await self._scope.call(lambda: asyncio.to_thread(write), interruptible=False)
        return MIND_CUT.format(path=path)

    async def _carried_skills(self, view: ContextView, cut: int, window: int) -> tuple[CarriedSkill, ...]:
        """The skills a checkpoint at ``cut`` carries back (section 7), rendered by the host as they read now: every
        candidate in one call, off the event loop (resolving skills reads the filesystem)."""
        host = self.context.host if self.context is not None else None
        candidates = skill_candidates(view, cut)
        if host is None or not candidates:
            return ()
        names = [name for name, _ in candidates]
        rendered = await self._scope.call(lambda: asyncio.to_thread(host.skill_contents, self.session_id, names))
        return carry_skills(candidates, rendered.get, window)

    # --- C-10 side turn (agent-core-contracts/fork.md section 7) ----------------

    def side_turn_fits(self, turn: SideTurn, route: ModelSelection, *, system: str) -> bool:
        """A dry compose of the first request ``turn`` would send on ``route``, budgeted: a choice, not admission."""
        view = context_view(self._rows)
        return self._side_request(system, route, turn, view, (), last_anchor(self._rows, view))[2].can_fit

    async def side_turn(self, turn: SideTurn, route: ModelSelection, *, system: str) -> SideTurnResult:
        """``turn`` inside the caller's run, first on ``route``, with the run's ``system`` (section 7).

        It goes through the run's model call, silently, and its calls through ``_side_tool``. A response that stops
        with ``stop`` and calls no tool is its reply, which ``turn.fold`` folds into the context; anything else fails
        it. A reply ``turn.check`` rejects gets one corrective follow-up, ``turn.repair``, in this turn; a second
        rejected reply fails it. One ``fork_turn`` audit on every exit but an abort (F7). v1 runs it only while a run
        is active, the Session's one writer; a side turn while idle is reserved.
        """
        view = context_view(self._rows)
        anchor = last_anchor(self._rows, view)
        point = {"as_of": view.context_seq, "units": turn.units}
        policy = turn.policy
        scratch_dir = self.context.scratch_dir if self.context is not None else None
        gate = PolicyGate(policy, cwd=self.cwd, scratch_dir=scratch_dir)
        # The most a call that does not run adds: one of the policy's two fixed texts (never cut).
        fixed = max(text_tokens(policy.denied), text_tokens(policy.budget_used))
        so_far: list[Message] = []  # what the turn's next request carries after its prompt
        produced: list[Message] = []  # the audit: every attempt's message from the ledger, and the tool results
        start = mark = len(self._attempts)
        measured: list[Budget] = []  # the budget of the turn's latest request, as sent
        rounds = 0
        error: Optional[str] = None
        overflow = False
        reply: Optional[SideReply] = None
        repaired: Optional[str] = None  # the problem the one corrective follow-up named
        closed = False  # a call the turn made got the used-up budget's text instead of running
        selection: Optional[ModelSelection] = route

        async def compose(selected: ModelSelection) -> tuple[ModelRequest, dict[str, Tool]]:
            request, tools, plan = self._side_request(system, selected, turn, view, so_far, anchor)
            if not plan.can_fit:
                raise _SideTurnTooLarge()
            measured[:] = [plan]
            return request, tools

        finished: dict[str, Any] = {"outcome": "failed", "error": None, "folded_event_id": None}
        aborted = False
        seeded = False

        async def files() -> Files:
            if not seeded:
                return None
            # Off the event loop, joined like any scratch call: the root closes only after it is done.
            return await self._scope.call(lambda: to_thread_joined(gate.read, list(turn.files)), interruptible=False)

        try:
            try:
                if turn.files and turn.refused is None:
                    # The purpose's files, written before the first call; read back whenever a reply is checked
                    # (C-9 section 6). Their writes are fsynced: never on the event loop.
                    seeded = await self._scope.call(
                        lambda: to_thread_joined(gate.seed, turn.files), interruptible=False
                    )
                while turn.refused is None:
                    try:
                        async with self._model(
                            system, _silent, selection, compose=compose, purpose="side_turn", after=(turn.prompt, *so_far)
                        ) as (terminal, tools):
                            selection = None
                    except _SideTurnTooLarge:
                        overflow = True
                        error = f"overflow: the {turn.purpose} request does not fit the model's input limit."
                        break
                    except ProviderProtocolViolation as violation:
                        # Not admitted: a failed side turn, and none of its calls runs.
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
                    so_far.append(message)
                    if message.stop_reason == "stop" and not message.tool_calls:
                        left = await files()
                        final = repaired is not None or turn.repair is None
                        problem = turn.check(message, left, final) if turn.check is not None else None
                        if problem is None:
                            reply = SideReply(message, rounds, self._turn_usage(start), left, closed)
                            break
                        if repaired is not None or turn.repair is None:
                            # Its text stays in the audit only; the caller's context is untouched.
                            error = f"The {turn.purpose} turn's reply is not a {turn.purpose}: {problem}."
                            break
                        # One corrective follow-up, inside this turn's own budget, policy, and admission.
                        repaired = problem
                        finished["repair"] = problem
                        follow_up = turn.repair(problem)
                        so_far.append(follow_up)
                        produced.append(follow_up)
                        continue
                    if message.stop_reason != "tool_use" or not message.tool_calls:
                        # Only a stop with no call is a reply, and only a tool-use stop carries calls. The provider's
                        # word on why it stopped (a refusal's category, say) is the audit's to keep.
                        why = f" ({message.error_message})" if message.error_message else ""
                        error = (
                            f"The {turn.purpose} turn stopped with {message.stop_reason}{why}"
                            f" and no usable {turn.purpose}."
                        )
                        break
                    if not gate.open:
                        # Its earlier calls were already answered with the policy's budget text.
                        error = f"The {turn.purpose} turn kept calling tools after its tool budget was used up."
                        break
                    rounds += 1
                    sent = measured[0]
                    grown = sent.est + message_tokens(message)
                    calls = message.tool_calls
                    for index, call in enumerate(calls):
                        self._scope.check()
                        # The bound is the window of the turn's route: a tool runs only while the next request can
                        # still grow by the floor, after the fixed-text results every later call of the batch may
                        # need are reserved (C-9 section 6).
                        reserved = (len(calls) - index - 1) * fixed
                        room = sent.input_limit - grown - sent.output - reserved
                        # A scratch write or edit answers with one short line: it runs above its own floor, uncut.
                        scratch = policy.scratch_floor > 0 and gate.scratch_call(call)
                        floor = policy.scratch_floor if scratch else policy.room_floor
                        gate.open = gate.open and rounds <= policy.max_rounds and room >= floor
                        closed = closed or not gate.open
                        limit = room if scratch else room - policy.room_slack
                        result = await self._side_tool(call, tools, gate, limit=limit)
                        so_far.append(result)
                        produced.append(result)
                        grown += message_tokens(result)
            finally:
                gate.close()  # the scratch root's descriptor lives for this turn only
            if turn.refused is not None:
                error = turn.refused
            folded = None
            if reply is not None:
                outcome = await turn.fold(reply)
                folded, error = outcome.row, outcome.error
            if reply is None or error is not None:
                finished["error"] = error
                return SideTurnResult("failed", None, error, overflow)
            finished.update(outcome="completed", folded_event_id=folded.row_id if folded is not None else None)
            return SideTurnResult("completed", folded, None)
        except (_Aborted, asyncio.CancelledError):
            aborted = True  # the cancelled run scope admits no further store write
            raise
        except BaseException as failure:
            if finished["outcome"] != "completed" and finished["error"] is None:
                finished["error"] = f"{type(failure).__name__}: {failure}"  # a failed fold, or a failed compose
            raise
        finally:
            if not aborted:
                await self._audit("fork_turn", self._side_record(turn, point, rounds, produced, start, finished))

    def _side_request(
        self,
        system: str,
        route: ModelSelection,
        turn: SideTurn,
        view: ContextView,
        so_far: Sequence[Message],
        anchor: Optional[Anchor],
    ) -> tuple[ModelRequest, dict[str, Tool], Budget]:
        """A side turn's request on ``route`` and its budget (section 7 step 1): after the caller's rehydrated
        messages, its view through the turn's units, the turn's prompt, and the turn so far. The dry run and the
        turn itself compose it here, so they cannot differ."""
        base = view.prefix(len(view.units) if turn.units is None else turn.units)
        transcript = (*base, turn.prompt, *so_far)
        request, tools = self._built(
            system, route, messages=(*self._rehydrated(), *transcript), max_tokens=turn.max_tokens(route.capabilities)
        )
        return request, tools, budget(request, route.capabilities, transcript=transcript, anchor=anchor)

    def _turn_usage(self, start: int) -> Optional[Usage]:
        """The usage every attempt of a side turn reported, from the ledger."""
        usage = None
        for item in self._attempts[start:]:
            usage = add_usage(usage, item.message.usage if item.message is not None else None)
        return usage

    def _side_record(
        self,
        turn: SideTurn,
        point: Mapping[str, Any],
        rounds: int,
        produced: Sequence[Message],
        start: int,
        finished: Mapping[str, Any],
    ) -> dict[str, Any]:
        """A side turn's audit (``ForkTurn``, section 15): its purpose, policy, point, detail, messages, usage, and
        outcome."""
        messages = []
        for message in produced:
            try:
                messages.append(message_to_dict(message))
            except (ValueError, RecursionError) as invalid:
                # Arguments a provider sent that are not JSON: refused at admission (the turn's error names
                # them), and no JSON row can hold them either.
                self._outcome.diagnostic(type(invalid).__name__, f"Side turn audit left out a message: {invalid}")
        record: dict[str, Any] = {
            "version": 1,
            "purpose": turn.purpose,
            "policy": turn.policy.name,
            "point": dict(point),
            "detail": asdict(turn.detail),
            "rounds": rounds,
            "messages": messages,
        }
        usage = self._turn_usage(start)
        if usage is not None:
            record["usage"] = usage_to_dict(usage)
        return {**record, **finished}

    async def _side_tool(
        self, original: ToolCallBlock, tools: Mapping[str, Tool], gate: PolicyGate, *, limit: int
    ) -> ToolResultMessage:
        """The side turn's tool pipeline (section 7 step 3); nothing it produces is committed.

        The budget first, then the policy, then execution (a scratch write or edit through the root's
        descriptor), then the bound on whatever result came out. No user hook runs in a side turn (v1).
        """
        call = deepcopy(original)
        decision = gate.decide(call) if gate.open else Decision(denial=gate.policy.budget_used)
        if decision.denial is not None:
            # The policy's fixed texts are short and never cut: a truncation note would invite another call.
            return ToolResultMessage(call.id, call.name, (text(decision.denial),), True)
        if decision.scratch is not None:
            # Relative to the scratch root's descriptor: no pathname is resolved again. Joined, never interrupted:
            # the worker thread outlives a cancelled await, and the root closes only after it is done (C-9 section 6).
            result = await self._scope.call(
                lambda: to_thread_joined(gate.run, call, decision.scratch), interruptible=False
            )
        elif (tool := tools.get(call.name)) is None:
            result = ToolResult((text(f"Tool {call.name} is not available."),), is_error=True)
        else:
            # A side turn's call has no committed response, so no instance: it starts no job (F4).
            result = await self._execute(tool, call, None, _silent)
        # Every other result that enters the turn is bounded to the room the window leaves (C-9 section 6).
        return ToolResultMessage(
            call.id, call.name, fit_result(result.content, limit), result.is_error, hosted=result.hosted
        )

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
        try:
            dropped = await self._dropped(view, cut, carry=carry, window=plan.window)
            if dropped.get("mind_shown", "") != dropped.get("mind", ""):
                await self._mind_copy(dropped["mind"])  # this route cuts the mind the drop carries: its copy must exist
        except (_Aborted, asyncio.CancelledError):
            raise
        except Exception:
            if not carry:
                raise
            # What it would carry cannot be gathered (the host, the mind's copy): the drop must still free the room.
            logger.warning("Cannot gather what a drop would carry; dropping without it", exc_info=True)
            dropped = await self._dropped(view, cut, carry=False, window=plan.window)
        payload, _ = await self._compaction(request, view, plan, cut, **dropped, hosted=hosted)
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
        mind: str = "",
        mind_shown: Optional[str] = None,
        memo: Sequence[MemoItem] = (),
        skills: Sequence[CarriedSkill] = (),
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
            mind=mind,
            mind_shown=mind_shown,
            memo=memo,
            skills=skills,
        )
        seq = max((row.context_seq for row in self._rows), default=0) + 1
        candidate = ContextEntry(self.session_id, seq, "compaction", "uncommitted-compaction", payload=payload)
        after = context_view([*self._rows, candidate])
        # What the next request would carry; it is budgeted for real when it is built.
        payload["tokens_after_estimate"] = request_tokens(
            request.system, request.tools, self._carried(after), native_hosted_tools(request)
        )
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

    async def _audit(self, kind: Literal["fork_turn", "attempt"], payload: Mapping[str, Any]) -> None:
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
