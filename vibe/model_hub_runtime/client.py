from __future__ import annotations

import asyncio
import errno
import io
import json
import logging
import re
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, AsyncIterator, BinaryIO, Literal, Mapping, TypeVar, cast

import aiohttp
import ijson

from config.v2_config import normalize_model_hub_base_url
from core.handlers.model_hub.adapter import (
    CLAUDE_UNIFIED_RATE_LIMIT_HEADER_NAMES,
    DiscoveredModel,
    ENGINE_TRANSPORT_TIMEOUT_SECONDS,
    InvokeCancelledError,
    RawCallOutcome,
    RawOutcomeKind,
    RESPONSE_CORRELATION_HEADER_NAMES,
    UpstreamRejection,
    sanitize_response_correlation_ids,
)
from core.handlers.model_hub.async_owner import run_owned_in_thread
from core.handlers.model_hub.classification import UPSTREAM_MACHINE_ERROR_CODES
from core.handlers.model_hub.events import bounded_diagnostic_detail
from core.handlers.model_hub.hosted_images import IMAGE_RESPONSE_MAX_BYTES
from core.handlers.model_hub.json_wire import (
    JSONEvent,
    JSONPath,
    JSONScope,
    project_json_reader,
)
from core.handlers.model_hub.request import FORWARDED_CALLER_HEADERS
from core.os_errors import local_os_errno
from core.handlers.model_hub.stream_wire import (
    ErrorEnvelopePath,
    ProtocolObservation,
    ProtocolSSEState,
    ProtocolUsageReport,
    observe_buffered_protocol_response,
)
from vibe.model_hub_runtime.api_key_vendors import (
    anthropic_api_key_header,
    google_api_base_url,
    official_api_key_base_urls,
    validate_api_key_auth_scheme,
)
from vibe.model_hub_runtime.state import SourceRecord


_STREAM_CHUNK_BYTES = 64 * 1024
# The engine answers a failed upstream connection itself, before any upstream
# response, with exactly its own generic envelope and Go's transport error text.
# That is a network fact, not an upstream server verdict.
_ENGINE_TRANSPORT_FAILURE = re.compile(
    r"\b(?:dial|read|write) (?:tcp|udp)\b|i/o timeout|connection reset by peer|"
    r"connection refused|broken pipe|\bunexpected EOF\b|: EOF$|TLS handshake timeout|"
    r"Client\.Timeout exceeded|context deadline exceeded|timeout awaiting response headers|"
    r"http2: (?:client connection lost|server sent GOAWAY)|server closed idle connection|"
    r"no such host|network is unreachable|proxyconnect"
)
# This threshold only selects memory or a temporary file; it never rejects or
# truncates upstream response bytes.
_PRELUDE_MEMORY_BYTES = 256 * 1024
# A buffered answer is spooled whole before it is read. An image answer, served or refused, is bounded while it
# downloads, so a relay that never stops cannot fill the disk before the image call rejects it
# (vibey-hosted-tools.md §7.4).
_BUFFERED_RESPONSE_MAX_BYTES: Mapping[str, int] = {"openai_images": IMAGE_RESPONSE_MAX_BYTES}
# The machine code of an answer cut at that bound; L1's own, never an upstream's.
RESPONSE_TOO_LARGE = "response_too_large"
_ERROR_OBSERVATION_BYTES = 256 * 1024
# Projecting a response body that has already arrived is local work. It has
# its own budget so the client's connection budget never limits it.
_LOCAL_PROJECTION_TIMEOUT_SECONDS = ENGINE_TRANSPORT_TIMEOUT_SECONDS
# A streamed response that sends the gateway no byte for this long is stalled.
# The engine sets no timeout once its upstream is connected, so the gateway,
# its downstream client, bounds it. Only (Source vendor, protocol) pairs whose
# untranslated streams were measured to keep sending in every phase are
# bounded: Anthropic sends a ping every 10 s, even while a relay buffers a tool
# call's input, and OpenAI's Responses events never paused for more than 36 s,
# including before the first event of a cold 250K-token prompt. The engine
# holds SSE comments and its translators drop heartbeats, so an xAI tool call
# or a translated stream can be silent for minutes while healthy
# (docs/plans/model-hub-inference-deadlines.md, "Stream idle bound").
_STREAM_IDLE_TIMEOUT_SECONDS = 120.0
_STREAM_IDLE_BOUNDED_SOURCES = frozenset({("anthropic", "anthropic"), ("openai", "openai_responses")})
_OFFICIAL_BASE_URLS = official_api_key_base_urls()
logger = logging.getLogger(__name__)
_ProjectedJSON = TypeVar("_ProjectedJSON")

_ENGINE_HEADER_VALUE_MAX_LENGTH = 128
_RETRY_AFTER_MS_MAX = 365 * 24 * 60 * 60 * 1000
_CLAUDE_RATE_LIMIT_STATUS_VALUES = frozenset({"allowed", "allowed_warning", "rejected"})
_CLAUDE_RATE_LIMIT_STATUS_HEADERS = frozenset({
    "anthropic-ratelimit-unified-status",
    "anthropic-ratelimit-unified-5h-status",
    "anthropic-ratelimit-unified-7d-status",
    "anthropic-ratelimit-unified-7d_oi-status",
    "anthropic-ratelimit-unified-overage-status",
})
_CLAUDE_RATE_LIMIT_RESET_HEADERS = frozenset({
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-reset",
    "anthropic-ratelimit-unified-7d_oi-reset",
    "anthropic-ratelimit-unified-reset",
    "anthropic-ratelimit-unified-overage-reset",
})
_CLAUDE_RATE_LIMIT_UTILIZATION_HEADERS = frozenset({
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-7d-utilization",
})

