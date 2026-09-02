from __future__ import annotations

import asyncio
import io
import json
import logging
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, AsyncIterator, BinaryIO, Mapping, TypeVar, cast

import aiohttp
import ijson

from config.v2_config import normalize_model_hub_base_url
from core.handlers.model_hub.adapter import (
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    RawCallOutcome,
    RawOutcomeKind,
)
from core.handlers.model_hub.async_owner import run_owned_in_thread
from core.handlers.model_hub.classification import UPSTREAM_MACHINE_ERROR_CODES
from core.handlers.model_hub.json_wire import (
    JSONEvent,
    JSONPath,
    JSONScope,
    project_json_reader,
)
from core.handlers.model_hub.stream_wire import (
    ErrorEnvelopePath,
    ProtocolObservation,
    ProtocolSSEState,
    ProtocolUsageReport,
    observe_buffered_protocol_response,
)
from vibe.model_hub_runtime.state import SourceRecord


_STREAM_CHUNK_BYTES = 64 * 1024
# This threshold only selects memory or a temporary file; it never rejects or
# truncates upstream response bytes.
_PRELUDE_MEMORY_BYTES = 256 * 1024
_ERROR_OBSERVATION_BYTES = 256 * 1024
_OFFICIAL_BASE_URLS = {
    "anthropic": "https://api.anthropic.com/v1",
    "openai": "https://api.openai.com/v1",
    "codex": "https://api.openai.com/v1",
}
_PROTOCOL_HEADERS = frozenset({"anthropic-beta", "anthropic-version", "openai-beta"})
logger = logging.getLogger(__name__)
_ProjectedJSON = TypeVar("_ProjectedJSON")


def upstream_api_url(root: str, path: str) -> str:
    """Resolve a standard v1 endpoint from either an origin or an API root."""

    normalized = normalize_model_hub_base_url(root)
    assert normalized is not None
    root_path = urllib.parse.urlsplit(normalized).path.rstrip("/")
    append_path = path if not root_path else path.removeprefix("/v1")
    resolved = normalize_model_hub_base_url(normalized, append_path=append_path)
    assert resolved is not None
    return resolved


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


class _StreamPrelude:
    """Per-invocation byte owner that spills pre-output data out of memory."""

    def __init__(
        self,
        *,
        memory_limit: int | None = None,
    ) -> None:
        self._memory_limit = _PRELUDE_MEMORY_BYTES if memory_limit is None else memory_limit
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

    def write(self, data: bytes) -> None:
        if self._closed:
            raise RuntimeError("stream prelude is closed")
        if self._file is None and len(self._memory) + len(data) <= self._memory_limit:
            self._memory.extend(data)
            self._stored_bytes += len(data)
            return
        if self._file is None:
            self._file = tempfile.TemporaryFile()
            self._file.write(self._memory)
            self._memory.clear()
        self._file.write(data)
        self._stored_bytes += len(data)

    async def write_async(self, data: bytes) -> None:
        if self._file is None and len(self._memory) + len(data) <= self._memory_limit:
            self.write(data)
            return
        await run_owned_in_thread(self.write, data)

    async def chunks(self) -> AsyncIterator[bytes]:
        if self._closed:
            return
        if self._file is None:
            if self._memory:
                yield bytes(self._memory)
            return
        await run_owned_in_thread(self._file.seek, 0)
        while chunk := await run_owned_in_thread(self._file.read, _STREAM_CHUNK_BYTES):
            yield chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._memory.clear()
        if self._file is not None:
            self._file.close()
            self._file = None

    def reader(self) -> BinaryIO:
        """Return the body from its beginning without copying spilled bytes."""

        if self._closed:
            raise RuntimeError("stream prelude is closed")
        if self._file is None:
            return io.BytesIO(self._memory)
        self._file.seek(0)
        return self._file

    def prefix(self, limit: int) -> bytes:
        """Return bounded diagnostic bytes without changing response ownership."""

        if self._closed:
            return b""
        if self._file is None:
            return bytes(self._memory[:limit])
        self._file.seek(0)
        return self._file.read(limit)

    async def prefix_async(self, limit: int) -> bytes:
        if self._file is None:
            return self.prefix(limit)
        return await run_owned_in_thread(self.prefix, limit)


