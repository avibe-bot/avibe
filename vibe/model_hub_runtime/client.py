from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping

import aiohttp

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from vibe.model_hub_runtime.state import SourceRecord


_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_MODEL_PROBE_BYTES = 4 * 1024 * 1024
_SAFE_ERROR_CODES = frozenset(
    {
        "api_error",
        "authentication_error",
        "billing_error",
        "context_length_exceeded",
        "insufficient_quota",
        "invalid_api_key",
        "invalid_request_error",
        "model_not_found",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "quota_exceeded",
        "rate_limit_error",
        "rate_limit_exceeded",
        "request_too_large",
        "server_error",
    }
)
_OFFICIAL_BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "codex": "https://api.openai.com/v1",
}


class EngineClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class _ResponseTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True)
class EngineConnection:
    base_url: str
    management_key: str = field(repr=False)
    gateway_token: str = field(repr=False)


class EngineInvokeHandle:
    """Concrete InvokeHandle with one-shot streaming ownership."""

    def __init__(
        self,
        *,
        stream: AsyncIterator[bytes] | None,
        outcome: asyncio.Future[RawCallOutcome],
    ) -> None:
        self._stream = stream
        self._outcome = outcome

    @property
    def stream(self) -> AsyncIterator[bytes] | None:
        return self._stream

    async def outcome(self) -> RawCallOutcome:
        return await asyncio.shield(self._outcome)