# The engine forwards an upstream's response headers (``passthrough-headers``),
# minus hop-by-hop ones, and on errors it filters nothing. Avibe reads only these
# allowlisted names and builds every gateway response from its own headers, so no
# other upstream header (``set-cookie``, organization ids, rate-limit details)
# reaches Vibey, the Web UI or a gateway client. Correlation values are projected
# again by ``sanitize_response_correlation_ids`` before they cross the outcome
# boundary.
ENGINE_RESPONSE_HEADER_ALLOW_LIST: frozenset[str] = frozenset(
    {
        "retry-after",
        "retry-after-ms",
        *RESPONSE_CORRELATION_HEADER_NAMES,
        *CLAUDE_UNIFIED_RATE_LIMIT_HEADER_NAMES,
    }
)


def _allowed_engine_header(headers: Mapping[str, str], name: str) -> str | None:
    """Read one exact allowlisted engine header case-insensitively."""

    normalized_name = name.lower()
    if normalized_name not in ENGINE_RESPONSE_HEADER_ALLOW_LIST:
        return None
    for header_name, value in headers.items():
        if isinstance(header_name, str) and header_name.lower() == normalized_name:
            return value if isinstance(value, str) else None
    return None


def _bounded_ascii_header_value(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _ENGINE_HEADER_VALUE_MAX_LENGTH
        or not value.isascii()
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        return None
    return value.strip() or None


def _retry_after_ms_value(headers: Mapping[str, str]) -> int | None:
    raw = _bounded_ascii_header_value(_allowed_engine_header(headers, "Retry-After-Ms"))
    if raw is None or not raw.isdecimal():
        return None
    try:
        milliseconds = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return milliseconds if milliseconds <= _RETRY_AFTER_MS_MAX else None


def _normalize_claude_rate_limit_header(name: str, value: object) -> str | None:
    normalized = _bounded_ascii_header_value(value)
    if normalized is None:
        return None
    if name in _CLAUDE_RATE_LIMIT_STATUS_HEADERS:
        normalized = normalized.lower()
        return normalized if normalized in _CLAUDE_RATE_LIMIT_STATUS_VALUES else None
    if name in _CLAUDE_RATE_LIMIT_RESET_HEADERS:
        try:
            numeric = Decimal(normalized)
            if numeric.is_finite() and numeric > 0:
                return normalized
        except (ArithmeticError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return normalized
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(normalized)
            if parsed.tzinfo is not None:
                return normalized
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
        return None
    if name in _CLAUDE_RATE_LIMIT_UTILIZATION_HEADERS:
        try:
            numeric = Decimal(normalized)
        except (ArithmeticError, ValueError):
            return None
        return normalized if numeric.is_finite() and numeric >= 0 else None
    return normalized


def _allowed_claude_rate_limit_headers(
    headers: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Project only validated, exact Claude unified rate-limit fields."""

    retained: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.lower()
        if name not in CLAUDE_UNIFIED_RATE_LIMIT_HEADER_NAMES or name in retained:
            continue
        value = _normalize_claude_rate_limit_header(name, raw_value)
        if value is not None:
            retained[name] = value
    return tuple(sorted(retained.items()))


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
        os_errno: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.error_code = error_code
        self.error_candidates = error_candidates
        self.os_errno = os_errno


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


@dataclass(frozen=True)
class EngineHealthFailure:
    path: str
    reason: Literal["timeout", "http_error", "invalid_response", "unavailable"]
    elapsed_seconds: float
    http_status: int | None = None
    os_errno: int | None = None


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
        self.health_failure: EngineHealthFailure | None = None

    def health(self) -> bool:
        self.health_failure = None
        for path, projector, headers in (
            ("/v1/models", _project_models_health, {"Authorization": f"Bearer {self.connection.gateway_token}"}),
            ("/v0/management/config", _project_root_map, {"X-Management-Key": self.connection.management_key}),
        ):
            started_at = time.monotonic()
            try:
                valid = self._request_json_projection(
                    "GET", path, projector, headers=headers, timeout=min(self.timeout, 1.0),
                )
            except EngineClientError as exc:
                reason = (
                    "http_error" if exc.status_code is not None
                    else "timeout" if exc.error_type == "TimeoutError"
                    else "invalid_response" if exc.error_type == "invalid_json"
                    else "unavailable"
                )
                self.health_failure = EngineHealthFailure(
                    path, reason, time.monotonic() - started_at, exc.status_code,
                    os_errno=local_os_errno(exc),
                )
                return False
            if not valid:
                self.health_failure = EngineHealthFailure(
                    path, "invalid_response", time.monotonic() - started_at,
                )
                return False
        return True

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

    def put_config_yaml(self, text: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Replace the running engine config through its hot-reload endpoint."""
        return self._request_json_projection(
            "PUT",
            "/v0/management/config.yaml",
            _load_json_object,
            data=text.encode(),
            content_type="application/yaml",
            headers={"X-Management-Key": self.connection.management_key},
            timeout=timeout,
        )

    def list_model_ids(self, *, timeout: float | None = None) -> frozenset[str]:
        payload = self._request_json(
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {self.connection.gateway_token}"},
            timeout=timeout,
        )
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise EngineClientError("engine API returned an invalid payload", error_type="invalid_json")
        return frozenset(
            str(entry["id"]) for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        )

    def list_model_names(self, *, timeout: float | None = None) -> dict[str, str]:
        """Routed model ID to registered display name.

        CPA's default OpenAI listing drops display names; its Grok-client
        listing keeps the routed ID and names every provider's registration.
        """
        payload = self._request_json(
            "GET",
            "/v1/models",
            headers={
                "Authorization": f"Bearer {self.connection.gateway_token}",
                "User-Agent": "grok-shell",
            },
            timeout=timeout,
        )
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise EngineClientError("engine API returned an invalid payload", error_type="invalid_json")
        return {
            str(entry["id"]): str(entry.get("name") or "")
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        }

    async def invoke(
        self,
        source: SourceRecord,
        model_id: str,
        request: Mapping[str, Any],
        *,
        stream: bool,
        request_protocol: str | None = None,
        request_headers: Mapping[str, str] | None = None,
        on_transport_done: Callable[[], None] | None = None,
        on_request_sent: Callable[[], None] | None = None,
        silence_bound_s: float | None = None,
    ) -> EngineInvokeHandle:
        """Send one request to the engine.

        ``silence_bound_s`` is the byte-silence bound a caller hands over for a
        stream the measured Source table leaves unbounded; ``None`` leaves it so.
        """

        request_protocol = request_protocol or source.protocol
        body = dict(request)
        routed_model = f"{source.prefix}/{model_id}"
        if request_protocol == "google":
            # CPA (MIT), sdk/api/handlers/gemini/gemini_handlers.go at
            # c404af96: model/action live in the path; alt=sse selects framing.
            action = "streamGenerateContent" if stream else "generateContent"
            endpoint = (
                f"/v1beta/models/{urllib.parse.quote(source.prefix, safe='')}/"
                f"{urllib.parse.quote(model_id, safe='')}:{action}"
            )
            if stream:
                endpoint += "?alt=sse"
            body.pop("model", None)
            body.pop("stream", None)
        elif request_protocol == "openai_images":
            # Reference images make an edit (vibey-hosted-tools.md §7.4). An images call is
            # always buffered, and names no stream: not every images endpoint reads one.
            endpoint = "/v1/images/edits" if "images" in body else "/v1/images/generations"
            body["model"] = routed_model
        else:
            endpoint = _endpoint_for_protocol(request_protocol)
            body["model"] = routed_model
            body["stream"] = stream
        headers = {
            key.lower(): value for key, value in (request_headers or {}).items() if key.lower() in FORWARDED_CALLER_HEADERS
        }
        headers.update(
            {
                "Authorization": f"Bearer {self.connection.gateway_token}",
                "Content-Type": "application/json",
            }
        )
        if request_protocol == "anthropic":
            headers.setdefault("anthropic-version", "2023-06-01")

        idle_bound: float | None = None
        if stream:
            measured = (
                request_protocol == source.protocol
                and (source.vendor, source.protocol) in _STREAM_IDLE_BOUNDED_SOURCES
            )
            # A caller that holds no bound of its own before model output hands
            # it over: until then the gateway withholds the whole response,
            # heartbeats included, so only this read sees the upstream's bytes.
            idle_bound = _STREAM_IDLE_TIMEOUT_SECONDS if measured else silence_bound_s
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=self.timeout,
            sock_connect=self.timeout,
            sock_read=idle_bound,
        )
        # Connecting to the local engine is bounded. Once connected, inference
        # can take any duration; completion, transport failure, or owner
        # cancellation ends it. A stream whose upstream keeps sending must keep
        # sending, though: from the request on, every byte restarts its idle
        # bound, and one that runs out closes the engine connection, which
        # cancels the upstream request. A buffered response sends nothing until
        # it is complete, so it has none.
        bound = _BUFFERED_RESPONSE_MAX_BYTES.get(request_protocol)
        # A body can carry megabytes of images: encoded off the controller loop.
        encoded = await run_owned_in_thread(lambda: json.dumps(body).encode("utf-8"))
        trace_configs: list[aiohttp.TraceConfig] = []
        if on_request_sent is not None:
            # CPA resolves the route as soon as it reads the body, while a
            # config write only applies after its 150ms reload debounce, so a
            # body written to the loopback socket precedes any route removal.
            # CPA sends no earlier acknowledgment: response headers wait for
            # the upstream's first chunk, which a save must never wait on.
            trace = aiohttp.TraceConfig()

            async def request_sent(*_args: Any) -> None:
                on_request_sent()

            trace.on_request_chunk_sent.append(request_sent)
            trace_configs.append(trace)
        session = aiohttp.ClientSession(timeout=timeout, trust_env=False, trace_configs=trace_configs)
        response: aiohttp.ClientResponse | None = None
        retry_after: str | None = None
        retry_after_ms: int | None = None
        response_received_at: datetime | None = None
        response_correlation_ids: tuple[tuple[str, str], ...] = ()
        claude_rate_limit_headers: tuple[tuple[str, str], ...] = ()
        first_received = False
        model_output_started = False
        ownership_transferred = False
        transport_transferred = False
        prelude: _StreamPrelude | None = None
        # Held here rather than inside the prelude reader so every way out of this
        # call can still see what the wire already reported. A stream that reports
        # usage before its first model output — Anthropic's `message_start` does —
        # and then fails has already been billed for those input tokens.
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
            if wire_state is not None and wire_state.model_output_started:
                outcome = replace(outcome, recovery_verified=True)
            if response_correlation_ids:
                outcome = replace(outcome, response_correlation_ids=response_correlation_ids)
            if response_received_at is not None:
                outcome = replace(
                    outcome,
                    retry_after=retry_after,
                    retry_after_ms=retry_after_ms,
                    response_received_at=response_received_at,
                    claude_rate_limit_headers=claude_rate_limit_headers,
                )
            return completed_handle(outcome)

        def cut(status: int) -> EngineInvokeHandle:
            # A local cut, like a transport failure: nothing past the bound was read or observed.
            return ended(_outcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                source=source,
                model_id=model_id,
                http_status=status,
                error_code=RESPONSE_TOO_LARGE,
                message="upstream response exceeded its bound",
            ))

        try:
            response = await session.post(
                self._url(endpoint),
                data=encoded,
                headers=headers,
                allow_redirects=False,
            )
            response_correlation_ids = sanitize_response_correlation_ids(response.headers)
            if response.status >= 300:
                response_received_at = datetime.now(timezone.utc)
                retry_after = _allowed_engine_header(response.headers, "Retry-After")
                retry_after_ms = _retry_after_ms_value(response.headers)
                if retry_after is not None and (
                    len(retry_after) > _ENGINE_HEADER_VALUE_MAX_LENGTH or not retry_after.isascii()
                ):
                    retry_after = None
                if request_protocol == "anthropic":
                    claude_rate_limit_headers = _allowed_claude_rate_limit_headers(response.headers)
                error_body = _StreamPrelude()
                try:
                    await asyncio.wait_for(
                        _read_response_into(response.content, error_body, max_bytes=bound),
                        timeout=self.timeout,
                    )
                    observed_payload = await _observe_buffered_protocol_response_async(
                        observe_buffered_protocol_response,
                        request_protocol,
                        error_body,
                        machine_error_codes=UPSTREAM_MACHINE_ERROR_CODES,
                        deadline=time.monotonic() + _LOCAL_PROJECTION_TIMEOUT_SECONDS,
                    )
                    payload = await error_body.prefix_async(_ERROR_OBSERVATION_BYTES)
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    payload = b""
                    observed_payload = ProtocolObservation()
                except _ResponseTooLarge:
                    response.close()
                    await session.close()
                    return cut(response.status)
                finally:
                    error_body.close()
                if retry_after is None:
                    retry_after = _upstream_reset_advice(
                        payload, observed_payload.error_envelope_paths or (("error",),),
                    )
                if _is_local_model_registration_failure(
                    response.status,
                    payload,
                    routed_model=routed_model,
                ):
                    outcome = _outcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        source=source,
                        model_id=model_id,
                        http_status=response.status,
                        error_code="engine_down",
                        message="engine has not registered the routed model",
                    )
                    response.close()
                    await session.close()
                    return ended(outcome)
                if response.status == 401 and await self._engine_refuses_gateway_key(session):
                    # The engine itself refused Avibe's key, so no upstream judged
                    # the Source's credential and nothing was sent past the engine.
                    response.close()
                    await session.close()
                    return ended(_outcome(
                        kind=RawOutcomeKind.NETWORK_ERROR,
                        source=source,
                        model_id=model_id,
                        http_status=response.status,
                        error_code="engine_down",
                        message="engine refused Avibe's gateway key",
                    ))
                outcome = _reduce_protocol_observation(
                    replace(
                        observed_payload,
                        outcome="failed_terminal",
                        error_payload=payload,
                        message=f"upstream returned HTTP {response.status}",
                        recovery_verified=False,
                    ),
                    source=source,
                    model_id=model_id,
                    http_status=response.status,
                    stream_started=False,
                    response_correlation_ids=response_correlation_ids,
                )
                assert outcome is not None
                response.close()
                await session.close()
                return ended(outcome)

            first = await response.content.read(_STREAM_CHUNK_BYTES)
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
                await buffered_body.write_async(first)
                try:
                    await _read_response_into(
                        response.content, buffered_body, max_bytes=None if bound is None else bound - len(first),
                    )
                except _ResponseTooLarge:
                    buffered_body.close()
                    response.close()
                    await session.close()
                    return cut(response.status)
                observation = await _observe_buffered_protocol_response_async(
                    observe_buffered_protocol_response,
                    request_protocol,
                    buffered_body,
                    machine_error_codes=UPSTREAM_MACHINE_ERROR_CODES,
                    deadline=time.monotonic() + _LOCAL_PROJECTION_TIMEOUT_SECONDS,
                )
                outcome = _reduce_protocol_observation(
                    observation,
                    source=source,
                    model_id=model_id,
                    http_status=response.status,
                    stream_started=observation.outcome == "served",
                    response_correlation_ids=response_correlation_ids,
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
                response_correlation_ids=response_correlation_ids,
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
                on_transport_done=on_transport_done,
                response_correlation_ids=response_correlation_ids,
            )

            async def close_response_stream() -> None:
                prelude.close()
                response.close()
                try:
                    await session.close()
                finally:
                    if on_transport_done is not None:
                        on_transport_done()
                outcome = _observed_stream_terminal_outcome(
                    wire_state,
                    source,
                    model_id,
                    response.status,
                )
                if outcome is not None:
                    outcome = replace(outcome, response_correlation_ids=response_correlation_ids)
                if outcome is not None and not outcome_future.done():
                    outcome_future.set_result(outcome)

            handle = EngineInvokeHandle(
                stream=response_stream,
                outcome=outcome_future,
                stream_closer=close_response_stream,
                observed=wire_state,
            )
            ownership_transferred = True
            transport_transferred = True
            return handle
        except asyncio.CancelledError as cancelled:
            if wire_state is not None:
                raise InvokeCancelledError(wire_state) from cancelled
            raise
        except asyncio.TimeoutError as exc:
            logger.warning("Model Hub engine request for source %s timed out: %s", source.source_id, exc)
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
                    os_errno=local_os_errno(exc),
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
            try:
                if not ownership_transferred:
                    if prelude is not None:
                        prelude.close()
                    if response is not None:
                        response.close()
                    await session.close()
            finally:
                if not transport_transferred and on_transport_done is not None:
                    on_transport_done()

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
        data: bytes | None = None,
        content_type: str = "application/json",
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> _ProjectedJSON:
        url = self._url(path, query=query)
        request_timeout = timeout or self.timeout
        deadline = time.monotonic() + request_timeout
        if payload is not None:
            data = json.dumps(payload, separators=(",", ":")).encode()
        request_headers = dict(headers or {})
        if data is not None:
            request_headers["Content-Type"] = content_type
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
                    os_errno=local_os_errno(read_error),
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
            raise EngineClientError(
                "engine API is unavailable", error_type=type(exc).__name__,
                os_errno=local_os_errno(exc),
            ) from None

    async def _engine_refuses_gateway_key(self, session: aiohttp.ClientSession) -> bool:
        """Whether the engine itself refuses Avibe's gateway key (#2755).

        A 401 body cannot tell the engine's refusal from an upstream's: the engine
        passes an upstream's JSON error body through unchanged, so a relay
        answering ``{"error":"Invalid API key"}`` reads exactly like the engine's
        own access check. ``/v1/models`` is answered by the engine alone, so the
        same key refused there is the engine's refusal. A witness that cannot be
        asked proves nothing, and the 401 stays the upstream's.
        """

        try:
            async with session.get(
                self._url("/v1/models"),
                headers={"Authorization": f"Bearer {self.connection.gateway_token}"},
                timeout=aiohttp.ClientTimeout(total=min(self.timeout, 2.0)),
                allow_redirects=False,
            ) as witness:
                return witness.status == 401
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return False

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
    secret: str | None,
    auth_scheme: str | None = None,
    timeout: float = 15.0,
) -> tuple[DiscoveredModel, ...]:
    """Probe the one allowlisted models path without redirecting credentials.

    ``secret=None`` sends no credential at all. It is not an altered or
    fabricated one -- there is no token to guess wrong and no other valid key to
    collide with -- so what it answers is unambiguous: a listing that still
    arrives belongs to anyone who asks and therefore attests to no credential.
    """
    normalized_vendor = vendor.strip().lower()
    validate_api_key_auth_scheme(normalized_vendor, protocol, base_url, secret, auth_scheme)
    root = base_url or _OFFICIAL_BASE_URLS.get(normalized_vendor)
    if not root:
        raise EngineClientError("source requires a base URL for model discovery")
    if protocol == "google":
        return await _probe_google_models(root, secret=secret, timeout=timeout)
    try:
        url = upstream_api_url(root, "/v1/models")
        credential = {} if secret is None else (
            anthropic_api_key_header(root, secret) if protocol == "anthropic" else {"Authorization": f"Bearer {secret}"}
        )
    except (TypeError, ValueError):
        raise EngineClientError("source base URL is invalid")
    assert url is not None
    headers = {"Accept": "application/json", **credential}
    if protocol == "anthropic":
        # The version header is part of the interface, not the credential, so it
        # stays on the uncredentialed request: it must differ in exactly one way.
        headers["anthropic-version"] = "2023-06-01"
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


