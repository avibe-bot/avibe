from __future__ import annotations

import asyncio
import json
import logging
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator, BinaryIO, Mapping

import aiohttp

from config.v2_config import normalize_model_hub_base_url
from core.handlers.model_hub.adapter import (
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.classification import UPSTREAM_MACHINE_ERROR_CODES
from core.handlers.model_hub.stream_wire import (
    ErrorEnvelopePath,
    ProtocolObservation,
    ProtocolSSEState,
    ProtocolUsageReport,
    SSE_MAX_FRAME_BYTES,
    SSE_MAX_PRELUDE_BYTES,
    SSEFrameLimitError,
    observe_protocol_response,
)
from vibe.model_hub_runtime.state import SourceRecord


_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_STREAM_CHUNK_BYTES = 64 * 1024
_MODEL_PROBE_BYTES = 4 * 1024 * 1024
_PRELUDE_MEMORY_BYTES = SSE_MAX_FRAME_BYTES
_OFFICIAL_BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "codex": "https://api.openai.com/v1",
}
_PROTOCOL_HEADERS = frozenset({"anthropic-beta", "anthropic-version", "openai-beta"})
logger = logging.getLogger(__name__)


class EngineClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        error_candidates: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.error_candidates = error_candidates


class _ResponseTooLargeError(RuntimeError):
    pass


class _StreamPrelude:
    """Per-invocation byte owner that spills pre-output data out of memory."""

    def __init__(
        self,
        *,
        memory_limit: int | None = None,
        total_limit: int | None = None,
    ) -> None:
        self._memory_limit = _PRELUDE_MEMORY_BYTES if memory_limit is None else memory_limit
        self._total_limit = SSE_MAX_PRELUDE_BYTES if total_limit is None else total_limit
        self._memory = bytearray()
        self._file: BinaryIO | None = None
        self._stored_bytes = 0
        self._closed = False

    @property
    def in_memory_bytes(self) -> int:
        return len(self._memory)

    @property
    def spilled(self) -> bool:
        return self._file is not None

    @property
    def stored_bytes(self) -> int:
        return self._stored_bytes

    @property
    def closed(self) -> bool:
        return self._closed

    def write(self, data: bytes) -> bool:
        if self._closed:
            raise RuntimeError("stream prelude is closed")
        if self._stored_bytes + len(data) > self._total_limit:
            return False
        if self._file is None and len(self._memory) + len(data) <= self._memory_limit:
            self._memory.extend(data)
            self._stored_bytes += len(data)
            return True
        if self._file is None:
            self._file = tempfile.TemporaryFile()
            self._file.write(self._memory)
            self._memory.clear()
        self._file.write(data)
        self._stored_bytes += len(data)
        return True

    async def chunks(self) -> AsyncIterator[bytes]:
        if self._closed:
            return
        if self._file is None:
            if self._memory:
                yield bytes(self._memory)
            return
        self._file.seek(0)
        while chunk := self._file.read(_STREAM_CHUNK_BYTES):
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._memory.clear()
        if self._file is not None:
            self._file.close()
            self._file = None


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
        stream_closer: Callable[[], Awaitable[None]] | None = None,
        observed: ProtocolSSEState | None = None,
    ) -> None:
        self._stream = stream
        self._outcome = outcome
        self._stream_closer = stream_closer
        self._observed = observed
        self._close_lock = asyncio.Lock()
        self._stream_closed = False

    @property
    def stream(self) -> AsyncIterator[bytes] | None:
        return self._stream

    @property
    def observed(self) -> ProtocolSSEState | None:
        return self._observed

    @property
    def outcome_available(self) -> bool:
        return self._outcome.done()

    async def close_stream(self) -> None:
        async with self._close_lock:
            if self._stream_closed:
                return
            try:
                stream_close = getattr(self._stream, "aclose", None)
                if stream_close is not None:
                    await stream_close()
            finally:
                if self._stream_closer is not None:
                    await self._stream_closer()
                self._stream_closed = True

    async def outcome(self) -> RawCallOutcome:
        return await asyncio.shield(self._outcome)