class EngineClient:
    """Narrow loopback-only client for the engine data and management APIs."""

    def __init__(self, connection: EngineConnection, *, timeout: float = 60.0) -> None:
        parsed = urllib.parse.urlparse(connection.base_url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise ValueError("engine client requires a credential-free 127.0.0.1 URL")
        self.connection = connection
        self.timeout = timeout

    def health(self) -> bool:
        try:
            models = self._request_json(
                "GET",
                "/v1/models",
                headers={"Authorization": f"Bearer {self.connection.gateway_token}"},
                timeout=min(self.timeout, 1.0),
            )
            config = self.management_request("GET", "/config", timeout=min(self.timeout, 1.0))
        except EngineClientError:
            return False
        return models.get("object") == "list" and isinstance(config, dict)

    def management_request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("management path must be relative to the allowlisted API root")
        return self._request_json(
            method,
            f"/v0/management{path}",
            query=query,
            payload=payload,
            headers={"X-Management-Key": self.connection.management_key},
            timeout=timeout,
        )

    async def invoke(
        self,
        source: SourceRecord,
        model_id: str,
        request: Mapping[str, Any],
        *,
        stream: bool,
    ) -> EngineInvokeHandle:
        endpoint = _endpoint_for_protocol(source.protocol)
        body = dict(request)
        body["model"] = f"{source.prefix}/{model_id}"
        body["stream"] = stream
        headers = {
            "Authorization": f"Bearer {self.connection.gateway_token}",
            "Content-Type": "application/json",
        }
        if source.protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"

        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self.timeout,
            sock_connect=self.timeout,
            sock_read=None,
        )
        session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        response: aiohttp.ClientResponse | None = None
        first_received = False
        try:
            response = await asyncio.wait_for(
                session.post(
                    self._url(endpoint),
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ),
                timeout=self.timeout,
            )
            if response.status >= 300:
                try:
                    payload = await asyncio.wait_for(
                        _read_limited(response.content, _MAX_RESPONSE_BYTES),
                        timeout=self.timeout,
                    )
                except (_ResponseTooLargeError, asyncio.TimeoutError, aiohttp.ClientError):
                    payload = b""
                outcome = _outcome(
                    kind=RawOutcomeKind.HTTP_ERROR,
                    source=source,
                    model_id=model_id,
                    http_status=response.status,
                    error_code=_error_code(payload),
                    message=f"upstream returned HTTP {response.status}",
                )
                response.close()
                await session.close()
                return completed_handle(outcome)

            first = await asyncio.wait_for(
                response.content.read(_STREAM_CHUNK_BYTES),
                timeout=self.timeout,
            )
            if not first:
                response.close()
                await session.close()
                return completed_handle(
                    _outcome(
                        kind=RawOutcomeKind.PROTOCOL_ERROR,
                        source=source,
                        model_id=model_id,
                        http_status=response.status,
                        message="upstream response ended before the first byte",
                    )
                )
            first_received = True
            if not stream:
                try:
                    first = await asyncio.wait_for(
                        _read_limited(
                            response.content,
                            _MAX_RESPONSE_BYTES,
                            initial=first,
                        ),
                        timeout=self.timeout,
                    )
                except _ResponseTooLargeError:
                    response.close()
                    await session.close()
                    return completed_handle(
                        _outcome(
                            kind=RawOutcomeKind.PROTOCOL_ERROR,
                            source=source,
                            model_id=model_id,
                            http_status=response.status,
                            message="upstream response exceeded the local limit",
                            stream_started=True,
                        )
                    )
                if not _is_json(first):
                    response.close()
                    await session.close()
                    return completed_handle(
                        _outcome(
                            kind=RawOutcomeKind.PROTOCOL_ERROR,
                            source=source,
                            model_id=model_id,
                            http_status=response.status,
                            message="upstream returned an invalid JSON response",
                            stream_started=True,
                        )
                    )
        except asyncio.TimeoutError:
            if response is not None:
                response.close()
            await session.close()
            return completed_handle(
                _outcome(
                    kind=RawOutcomeKind.TIMEOUT,
                    source=source,
                    model_id=model_id,
                    http_status=response.status if response is not None and first_received else None,
                    message="upstream request timed out",
                    stream_started=first_received,
                )
            )
        except aiohttp.ClientError:
            if response is not None:
                response.close()
            await session.close()
            return completed_handle(
                _outcome(
                    kind=RawOutcomeKind.NETWORK_ERROR,
                    source=source,
                    model_id=model_id,
                    http_status=response.status if response is not None and first_received else None,
                    message="upstream request failed",
                    stream_started=first_received,
                )
            )

        loop = asyncio.get_running_loop()
        outcome_future: asyncio.Future[RawCallOutcome] = loop.create_future()
        response_stream = _response_stream(
            response=response,
            session=session,
            first=first,
            source=source,
            model_id=model_id,
            outcome_future=outcome_future,
        )
        return EngineInvokeHandle(stream=response_stream, outcome=outcome_future)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = self._url(path, query=query)
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                _NoRedirectHandler(),
            )
            with opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(_MAX_RESPONSE_BYTES)
            raise EngineClientError(
                f"engine API returned HTTP {exc.code}",
                status_code=exc.code,
                error_type=_error_code(raw),
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise EngineClientError("engine API is unavailable", error_type=type(exc).__name__) from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise EngineClientError("engine API response is too large", error_type="response_too_large")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise EngineClientError("engine API returned an invalid payload", error_type="invalid_json") from None
        if not isinstance(decoded, dict):
            raise EngineClientError("engine API returned an invalid payload", error_type="invalid_json")
        return decoded

    def _url(self, path: str, *, query: Mapping[str, str] | None = None) -> str:
        url = f"{self.connection.base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url


async def probe_models(
    *,
    vendor: str,
    protocol: str,
    base_url: str | None,
    secret: str,
    timeout: float = 15.0,
) -> tuple[str, ...]:
    """Probe the one allowlisted models path without redirecting credentials."""
    normalized_vendor = vendor.strip().lower()
    root = base_url or _OFFICIAL_BASE_URLS.get(normalized_vendor)
    if not root:
        raise EngineClientError("source requires a base URL for model discovery")
    parsed = urllib.parse.urlparse(root)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise EngineClientError("source base URL is invalid")
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    if protocol == "anthropic":
        headers = {
            "x-api-key": secret,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
    url = f"{root.rstrip('/')}/models"
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status >= 300:
                    raise EngineClientError(
                        f"model discovery returned HTTP {response.status}",
                        status_code=response.status,
                    )
                payload = await _read_limited(response.content, _MODEL_PROBE_BYTES)
    except _ResponseTooLargeError:
        raise EngineClientError("model discovery response is too large") from None
    except asyncio.TimeoutError:
        raise EngineClientError("model discovery timed out", error_type="timeout") from None
    except aiohttp.ClientError:
        raise EngineClientError("model discovery failed", error_type="network_error") from None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EngineClientError("model discovery returned an invalid payload") from None
    if not isinstance(decoded, dict):
        raise EngineClientError("model discovery returned an invalid payload")
    items = decoded.get("data", decoded.get("models"))
    if not isinstance(items, list):
        raise EngineClientError("model discovery returned an invalid payload")
    model_ids: list[str] = []
    for item in items:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, str) and value and value not in model_ids:
            model_ids.append(value)
    return tuple(model_ids)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


async def _read_limited(
    content: aiohttp.StreamReader,
    limit: int,
    *,
    initial: bytes = b"",
) -> bytes:
    payload = bytearray(initial)
    if len(payload) > limit:
        raise _ResponseTooLargeError
    while True:
        chunk = await content.read(min(_STREAM_CHUNK_BYTES, limit + 1 - len(payload)))
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
        if len(payload) > limit:
            raise _ResponseTooLargeError


async def _response_stream(
    *,
    response: aiohttp.ClientResponse,
    session: aiohttp.ClientSession,
    first: bytes,
    source: SourceRecord,
    model_id: str,
    outcome_future: asyncio.Future[RawCallOutcome],
) -> AsyncIterator[bytes]:
    outcome: RawCallOutcome | None = None
    try:
        yield first
        async for chunk in response.content.iter_chunked(_STREAM_CHUNK_BYTES):
            if chunk:
                yield chunk
        outcome = _outcome(
            kind=RawOutcomeKind.SUCCESS,
            source=source,
            model_id=model_id,
            http_status=response.status,
            stream_started=True,
        )
    except asyncio.TimeoutError:
        outcome = _outcome(
            kind=RawOutcomeKind.TIMEOUT,
            source=source,
            model_id=model_id,
            http_status=response.status,
            message="upstream response timed out after streaming started",
            stream_started=True,
        )
    except aiohttp.ClientError:
        outcome = _outcome(
            kind=RawOutcomeKind.NETWORK_ERROR,
            source=source,
            model_id=model_id,
            http_status=response.status,
            message="upstream response failed after streaming started",
            stream_started=True,
        )
    finally:
        response.close()
        await session.close()
        if outcome is None:
            outcome = _outcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                source=source,
                model_id=model_id,
                http_status=response.status,
                message="upstream response stream was not fully consumed",
                stream_started=True,
            )
        if not outcome_future.done():
            outcome_future.set_result(outcome)


