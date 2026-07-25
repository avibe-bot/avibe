"""Loopback HTTP gateway that applies Model Hub resolution to live turns."""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from typing import Final

from aiohttp import web

from .adapter import RawCallOutcome, RawOutcomeKind
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

    def __init__(self, service: ModelHubService) -> None:
        self.service = service
        self._tokens = {
            backend: secrets.token_urlsafe(32)
            for backend in ("claude", "codex", "opencode")
        }
        self._start_lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._site: web.SockSite | None = None
        self._base_url: str | None = None

    async def endpoint(self, backend: str) -> tuple[str, str]:
        if backend not in {"claude", "codex", "opencode"}:
            raise ModelHubError("mapping_target_unavailable", status=409)
        await self._ensure_started()
        assert self._base_url is not None
        return f"{self._base_url}/{backend}", self._tokens[backend]

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

    def _authorized(self, request: web.Request, backend: str) -> bool:
        expected = self._tokens.get(backend)
        if expected is None:
            return False
        authorization = request.headers.get("Authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        api_key = request.headers.get("x-api-key", "")
        return secrets.compare_digest(bearer, expected) or secrets.compare_digest(api_key, expected)

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        backend = request.match_info["backend"]
        if not self._authorized(request, backend):
            return self._error_response(status=401, code="authentication_error")
        endpoint = request.match_info["endpoint"].strip("/")
        if backend not in {"claude", "codex", "opencode"} or endpoint not in _SUPPORTED_PATHS:
            return self._error_response(status=404, code="not_found_error")
        try:
            payload = await request.json(loads=json.loads)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._error_response(status=400, code="invalid_request_error")
        if not isinstance(payload, dict):
            return self._error_response(status=400, code="invalid_request_error")
        model_id = payload.get("model")
        stream = payload.get("stream", False)
        if not isinstance(model_id, str) or not model_id or not isinstance(stream, bool):
            return self._error_response(status=400, code="invalid_request_error")

        try:
            protocol_headers = {
                name.lower(): value
                for name, value in request.headers.items()
                if name.lower() in _PROTOCOL_HEADERS
            }
            resolved = await self.service.resolve(
                backend=backend,
                model_id=model_id,
                request=ModelHubRequest(
                    payload,
                    protocol=_REQUEST_PROTOCOLS[endpoint],
                    headers=protocol_headers,
                ),
                stream=stream,
                supply_channel="hub",
            )
        except ModelHubError as exc:
            return self._error_response(status=exc.status, code=exc.code)
        return await self._resolved_response(request, resolved, stream=stream)

    async def _resolved_response(
        self,
        request: web.Request,
        resolved: ResolvedInvocation,
        *,
        stream: bool,
    ) -> web.StreamResponse:
        if resolved.supply_channel != "hub":
            return self._error_response(status=409, code="mode_switch_blocked")
        if resolved.outcome is not None:
            return self._outcome_response(resolved.outcome)
        handle = resolved.handle
        if handle is None or handle.stream is None:
            return self._error_response(status=502, code="engine_down")

        if not stream:
            payload = bytearray()
            async for chunk in handle.stream:
                payload.extend(chunk)
            await handle.outcome()
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
                await response.write(chunk)
        finally:
            await handle.outcome()
        await response.write_eof()
        return response

    @staticmethod
    def _outcome_response(outcome: RawCallOutcome) -> web.Response:
        if outcome.kind == RawOutcomeKind.SUCCESS:
            return web.Response(status=200, body=b"{}", content_type="application/json")
        status = outcome.http_status if outcome.http_status and 400 <= outcome.http_status <= 599 else 502
        return ModelHubTurnGateway._error_response(
            status=status,
            code=outcome.error_code or "api_error",
        )

    @staticmethod
    def _error_response(*, status: int, code: str) -> web.Response:
        return web.json_response(
            {
                "error": {
                    "type": code,
                    "code": code,
                    "message": "Model Hub request failed",
                }
            },
            status=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