def _project_google_inventory(reader: BinaryIO) -> tuple[tuple[DiscoveredModel, ...], str | None]:
    """Project routing identities and the continuation token, never model limits."""
    names: dict[JSONScope, str] = {}
    objects: set[JSONScope] = set()
    valid_root = False
    valid_models = False
    page_token: str | None = None
    valid_token = True

    def visit(path: JSONPath, event: JSONEvent, value: object, scope: JSONScope) -> None:
        nonlocal valid_root, valid_models, page_token, valid_token
        if path == () and event == "start_map":
            valid_root = True
        elif path == ("models",):
            if event == "replace":
                names.clear()
                objects.clear()
                valid_models = False
            elif event != "nonempty":
                valid_models = event == "start_array"
        elif path == ("models", "*"):
            if event == "replace":
                names.pop(scope, None)
            elif event in {"start_map", "scalar", "start_array"}:
                objects.add(scope)
        elif path == ("models", "*", "name"):
            if event == "replace":
                names.pop(scope, None)
            elif event == "scalar" and isinstance(value, str) and value.startswith("models/") and value[7:]:
                names[scope] = value[7:]
        elif path == ("nextPageToken",):
            if event == "replace":
                page_token, valid_token = None, True
            elif event != "nonempty":
                valid_token = event == "scalar" and isinstance(value, str)
                page_token = (value or None) if valid_token else None

    parsed = project_json_reader(
        reader, {(), ("models",), ("models", "*"), ("models", "*", "name"), ("nextPageToken",)}, visit,
        lossless_string_paths={("models", "*", "name"), ("nextPageToken",)},
    )
    if not parsed or not valid_root or not valid_models or not valid_token or objects != names.keys():
        raise EngineClientError("model discovery returned an invalid Google payload")
    return tuple(DiscoveredModel(id=name) for name in dict.fromkeys(names.values())), page_token