class EngineClient:
    """Narrow loopback-only client for the engine data and management APIs."""

    def __init__(
        self,
        connection: EngineConnection,
        *,
        timeout: float = ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    ) -> None:
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
        request_protocol: str | None = None,
        request_headers: Mapping[str, str] | None = None,
    ) -> EngineInvokeHandle:
        request_protocol = request_protocol or source.protocol
        endpoint = _endpoint_for_protocol(request_protocol)
        body = dict(request)
        body["model"] = f"{source.prefix}/{model_id}"
        body["stream"] = stream
        headers = {
            key.lower(): value for key, value in (request_headers or {}).items() if key.lower() in _PROTOCOL_HEADERS
        }
        headers.update(
            {
                "Authorization": f"Bearer {self.connection.gateway_token}",
                "Content-Type": "application/json",
            }
        )
        if request_protocol == "anthropic":
            headers.setdefault("anthropic-version", "2023-06-01")

        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self.timeout,
            sock_connect=self.timeout,
            sock_read=None,
        )
        session = aiohttp.ClientSession(timeout=timeout, trust_env=False)
        response: aiohttp.ClientResponse | None = None
        first_received = False
        model_output_started = False
        ownership_transferred = False
        prelude: _StreamPrelude | None = None
        # Held here rather than inside the prelude reader so every way out of this
        # call can still see what the wire already reported. A stream that reports
        # usage before its first model output — Anthropic's `message_start` does —
        # and then times out has already been billed for those input tokens.
        wire_state: ProtocolSSEState | None = None

        def ended(outcome: RawCallOutcome) -> EngineInvokeHandle:
            """Finish a call whose body never becomes the gateway's to forward.

            The sole exit for that population, so the tokens the wire reported are
            attached in one place instead of at each construction site: the resolver
            meters exactly what this returns, and an outcome that lost its report on
            the way out is a call the vendor billed and the ledger never saw.
            """

            if outcome.usage is None and wire_state is not None and wire_state.usage is not None:
                outcome = replace(outcome, usage=wire_state.usage)
            return completed_handle(outcome)

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
                observed_payload = observe_protocol_response(
                    request_protocol,
                    streamed=False,
                    data=payload,
                )
                outcome = _reduce_protocol_observation(
                    ProtocolObservation(
                        outcome="failed_terminal",
                        error_payload=payload,
                        error_envelope_paths=observed_payload.error_envelope_paths,
                        message=f"upstream returned HTTP {response.status}",
                        usage=observed_payload.usage,
                    ),
                    source=source,
                    model_id=model_id,
                    http_status=response.status,
                    stream_started=False,
                )
                assert outcome is not None
                response.close()
                await session.close()
                return ended(outcome)

            first = await asyncio.wait_for(
                response.content.read(_STREAM_CHUNK_BYTES),
                timeout=self.timeout,
            )
            if not first:
                response.close()
                await session.close()
                return ended(
                    _outcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        source=source,
                        model_id=model_id,
                        http_status=response.status,
                        message="upstream response ended before a protocol terminal event",
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
                    return ended(
                        _protocol_error_outcome(
                            ProtocolObservation(
                                outcome="protocol_error",
                                message="upstream response exceeded the local limit",
                            ),
                            source,
                            model_id,
                            response.status,
                            False,
                        )
                    )
                observation = observe_protocol_response(
                    request_protocol,
                    streamed=False,
                    data=first,
                )
                outcome = _reduce_protocol_observation(
                    observation,
                    source=source,
                    model_id=model_id,
                    http_status=response.status,
                    stream_started=observation.outcome == "served",
                )
                assert outcome is not None
                response.close()
                await session.close()
                ownership_transferred = True
                return (
                    buffered_handle(first, outcome)
                    if outcome.kind == RawOutcomeKind.SUCCESS
                    else ended(outcome)
                )

            prelude = _StreamPrelude()
            wire_state = ProtocolSSEState(request_protocol)
            prelude_outcome = await _read_stream_prelude(
                response=response,
                first=first,
                prelude=prelude,
                wire_state=wire_state,
                source=source,
                model_id=model_id,
                timeout=self.timeout,
            )
            model_output_started = wire_state.model_output_started
            if prelude_outcome is not None:
                response.close()
                await session.close()
                if prelude_outcome.kind == RawOutcomeKind.SUCCESS:
                    handle = buffered_prelude_handle(prelude, prelude_outcome, wire_state)
                    ownership_transferred = True
                    return handle
                prelude.close()
                return ended(prelude_outcome)

            loop = asyncio.get_running_loop()
            outcome_future: asyncio.Future[RawCallOutcome] = loop.create_future()
            response_stream = _response_stream(
                response=response,
                session=session,
                prelude=prelude,
                source=source,
                model_id=model_id,
                protocol=request_protocol,
                outcome_future=outcome_future,
                wire_state=wire_state,
            )

            async def close_response_stream() -> None:
                prelude.close()
                response.close()
                await session.close()

            handle = EngineInvokeHandle(
                stream=response_stream,
                outcome=outcome_future,
                stream_closer=close_response_stream,
                observed=wire_state,
            )
            ownership_transferred = True
            return handle
        except asyncio.TimeoutError:
            if response is not None:
                response.close()
            await session.close()
            return ended(
                _outcome(
                    kind=RawOutcomeKind.TIMEOUT,
                    source=source,
                    model_id=model_id,
                    http_status=response.status if response is not None and first_received else None,
                    message="upstream request timed out",
                    stream_started=model_output_started if stream else False,
                )
            )
        except SSEFrameLimitError as exc:
            if response is not None:
                response.close()
            await session.close()
            return ended(
                _protocol_error_outcome(
                    ProtocolObservation(outcome="protocol_error", message=str(exc)),
                    source,
                    model_id,
                    response.status if response is not None else None,
                    model_output_started if stream else False,
                )
            )
        except aiohttp.ClientError:
            if response is not None:
                response.close()
            await session.close()
            return ended(
                _outcome(
                    kind=RawOutcomeKind.NETWORK_ERROR,
                    source=source,
                    model_id=model_id,
                    error_code="engine_down",
                    http_status=response.status if response is not None and first_received else None,
                    message="upstream request failed",
                    stream_started=model_output_started if stream else False,
                )
            )
        finally:
            if not ownership_transferred:
                if prelude is not None:
                    prelude.close()
                if response is not None:
                    response.close()
                await session.close()

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
            error_type, error_code, error_candidates = _raw_error_fields(raw)
            raise EngineClientError(
                f"engine API returned HTTP {exc.code}",
                status_code=exc.code,
                error_type=error_type,
                error_code=error_code,
                error_candidates=error_candidates,
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
    try:
        url = normalize_model_hub_base_url(root, append_path="/models")
    except (TypeError, ValueError):
        raise EngineClientError("source base URL is invalid")
    assert url is not None
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json"}
    if protocol == "anthropic":
        headers = {
            "x-api-key": secret,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }
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


async def _read_stream_prelude(
    *,
    response: aiohttp.ClientResponse,
    first: bytes,
    prelude: _StreamPrelude,
    wire_state: ProtocolSSEState,
    source: SourceRecord,
    model_id: str,
    timeout: float,
) -> RawCallOutcome | None:
    """Buffer transport metadata until the sole first-model-output fact.

    The tracker belongs to the caller: this coroutine can also end by raising a
    transport error, and what the wire reported before that has to survive the
    raise for the call to be metered.
    """

    if not _received(first, prelude=prelude, wire_state=wire_state):
        return _prelude_ended_outcome(source, model_id, response.status)
    while not wire_state.model_output_started:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is not None:
            return outcome
        chunk = await asyncio.wait_for(
            response.content.read(_STREAM_CHUNK_BYTES),
            timeout=timeout,
        )
        if not chunk:
            completion = _observed_stream_terminal_outcome(
                wire_state,
                source,
                model_id,
                response.status,
            )
            if completion is not None:
                return completion
            return _prelude_ended_outcome(source, model_id, response.status)
        if not _received(chunk, prelude=prelude, wire_state=wire_state):
            return _prelude_ended_outcome(source, model_id, response.status)
    return None


def _received(
    chunk: bytes,
    *,
    prelude: _StreamPrelude,
    wire_state: ProtocolSSEState,
) -> bool:
    """Take receipt of bytes the wire delivered: read them, then keep a copy.

    Two different questions get asked about the same bytes, and only the second
    one can fail: what they say, and whether there is room to store a replay of
    them. Asking the storage question first lets a full prelude erase the report
    that arrived in the chunk that filled it — tokens the vendor billed, delivered
    on the socket, dropped because we had nowhere to put a copy. Whether we can
    hold a copy is not a question about what happened.

    Every earlier round of this class was the reading half: a fact was observed
    and some ending could not reach it. This is the writing half, and it has no
    reader-side remedy at all — bytes nobody observed leave nothing behind to fall
    back to. So this is the sole caller of ``prelude.write``, and an arrival site
    added later cannot reorder the two questions by forgetting which comes first.
    """

    wire_state.observe(chunk)
    return prelude.write(chunk)


def _prelude_ended_outcome(
    source: SourceRecord,
    model_id: str,
    http_status: int,
) -> RawCallOutcome:
    return _outcome(
        kind=RawOutcomeKind.NETWORK_ERROR,
        source=source,
        model_id=model_id,
        http_status=http_status,
        message="upstream stream ended before model output",
    )


async def _response_stream(
    *,
    response: aiohttp.ClientResponse,
    session: aiohttp.ClientSession,
    prelude: _StreamPrelude,
    source: SourceRecord,
    model_id: str,
    protocol: str,
    outcome_future: asyncio.Future[RawCallOutcome],
    wire_state: ProtocolSSEState,
) -> AsyncIterator[bytes]:
    outcome: RawCallOutcome | None = None
    try:
        async for chunk in prelude.chunks():
            yield chunk
        prelude.close()
        async for chunk in response.content.iter_chunked(_STREAM_CHUNK_BYTES):
            if chunk:
                wire_state.observe(chunk)
                yield chunk
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is None:
            outcome = _outcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                source=source,
                model_id=model_id,
                http_status=response.status,
                message="upstream stream ended before a protocol terminal event",
                stream_started=wire_state.model_output_started,
            )
    except SSEFrameLimitError as exc:
        outcome = _protocol_error_outcome(
            ProtocolObservation(outcome="protocol_error", message=str(exc)),
            source,
            model_id,
            response.status,
            wire_state.model_output_started,
        )
    except asyncio.TimeoutError:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is None:
            outcome = _outcome(
                kind=RawOutcomeKind.TIMEOUT,
                source=source,
                model_id=model_id,
                http_status=response.status,
                message="upstream response timed out after streaming started",
                stream_started=wire_state.model_output_started,
            )
    except aiohttp.ClientError:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is not None:
            logger.debug("ignoring transport error after protocol terminal marker")
        else:
            outcome = _outcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                source=source,
                model_id=model_id,
                http_status=response.status,
                error_code="engine_down",
                message="upstream response failed after streaming started",
                stream_started=wire_state.model_output_started,
            )
    finally:
        if outcome is None:
            outcome = _observed_stream_terminal_outcome(
                wire_state,
                source,
                model_id,
                response.status,
            )
        prelude.close()
        response.close()
        await session.close()
        if outcome is not None and not outcome_future.done():
            outcome_future.set_result(outcome)