class _DeadlineReader:
    """Keep local response projection inside the request's absolute deadline."""

    def __init__(self, reader: BinaryIO, deadline: float) -> None:
        self._reader = reader
        self._deadline = deadline

    def _check_deadline(self) -> None:
        if time.monotonic() >= self._deadline:
            raise asyncio.TimeoutError("engine API response exceeded its request deadline")

    def read(self, size: int = -1) -> bytes:
        self._check_deadline()
        payload = self._reader.read(size)
        self._check_deadline()
        return payload

    def readinto(self, buffer: bytearray) -> int | None:
        self._check_deadline()
        readinto = getattr(self._reader, "readinto", None)
        if readinto is None:
            payload = self._reader.read(len(buffer))
            count = len(payload)
            buffer[:count] = payload
        else:
            count = readinto(buffer)
        self._check_deadline()
        return count

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._reader.seek(offset, whence)

    def tell(self) -> int:
        return self._reader.tell()

    def readable(self) -> bool:
        return self._reader.readable()

    def seekable(self) -> bool:
        return self._reader.seekable()


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
            models_ok = self._request_json_projection(
                "GET",
                "/v1/models",
                _project_models_health,
                headers={"Authorization": f"Bearer {self.connection.gateway_token}"},
                timeout=min(self.timeout, 1.0),
            )
            config_ok = self._request_json_projection(
                "GET",
                "/v0/management/config",
                _project_root_map,
                headers={"X-Management-Key": self.connection.management_key},
                timeout=min(self.timeout, 1.0),
            )
        except EngineClientError:
            return False
        return models_ok and config_ok

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
        routed_model = f"{source.prefix}/{model_id}"
        body["model"] = routed_model
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
                error_body = _StreamPrelude()
                response_deadline = time.monotonic() + self.timeout
                try:
                    await asyncio.wait_for(
                        _read_response_into(response.content, error_body),
                        timeout=self.timeout,
                    )
                    observed_payload = await _observe_buffered_protocol_response_async(
                        observe_buffered_protocol_response,
                        request_protocol,
                        error_body,
                        machine_error_codes=UPSTREAM_MACHINE_ERROR_CODES,
                        deadline=response_deadline,
                    )
                    payload = await error_body.prefix_async(_ERROR_OBSERVATION_BYTES)
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    payload = b""
                    observed_payload = ProtocolObservation()
                finally:
                    error_body.close()
                if response.status == 502 and _is_local_model_registration_failure(
                    payload,
                    routed_model=routed_model,
                ):
                    outcome = _outcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        source=source,
                        model_id=model_id,
                        http_status=response.status,
                        error_code="engine_down",
                        message="engine rejected its registered routed model",
                    )
                    response.close()
                    await session.close()
                    return ended(outcome)
                outcome = _reduce_protocol_observation(
                    replace(
                        observed_payload,
                        outcome="failed_terminal",
                        error_payload=payload,
                        message=f"upstream returned HTTP {response.status}",
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
                buffered_body = _StreamPrelude()
                prelude = buffered_body
                response_deadline = time.monotonic() + self.timeout
                await buffered_body.write_async(first)
                await asyncio.wait_for(
                    _read_response_into(response.content, buffered_body),
                    timeout=self.timeout,
                )
                observation = await _observe_buffered_protocol_response_async(
                    observe_buffered_protocol_response,
                    request_protocol,
                    buffered_body,
                    machine_error_codes=UPSTREAM_MACHINE_ERROR_CODES,
                    deadline=response_deadline,
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
                if outcome.kind != RawOutcomeKind.SUCCESS:
                    buffered_body.close()
                    return ended(outcome)
                return buffered_prelude_handle(
                    buffered_body,
                    outcome,
                    None,
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
                outcome = _observed_stream_terminal_outcome(
                    wire_state,
                    source,
                    model_id,
                    response.status,
                )
                if outcome is not None and not outcome_future.done():
                    outcome_future.set_result(outcome)

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
                    stream_started=(
                        wire_state.model_output_started
                        if stream and wire_state is not None
                        else model_output_started if stream else False
                    ),
                )
            )
        except (aiohttp.ClientError, OSError) as exc:
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
                    message=(
                        "local response replay failed"
                        if isinstance(exc, OSError)
                        else "upstream request failed"
                    ),
                    stream_started=(
                        wire_state.model_output_started
                        if stream and wire_state is not None
                        else model_output_started if stream else False
                    ),
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
        return self._request_json_projection(
            method,
            path,
            _load_json_object,
            query=query,
            payload=payload,
            headers=headers,
            timeout=timeout,
        )

    def _request_json_projection(
        self,
        method: str,
        path: str,
        projector: Callable[[BinaryIO], _ProjectedJSON],
        *,
        query: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> _ProjectedJSON:
        url = self._url(path, query=query)
        request_timeout = timeout or self.timeout
        deadline = time.monotonic() + request_timeout
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
            with opener.open(request, timeout=request_timeout) as response:
                with tempfile.SpooledTemporaryFile(max_size=_PRELUDE_MEMORY_BYTES) as response_body:
                    _copy_sync_response(response, response_body, deadline=deadline)
                    response_body.seek(0)
                    try:
                        return _project_before_deadline(
                            response_body,
                            projector,
                            deadline=deadline,
                        )
                    except (ijson.JSONError, UnicodeDecodeError, ValueError, OverflowError):
                        raise EngineClientError(
                            "engine API returned an invalid payload",
                            error_type="invalid_json",
                        ) from None
        except urllib.error.HTTPError as exc:
            try:
                with tempfile.SpooledTemporaryFile(max_size=_PRELUDE_MEMORY_BYTES) as response_body:
                    _copy_sync_response(exc, response_body, deadline=deadline)
                    response_body.seek(0)
                    error_type, error_code, error_candidates = _project_before_deadline(
                        response_body,
                        _project_raw_error_fields,
                        deadline=deadline,
                    )
            except (asyncio.TimeoutError, TimeoutError, socket.timeout, OSError) as read_error:
                raise EngineClientError(
                    "engine API is unavailable",
                    error_type=type(read_error).__name__,
                ) from None
            raise EngineClientError(
                f"engine API returned HTTP {exc.code}",
                status_code=exc.code,
                error_type=error_type,
                error_code=error_code,
                error_candidates=error_candidates,
            ) from None
        except (
            urllib.error.URLError,
            asyncio.TimeoutError,
            TimeoutError,
            socket.timeout,
            OSError,
        ) as exc:
            raise EngineClientError("engine API is unavailable", error_type=type(exc).__name__) from None

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
        url = upstream_api_url(root, "/v1/models")
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
    deadline = time.monotonic() + timeout
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                if response.status >= 300:
                    raise EngineClientError(
                        f"model discovery returned HTTP {response.status}",
                        status_code=response.status,
                    )
                with tempfile.SpooledTemporaryFile(max_size=_PRELUDE_MEMORY_BYTES) as payload:
                    while chunk := await response.content.read(_STREAM_CHUNK_BYTES):
                        await run_owned_in_thread(payload.write, chunk)
                    projected = await run_owned_in_thread(
                        _project_model_inventory_before_deadline,
                        payload,
                        deadline=deadline,
                    )
    except asyncio.TimeoutError:
        raise EngineClientError("model discovery timed out", error_type="timeout") from None
    except aiohttp.ClientError:
        raise EngineClientError("model discovery failed", error_type="network_error") from None
    except OSError as exc:
        raise EngineClientError(
            "model discovery failed",
            error_type=type(exc).__name__,
        ) from None
    if projected is None:
        raise EngineClientError("model discovery returned an invalid payload")
    data_seen, data_is_array, data_models, fallback_seen, fallback_is_array, fallback_models = projected
    if data_seen:
        if not data_is_array:
            raise EngineClientError("model discovery returned an invalid payload")
        return tuple(data_models)
    if not fallback_seen or not fallback_is_array:
        raise EngineClientError("model discovery returned an invalid payload")
    return tuple(fallback_models)


def _project_model_inventory(
    reader: BinaryIO,
) -> tuple[bool, bool, list[str], bool, bool, list[str]] | None:
    paths = {
        (),
        ("data",),
        ("data", "*"),
        ("data", "*", "id"),
        ("models",),
        ("models", "*"),
        ("models", "*", "id"),
    }
    root_is_map = False
    data_seen = False
    data_is_array = False
    fallback_seen = False
    fallback_is_array = False
    data_models: dict[JSONScope, str] = {}
    fallback_models: dict[JSONScope, str] = {}
    invalid_data_models: set[JSONScope] = set()
    invalid_fallback_models: set[JSONScope] = set()

    def visit(
        path: JSONPath,
        event: JSONEvent,
        value: object | None,
        scope: JSONScope,
    ) -> None:
        nonlocal root_is_map, data_seen, data_is_array, fallback_seen, fallback_is_array
        if path == () and event == "start_map":
            root_is_map = True
        elif path == ("data",):
            if event == "replace":
                data_models.clear()
                invalid_data_models.clear()
            data_seen = True
            if event != "nonempty":
                data_is_array = event == "start_array"
        elif path == ("models",):
            if event == "replace":
                fallback_models.clear()
                invalid_fallback_models.clear()
            fallback_seen = True
            if event != "nonempty":
                fallback_is_array = event == "start_array"
        elif path in {("data", "*"), ("data", "*", "id")}:
            if event == "replace":
                data_models.pop(scope, None)
                invalid_data_models.discard(scope)
            elif event == "elided_string":
                invalid_data_models.add(scope)
            elif event == "scalar" and isinstance(value, str) and value:
                data_models[scope] = value
        elif path in {("models", "*"), ("models", "*", "id")}:
            if event == "replace":
                fallback_models.pop(scope, None)
                invalid_fallback_models.discard(scope)
            elif event == "elided_string":
                invalid_fallback_models.add(scope)
            elif event == "scalar" and isinstance(value, str) and value:
                fallback_models[scope] = value

    def ordered_unique(values: Mapping[JSONScope, str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for scope in sorted(values):
            value = values[scope]
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    if not project_json_reader(reader, paths, visit) or not root_is_map:
        return None
    if (data_seen and invalid_data_models) or (
        not data_seen and fallback_seen and invalid_fallback_models
    ):
        return None
    return (
        data_seen,
        data_is_array,
        ordered_unique(data_models),
        fallback_seen,
        fallback_is_array,
        ordered_unique(fallback_models),
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _project_before_deadline(
    reader: BinaryIO,
    projector: Callable[[BinaryIO], _ProjectedJSON],
    *,
    deadline: float,
) -> _ProjectedJSON:
    guarded = _DeadlineReader(reader, deadline)
    guarded._check_deadline()
    projected = projector(cast(BinaryIO, guarded))
    guarded._check_deadline()
    return projected


async def _observe_buffered_protocol_response_async(
    projector: Callable[..., ProtocolObservation],
    protocol: str,
    prelude: _StreamPrelude,
    *,
    machine_error_codes: frozenset[str],
    deadline: float,
) -> ProtocolObservation:
    """Drain the finite local projection before its reader can be closed."""

    return await run_owned_in_thread(
        _project_buffered_protocol_response,
        projector,
        protocol,
        prelude,
        machine_error_codes=machine_error_codes,
        deadline=deadline,
    )


def _project_buffered_protocol_response(
    projector: Callable[..., ProtocolObservation],
    protocol: str,
    prelude: _StreamPrelude,
    *,
    machine_error_codes: frozenset[str],
    deadline: float,
) -> ProtocolObservation:
    return _project_before_deadline(
        prelude.reader(),
        lambda reader: projector(
            protocol,
            reader,
            machine_error_codes=machine_error_codes,
        ),
        deadline=deadline,
    )


def _project_model_inventory_before_deadline(
    payload: BinaryIO,
    *,
    deadline: float,
) -> tuple[bool, bool, list[str], bool, bool, list[str]] | None:
    payload.seek(0)
    return _project_before_deadline(
        payload,
        _project_model_inventory,
        deadline=deadline,
    )


def _is_local_model_registration_failure(
    payload: bytes,
    *,
    routed_model: str,
) -> bool:
    """Identify CLIProxyAPI's local pre-egress model registry rejection."""

    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if (
        not isinstance(decoded, dict)
        or "type" not in decoded
        or decoded["type"] != "error"
    ):
        return False
    error = decoded.get("error")
    error_type, _, _ = _raw_error_fields(payload)
    return (
        isinstance(error, dict)
        and error_type == "api_error"
        and error.get("message") == f"unknown provider for model {routed_model}"
    )


def _copy_sync_response(
    source: BinaryIO,
    target: BinaryIO,
    *,
    deadline: float,
) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("engine API response exceeded its request deadline")
        _set_sync_response_timeout(source, remaining)
        chunk = source.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            return
        target.write(chunk)


def _set_sync_response_timeout(source: BinaryIO, timeout: float) -> None:
    fp = getattr(source, "fp", None)
    raw = getattr(fp, "raw", None)
    response_socket = getattr(raw, "_sock", None)
    if response_socket is not None:
        response_socket.settimeout(max(timeout, 0.001))


def _load_json_object(reader: BinaryIO) -> dict[str, Any]:
    if reader.read(3) != b"\xef\xbb\xbf":
        reader.seek(0)
    items = ijson.items(reader, "")
    try:
        decoded = next(items)
    except StopIteration:
        raise ValueError("empty JSON document") from None
    try:
        next(items)
    except StopIteration:
        pass
    else:
        raise ValueError("multiple JSON documents")
    if not isinstance(decoded, dict):
        raise ValueError("root JSON value is not an object")
    containers: list[dict[str, Any] | list[Any]] = [decoded]
    while containers:
        container = containers.pop()
        entries = container.items() if isinstance(container, dict) else enumerate(container)
        for key, value in entries:
            if isinstance(value, Decimal):
                container[key] = float(value)
            elif isinstance(value, (dict, list)):
                containers.append(value)
    return decoded


def _project_models_health(reader: BinaryIO) -> bool:
    root_is_map = False
    object_value: object | None = None

    def visit(
        path: JSONPath,
        event: JSONEvent,
        value: object | None,
        _scope: JSONScope,
    ) -> None:
        nonlocal root_is_map, object_value
        if path == () and event == "start_map":
            root_is_map = True
        elif path == ("object",):
            if event == "replace":
                object_value = None
            elif event == "scalar":
                object_value = value

    if not project_json_reader(reader, {(), ("object",)}, visit):
        raise ValueError("invalid models health response")
    return root_is_map and object_value == "list"


def _project_root_map(reader: BinaryIO) -> bool:
    root_is_map = False

    def visit(
        path: JSONPath,
        event: JSONEvent,
        _value: object | None,
        _scope: JSONScope,
    ) -> None:
        nonlocal root_is_map
        if path == () and event in {"start_map", "start_array", "scalar"}:
            root_is_map = event == "start_map"

    if not project_json_reader(reader, {()}, visit):
        raise ValueError("invalid JSON response")
    return root_is_map


def _project_raw_error_fields(
    reader: BinaryIO,
    envelope_paths: tuple[ErrorEnvelopePath, ...] = (("error",),),
) -> tuple[str | None, str | None, tuple[str, ...]]:
    selected_paths = {
        path
        for envelope_path in envelope_paths
        for path in (envelope_path, (*envelope_path, "type"), (*envelope_path, "code"))
    }
    maps: set[ErrorEnvelopePath] = set()
    values: dict[JSONPath, object] = {}

    def visit(
        path: JSONPath,
        event: JSONEvent,
        value: object | None,
        _scope: JSONScope,
    ) -> None:
        for envelope_path in envelope_paths:
            if path == envelope_path:
                if event == "replace":
                    maps.discard(envelope_path)
                    values.pop((*envelope_path, "type"), None)
                    values.pop((*envelope_path, "code"), None)
                elif event == "start_map":
                    maps.add(envelope_path)
                elif event == "start_array":
                    maps.discard(envelope_path)
            elif path in {(*envelope_path, "type"), (*envelope_path, "code")}:
                if event == "replace":
                    values.pop(path, None)
                elif event == "scalar":
                    values[path] = value

    if not project_json_reader(reader, selected_paths, visit):
        return None, None, ()
    types = [
        code
        for path in envelope_paths
        if path in maps
        if (code := _safe_error_code(values.get((*path, "type")))) is not None
    ]
    codes = [
        code
        for path in envelope_paths
        if path in maps
        if (code := _safe_error_code(values.get((*path, "code")))) is not None
    ]
    candidates = tuple(dict.fromkeys((*types, *codes)))
    return types[0] if types else None, codes[0] if codes else None, candidates


async def _read_response_into(
    content: aiohttp.StreamReader,
    target: _StreamPrelude,
) -> None:
    while chunk := await content.read(_STREAM_CHUNK_BYTES):
        await target.write_async(chunk)


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

    await _received(first, prelude=prelude, wire_state=wire_state)
    deadline = asyncio.get_running_loop().time() + timeout
    while not wire_state.model_output_started:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is not None:
            return outcome
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        chunk = await asyncio.wait_for(
            response.content.read(_STREAM_CHUNK_BYTES),
            timeout=remaining,
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
        await _received(chunk, prelude=prelude, wire_state=wire_state)
    return None


async def _received(
    chunk: bytes,
    *,
    prelude: _StreamPrelude,
    wire_state: ProtocolSSEState,
) -> None:
    """Observe delivered bytes, then retain their exact replay without truncation."""

    await wire_state.observe_async(chunk)
    await prelude.write_async(chunk)


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
                await wire_state.observe_async(chunk)
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
        projected_types = tuple(
            code
            for value in observation.error_type_candidates
            if (code := _safe_error_code(value)) is not None
        )
        projected_codes = tuple(
            code
            for value in observation.error_code_candidates
            if (code := _safe_error_code(value)) is not None
        )
        if projected_types or projected_codes:
            error_type = projected_types[0] if projected_types else None
            error_code = projected_codes[0] if projected_codes else None
            candidates = tuple(dict.fromkeys((*projected_types, *projected_codes)))
        else:
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


def buffered_prelude_handle(
    prelude: _StreamPrelude,
    outcome: RawCallOutcome,
    observed: ProtocolSSEState | None,
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
    return _project_raw_error_fields(io.BytesIO(payload), envelope_paths)


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str) or value not in UPSTREAM_MACHINE_ERROR_CODES:
        return None
    return value
