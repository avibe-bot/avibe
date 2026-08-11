"""Loopback HTTP gateway that applies Model Hub resolution to live turns."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Final, Optional

from aiohttp import web

from config import paths
from vibe.i18n import t as i18n_t

from .adapter import InvokeHandle, RawCallOutcome, RawOutcomeKind
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
from .stream_wire import ProtocolSSEState, render_protocol_terminal_event
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


class _SSEWireState(ProtocolSSEState):
    """Gateway-local name for the shared protocol stream tracker."""


class _DownstreamDisconnected(ConnectionError):
    pass


class ModelHubTurnGateway:
    """Expose the controller-owned resolver to backend CLI HTTP clients."""

    def __init__(
        self,
        service: ModelHubService,
        *,
        correlation: Optional[TurnCorrelationRegistry] = None,
        language_provider: Callable[[], str] | None = None,
    ) -> None:
        self.service = service
        self._language_provider = language_provider or (lambda: "en")
        self.correlation = correlation or TurnCorrelationRegistry(
            getattr(
                service,
                "provenance",
                BoundedProvenanceStore(
                    paths.get_state_dir() / "model_hub_turn_provenance.json"
                ),
            )
        )
        self._start_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._base_url: str | None = None

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
            try:
                async with AsyncExitStack() as resources:
                    return await self._run_request_turn(
                        request,
                        backend=backend,
                        terminalizer=terminalizer,
                        execution=execution,
                        resources=resources,
                    )
            except asyncio.CancelledError:
                await self._settle_boundary_termination(
                    execution,
                    terminalizer,
                    termination_origin="downstream_cancel",
                )
                raise
            except _DownstreamDisconnected:
                await self._settle_boundary_termination(
                    execution,
                    terminalizer,
                    termination_origin="downstream_cancel",
                )
                raise
            except Exception:
                if execution.handle is not None:
                    _outcome, settlement = await self._settle_turn_handle(
                        execution,
                        terminalizer,
                        termination_origin="upstream_terminal",
                    )
                    terminalizer.record_turn_outcome(settlement.turn_outcome)
                raise

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
            return self._error_response(
                status=404,
                code="not_found_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            terminalizer.fail("invalid_parameter")
            return self._error_response(
                status=400,
                code="invalid_request_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        if not isinstance(payload, dict):
            terminalizer.fail("invalid_parameter")
            return self._error_response(
                status=400,
                code="invalid_request_error",
                turn_outcome=REQUEST_NONFALLBACK_TURN_OUTCOME,
            )
        model_id = payload.get("model")
        stream = payload.get("stream", False)
        if not isinstance(model_id, str) or not model_id or not isinstance(stream, bool):
            terminalizer.fail("invalid_parameter")
            return self._error_response(
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
                name.lower(): value
                for name, value in request.headers.items()
                if name.lower() in _PROTOCOL_HEADERS
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
            if (
                turn_outcome is not None
                and turn_outcome.discriminator == "engine_down"
            ):
                terminalizer.engine_down()
            elif (
                turn_outcome is not None
                and turn_outcome.outcome == "no_candidate"
                and exc.supply_state is not None
            ):
                terminalizer.mark_no_candidate(exc.supply_state, exc.blockers)
            return self._error_response(
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
            return self._outcome_response(resolved.outcome)
        handle = resolved.handle
        if handle is None or handle.stream is None:
            terminalizer.engine_down()
            return self._error_response(
                status=502,
                code="engine_down",
                turn_outcome=ENGINE_DOWN_TURN_OUTCOME,
            )

        if not stream:
            payload = bytearray()
            async for chunk in handle.stream:
                payload.extend(chunk)
            outcome, settlement = await self._settle_turn_handle(
                execution,
                terminalizer,
                termination_origin="upstream_terminal",
            )
            terminalizer.record_turn_outcome(settlement.turn_outcome)
            assert outcome is not None
            assert settlement.decision is not None
            if settlement.decision.action != "return":
                return self._outcome_response(
                    outcome,
                    error_code=settlement.decision.error_code,
                    status_override=settlement.decision.downstream_status,
                    turn_outcome=settlement.turn_outcome,
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
        async for chunk in handle.stream:
            terminalizer.mark_stream_started()
            await self._downstream_io(response.write(chunk))
            wire_state.observe(chunk)
        _outcome, settlement = await self._settle_turn_handle(
            execution,
            terminalizer,
            termination_origin="upstream_terminal",
        )
        terminalizer.record_turn_outcome(settlement.turn_outcome)
        await self._write_stream_terminal_copy(
            response,
            protocol,
            settlement.turn_outcome,
            wire_state,
        )
        await self._downstream_io(response.write_eof())
        return response

    async def _write_stream_terminal_copy(
        self,
        response: web.StreamResponse,
        protocol: str,
        turn_outcome: TurnOutcomeProjectionInput | None,
        wire_state: _SSEWireState,
    ) -> None:
        if turn_outcome is None:
            return
        copy = project_turn_outcome_copy(turn_outcome)
        message = render_turn_outcome_copy(turn_outcome, self._language_provider() or "en")
        if copy is None or message is None:
            return
        frame_prefix = wire_state.close_partial_frame()
        payload = json.dumps(
            render_protocol_terminal_event(
                protocol,
                copy.key,
                message,
                wire_state.next_sequence_number,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await self._downstream_io(
            response.write(
                frame_prefix
                + b"event: error\ndata: "
                + payload
                + b"\n\n"
            )
        )

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
        if (
            termination_origin == "downstream_cancel"
            and execution.handle is not None
            and execution.handle.outcome_available
        ):
            # The producer has already recorded the upstream history. A later
            # downstream write failure cannot rewrite that history as cancel.
            termination_origin = "upstream_terminal"
        _outcome, settlement = await self._settle_turn_handle(
            execution,
            terminalizer,
            termination_origin=termination_origin,
        )
        terminalizer.record_turn_outcome(settlement.turn_outcome)
        if execution.settlement_origin == "downstream_cancel":
            terminalizer.mark_downstream_canceled()

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
                    execution.resolved,
                    execution.handle,
                    terminalizer,
                    termination_origin=termination_origin,
                )
            )
        return await asyncio.shield(execution.settlement_task)

    async def _settle_consumed_handle(
        self,
        resolved: ResolvedInvocation | None,
        handle: InvokeHandle | None,
        terminalizer: GatewayTurnTerminalizer,
        *,
        termination_origin: HandleTerminationOrigin,
    ) -> tuple[RawCallOutcome | None, HandleSettlement]:
        """Route every handle terminal through the service settlement owner."""

        if handle is not None:
            await handle.close_stream()
        outcome = (
            await handle.outcome()
            if handle is not None and handle.outcome_available
            else None
        )
        settlement = await self.service.settle_handle_outcome(
            resolved,
            outcome,
            termination_origin=termination_origin,
            record_attempt=lambda terminal_outcome, decision: terminalizer.finish_attempt(
                outcome=terminal_outcome,
                decision=decision,
            ),
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
        turn_outcome: TurnOutcomeProjectionInput | None = None,
    ) -> web.Response:
        if outcome.kind == RawOutcomeKind.SUCCESS:
            return web.Response(status=200, body=b"{}", content_type="application/json")
        status = status_override or (
            outcome.http_status
            if outcome.http_status and 400 <= outcome.http_status <= 599
            else 502
        )
        return self._error_response(
            status=status,
            code=error_code or outcome.error_code or "api_error",
            turn_outcome=turn_outcome,
        )

    def _error_response(
        self,
        *,
        status: int,
        code: str,
        turn_outcome: TurnOutcomeProjectionInput | None = None,
    ) -> web.Response:
        language = self._language_provider() or "en"
        message = (
            render_turn_outcome_copy(turn_outcome, language)
            if turn_outcome is not None
            else None
        )
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