def _observed_stream_terminal_outcome(
    wire_state: ProtocolSSEState,
    source: SourceRecord,
    model_id: str,
    http_status: int,
) -> RawCallOutcome | None:
    observation = wire_state.terminal_observation()
    return _reduce_protocol_observation(
        observation,
        source=source,
        model_id=model_id,
        http_status=http_status,
        stream_started=wire_state.model_output_started,
    )


def _reduce_protocol_observation(
    observation: ProtocolObservation | None,
    *,
    source: SourceRecord,
    model_id: str,
    http_status: int | None,
    stream_started: bool,
) -> RawCallOutcome | None:
    """Sole conversion from protocol observations to runtime call outcomes.

    Every branch carries the observation's token report onward, including the
    error ones: a vendor that reported tokens billed for them whether or not the
    response ended well, and this is the only hop where a call that settles
    without ever handing its body downstream can still report them.
    """

    if observation is None or observation.outcome is None:
        return None
    if observation.outcome == "served":
        return _outcome(
            kind=RawOutcomeKind.SUCCESS,
            source=source,
            model_id=model_id,
            http_status=http_status,
            stream_started=stream_started,
            usage=observation.usage,
        )
    if observation.outcome == "failed_terminal":
        error_type, error_code, candidates = _raw_error_fields(
            observation.error_payload or b"",
            observation.error_envelope_paths,
        )
        return _outcome(
            kind=RawOutcomeKind.HTTP_ERROR,
            source=source,
            model_id=model_id,
            http_status=http_status,
            error_type=error_type,
            error_code=error_code,
            error_candidates=candidates,
            message=observation.message or "upstream returned a protocol error event",
            stream_started=stream_started,
            usage=observation.usage,
        )
    return _outcome(
        kind=RawOutcomeKind.PROTOCOL_ERROR,
        source=source,
        model_id=model_id,
        http_status=http_status,
        message=observation.message or "upstream emitted invalid protocol data",
        stream_started=stream_started,
        usage=observation.usage,
    )