def completed_handle(outcome: RawCallOutcome) -> EngineInvokeHandle:
    future = asyncio.get_running_loop().create_future()
    future.set_result(outcome)
    return EngineInvokeHandle(stream=None, outcome=future)


def _outcome(
    *,
    kind: RawOutcomeKind,
    source: SourceRecord,
    model_id: str,
    http_status: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
    stream_started: bool = False,
) -> RawCallOutcome:
    return RawCallOutcome(
        kind=kind,
        http_status=http_status,
        error_code=error_code,
        redacted_message=message,
        stream_started=stream_started,
        model_id=model_id,
        source_id=source.source_id,
    )


def _endpoint_for_protocol(protocol: str) -> str:
    if protocol == "anthropic":
        return "/v1/messages"
    if protocol == "openai_responses":
        return "/v1/responses"
    if protocol in {"openai_chat", "openai_compatible"}:
        return "/v1/chat/completions"
    raise ValueError("unsupported source protocol")


def _error_code(payload: bytes) -> str | None:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    error = decoded.get("error")
    if isinstance(error, dict):
        value = error.get("type") or error.get("code")
        return _safe_error_code(value)
    value = decoded.get("code")
    return _safe_error_code(value)


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str) or value not in _SAFE_ERROR_CODES:
        return None
    return value


def _is_json(payload: bytes) -> bool:
    try:
        json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return True
