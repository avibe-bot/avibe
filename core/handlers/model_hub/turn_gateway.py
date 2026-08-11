"""Loopback HTTP gateway that applies Model Hub resolution to live turns."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from typing import Final, Optional

from aiohttp import web

from config import paths
from vibe.i18n import t as i18n_t

from .adapter import RawCallOutcome, RawOutcomeKind
from .classification import classify_outcome
from .provenance import (
    BoundedProvenanceStore,
    GatewayTurnTerminalizer,
    TurnCorrelationRegistry,
)
from .request import ModelHubRequest
from .service import ModelHubError, ModelHubService, ResolvedInvocation


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
            runner = web.AppRunner(app, access_log=None)
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
            endpoint = request.match_info["endpoint"].strip("/")
            if backend not in {"claude", "codex", "opencode"} or endpoint not in _SUPPORTED_PATHS:
                terminalizer.fail("protocol_error")
                return self._error_response(status=404, code="not_found_error")
            try:
                payload = await request.json(loads=json.loads)
            except (json.JSONDecodeError, UnicodeDecodeError):
                terminalizer.fail("invalid_parameter")
                return self._error_response(status=400, code="invalid_request_error")
            if not isinstance(payload, dict):
                terminalizer.fail("invalid_parameter")
                return self._error_response(status=400, code="invalid_request_error")
            model_id = payload.get("model")
            stream = payload.get("stream", False)
            if not isinstance(model_id, str) or not model_id or not isinstance(stream, bool):
                terminalizer.fail("invalid_parameter")
                return self._error_response(status=400, code="invalid_request_error")
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
                if exc.code == "engine_down":
                    terminalizer.engine_down()
                elif exc.supply_state is not None:
                    terminalizer.mark_no_candidate(exc.supply_state, exc.blockers)
                return self._error_response(status=exc.status, code=exc.code)
            return await self._resolved_response(
                request,
                resolved,
                stream=stream,
                terminalizer=terminalizer,
            )

    async def _resolved_response(
        self,
        request: web.Request,
        resolved: ResolvedInvocation,
        *,
        stream: bool,
        terminalizer: GatewayTurnTerminalizer,
    ) -> web.StreamResponse:
        if resolved.supply_channel != "hub":
            return self._error_response(status=409, code="mode_switch_blocked")
        if resolved.outcome is not None:
            return self._outcome_response(resolved.outcome)
        handle = resolved.handle
        if handle is None or handle.stream is None:
            terminalizer.engine_down()
            return self._error_response(status=502, code="engine_down")

        if not stream:
            payload = bytearray()
            async for chunk in handle.stream:
                payload.extend(chunk)
            outcome = await handle.outcome()
            decision = classify_outcome(outcome)
            terminalizer.finish_attempt(
                outcome=outcome,
                decision=decision,
            )
            if decision.action != "return":
                return self._outcome_response(
                    outcome,
                    error_code=decision.error_code,
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
        await response.prepare(request)
        try:
            async for chunk in handle.stream:
                terminalizer.mark_stream_started()
                await response.write(chunk)
        finally:
            outcome = await handle.outcome()
            terminalizer.finish_attempt(
                outcome=outcome,
                decision=classify_outcome(outcome),
            )
        await response.write_eof()
        return response

    def _outcome_response(
        self,
        outcome: RawCallOutcome,
        *,
        error_code: Optional[str] = None,
    ) -> web.Response:
        if outcome.kind == RawOutcomeKind.SUCCESS:
            return web.Response(status=200, body=b"{}", content_type="application/json")
        status = outcome.http_status if outcome.http_status and 400 <= outcome.http_status <= 599 else 502
        return self._error_response(
            status=status,
            code=error_code or outcome.error_code or "api_error",
        )

    def _error_response(self, *, status: int, code: str) -> web.Response:
        message_key = f"modelHub.launch.{code}"
        message = i18n_t(message_key, self._language_provider() or "en")
        if message == message_key:
            message = "Model Hub request failed"
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