def _protocol_error_outcome(
    observation: ProtocolObservation,
    source: SourceRecord,
    model_id: str,
    http_status: int | None,
    stream_started: bool,
) -> RawCallOutcome:
    outcome = _reduce_protocol_observation(
        observation,
        source=source,
        model_id=model_id,
        http_status=http_status,
        stream_started=stream_started,
    )
    assert outcome is not None and outcome.kind is RawOutcomeKind.PROTOCOL_ERROR
    return outcome


def completed_handle(outcome: RawCallOutcome) -> EngineInvokeHandle:
    future = asyncio.get_running_loop().create_future()
    future.set_result(outcome)
    return EngineInvokeHandle(stream=None, outcome=future)


def buffered_handle(payload: bytes, outcome: RawCallOutcome) -> EngineInvokeHandle:
    async def body() -> AsyncIterator[bytes]:
        yield payload

    future = asyncio.get_running_loop().create_future()
    future.set_result(outcome)
    return EngineInvokeHandle(stream=body(), outcome=future)


def buffered_prelude_handle(
    prelude: _StreamPrelude,
    outcome: RawCallOutcome,
    observed: ProtocolSSEState,
) -> EngineInvokeHandle:
    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in prelude.chunks():
                yield chunk
        finally:
            prelude.close()

    async def close() -> None:
        prelude.close()

    future = asyncio.get_running_loop().create_future()
    future.set_result(outcome)
    return EngineInvokeHandle(
        stream=body(),
        outcome=future,
        stream_closer=close,
        observed=observed,
    )


