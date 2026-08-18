"""Loopback HTTP gateway that applies Model Hub resolution to live turns."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections import deque
from collections.abc import Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Optional

from aiohttp import web

from config import paths
from vibe.i18n import t as i18n_t

from .adapter import (
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    InvokeHandle,
    RawCallOutcome,
    RawOutcomeKind,
)
from .classification import ResolutionDecision
from .provenance import (
    BoundedProvenanceStore,
    ENGINE_DOWN_TURN_OUTCOME,
    GatewayTurnTerminalizer,
    REQUEST_NONFALLBACK_TURN_OUTCOME,
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
    observe_protocol_response,
    render_protocol_terminal_event,
    render_protocol_terminal_frame,
)
from .usage import BoundedUsageLedger
from .service import (
    HandleSettlement,
    HandleTerminationOrigin,
    ModelHubError,
    ModelHubService,
    ResolvedInvocation,
)


_MAX_REQUEST_BYTES: Final = 16 * 1024 * 1024
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
    handle: InvokeHandle | None = None
    settlement_task: asyncio.Task[tuple[RawCallOutcome | None, HandleSettlement]] | None = None
    settlement_origin: HandleTerminationOrigin | None = None
    settlement_recorded: bool = False
    terminal_fact_committed: bool = False
    rendered_turn_outcome: _RenderedTurnOutcome | None = None
    # The live stream tracker, published here so a boundary that ends the turn
    # without reaching the end of the chunk loop can still read the tokens the
    # upstream had already reported.
    wire_state: _SSEWireState | None = None
    # The same two facts for a response the gateway buffered whole, which has no
    # wire tracker to read them from.
    buffered_usage: ProtocolUsageReport | None = None
    buffered_outcome: StreamTerminalOutcome | None = None
    usage_recorded: bool = False

    @property
    def reported_usage(self) -> ProtocolUsageReport | None:
        """Tokens upstream reported for this turn, whichever shape it answered in.

        One owner for the question, because a boundary that ends the turn early
        cannot know which shape the response took — and a turn the vendor billed
        was billed in both.
        """

        if self.wire_state is not None:
            return self.wire_state.usage
        return self.buffered_usage

    @property
    def reached_model(self) -> bool:
        """Whether this turn's upstream call was billed, whichever shape it took.

        Sibling of `reported_usage`, for the same reason and with the same owner:
        a boundary that ends the turn early cannot know which shape the response
        took. Leaving it to the endings meant each one answered in the vocabulary
        of the shape it happened to see — the boundary read a wire tracker a
        buffered turn never has, so a complete upstream body that reported no
        tokens counted as never having reached the model.

        The buffered half is the observation the gateway already made of the whole
        body: a complete model answer was served upstream exactly as a stream that
        reached its terminal was, and an error envelope reached no model exactly as
        a stream that forwarded no output did.
        """

        if self.wire_state is not None:
            return self.wire_state.reached_model
        return self.buffered_outcome == "served"


@dataclass(frozen=True)
class _RenderedTurnOutcome:
    key: str | None
    message: str | None


class _SSEWireState(ProtocolSSEState):
    """Gateway-local name for the shared protocol stream tracker."""

    @property
    def reached_model(self) -> bool:
        """Whether this stream got far enough upstream to have been billed.

        Forwarded model output proves the call reached the model even when the
        stream then ended without a recognized terminal — a connection lost after
        a text delta is a request that happened, and `token_reports` staying at
        zero is exactly how the ledger records that nobody reported its tokens.

        One owner for the question, because every ending of a stream has to
        answer it the same way.
        """

        return self.terminal_outcome == "served" or self.model_output_started


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
                    if execution.handle is not None and not execution.settlement_recorded:
                        _outcome, settlement = await self._settle_turn_handle(
                            execution,
                            terminalizer,
                            termination_origin="upstream_terminal",
                        )
                        self._commit_and_render_handle_settlement(
                            execution,
                            terminalizer,
                            settlement,
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
                if not execution.settlement_recorded:
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
        resolution_model = terminalizer.resolution_model(model_id)

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
            payload = bytearray()
            async for chunk in handle.stream:
                payload.extend(chunk)
            # Published before settling, not after: settling can be cancelled by a
            # downstream disconnect, and facts only this frame's local knew about
            # would leave the boundary with nothing to meter.
            observation = observe_protocol_response(
                protocol,
                streamed=False,
                data=bytes(payload),
            )
            execution.buffered_usage = observation.usage
            execution.buffered_outcome = observation.outcome
            outcome, settlement = await self._settle_turn_handle(
                execution,
                terminalizer,
                termination_origin="upstream_terminal",
            )
            rendered = self._commit_and_render_handle_settlement(
                execution,
                terminalizer,
                settlement,
            )
            assert outcome is not None
            assert settlement.decision is not None
            await self._record_usage(execution)
            if settlement.decision.action != "return":
                return self._outcome_response(
                    outcome,
                    error_code=settlement.decision.error_code,
                    status_override=settlement.decision.downstream_status,
                    rendered=rendered,
                )
            return web.Response(
                status=200,
                body=bytes(payload),
                content_type="application/json",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

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
        wire_state = _SSEWireState(protocol)
        execution.wire_state = wire_state
        async for chunk in handle.stream:
            output_started_before = wire_state.model_output_started
            wire_state.observe(chunk)
            if wire_state.model_output_started and not output_started_before:
                terminalizer.mark_stream_started()
            await self._downstream_io(response.write(chunk))
        _outcome, settlement = await self._settle_turn_handle(
            execution,
            terminalizer,
            termination_origin="upstream_terminal",
        )
        rendered = self._commit_and_render_handle_settlement(
            execution,
            terminalizer,
            settlement,
        )
        await self._record_usage(execution)
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

        Which is why no ending is asked what the call did. Both facts are read from
        the turn itself, so an ending is only ever a *when*, never a *what*: a
        boundary that ends the turn early sees one shape's evidence and cannot
        answer for the other, and every ending that answered locally answered
        differently. A future ending cannot get it wrong, because it has nothing to
        get wrong.

        The ledger read-modify-write is file I/O, so it runs off the event loop;
        metering is a report, never a control input, and a ledger failure must not
        change the turn the caller sees.
        """

        resolved = execution.resolved
        if execution.usage_recorded or resolved is None:
            return
        usage = execution.reported_usage
        if not execution.reached_model and usage is None:
            return
        execution.usage_recorded = True
        try:
            await asyncio.to_thread(
                self.usage.record,
                source_id=resolved.source_id,
                model_id=resolved.model_id,
                usage=usage,
                at=self._now(),
            )
        except (OSError, ValueError) as exc:
            logger.debug("Model Hub usage metering skipped one turn: %s", exc)

    async def _write_stream_terminal_copy(
        self,
        response: web.StreamResponse,
        protocol: str,
        rendered: _RenderedTurnOutcome | None,
        wire_state: _SSEWireState,
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

    async def _settle_boundary_termination(
        self,
        execution: _TurnExecution,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> None:
        # Ahead of the settlement guard: a downstream disconnect can arrive after
        # the upstream already answered, so the turn was billed even though the
        # request path never reached its own metering call. The recorder is
        # idempotent, so an ending that already metered costs nothing here.
        await self._record_usage(execution)
        if execution.settlement_recorded:
            return
        _outcome, settlement = await self._settle_turn_handle(
            execution,
            terminalizer,
            termination_origin=termination_origin,
        )
        self._commit_and_render_handle_settlement(
            execution,
            terminalizer,
            settlement,
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
