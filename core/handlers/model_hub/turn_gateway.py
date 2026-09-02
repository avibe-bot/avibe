"""Loopback HTTP gateway that applies Model Hub resolution to live turns."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import tempfile
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO, Final, Optional

from aiohttp import web

from config import paths
from vibe.i18n import t as i18n_t

from .adapter import (
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    InvokeHandle,
    RawCallOutcome,
    RawOutcomeKind,
)
from .async_owner import run_owned_in_thread
from .classification import ResolutionDecision
from .provenance import (
    BoundedProvenanceStore,
    ENGINE_DOWN_TURN_OUTCOME,
    GatewayTurnTerminalizer,
    REQUEST_NONFALLBACK_TURN_OUTCOME,
    REQUEST_UNROUTABLE_TURN_OUTCOME,
    TurnOutcomeProjectionInput,
    TurnCorrelationRegistry,
    project_turn_outcome_copy,
    render_turn_outcome_copy,
)
from .request import ModelHubRequest
from .stream_wire import (
    ProtocolSSEState,
    ProtocolUsageReport,
    StreamTerminalOutcome,
    render_protocol_terminal_event,
    render_protocol_terminal_frame,
)
from .tool_names import (
    StreamingToolNameRewriter,
    rewrite_buffered_tool_names_file,
    translate_opencode_tool_names,
)
from .usage import BoundedUsageLedger, UsageWriter
from .service import (
    HandleSettlement,
    HandleTerminationOrigin,
    ModelHubError,
    ModelHubService,
    ResolvedInvocation,
)


_MAX_REQUEST_BYTES: Final = 16 * 1024 * 1024
_BUFFERED_RESPONSE_MEMORY_BYTES: Final = 256 * 1024
_RESPONSE_CHUNK_BYTES: Final = 64 * 1024
_SUPPORTED_PATHS: Final = frozenset(
    {
        "messages",
        "responses",
        "chat/completions",
    }
)
_REQUEST_PROTOCOLS: Final = {
    "messages": "anthropic",
    "responses": "openai_responses",
    "chat/completions": "openai_chat",
}
logger = logging.getLogger(__name__)


def _gateway_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rewind_and_measure(payload: BinaryIO) -> int:
    payload.seek(0, 2)
    size = payload.tell()
    payload.seek(0)
    return size


_PROTOCOL_HEADERS: Final = frozenset(
    {
        "anthropic-beta",
        "anthropic-version",
        "openai-beta",
    }
)


@dataclass
class _TurnExecution:
    """Resources and settlement state owned by one gateway request boundary."""

    resolved: ResolvedInvocation | None = None
    # Set the moment this gateway takes the upstream body, which is also the
    # moment it takes the call's metering from the resolver. Assigned before
    # anything downstream can fail, so it outlives every ending after it.
    handle: InvokeHandle | None = None
    settlement_task: asyncio.Task[tuple[RawCallOutcome | None, HandleSettlement]] | None = None
    settlement_origin: HandleTerminationOrigin | None = None
    settlement_recorded: bool = False
    terminal_fact_committed: bool = False
    rendered_turn_outcome: _RenderedTurnOutcome | None = None
    # The live stream tracker, published here so a boundary that ends the turn
    # without reaching the end of the chunk loop can still read the tokens the
    # upstream had already reported.
    wire_state: ProtocolSSEState | None = None
    # A buffered response is already classified by the engine before its body is
    # handed to this gateway. Keep that adapter-owned result instead of parsing
    # the same bytes a second time at a weaker lifecycle boundary.
    handle_outcome: RawCallOutcome | None = None
    # When the upstream body ended, published here for the same reason the two
    # facts above are: it is the instant the call belongs to, and everything after
    # it is bookkeeping whose duration is not the call's. A settlement that hangs
    # for a minute, or across local midnight, would otherwise move the row to a
    # day the vendor never billed.
    completed_at: datetime | None = None
    # The writer-owned task that persists this turn's row, and by its existence
    # the record that this turn has already been metered. A flag would only say
    # the write was started by something whose own death could still take it.
    usage_write: asyncio.Future[None] | None = None
    # OpenCode owns its public tool names; the gateway may use collision-free
    # upstream aliases, but those names must never escape back to OpenCode.
    response_tool_aliases: Mapping[str, str] = field(default_factory=dict)

    @property
    def upstream_observation(self) -> ProtocolSSEState | None:
        """What the engine read of this body, from before it was ours to forward.

        Every other fact this turn holds is built from bytes this gateway has
        already pulled, and the pulling starts after the downstream response is
        prepared. The engine read the head of the same body to decide there was
        one and keeps reading as it yields, so its tracker answers in the window
        where ours does not exist yet and is never behind it afterwards.

        Which is what makes it the first thing both facts below ask. They used
        to ask the shapes this gateway builds, and each new ending that opened
        before those shapes existed cost a review round teaching one more fact
        to survive it.
        """

        return self.handle.observed if self.handle is not None else None

    @property
    def reported_usage(self) -> ProtocolUsageReport | None:
        """Tokens upstream reported for this turn, whichever shape it answered in.

        One owner for the question, because a boundary that ends the turn early
        cannot know which shape the response took — and a turn the vendor billed
        was billed in both.
        """

        upstream = self.upstream_observation
        if upstream is not None and upstream.usage is not None:
            return upstream.usage
        if self.wire_state is not None:
            return self.wire_state.usage
        if self.handle_outcome is not None:
            return self.handle_outcome.usage
        return None

    @property
    def reached_model(self) -> bool:
        """Whether this turn's upstream call was billed, whichever shape it took.

        Sibling of `reported_usage`, for the same reason and with the same owner:
        a boundary that ends the turn early cannot know which shape the response
        took. Leaving it to the endings meant each one answered in the vocabulary
        of the shape it happened to see — the boundary read a wire tracker a
        buffered turn never has, so a complete upstream body that reported no
        tokens counted as never having reached the model.

        The engine adapter classifies a buffered body before handing it over. A
        complete model answer is served there exactly as a stream that reached its
        terminal is; an error envelope reaches no model exactly as a stream that
        forwarded no output does. The gateway therefore retains that outcome
        instead of parsing the same bytes a second time.

        A streaming turn can still end before the gateway's wire tracker exists:
        the gateway adopts the body first, and only then prepares the downstream
        response and starts reading. The engine's live observation is asked first
        because it already exists in that window and stays at least as current as
        the gateway tracker afterwards.

        The adoption itself remains the floor, for a handle that tokenized no
        stream to hand over. It is an answer, not a guess: the engine hands this
        gateway a body only after the prelude observed the first model output, or
        for a call it had already completed upstream, and it keeps every other
        call to meter in the resolver. An adopted body is therefore a call the
        vendor billed, and `token_reports` staying at zero is how the ledger says
        nobody got to read its tokens.
        """

        upstream = self.upstream_observation
        if upstream is not None and upstream.reached_model:
            return True
        if self.wire_state is not None:
            return self.wire_state.reached_model
        if self.handle_outcome is not None:
            return (
                self.handle_outcome.kind is RawOutcomeKind.SUCCESS
                or self.handle_outcome.stream_started
            )
        return self.handle is not None

    @property
    def owes_metering(self) -> bool:
        """Whether a row for this turn is still owed to the ledger.

        Asked by the recorder, and by the boundary deciding whether an ending has
        anything left to do. Two readers, one answer: a boundary that asked
        "settled?" instead skipped the ending for a turn that was settled by force
        after its upstream call had already reported tokens, and the vendor billed
        that call either way.
        """

        if self.usage_write is not None or self.resolved is None:
            return False
        return self.reached_model or self.reported_usage is not None


@dataclass(frozen=True)
class _RenderedTurnOutcome:
    key: str | None
    message: str | None


class _DownstreamDisconnected(ConnectionError):
    pass


class ModelHubTurnGateway:
    """Expose the controller-owned resolver to backend CLI HTTP clients."""

    def __init__(
        self,
        service: ModelHubService,
        *,
        correlation: Optional[TurnCorrelationRegistry] = None,
        usage: Optional[BoundedUsageLedger] = None,
        language_provider: Callable[[], str] | None = None,
        transport_timeout: float = ENGINE_TRANSPORT_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self._language_provider = language_provider or (lambda: "en")
        self._transport_timeout = transport_timeout
        # One clock per hub: the service already owns it, so a test that fixes the
        # service clock gets deterministic usage days without a second injection.
        self._now = now or getattr(service, "now", None) or _gateway_utc_now
        self._resource_leak_records: deque[tuple[str, str | None]] = deque(maxlen=100)
        self.correlation = correlation or TurnCorrelationRegistry(
            getattr(
                service,
                "provenance",
                BoundedProvenanceStore(paths.get_state_dir() / "model_hub_turn_provenance.json"),
            )
        )
        self.usage = (
            usage
            or getattr(service, "usage", None)
            or BoundedUsageLedger(
                paths.get_state_dir() / "model_hub_usage.json",
                now=self._now,
            )
        )
        # The service's writer whenever both write the same ledger, so one drain
        # covers both metering populations rather than one per population.
        service_writer = getattr(service, "usage_writer", None)
        self._usage_writer = (
            service_writer
            if isinstance(service_writer, UsageWriter) and service_writer.ledger is self.usage
            else UsageWriter(self.usage)
        )
        self._start_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._base_url: str | None = None

    @property
    def resource_leak_records(self) -> tuple[tuple[str, str | None], ...]:
        return tuple(self._resource_leak_records)

    def _abandon_owned_task(
        self,
        task: asyncio.Task,
        *,
        phase: str,
        terminalizer: GatewayTurnTerminalizer,
        execution: _TurnExecution,
    ) -> HandleSettlement | None:
        task.cancel()
        if phase == "settlement" and execution.settlement_task is not None:
            execution.settlement_task.cancel()
        self._resource_leak_records.append((phase, terminalizer.turn_id))
        logger.error(
            "Abandoned Model Hub turn resource after the engine transport deadline",
            extra={"phase": phase, "turn_id": terminalizer.turn_id},
        )
        if execution.settlement_recorded or execution.terminal_fact_committed:
            return None
        terminalizer.engine_down()
        execution.settlement_origin = "upstream_terminal"
        return HandleSettlement(
            outcome=None,
            decision=None,
            turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
        )

    async def endpoint(
        self,
        backend: str,
        *,
        process_scope: Optional[str] = None,
        turn_id: Optional[str] = None,
        requested_model_id: Optional[str] = None,
        resolved_model_id: Optional[str] = None,
        source_id: Optional[str] = None,
        via_mapping: bool = False,
    ) -> tuple[str, str]:
        if backend not in {"claude", "codex", "opencode"}:
            raise ModelHubError("mapping_target_unavailable", status=409)
        scope = str(process_scope or "").strip() or f"{backend}:untracked"
        token = self.correlation.credentials(backend, scope, turn_id)
        if requested_model_id and resolved_model_id and source_id:
            self.correlation.prepare_gateway_turn(
                backend=backend,
                token=token,
                turn_id=turn_id,
                requested_model_id=requested_model_id,
                resolved_model_id=resolved_model_id,
                source_id=source_id,
                via_mapping=via_mapping,
            )
        await self._ensure_started()
        assert self._base_url is not None
        return f"{self._base_url}/{backend}", token

    async def close(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        self._base_url = None
        if runner is not None:
            await runner.cleanup()
        # After the runner, so the handlers it cancels have queued their last
        # writes first. Bounded like every other owned drain: a ledger that
        # cannot be reached must not hold shutdown open.
        unfinished = await self._usage_writer.drain(timeout=self._transport_timeout)
        if unfinished:
            logger.warning(
                "Model Hub usage metering left %d write(s) unfinished at shutdown",
                unfinished,
            )

    async def _ensure_started(self) -> None:
        if self._runner is not None:
            return
        async with self._start_lock:
            if self._runner is not None:
                return
            app = web.Application(client_max_size=_MAX_REQUEST_BYTES)
            app.router.add_post("/{backend}/v1/{endpoint:.*}", self._handle_request)
            runner = web.AppRunner(
                app,
                access_log=None,
                handler_cancellation=True,
            )
            await runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            listener.setblocking(False)
            site = web.SockSite(runner, listener)
            try:
                await site.start()
            except Exception:
                listener.close()
                await runner.cleanup()
                raise
            port = int(listener.getsockname()[1])
            self._runner = runner
            self._site = site
            self._base_url = f"http://127.0.0.1:{port}"

    def _authorized_token(self, request: web.Request, backend: str) -> Optional[str]:
        authorization = request.headers.get("Authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        api_key = request.headers.get("x-api-key", "")
        for candidate in (bearer, api_key):
            if candidate and self.correlation.authenticates(backend, candidate):
                return candidate
        return None

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        backend = request.match_info["backend"]
        token = self._authorized_token(request, backend)
        if token is None:
            return self._error_response(status=401, code="authentication_error")
        with self.correlation.gateway_terminalizer(
            backend=backend,
            token=token,
        ) as terminalizer:
            execution = _TurnExecution()
            resources = AsyncExitStack()
            await resources.__aenter__()
            request_task = asyncio.create_task(
                self._run_request_turn(
                    request,
                    backend=backend,
                    terminalizer=terminalizer,
                    execution=execution,
                    resources=resources,
                )
            )
            exit_task: asyncio.Task[bool] | None = None
            try:
                try:
                    response = await asyncio.shield(request_task)
                    error: BaseException | None = None
                except Exception as caught:
                    response = None
                    error = caught
                exit_task = asyncio.create_task(
                    resources.__aexit__(
                        type(error) if error is not None else None,
                        error,
                        error.__traceback__ if error is not None else None,
                    )
                )
                exit_done, _pending = await asyncio.wait(
                    {exit_task},
                    timeout=self._transport_timeout,
                )
                if not exit_done:
                    timeout_settlement = self._abandon_owned_task(
                        exit_task,
                        phase="resource_teardown",
                        terminalizer=terminalizer,
                        execution=execution,
                    )
                    if timeout_settlement is not None:
                        self._commit_and_render_handle_settlement(
                            execution,
                            terminalizer,
                            timeout_settlement,
                        )
                else:
                    exit_task.result()
                if isinstance(error, _DownstreamDisconnected):
                    await self._settle_boundary_termination(
                        execution,
                        terminalizer,
                        termination_origin="downstream_cancel",
                    )
                    raise error
                if error is not None:
                    await self._settle_boundary_termination(
                        execution,
                        terminalizer,
                        termination_origin="upstream_terminal",
                    )
                    raise error
                assert response is not None
                return response
            except asyncio.CancelledError as cancelled:
                # Fact barrier: t0 resources -> t2 protocol terminal -> t3
                # close/finally -> t4 settlement/history -> t5 render -> t6 EOF.
                # Only the request is canceled. Owned teardown and settlement
                # are shielded and drained before this boundary re-raises.
                drain_deadline = asyncio.get_running_loop().time() + self._transport_timeout
                if not request_task.done():
                    request_task.cancel()
                while not request_task.done() and asyncio.get_running_loop().time() < drain_deadline:
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait(
                            {request_task},
                            timeout=max(
                                0.0,
                                drain_deadline - asyncio.get_running_loop().time(),
                            ),
                        )
                if request_task.done():
                    with suppress(BaseException):
                        request_task.result()
                else:
                    timeout_settlement = self._abandon_owned_task(
                        request_task,
                        phase="request",
                        terminalizer=terminalizer,
                        execution=execution,
                    )
                    if timeout_settlement is not None:
                        self._commit_and_render_handle_settlement(
                            execution,
                            terminalizer,
                            timeout_settlement,
                        )
                if exit_task is None:
                    exit_task = asyncio.create_task(
                        resources.__aexit__(
                            asyncio.CancelledError,
                            cancelled,
                            cancelled.__traceback__,
                        )
                    )
                while not exit_task.done() and asyncio.get_running_loop().time() < drain_deadline:
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait(
                            {exit_task},
                            timeout=max(
                                0.0,
                                drain_deadline - asyncio.get_running_loop().time(),
                            ),
                        )
                if exit_task.done():
                    with suppress(BaseException):
                        exit_task.result()
                else:
                    timeout_settlement = self._abandon_owned_task(
                        exit_task,
                        phase="resource_teardown",
                        terminalizer=terminalizer,
                        execution=execution,
                    )
                    if timeout_settlement is not None:
                        self._commit_and_render_handle_settlement(
                            execution,
                            terminalizer,
                            timeout_settlement,
                        )
                # Settle if unsettled, meter if unmetered — two debts, either one
                # worth the ending. Asking only about settlement dropped the row for
                # a call the upstream had already answered, because abandoning the
                # request above writes an engine-down terminal and that made the turn
                # settled. Asking about neither would be simpler still, but the
                # deadline is spent by the time we get here in exactly the case that
                # matters, and a task with no debt to discharge could not be drained
                # before it was abandoned — a resource leak recorded against a turn
                # that had nothing left to do.
                if not execution.settlement_recorded or execution.owes_metering:
                    settlement_task = asyncio.create_task(
                        self._settle_boundary_termination(
                            execution,
                            terminalizer,
                            termination_origin="downstream_cancel",
                        )
                    )
                    while not settlement_task.done() and asyncio.get_running_loop().time() < drain_deadline:
                        with suppress(asyncio.CancelledError):
                            await asyncio.wait(
                                {settlement_task},
                                timeout=max(
                                    0.0,
                                    drain_deadline - asyncio.get_running_loop().time(),
                                ),
                            )
                    if settlement_task.done():
                        settlement_task.result()
                    else:
                        timeout_settlement = self._abandon_owned_task(
                            settlement_task,
                            phase="settlement",
                            terminalizer=terminalizer,
                            execution=execution,
                        )
                        if timeout_settlement is not None:
                            self._commit_and_render_handle_settlement(
                                execution,
                                terminalizer,
                                timeout_settlement,
                            )
                raise cancelled

    async def _run_request_turn(
        self,
        request: web.Request,
        *,
        backend: str,
        terminalizer: GatewayTurnTerminalizer,
        execution: _TurnExecution,
        resources: AsyncExitStack,
    ) -> web.StreamResponse:
        endpoint = request.match_info["endpoint"].strip("/")
        if backend not in {"claude", "codex", "opencode"} or endpoint not in _SUPPORTED_PATHS:
            terminalizer.fail("protocol_error")
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=404,
                code="not_found_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            terminalizer.fail("invalid_parameter")
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=400,
                code="invalid_request_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        if not isinstance(payload, dict):
            terminalizer.fail("invalid_parameter")
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=400,
                code="invalid_request_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        model_id = payload.get("model")
        stream = payload.get("stream", False)
        if not isinstance(model_id, str) or not model_id or not isinstance(stream, bool):
            terminalizer.fail("invalid_parameter")
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=400,
                code="invalid_request_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        resolution = terminalizer.resolution(model_id)
        resolution_model = resolution.model_id
        if resolution_model is None:
            terminalizer.fail("protocol_error")
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=409,
                code="mapping_target_unavailable",
                # An ambiguous scope has the model configured, so telling the
                # user to reselect one is both wrong and no help; that case
                # keeps the request-scoped copy it has always rendered.
                turn_outcome=(
                    REQUEST_NONFALLBACK_TURN_OUTCOME
                    if resolution.ambiguous
                    else REQUEST_UNROUTABLE_TURN_OUTCOME
                ),
            )

        def observe_attempt(
            source_id: str,
            resolved_model_id: str,
            channel: str,
            via_mapping: bool,
            outcome: Optional[RawCallOutcome],
            decision,
        ) -> None:
            if outcome is None or decision is None:
                terminalizer.begin_attempt(
                    source_id=source_id,
                    resolved_model_id=resolved_model_id,
                    channel=channel,
                    via_mapping=via_mapping,
                )
                return
            terminalizer.finish_attempt(
                outcome=outcome,
                decision=decision,
            )

        try:
            protocol_headers = {
                name.lower(): value for name, value in request.headers.items() if name.lower() in _PROTOCOL_HEADERS
            }
            if backend == "opencode" and _REQUEST_PROTOCOLS[endpoint] == "openai_chat":
                translation = translate_opencode_tool_names(payload)
                payload = translation.request
                execution.response_tool_aliases = translation.response_aliases
            resolved = await self.service.resolve(
                backend=backend,
                model_id=resolution_model,
                request=ModelHubRequest(
                    payload,
                    protocol=_REQUEST_PROTOCOLS[endpoint],
                    headers=protocol_headers,
                ),
                stream=stream,
                supply_channel="hub",
                attempt_observer=observe_attempt,
            )
        except ModelHubError as exc:
            turn_outcome = exc.turn_outcome
            if turn_outcome is None and exc.code == "engine_down":
                turn_outcome = ENGINE_DOWN_TURN_OUTCOME
            if turn_outcome is not None and turn_outcome.discriminator == "engine_down":
                terminalizer.engine_down()
            elif turn_outcome is not None and turn_outcome.outcome == "no_candidate" and exc.supply_state is not None:
                terminalizer.mark_no_candidate(exc.supply_state, exc.blockers)
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=exc.status,
                code=exc.code,
                turn_outcome=turn_outcome,
            )

        execution.resolved = resolved
        if resolved.handle is not None and resolved.handle.stream is not None:
            execution.handle = resolved.handle
            resources.push_async_callback(resolved.handle.close_stream)
        return await self._resolved_response(
            request,
            resolved,
            protocol=_REQUEST_PROTOCOLS[endpoint],
            stream=stream,
            terminalizer=terminalizer,
            execution=execution,
        )

    async def _resolved_response(
        self,
        request: web.Request,
        resolved: ResolvedInvocation,
        *,
        protocol: str,
        stream: bool,
        terminalizer: GatewayTurnTerminalizer,
        execution: _TurnExecution,
    ) -> web.StreamResponse:
        if resolved.supply_channel != "hub":
            return self._error_response(status=409, code="mode_switch_blocked")
        if resolved.outcome is not None:
            # A call that reached the resolver's own hands has already been
            # metered there; its body never becomes this gateway's to forward.
            return self._outcome_response(resolved.outcome)
        handle = resolved.handle
        if handle is None or handle.stream is None:
            terminalizer.engine_down()
            return self._terminal_error_response(
                execution,
                terminalizer,
                status=502,
                code="engine_down",
                turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
            )

        if not stream:
            with tempfile.SpooledTemporaryFile(
                max_size=_BUFFERED_RESPONSE_MEMORY_BYTES
            ) as payload:
                async for chunk in handle.stream:
                    await run_owned_in_thread(payload.write, chunk)
                execution.completed_at = self._now()
                outcome, settlement, rendered = await self._settle_metered_turn(
                    execution,
                    terminalizer,
                    termination_origin="upstream_terminal",
                )
                assert outcome is not None
                assert settlement is not None
                assert settlement.decision is not None
                if settlement.decision.action != "return":
                    return self._outcome_response(
                        outcome,
                        error_code=settlement.decision.error_code,
                        status_override=settlement.decision.downstream_status,
                        rendered=rendered,
                    )
                await run_owned_in_thread(payload.seek, 0)
                rewritten_payload = await run_owned_in_thread(
                    rewrite_buffered_tool_names_file,
                    payload,
                    execution.response_tool_aliases,
                )
                response_payload = rewritten_payload or payload
                try:
                    response_size = await run_owned_in_thread(
                        _rewind_and_measure,
                        response_payload,
                    )
                    if response_size <= _BUFFERED_RESPONSE_MEMORY_BYTES:
                        body = await run_owned_in_thread(response_payload.read)
                        return web.Response(
                            status=200,
                            body=body,
                            content_type="application/json",
                            headers={
                                "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff",
                            },
                        )
                    response = web.StreamResponse(
                        status=200,
                        headers={
                            "Cache-Control": "no-store",
                            "Content-Length": str(response_size),
                            "Content-Type": "application/json",
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                    await self._downstream_io(response.prepare(request))
                    while chunk := await run_owned_in_thread(
                        response_payload.read,
                        _RESPONSE_CHUNK_BYTES,
                    ):
                        await self._downstream_io(response.write(chunk))
                    await self._downstream_io(response.write_eof())
                    return response
                finally:
                    if rewritten_payload is not None:
                        await run_owned_in_thread(rewritten_payload.close)

        response = web.StreamResponse(
            status=200,
            headers={
                "Cache-Control": "no-store",
                "Content-Type": "text/event-stream",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )
        await self._downstream_io(response.prepare(request))
        wire_state = ProtocolSSEState(protocol)
        execution.wire_state = wire_state
        tool_name_rewriter = StreamingToolNameRewriter(execution.response_tool_aliases)
        async for chunk in handle.stream:
            output_started_before = wire_state.model_output_started
            await wire_state.observe_async(chunk)
            if wire_state.model_output_started and not output_started_before:
                terminalizer.mark_stream_started()
            rewritten = tool_name_rewriter.feed(chunk)
            if rewritten:
                await self._downstream_io(response.write(rewritten))
        trailing = tool_name_rewriter.finish()
        if trailing:
            await self._downstream_io(response.write(trailing))
        execution.completed_at = self._now()
        _outcome, _settlement, rendered = await self._settle_metered_turn(
            execution,
            terminalizer,
            termination_origin="upstream_terminal",
        )
        await self._write_stream_terminal_copy(
            response,
            protocol,
            rendered,
            wire_state,
            forwarded_terminal=wire_state.terminal_outcome,
        )
        await self._downstream_io(response.write_eof())
        return response

    async def _record_usage(self, execution: _TurnExecution) -> None:
        """Fold one forwarded upstream call into the usage ledger, best-effort.

        A call counts when it reached the model: either the hub returned upstream
        output downstream, or upstream reported usage for it — a vendor that
        reported tokens billed us even when the stream ended in an error. Calls
        that never reached the model already have their own surface in the
        resolution feed and source health, so counting them here would duplicate
        that concept.

        This is the gateway's half of metering, and it covers exactly the calls
        whose body the gateway forwards. A call the resolver consumed itself was
        already metered by ``ModelHubService._meter_call``, so no call is counted
        twice and none is missed.

        Sole metering owner for the boundary, and it takes only the turn. Several
        endings can reach it for one forwarded call — a stream that finished and
        then failed to flush, a disconnect that raced the terminal chunk — so the
        first ending to describe it wins and every later one is a no-op. One call
        is one request, whatever killed it.

        The no-op is `execution.owes_metering` rather than a condition spelled
        here, because an ending that has to decide whether to run at all asks the
        same question. Answering it in two places is how the abandonment path came
        to skip a billed call: it asked whether the turn was *settled*, which a
        forced terminal had already made true.

        Which is why no ending is asked what the call did. Both facts are read from
        the turn itself, so an ending is only ever a *when*, never a *what*: a
        boundary that ends the turn early sees one shape's evidence and cannot
        answer for the other, and every ending that answered locally answered
        differently. A future ending cannot get it wrong, because it has nothing to
        get wrong.

        The ledger read-modify-write is file I/O, so it runs off the event loop;
        metering is a report, never a control input, and a ledger failure must not
        change the turn the caller sees.

        Which also means the turn cannot own the write. `UsageWriter` holds it, and
        holds the waiting for it too, so an ending is a *when* in one more sense: it
        decides when a call is metered and never whether the metering survives, nor
        how long the turn waits on a disk. Nothing suspends between reading the
        facts and handing the write over, so a cancellation lands either before this
        turn was ever going to be metered or after the write is already someone
        else's to finish.

        Every ending calls this before it settles, and the completion instant is
        read from the turn rather than here. Both say the same thing: a metered call
        is the upstream call, and settlement, history, and rendering are bookkeeping
        that happens to follow it. Reading the clock here dated the row by when the
        bookkeeping got around to it — across local midnight, the wrong day — and
        settling first meant an ending that raised on the way through never reached
        this at all, for a call the vendor had already billed.
        """

        if not execution.owes_metering:
            return
        resolved = execution.resolved
        assert resolved is not None
        usage = execution.reported_usage
        execution.usage_write = self._usage_writer.record(
            source_id=resolved.source_id,
            model_id=resolved.model_id,
            usage=usage,
            at=execution.completed_at or self._now(),
        )
        await self._usage_writer.wait_recorded(execution.usage_write)

    async def _write_stream_terminal_copy(
        self,
        response: web.StreamResponse,
        protocol: str,
        rendered: _RenderedTurnOutcome | None,
        wire_state: ProtocolSSEState,
        *,
        forwarded_terminal: StreamTerminalOutcome | None,
    ) -> None:
        if rendered is None or forwarded_terminal is not None:
            return
        if rendered.key is None or rendered.message is None:
            return
        frame_prefix = wire_state.invalidate_partial_frame()
        payload = json.dumps(
            render_protocol_terminal_event(
                protocol,
                rendered.key,
                rendered.message,
                wire_state.next_sequence_number,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await self._downstream_io(response.write(frame_prefix + render_protocol_terminal_frame(protocol, payload)))

    @staticmethod
    async def _downstream_io(operation):
        try:
            return await operation
        except (ConnectionResetError, BrokenPipeError) as exc:
            raise _DownstreamDisconnected(str(exc)) from exc

    async def _settle_metered_turn(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> tuple[RawCallOutcome | None, HandleSettlement | None, _RenderedTurnOutcome | None]:
        """End one turn: meter the call, then settle it, then commit what it was.

        One owner for the order, because the order is the whole property. Metering
        used to be each ending's own step, and every ending placed it after its
        bookkeeping — where a settlement that raised skipped it entirely, for a call
        the vendor had already billed. The settlement owner now captures the adapter
        outcome and records usage before service settlement can raise.

        Bookkeeping after metering is also why the recorder needs no ending to be
        careful. A settlement is a fact about a handle and the settlement owner
        rejects one without it, so a turn that failed before adopting a body has
        nothing to settle — and, having reached no model, nothing to meter either.
        The guard for that sits behind the recorder rather than in front of it,
        which is what makes "settled here" imply "metered here" for every ending
        there is and every ending there will be.

        Answers with all three facts so an ending can render its own response
        without settling a second time. An ending that arrives after the turn was
        already committed gets the committed rendering and no settlement, which is
        the same thing the projection choke would have handed it.
        """

        if execution.settlement_recorded:
            return None, None, execution.rendered_turn_outcome
        if termination_origin == "upstream_terminal" and execution.handle is None:
            return None, None, execution.rendered_turn_outcome
        outcome, settlement = await self._settle_turn_handle(
            execution,
            terminalizer,
            termination_origin=termination_origin,
        )
        rendered = self._commit_and_render_handle_settlement(
            execution,
            terminalizer,
            settlement,
        )
        return outcome, settlement, rendered

    async def _settle_boundary_termination(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> None:
        """End a turn the boundary is ending, rather than the request path.

        A downstream disconnect can arrive after the upstream already answered, so
        the turn was billed even though the request path never reached its own
        ending. Which is the whole of what a boundary ending adds over that ending:
        it is a *when*, and the turn already holds every *what*.
        """

        await self._settle_metered_turn(
            execution,
            terminalizer,
            termination_origin=termination_origin,
        )
        if execution.settlement_origin == "downstream_cancel":
            terminalizer.mark_downstream_canceled()

    def _commit_and_render_handle_settlement(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        settlement: HandleSettlement,
    ) -> _RenderedTurnOutcome | None:
        """Commit and render a handle settlement at the sole projection choke."""

        if execution.settlement_recorded:
            return execution.rendered_turn_outcome
        rendered = self._commit_and_render_turn_outcome(
            execution,
            terminalizer,
            settlement.turn_outcome,
            fallback_code=(settlement.decision.error_code if settlement.decision is not None else None),
        )
        return rendered

    def _commit_and_render_turn_outcome(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        turn_outcome: TurnOutcomeProjectionInput | None,
        *,
        fallback_code: str | None = None,
    ) -> _RenderedTurnOutcome:
        """Commit terminal history before deriving any user-visible copy."""

        if execution.settlement_recorded:
            return execution.rendered_turn_outcome or _RenderedTurnOutcome(None, None)
        if turn_outcome is not None:
            terminalizer.record_turn_outcome(turn_outcome)
        language = self._language_provider() or "en"
        copy = project_turn_outcome_copy(turn_outcome) if turn_outcome is not None else None
        message = render_turn_outcome_copy(turn_outcome, language) if turn_outcome is not None else None
        key = copy.key if copy is not None else None
        if message is None and fallback_code is not None:
            fallback_key = f"modelHub.errors.{fallback_code}"
            fallback_message = i18n_t(fallback_key, language)
            if fallback_message == fallback_key:
                fallback_key = "modelHub.errors.upstream_error"
                fallback_message = i18n_t(fallback_key, language)
            key = fallback_key
            message = fallback_message
        rendered = _RenderedTurnOutcome(key, message)
        execution.rendered_turn_outcome = rendered
        execution.settlement_recorded = True
        return rendered

    def _terminal_error_response(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        *,
        status: int,
        code: str,
        turn_outcome: TurnOutcomeProjectionInput | None,
    ) -> web.Response:
        rendered = self._commit_and_render_turn_outcome(
            execution,
            terminalizer,
            turn_outcome,
            fallback_code=code,
        )
        return self._error_response(status=status, code=code, rendered=rendered)

    async def _settle_turn_handle(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> tuple[RawCallOutcome | None, HandleSettlement]:
        if execution.settlement_task is None:
            execution.settlement_origin = termination_origin
            execution.settlement_task = asyncio.create_task(
                self._settle_consumed_handle(
                    execution,
                    execution.resolved,
                    execution.handle,
                    terminalizer,
                    termination_origin=termination_origin,
                )
            )
        return await asyncio.shield(execution.settlement_task)

    async def _settle_consumed_handle(
        self,
        execution: _TurnExecution,
        resolved: ResolvedInvocation | None,
        handle: InvokeHandle | None,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> tuple[RawCallOutcome | None, HandleSettlement]:
        """Route every handle terminal through the service settlement owner."""

        # Fact barrier: t0 resource acquisition -> t1 handle -> t2 protocol
        # terminal observation -> t3 close/finally commits outcome -> t4
        # settlement/history -> t5 render settlement -> t6 downstream EOF.
        if handle is not None:
            await handle.close_stream()
        if termination_origin == "downstream_cancel" and handle is not None and handle.outcome_available:
            # A producer outcome that became available during close owns history;
            # a downstream write/cancel after that barrier cannot rewrite it.
            termination_origin = "upstream_terminal"
            execution.settlement_origin = termination_origin
        outcome = await handle.outcome() if handle is not None and handle.outcome_available else None
        execution.handle_outcome = outcome
        await self._record_usage(execution)

        def record_attempt(
            terminal_outcome: RawCallOutcome,
            decision: ResolutionDecision,
        ) -> None:
            terminalizer.finish_attempt(
                outcome=terminal_outcome,
                decision=decision,
            )
            execution.terminal_fact_committed = True

        settlement = await self.service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin=termination_origin,
            record_attempt=record_attempt,
        )
        if termination_origin == "downstream_cancel":
            return settlement.outcome, settlement
        assert settlement.outcome is not None
        assert settlement.decision is not None
        return settlement.outcome, settlement

    def _outcome_response(
        self,
        outcome: RawCallOutcome,
        *,
        error_code: Optional[str] = None,
        status_override: int | None = None,
        rendered: _RenderedTurnOutcome | None = None,
    ) -> web.Response:
        if outcome.kind == RawOutcomeKind.SUCCESS:
            return web.Response(status=200, body=b"{}", content_type="application/json")
        status = status_override or (
            outcome.http_status if outcome.http_status and 400 <= outcome.http_status <= 599 else 502
        )
        return self._error_response(
            status=status,
            code=error_code or outcome.error_code or "api_error",
            rendered=rendered,
        )

    def _error_response(
        self,
        *,
        status: int,
        code: str,
        rendered: _RenderedTurnOutcome | None = None,
    ) -> web.Response:
        language = self._language_provider() or "en"
        message = rendered.message if rendered is not None else None
        message_key = f"modelHub.errors.{code}"
        if message is None:
            message = i18n_t(message_key, language)
        if message == message_key:
            message = i18n_t("modelHub.errors.upstream_error", language)
        return web.json_response(
            {
                "error": {
                    "type": code,
                    "code": code,
                    "message": message,
                }
            },
            status=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