def _outcome(
    *,
    kind: RawOutcomeKind,
    source: SourceRecord,
    model_id: str,
    http_status: int | None = None,
    error_code: str | None = None,
    error_type: str | None = None,
    error_candidates: tuple[str, ...] = (),
    message: str | None = None,
    stream_started: bool = False,
    usage: ProtocolUsageReport | None = None,
) -> RawCallOutcome:
    return RawCallOutcome(
        kind=kind,
        http_status=http_status,
        error_code=error_code,
        redacted_message=message,
        stream_started=stream_started,
        model_id=model_id,
        source_id=source.source_id,
        error_type=error_type,
        error_candidates=error_candidates,
        usage=usage,
    )


def _endpoint_for_protocol(protocol: str) -> str:
    if protocol == "anthropic":
        return "/v1/messages"
    if protocol == "openai_responses":
        return "/v1/responses"
    if protocol == "openai_chat":
        return "/v1/chat/completions"
    raise ValueError("unsupported source protocol")


def _raw_error_fields(
    payload: bytes,
    envelope_paths: tuple[ErrorEnvelopePath, ...] = (("error",),),
) -> tuple[str | None, str | None, tuple[str, ...]]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, None, ()
    if not isinstance(decoded, dict):
        return None, None, ()
    types: list[str] = []
    codes: list[str] = []
    for path in envelope_paths:
        envelope: object = decoded
        for component in path:
            if not isinstance(envelope, Mapping) or component not in envelope:
                envelope = None
                break
            envelope = envelope[component]
        if not isinstance(envelope, Mapping):
            continue
        error_type = _safe_error_code(envelope["type"]) if "type" in envelope else None
        error_code = _safe_error_code(envelope["code"]) if "code" in envelope else None
        if error_type is not None:
            types.append(error_type)
        if error_code is not None:
            codes.append(error_code)
    candidates = tuple(dict.fromkeys((*types, *codes)))
    return (types[0] if types else None, codes[0] if codes else None, candidates)


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str) or value not in UPSTREAM_MACHINE_ERROR_CODES:
        return None
    return value