async def _probe_google_models(root: str, *, secret: str | None, timeout: float) -> tuple[DiscoveredModel, ...]:
    """Follow only page tokens on the same declared, credential-gated endpoint."""
    try:
        url = google_api_base_url(root) + "/v1beta/models"
    except (TypeError, ValueError):
        raise EngineClientError("source base URL is invalid") from None
    headers = {"Accept": "application/json"}
    if secret is not None:
        headers["x-goog-api-key"] = secret
    deadline = time.monotonic() + timeout
    models: dict[str, DiscoveredModel] = {}
    seen_tokens: set[str] = set()
    page_token: str | None = None
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                async with session.get(
                    url, headers=headers, params={"pageToken": page_token} if page_token else {},
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=remaining),
                ) as response:
                    if response.status >= 300:
                        raise EngineClientError(
                            f"model discovery returned HTTP {response.status}", status_code=response.status,
                        )
                    with tempfile.SpooledTemporaryFile(max_size=_PRELUDE_MEMORY_BYTES) as body:
                        while chunk := await response.content.read(_STREAM_CHUNK_BYTES):
                            await run_owned_in_thread(body.write, chunk)
                        await run_owned_in_thread(body.seek, 0)
                        page, page_token = await run_owned_in_thread(
                            _project_before_deadline, body, _project_google_inventory, deadline=deadline,
                        )
                models.update((model.id, model) for model in page)
                if page_token is None:
                    return tuple(models.values())
                if page_token in seen_tokens:
                    raise EngineClientError("model discovery repeated a Google page token")
                seen_tokens.add(page_token)
    except asyncio.TimeoutError:
        raise EngineClientError("model discovery timed out", error_type="timeout") from None
    except (aiohttp.ClientError, OSError):
        raise EngineClientError("model discovery failed", error_type="network_error") from None


def _project_model_inventory(
    reader: BinaryIO,
) -> tuple[
    bool,
    bool,
    list[DiscoveredModel],
    bool,
    bool,
    list[DiscoveredModel],
] | None:
    paths = {
        (),
        ("data",),
        ("data", "*"),
        ("data", "*", "id"),
        ("data", "*", "supported_parameters"),
        ("data", "*", "supported_parameters", "*"),
        ("models",),
        ("models", "*"),
        ("models", "*", "id"),
        ("models", "*", "supported_parameters"),
        ("models", "*", "supported_parameters", "*"),
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
    data_supported_seen: set[JSONScope] = set()
    fallback_supported_seen: set[JSONScope] = set()
    data_supported_arrays: set[JSONScope] = set()
    fallback_supported_arrays: set[JSONScope] = set()
    data_supported: dict[JSONScope, dict[int, str]] = {}
    fallback_supported: dict[JSONScope, dict[int, str]] = {}
    invalid_data_supported: set[JSONScope] = set()
    invalid_fallback_supported: set[JSONScope] = set()

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
                data_supported_seen.clear()
                data_supported_arrays.clear()
                data_supported.clear()
                invalid_data_supported.clear()
            data_seen = True
            if event != "nonempty":
                data_is_array = event == "start_array"
        elif path == ("models",):
            if event == "replace":
                fallback_models.clear()
                invalid_fallback_models.clear()
                fallback_supported_seen.clear()
                fallback_supported_arrays.clear()
                fallback_supported.clear()
                invalid_fallback_supported.clear()
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

        elif path in {
            ("data", "*", "supported_parameters"),
            ("models", "*", "supported_parameters"),
        }:
            seen = data_supported_seen if path[0] == "data" else fallback_supported_seen
            arrays = data_supported_arrays if path[0] == "data" else fallback_supported_arrays
            values = data_supported if path[0] == "data" else fallback_supported
            invalid = invalid_data_supported if path[0] == "data" else invalid_fallback_supported
            if event == "replace":
                values.pop(scope, None)
                invalid.discard(scope)
                arrays.discard(scope)
            seen.add(scope)
            if event == "start_array":
                arrays.add(scope)
            elif event not in {"replace", "nonempty"}:
                invalid.add(scope)
        elif path in {
            ("data", "*", "supported_parameters", "*"),
            ("models", "*", "supported_parameters", "*"),
        }:
            model_scope = scope[:-1]
            values = data_supported if path[0] == "data" else fallback_supported
            invalid = invalid_data_supported if path[0] == "data" else invalid_fallback_supported
            if event == "scalar" and isinstance(value, str) and value:
                values.setdefault(model_scope, {})[scope[-1]] = value
            elif event in {"scalar", "elided_string", "start_map", "start_array"}:
                invalid.add(model_scope)

    def ordered_unique(
        values: Mapping[JSONScope, str],
        supported_seen: set[JSONScope],
        supported_arrays: set[JSONScope],
        supported: Mapping[JSONScope, Mapping[int, str]],
        invalid_supported: set[JSONScope],
    ) -> list[DiscoveredModel]:
        result: list[DiscoveredModel] = []
        seen: set[str] = set()
        for scope in sorted(values):
            value = values[scope]
            if value in seen:
                continue
            seen.add(value)
            parameters = None
            if (
                scope in supported_seen
                and scope in supported_arrays
                and scope not in invalid_supported
            ):
                parameters = tuple(
                    parameter
                    for _index, parameter in sorted(supported.get(scope, {}).items())
                )
                parameters = tuple(dict.fromkeys(parameters))
            result.append(
                DiscoveredModel(
                    id=value,
                    supported_parameters=parameters,
                )
            )
        return result

    # IDs select routes; supported parameters feed the reasoning-tier consumer.
    # Neither is a diagnostic copy that can be elided without changing behavior.
    lossless_paths = {
        (member, *suffix)
        for member in ("data", "models")
        for suffix in (("*",), ("*", "id"), ("*", "supported_parameters", "*"))
    }
    if not project_json_reader(
        reader, paths, visit, lossless_string_paths=lossless_paths
    ) or not root_is_map:
        return None
    if (data_seen and invalid_data_models) or (
        not data_seen and fallback_seen and invalid_fallback_models
    ):
        return None
    return (
        data_seen,
        data_is_array,
        ordered_unique(
            data_models,
            data_supported_seen,
            data_supported_arrays,
            data_supported,
            invalid_data_supported,
        ),
        fallback_seen,
        fallback_is_array,
        ordered_unique(
            fallback_models,
            fallback_supported_seen,
            fallback_supported_arrays,
            fallback_supported,
            invalid_fallback_supported,
        ),
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
) -> tuple[
    bool,
    bool,
    list[DiscoveredModel],
    bool,
    bool,
    list[DiscoveredModel],
] | None:
    payload.seek(0)
    return _project_before_deadline(
        payload,
        _project_model_inventory,
        deadline=deadline,
    )


def engine_disowned_call(outcome: RawCallOutcome) -> bool:
    """Whether a call ended on the engine itself showing it may not run Avibe's config.

    It refused Avibe's gateway key (a 401 the client witnessed), did not know
    the routed model Avibe registered (the local registration failure), or
    refused the connection on the port Avibe gave it. The owner checks the
    engine before the next call instead of trusting its last health check.
    """

    if outcome.kind is not RawOutcomeKind.NETWORK_ERROR:
        return False
    return (outcome.error_code == "engine_down" and outcome.http_status in (400, 401)) or (
        outcome.os_errno == errno.ECONNREFUSED
    )


def _is_local_model_registration_failure(
    status: int,
    payload: bytes,
    *,
    routed_model: str,
) -> bool:
    """Identify CLIProxyAPI's local pre-egress model registry rejection.

    CLIProxyAPI (sdk/api/handlers/handlers_routing.go at c404af96 and in v8)
    answers a model it has not registered with 400
    ``{"error":{"message":"unknown provider for model <prefix>/<model>",
    "type":"invalid_request_error","code":"model_not_found","param":"model"}}``;
    its Claude surface rebuilds that as ``{"type":"error","error":{"type":
    "invalid_request_error","message":...}}``. The routed model is Avibe's own
    ``<prefix>/<model>``, so the exact message names a local registration gap,
    not an upstream rejection.
    """

    if status != 400:
        return False
    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(decoded, dict) or ("type" in decoded and decoded["type"] != "error"):
        return False
    error = decoded.get("error")
    error_type, _, _ = _raw_error_fields(payload)
    message = error.get("message") if isinstance(error, dict) else None
    return (
        error_type == "invalid_request_error"
        and isinstance(message, str)
        and (
            message == f"unknown provider for model {routed_model}"
            # Its images handler refuses, before any upstream, a model it does not route
            # there (sdk/api/handlers/openai/openai_images_handlers.go at d318bcc3).
            or message.startswith(
                f"Model {routed_model} is not supported on /v1/images/generations or /v1/images/edits."
            )
        )
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


class _ResponseTooLarge(Exception):
    """A response grew past the bound its request protocol sets."""


async def _read_response_into(
    content: aiohttp.StreamReader,
    target: _StreamPrelude,
    *,
    max_bytes: int | None = None,
) -> None:
    received = 0
    while chunk := await content.read(_STREAM_CHUNK_BYTES):
        received += len(chunk)
        if max_bytes is not None and received > max_bytes:
            # Stop reading: what is past the bound is never written anywhere.
            raise _ResponseTooLarge
        await target.write_async(chunk)


async def _read_stream_prelude(
    *,
    response: aiohttp.ClientResponse,
    first: bytes,
    prelude: _StreamPrelude,
    wire_state: ProtocolSSEState,
    source: SourceRecord,
    model_id: str,
    response_correlation_ids: tuple[tuple[str, str], ...] = (),
) -> RawCallOutcome | None:
    """Buffer transport metadata until the sole first-model-output fact.

    The tracker belongs to the caller: this coroutine can also end by raising a
    transport error, and what the wire reported before that has to survive the
    raise for the call to be metered.
    """

    await _received(first, prelude=prelude, wire_state=wire_state)
    while not wire_state.model_output_started:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is not None:
            return replace(outcome, response_correlation_ids=response_correlation_ids)
        chunk = await response.content.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            completion = _observed_stream_terminal_outcome(
                wire_state,
                source,
                model_id,
                response.status,
            )
            if completion is not None:
                return replace(completion, response_correlation_ids=response_correlation_ids)
            return _prelude_ended_outcome(
                source, model_id, response.status, response_correlation_ids=response_correlation_ids,
            )
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
    response_correlation_ids: tuple[tuple[str, str], ...] = (),
) -> RawCallOutcome:
    return _outcome(
        kind=RawOutcomeKind.NETWORK_ERROR,
        source=source,
        model_id=model_id,
        http_status=http_status,
        message="upstream stream ended before model output",
        response_correlation_ids=response_correlation_ids,
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
    response_correlation_ids: tuple[tuple[str, str], ...] = (),
    on_transport_done: Callable[[], None] | None = None,
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
    except asyncio.TimeoutError as exc:
        outcome = _observed_stream_terminal_outcome(
            wire_state,
            source,
            model_id,
            response.status,
        )
        if outcome is None:
            logger.warning(
                "Model Hub engine stream for source %s timed out after model output: %s", source.source_id, exc,
            )
            outcome = _outcome(
                kind=RawOutcomeKind.TIMEOUT,
                source=source,
                model_id=model_id,
                http_status=response.status,
                message="upstream response timed out after streaming started",
                stream_started=wire_state.model_output_started,
            )
    except (aiohttp.ClientError, OSError) as exc:
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
                os_errno=local_os_errno(exc),
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
        if outcome is not None:
            outcome = replace(outcome, response_correlation_ids=response_correlation_ids)
        prelude.close()
        response.close()
        try:
            await session.close()
        finally:
            if on_transport_done is not None:
                on_transport_done()
        if outcome is not None and not outcome_future.done():
            if wire_state.model_output_started:
                outcome = replace(outcome, recovery_verified=True)
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
    response_correlation_ids: tuple[tuple[str, str], ...] = (),
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
            response_correlation_ids=response_correlation_ids,
            usage=observation.usage,
            recovery_verified=observation.recovery_verified,
            hosted_tools=observation.hosted_tools,
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
        upstream_detail = (
            bounded_diagnostic_detail(observation.error_message)
            if observation.error_message is not None
            else _upstream_error_detail(
                observation.error_payload or b"",
                observation.error_envelope_paths or (("error",),),
            )
        )
        if (
            http_status is not None and http_status >= 500
            and observation.error_payload
            and _engine_transport_failure(observation.error_payload, observation.error_envelope_paths)
        ):
            # The generic type is the engine's label for its own failure, so no
            # machine code is carried that could outrank the network fact. Its
            # status is the engine's too: no upstream answered.
            return _outcome(
                kind=RawOutcomeKind.NETWORK_ERROR,
                source=source,
                model_id=model_id,
                message="engine could not reach the upstream",
                stream_started=stream_started,
                response_correlation_ids=response_correlation_ids,
                usage=observation.usage,
                recovery_verified=observation.recovery_verified,
                upstream_detail=upstream_detail,
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
            response_correlation_ids=response_correlation_ids,
            usage=observation.usage,
            recovery_verified=observation.recovery_verified,
            upstream_detail=upstream_detail,
            rejection=_upstream_rejection(
                http_status,
                stream_started,
                error_code,
                error_type,
                observation.error_payload or b"",
                observation.error_envelope_paths or (("error",),),
                observation.error_message,
            ),
        )
    return _outcome(
        kind=RawOutcomeKind.PROTOCOL_ERROR,
        source=source,
        model_id=model_id,
        http_status=http_status,
        message=observation.message or "upstream emitted invalid protocol data",
        stream_started=stream_started,
        response_correlation_ids=response_correlation_ids,
        usage=observation.usage,
        recovery_verified=observation.recovery_verified,
    )


def _engine_transport_failure(payload: bytes, envelope_paths: tuple[ErrorEnvelopePath, ...]) -> bool:
    """Whether an error body is exactly the engine's own envelope for a transport failure.

    An upstream that answered adds its own fields (a request id, a specific code),
    so only a body equal to one of the engine's two shapes qualifies.
    """

    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return False
    for path in envelope_paths or (("error",),):
        message: object = document
        for key in (*path, "message"):
            message = message.get(key) if isinstance(message, dict) else None
        if not isinstance(message, str) or not _ENGINE_TRANSPORT_FAILURE.search(message):
            continue
        if document in (
            {"type": "error", "error": {"type": "api_error", "message": message}},
            {"error": {"type": "server_error", "code": "internal_server_error", "message": message}},
        ):
            return True
    return False


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
    recovery_verified: bool = False,
    response_correlation_ids: tuple[tuple[str, str], ...] = (),
    upstream_detail: str | None = None,
    os_errno: int | None = None,
    rejection: UpstreamRejection | None = None,
    hosted_tools: frozenset[str] = frozenset(),
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
        recovery_verified=recovery_verified,
        response_correlation_ids=response_correlation_ids,
        upstream_detail=upstream_detail,
        os_errno=os_errno,
        rejection=rejection,
        hosted_tools=hosted_tools,
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


def _upstream_error_detail(
    payload: bytes,
    envelope_paths: tuple[ErrorEnvelopePath, ...],
) -> str | None:
    """Project the first envelope's ``message`` as bounded, redacted display text."""

    message_paths = tuple((*path, "message") for path in envelope_paths)
    values: dict[JSONPath, str] = {}

    def visit(
        path: JSONPath,
        event: JSONEvent,
        value: object | None,
        _scope: JSONScope,
    ) -> None:
        if event == "replace":
            values.pop(path, None)
        elif event == "scalar" and isinstance(value, str):
            values[path] = value

    if not payload or not project_json_reader(io.BytesIO(payload), message_paths, visit):
        return None
    for path in message_paths:
        text = bounded_diagnostic_detail(values.get(path, ""))
        if text:
            return text
    return None


_REJECTION_TOKEN = re.compile(r"[A-Za-z0-9_.\[\]-]{1,64}")
_REJECTION_MESSAGE_SCAN_CHARS = 2000
_INVALID_VALUE_MESSAGE = re.compile(r"Invalid value: '([A-Za-z0-9_.-]{1,64})'")
_UNSUPPORTED_TOOL_TYPE_MESSAGE = re.compile(r"Unsupported tool type: ([A-Za-z0-9_.-]{1,64})")
_INVALID_ENCRYPTED_CONTENT_MESSAGE = re.compile(r"Invalid encrypted_content in ([a-z_]{1,64}) block")


def _upstream_rejection(
    http_status: int | None,
    stream_started: bool,
    error_code: str | None,
    error_type: str | None,
    payload: bytes,
    envelope_paths: tuple[ErrorEnvelopePath, ...],
    message: str | None,
) -> UpstreamRejection | None:
    """Project a 400 before output into the closed tokens the evidence rules read.

    The machine code and type come from the one extractor (``_raw_error_fields``
    and its projection); this adds only the first envelope's ``param`` and the
    single token each recognized message names. The prose itself never leaves.
    """

    if http_status != 400 or stream_started:
        return None
    names = ("param", "message")
    paths = tuple((*path, name) for path in envelope_paths for name in names)
    values: dict[JSONPath, str] = {}

    def visit(path: JSONPath, event: JSONEvent, value: object | None, _scope: JSONScope) -> None:
        if event == "replace":
            values.pop(path, None)
        elif event == "scalar" and isinstance(value, str):
            values[path] = value

    if payload:
        project_json_reader(io.BytesIO(payload), paths, visit)
    envelope = next(
        (path for path in envelope_paths if any((*path, name) in values for name in names)),
        None,
    )
    fields = {name: values.get((*envelope, name)) for name in names} if envelope is not None else {}
    text = (fields.get("message") or message or "")[:_REJECTION_MESSAGE_SCAN_CHARS]

    def token(value: object) -> str | None:
        return value if isinstance(value, str) and _REJECTION_TOKEN.fullmatch(value) else None

    def named(pattern: re.Pattern[str]) -> str | None:
        found = pattern.search(text)
        return found.group(1) if found else None

    return UpstreamRejection(
        status=400,
        code=token(error_code),
        error_type=token(error_type),
        param=token(fields.get("param")),
        invalid_value=named(_INVALID_VALUE_MESSAGE),
        unsupported_tool_type=named(_UNSUPPORTED_TOOL_TYPE_MESSAGE),
        invalid_encrypted_content_block=named(_INVALID_ENCRYPTED_CONTENT_MESSAGE),
    )


def _upstream_reset_advice(
    payload: bytes,
    envelope_paths: tuple[ErrorEnvelopePath, ...],
) -> str | None:
    """Project a usage cap's own reset as Retry-After advice, in that header's syntax.

    A ChatGPT subscription past its cap answers 429 ``usage_limit_reached`` with
    ``resets_at`` (Unix seconds) and ``resets_in_seconds`` in the error envelope
    and no header. The first becomes an HTTP-date and the second delay-seconds;
    L2 validates either exactly like the header. Only that envelope's reset is
    advice: the same field names in any other error say nothing about when the
    Source can serve again.
    """

    fields = ("type", "resets_at", "resets_in_seconds")
    reset_paths = tuple((*path, field) for path in envelope_paths for field in fields)
    values: dict[JSONPath, object] = {}

    def visit(
        path: JSONPath,
        event: JSONEvent,
        value: object | None,
        _scope: JSONScope,
    ) -> None:
        if event == "replace":
            values.pop(path, None)
        elif event == "scalar":
            values[path] = value

    if not payload or not project_json_reader(io.BytesIO(payload), reset_paths, visit):
        return None
    for path in envelope_paths:
        if values.get((*path, "type")) != "usage_limit_reached":
            continue
        resets_at = values.get((*path, "resets_at"))
        if type(resets_at) is int and resets_at > 0:
            try:
                return format_datetime(datetime.fromtimestamp(resets_at, timezone.utc), usegmt=True)
            except (OverflowError, OSError, ValueError):
                pass
        resets_in_seconds = values.get((*path, "resets_in_seconds"))
        if type(resets_in_seconds) is int and resets_in_seconds >= 0:
            return str(resets_in_seconds)
    return None


def _safe_error_code(value: object) -> str | None:
    if not isinstance(value, str) or value not in UPSTREAM_MACHINE_ERROR_CODES:
        return None
    return value
