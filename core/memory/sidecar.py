"""Child-only UDS launcher for the pinned EverOS ASGI factory."""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextvars
import errno
import importlib
import json
import logging
import math
import mmap
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from core.memory.artifact import EVEROS_VERSION
from core.memory.blocking import run_blocking
from core.memory.project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
)
from core.memory.secret_scrubber import install_error_scrubbers
from core.memory.modality import SUPPORTED_ATTACHMENT_EXTENSIONS
from core.memory.store import is_memory_owner_id
from core.memory.types import MAX_AGENTIC_TIMEOUT_SECONDS


_APP_ID = "avibe"
_AGENTIC_TIMEOUT_HEADER = "X-Avibe-Memory-Agentic-Timeout-Seconds"
_AGENTIC_ROUND_HEADER = "X-Avibe-Memory-Agentic-Round"
_SESSION_PATTERN = re.compile(r"src--[0-9a-f]{64}--e(?:0|[1-9][0-9]*)\Z")
_AGENTIC_ROUND_STATE: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("avibe_memory_agentic_round", default=None)
)
logger = logging.getLogger(__name__)

_REQUEST_PATHS = frozenset(
    {
        "/api/v2/memory/add",
        "/api/v2/memory/flush",
        "/api/v2/memory/search",
        "/api/v2/memory/get",
    }
)
_SPOOL_REPLAY_CHUNK_BYTES = 64 * 1024
_REQUEST_REPLAY_CHUNK_BYTES = _SPOOL_REPLAY_CHUNK_BYTES
_MIN_SPOOL_FREE_BYTES = 512 * 1024 * 1024
_SPOOL_WRITE_LOCK = threading.Lock()


def serve(uds: Path) -> None:
    if version("everos") != EVEROS_VERSION:
        raise RuntimeError("everos version mismatch")
    if uds.exists() or not uds.parent.is_dir():
        raise RuntimeError("invalid sidecar socket path")
    os.umask(0o077)

    import uvicorn

    install_error_scrubbers()
    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()
    round_handler = _AgenticRoundHandler()
    round_logger = logging.getLogger("everos.memory.search.agentic")
    original_round_logger_level = round_logger.level
    round_logger.setLevel(logging.INFO)
    round_logger.addHandler(round_handler)
    attachments_root = Path(os.environ["AVIBE_MEMORY_ATTACHMENTS_ROOT"])

    config = uvicorn.Config(
        _AgenticDeadlineProjection(app, attachments_root=attachments_root),
        uds=str(uds),
        access_log=False,
        log_level="warning",
        log_config=None,
        timeout_graceful_shutdown=1,
    )
    try:
        uvicorn.Server(config).run()
    finally:
        round_logger.removeHandler(round_handler)
        round_logger.setLevel(original_round_logger_level)


class _AgenticRoundHandler(logging.Handler):
    """Capture only the bounded EverOS round token in the request context."""

    def emit(self, record: logging.LogRecord) -> None:
        state = _AGENTIC_ROUND_STATE.get()
        if state is None or not isinstance(record.msg, dict):
            return
        if record.msg.get("event") != "agentic_search_decision":
            return
        round_value = record.msg.get("round")
        if round_value in {"round1", "round2"}:
            state["round"] = round_value


class _AgenticDeadlineProjection:
    """Stream-guard requests and own the bounded agentic downstream task."""

    def __init__(self, app: Any, *, attachments_root: Path | None = None) -> None:
        self._app = app
        self._attachments_root = attachments_root

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method")
        path = scope.get("path")
        if method == "GET" and path == "/health":
            await self._app(scope, receive, send)
            return
        if method != "POST" or path not in _REQUEST_PATHS:
            await _send_json_error(send, status=403, detail="memory_request_rejected")
            return

        request_deadline = _request_header_deadline(
            path,
            scope.get("headers", []),
        )
        try:
            spool = tempfile.TemporaryFile(mode="w+b")
        except OSError:
            await _send_json_error(
                send,
                status=507,
                detail="memory_temporary_storage_unavailable",
            )
            return
        try:
            try:
                body_size = await _spool_request(
                    receive,
                    spool,
                    deadline=request_deadline,
                )
            except _RequestDeadlineExceeded:
                await _send_json_error(
                    send,
                    status=504,
                    detail="memory_request_timed_out",
                )
                return
            except OSError:
                await _send_json_error(
                    send,
                    status=507,
                    detail="memory_temporary_storage_unavailable",
                )
                return
            if body_size is None:
                return
            try:
                payload = await run_blocking(
                    _project_spooled_request,
                    spool,
                    path,
                    request_deadline,
                )
            except _RequestDeadlineExceeded:
                await _send_json_error(
                    send,
                    status=504,
                    detail="memory_request_timed_out",
                )
                return
            if not isinstance(payload, dict) or _request_payload_rejection(
                path,
                payload,
                attachments_root=self._attachments_root,
            ) is not None:
                await _send_json_error(
                    send,
                    status=403,
                    detail="memory_request_rejected",
                )
                return
            await run_blocking(spool.seek, 0)
            replay_receive = _spooled_request_receive(
                spool,
                body_size=body_size,
                fallback_receive=receive,
            )
            agentic_timeout = _agentic_timeout_from_payload(
                path,
                payload,
                scope.get("headers", []),
            )
            if agentic_timeout is False:
                await _send_json_error(
                    send,
                    status=403,
                    detail="memory_request_rejected",
                )
                return
            if agentic_timeout is None and request_deadline is None:
                await self._app(scope, replay_receive, send)
                return
            if request_deadline is None:
                assert isinstance(agentic_timeout, float)
                request_deadline = time.monotonic() + agentic_timeout

            try:
                response_spool = tempfile.TemporaryFile(mode="w+b")
            except OSError:
                await _send_json_error(
                    send,
                    status=507,
                    detail="memory_temporary_storage_unavailable",
                )
                return
            try:
                response_start: dict[str, Any] | None = None
                response_body_size = 0

                async def capture_send(message: dict[str, Any]) -> None:
                    nonlocal response_start, response_body_size
                    message_type = message.get("type")
                    if message_type == "http.response.start":
                        if response_start is not None:
                            raise RuntimeError("duplicate sidecar response start")
                        response_start = dict(message)
                        response_start["headers"] = list(message.get("headers", []))
                        return
                    if message_type != "http.response.body":
                        raise RuntimeError("unsupported sidecar response message")
                    chunk = message.get("body", b"")
                    if not isinstance(chunk, bytes):
                        raise TypeError("sidecar response body must be bytes")
                    if chunk:
                        await _spool_bytes(
                            response_spool,
                            chunk,
                            deadline=request_deadline,
                        )
                        response_body_size += len(chunk)

                round_state: dict[str, str] = {}
                token = _AGENTIC_ROUND_STATE.set(round_state)
                try:
                    await asyncio.wait_for(
                        self._app(scope, replay_receive, capture_send),
                        timeout=_remaining_request_deadline(request_deadline),
                    )
                except (asyncio.TimeoutError, _RequestDeadlineExceeded):
                    await _send_json_error(
                        send,
                        status=504,
                        detail="memory_request_timed_out",
                        agentic_round=round_state.get("round"),
                    )
                    return
                except OSError:
                    await _send_json_error(
                        send,
                        status=507,
                        detail="memory_temporary_storage_unavailable",
                        agentic_round=round_state.get("round"),
                    )
                    return
                finally:
                    _AGENTIC_ROUND_STATE.reset(token)

                if response_start is None:
                    raise RuntimeError("sidecar response start missing")
                round_value = round_state.get("round")
                if round_value in {"round1", "round2"}:
                    response_start = _with_response_header(
                        response_start,
                        _AGENTIC_ROUND_HEADER,
                        round_value,
                    )
                await send(response_start)
                await run_blocking(response_spool.seek, 0)
                await _replay_spooled_response(
                    response_spool,
                    body_size=response_body_size,
                    send=send,
                )
            finally:
                response_spool.close()
        finally:
            spool.close()


async def _spool_request(
    receive: Any,
    spool: Any,
    *,
    deadline: float | None = None,
) -> int | None:
    body_size = 0
    while True:
        if deadline is None:
            message = await receive()
        else:
            try:
                message = await asyncio.wait_for(
                    receive(),
                    timeout=_remaining_request_deadline(deadline),
                )
            except asyncio.TimeoutError:
                raise _RequestDeadlineExceeded from None
        if message.get("type") == "http.disconnect":
            return None
        if message.get("type") != "http.request":
            continue
        chunk = message.get("body", b"")
        if not isinstance(chunk, bytes):
            return None
        if chunk:
            await _spool_bytes(spool, chunk, deadline=deadline)
            body_size += len(chunk)
        if not message.get("more_body", False):
            await run_blocking(spool.flush)
            _check_request_deadline(deadline)
            return body_size


def _spooled_request_receive(
    spool: Any,
    *,
    body_size: int,
    fallback_receive: Any,
) -> Any:
    delivered = 0
    delivered_empty = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered, delivered_empty
        if body_size == 0 and not delivered_empty:
            delivered_empty = True
            return {"type": "http.request", "body": b"", "more_body": False}
        if delivered < body_size:
            chunk = await run_blocking(
                spool.read,
                min(_REQUEST_REPLAY_CHUNK_BYTES, body_size - delivered),
            )
            if not isinstance(chunk, bytes) or not chunk:
                return {"type": "http.disconnect"}
            delivered += len(chunk)
            return {
                "type": "http.request",
                "body": chunk,
                "more_body": delivered < body_size,
            }
        return await fallback_receive()

    return receive


async def _spool_bytes(
    spool: Any,
    chunk: bytes,
    *,
    deadline: float | None = None,
) -> None:
    view = memoryview(chunk)
    for offset in range(0, len(view), _SPOOL_REPLAY_CHUNK_BYTES):
        _check_request_deadline(deadline)
        await run_blocking(
            _write_spool_chunk,
            spool,
            view[offset : offset + _SPOOL_REPLAY_CHUNK_BYTES],
            deadline,
        )
        _check_request_deadline(deadline)


def _write_spool_chunk(
    spool: Any,
    chunk: memoryview,
    deadline: float | None = None,
) -> None:
    _check_request_deadline(deadline)
    if deadline is None:
        acquired = _SPOOL_WRITE_LOCK.acquire()
    else:
        remaining = deadline - time.monotonic()
        acquired = remaining > 0 and _SPOOL_WRITE_LOCK.acquire(timeout=remaining)
    if not acquired:
        raise _RequestDeadlineExceeded
    try:
        _check_request_deadline(deadline)
        free_bytes = shutil.disk_usage(tempfile.gettempdir()).free
        _check_request_deadline(deadline)
        if free_bytes - len(chunk) < _MIN_SPOOL_FREE_BYTES:
            raise OSError(
                errno.ENOSPC,
                "insufficient temporary storage for memory sidecar payload",
            )
        written = spool.write(chunk)
        if written != len(chunk):
            raise OSError(errno.EIO, "short write while spooling sidecar payload")
        spool.flush()
        _check_request_deadline(deadline)
    finally:
        _SPOOL_WRITE_LOCK.release()


async def _replay_spooled_response(
    spool: Any,
    *,
    body_size: int,
    send: Any,
) -> None:
    delivered = 0
    while delivered < body_size:
        chunk = await run_blocking(
            spool.read,
            min(_SPOOL_REPLAY_CHUNK_BYTES, body_size - delivered),
        )
        if not isinstance(chunk, bytes) or not chunk:
            raise OSError(errno.EIO, "short read while replaying sidecar response")
        delivered += len(chunk)
        await send(
            {
                "type": "http.response.body",
                "body": chunk,
                "more_body": delivered < body_size,
            }
        )
    if body_size == 0:
        await send(
            {
                "type": "http.response.body",
                "body": b"",
                "more_body": False,
            }
        )


async def _send_json_error(
    send: Any,
    *,
    status: int,
    detail: str,
    agentic_round: str | None = None,
) -> None:
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if agentic_round in {"round1", "round2"}:
        headers.append(
            (
                _AGENTIC_ROUND_HEADER.lower().encode("ascii"),
                agentic_round.encode("ascii"),
            )
        )
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})


def _with_response_header(
    message: dict[str, Any],
    name: str,
    value: str,
) -> dict[str, Any]:
    encoded_name = name.lower().encode("ascii")
    encoded_value = value.encode("ascii")
    projected = dict(message)
    projected["headers"] = [
        (header_name, header_value)
        for header_name, header_value in message.get("headers", [])
        if header_name.lower() != encoded_name
    ]
    projected["headers"].append((encoded_name, encoded_value))
    return projected


_ROOT_KEYS = {
    "/api/v2/memory/add": frozenset(
        {"session_id", "app_id", "project_id", "messages"}
    ),
    "/api/v2/memory/flush": frozenset({"session_id", "app_id", "project_id"}),
    "/api/v2/memory/search": frozenset(
        {
            "user_id",
            "app_id",
            "project_id",
            "query",
            "method",
            "top_k",
            "include_profile",
            "enable_llm_rerank",
            "filters",
        }
    ),
    "/api/v2/memory/get": frozenset(
        {
            "user_id",
            "app_id",
            "project_id",
            "memory_type",
            "page",
            "page_size",
            "sort_by",
            "sort_order",
        }
    ),
}
_NESTED_KEYS = {
    "messages.item": frozenset(
        {"sender_id", "role", "timestamp", "content"}
    ),
    "messages.item.content.item": frozenset(
        {"type", "text", "name", "uri", "ext"}
    ),
    "filters": frozenset({"session_id"}),
}
_OBJECT_PATHS = frozenset(
    {"messages.item", "messages.item.content.item", "filters"}
)
_ARRAY_PATHS = {
    "messages": ("messages.item", 1),
    "messages.item.content": ("messages.item.content.item", 9),
}
_UNBOUNDED_TEXT_PATHS = frozenset(
    {"query", "messages.item.content", "messages.item.content.item.text"}
)
_STRING_DECODE_LIMITS = {
    "messages.item.content.item.name": 512,
    "messages.item.content.item.uri": 16 * 1024,
    "messages.item.content.item.ext": 8,
}
_DEFAULT_STRING_DECODE_BYTES = 512
_STRING_SCAN_CHUNK_BYTES = 64 * 1024
_JSON_NUMBER_BYTES = 4096
_HEX_BYTES = frozenset(b"0123456789abcdefABCDEF")
_JSON_WHITESPACE = frozenset(b" \t\r\n")
_OVERSIZED_SCALAR = object()
_SCHEMA_REJECTED = object()
_SHAPE_REJECTED = object()


class _RequestParseError(ValueError):
    pass


class _RequestSchemaError(_RequestParseError):
    pass


class _RequestShapeError(_RequestParseError):
    pass


class _RequestDeadlineExceeded(RuntimeError):
    pass


class _MappedRequestParser:
    """Project only the bounded fields needed by the private request guard."""

    def __init__(
        self,
        data: mmap.mmap,
        path: str,
        deadline: float | None = None,
    ) -> None:
        self._data = data
        self._path = path
        self._index = 0
        self._deadline = deadline
        self._next_deadline_check = 0

    def parse(self) -> dict[str, Any]:
        self._check_deadline(force=True)
        self._skip_whitespace()
        if self._peek(default=None) != ord("{"):
            raise _RequestShapeError("root shape")
        payload = self._parse_object("")
        self._skip_whitespace()
        if self._index != len(self._data):
            raise _RequestParseError("trailing data")
        return payload

    def _parse_object(self, prefix: str) -> dict[str, Any]:
        self._expect(ord("{"))
        allowed_keys = _ROOT_KEYS[self._path] if not prefix else _NESTED_KEYS[prefix]
        projected: dict[str, Any] = {}
        seen: set[str] = set()
        self._skip_whitespace()
        if self._consume(ord("}")):
            return projected
        while True:
            self._check_deadline()
            key = self._parse_string(decode_limit=64)
            if not isinstance(key, str) or key not in allowed_keys or key in seen:
                raise _RequestSchemaError("object key")
            seen.add(key)
            self._skip_whitespace()
            self._expect(ord(":"))
            self._skip_whitespace()
            child_path = key if not prefix else f"{prefix}.{key}"
            projected[key] = self._parse_path_value(child_path)
            self._skip_whitespace()
            if self._consume(ord("}")):
                return projected
            self._expect(ord(","))
            self._skip_whitespace()

    def _parse_path_value(self, path: str) -> Any:
        current = self._peek()
        if path in _OBJECT_PATHS:
            if current != ord("{"):
                raise _RequestSchemaError("object shape")
            return self._parse_object(path)
        if path in _ARRAY_PATHS:
            if path == "messages.item.content" and current == ord('"'):
                return self._parse_string(decode_limit=None)
            if current != ord("["):
                raise _RequestSchemaError("array shape")
            return self._parse_array(path)
        if current in {ord("{"), ord("[")}:
            raise _RequestSchemaError("scalar shape")
        return self._parse_scalar(path)

    def _parse_array(self, path: str) -> list[Any]:
        item_path, maximum = _ARRAY_PATHS[path]
        self._expect(ord("["))
        projected: list[Any] = []
        self._skip_whitespace()
        if self._consume(ord("]")):
            return projected
        while True:
            self._check_deadline()
            if len(projected) >= maximum:
                raise _RequestSchemaError("array length")
            projected.append(self._parse_path_value(item_path))
            self._skip_whitespace()
            if self._consume(ord("]")):
                return projected
            self._expect(ord(","))
            self._skip_whitespace()

    def _parse_scalar(self, path: str) -> Any:
        current = self._peek()
        if current == ord('"'):
            limit = (
                None
                if path in _UNBOUNDED_TEXT_PATHS
                else _STRING_DECODE_LIMITS.get(
                    path,
                    _DEFAULT_STRING_DECODE_BYTES,
                )
            )
            return self._parse_string(decode_limit=limit)
        if current == ord("t"):
            self._expect_literal(b"true")
            return True
        if current == ord("f"):
            self._expect_literal(b"false")
            return False
        if current == ord("n"):
            self._expect_literal(b"null")
            return None
        return self._parse_number()

    def _parse_string(self, *, decode_limit: int | None) -> Any:
        start = self._index
        self._expect(ord('"'))
        segment_start = self._index
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        while self._index < len(self._data):
            current = self._data[self._index]
            if current == ord('"'):
                self._decode_segment(decoder, segment_start, self._index, final=True)
                self._index += 1
                if decode_limit is None:
                    return ""
                raw_length = self._index - start
                if raw_length > (decode_limit * 6) + 2:
                    return _OVERSIZED_SCALAR
                try:
                    value = json.loads(self._data[start : self._index])
                    encoded = value.encode("utf-8")
                except (TypeError, ValueError, UnicodeError):
                    raise _RequestParseError("string") from None
                return value if len(encoded) <= decode_limit else _OVERSIZED_SCALAR
            if current == ord("\\"):
                self._decode_segment(decoder, segment_start, self._index, final=True)
                self._index += 1
                if self._index >= len(self._data):
                    raise _RequestParseError("escape")
                escape = self._data[self._index]
                if escape == ord("u"):
                    escape_end = self._index + 5
                    if escape_end > len(self._data) or any(
                        value not in _HEX_BYTES
                        for value in self._data[self._index + 1 : escape_end]
                    ):
                        raise _RequestParseError("unicode escape")
                    self._index = escape_end
                elif escape in b'"\\/bfnrt':
                    self._index += 1
                else:
                    raise _RequestParseError("escape")
                segment_start = self._index
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                continue
            if current < 0x20:
                raise _RequestParseError("control character")
            self._index += 1
            if self._index - segment_start >= _STRING_SCAN_CHUNK_BYTES:
                self._check_deadline(force=True)
                self._decode_segment(
                    decoder,
                    segment_start,
                    self._index,
                    final=False,
                )
                segment_start = self._index
        raise _RequestParseError("unterminated string")

    def _decode_segment(
        self,
        decoder: Any,
        start: int,
        end: int,
        *,
        final: bool,
    ) -> None:
        try:
            decoder.decode(self._data[start:end], final=final)
        except UnicodeDecodeError:
            raise _RequestParseError("utf-8") from None

    def _parse_number(self) -> Any:
        start = self._index
        self._consume(ord("-"))
        if self._consume(ord("0")):
            if self._peek(default=None) in range(ord("0"), ord("9") + 1):
                raise _RequestParseError("leading zero")
        else:
            self._expect_digit(nonzero=True)
            while self._consume_digit():
                pass
        if self._consume(ord(".")):
            self._expect_digit()
            while self._consume_digit():
                pass
        if self._peek(default=None) in {ord("e"), ord("E")}:
            self._index += 1
            if self._peek(default=None) in {ord("+"), ord("-")}:
                self._index += 1
            self._expect_digit()
            while self._consume_digit():
                pass
        if self._index - start > _JSON_NUMBER_BYTES:
            return _OVERSIZED_SCALAR
        try:
            return json.loads(self._data[start : self._index])
        except (TypeError, ValueError, OverflowError):
            raise _RequestParseError("number") from None

    def _expect_digit(self, *, nonzero: bool = False) -> None:
        current = self._peek(default=None)
        minimum = ord("1") if nonzero else ord("0")
        if current is None or not minimum <= current <= ord("9"):
            raise _RequestParseError("digit")
        self._index += 1
        self._check_deadline()

    def _consume_digit(self) -> bool:
        current = self._peek(default=None)
        if current is None or not ord("0") <= current <= ord("9"):
            return False
        self._index += 1
        self._check_deadline()
        return True

    def _expect_literal(self, value: bytes) -> None:
        end = self._index + len(value)
        if self._data[self._index : end] != value:
            raise _RequestParseError("literal")
        self._index = end

    def _skip_whitespace(self) -> None:
        while self._index < len(self._data) and self._data[self._index] in _JSON_WHITESPACE:
            self._index += 1
            self._check_deadline()

    def _check_deadline(self, *, force: bool = False) -> None:
        if self._deadline is None:
            return
        if not force and self._index < self._next_deadline_check:
            return
        self._next_deadline_check = self._index + _STRING_SCAN_CHUNK_BYTES
        _check_request_deadline(self._deadline)

    def _peek(self, *, default: int | None = None) -> int | None:
        return self._data[self._index] if self._index < len(self._data) else default

    def _expect(self, value: int) -> None:
        if not self._consume(value):
            raise _RequestParseError("token")

    def _consume(self, value: int) -> bool:
        if self._peek(default=None) != value:
            return False
        self._index += 1
        return True


def _project_spooled_request(
    spool: Any,
    path: str,
    deadline: float | None = None,
) -> Any:
    try:
        _check_request_deadline(deadline)
        spool.flush()
        size = os.fstat(spool.fileno()).st_size
        if size <= 0:
            return None
        with mmap.mmap(spool.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            return _MappedRequestParser(mapped, path, deadline).parse()
    except _RequestShapeError:
        return _SHAPE_REJECTED
    except _RequestSchemaError:
        return _SCHEMA_REJECTED
    except (OSError, TypeError, ValueError, UnicodeError):
        return None


def _request_rejection(
    method: str,
    path: str,
    body: bytes,
    *,
    attachments_root: Path | None = None,
) -> str | None:
    if method == "GET" and path == "/health":
        return None
    if method != "POST" or path not in _REQUEST_PATHS:
        return "route"
    if not isinstance(body, bytes):
        return "json"
    with tempfile.TemporaryFile(mode="w+b") as spool:
        spool.write(body)
        payload = _project_spooled_request(spool, path)
    if payload is None:
        return "json"
    if payload is _SHAPE_REJECTED:
        return "shape"
    if payload is _SCHEMA_REJECTED:
        return path.rsplit("/", 1)[-1]
    if not isinstance(payload, dict):
        return "shape"
    return _request_payload_rejection(
        path,
        payload,
        attachments_root=attachments_root,
    )


def _request_payload_rejection(
    path: str,
    payload: dict[str, Any],
    *,
    attachments_root: Path | None,
) -> str | None:
    if path == "/api/v2/memory/add":
        return _validate_add(payload, attachments_root=attachments_root)
    if path == "/api/v2/memory/flush":
        return _validate_flush(payload)
    if path == "/api/v2/memory/search":
        return _validate_search(payload)
    return _validate_get(payload)


def _agentic_request_timeout(
    path: str,
    body: bytes,
    headers: Any,
) -> float | Literal[False] | None:
    if path != "/api/v2/memory/search":
        return None
    if not isinstance(body, bytes):
        return False
    with tempfile.TemporaryFile(mode="w+b") as spool:
        spool.write(body)
        payload = _project_spooled_request(spool, path)
    if not isinstance(payload, dict):
        return False
    return _agentic_timeout_from_payload(path, payload, headers)


def _agentic_timeout_from_payload(
    path: str,
    payload: dict[str, Any],
    headers: Any,
) -> float | Literal[False] | None:
    if path != "/api/v2/memory/search" or payload.get("method") != "agentic":
        return None
    try:
        timeout = float(_header_value(headers, _AGENTIC_TIMEOUT_HEADER))
    except (TypeError, ValueError):
        return False
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_AGENTIC_TIMEOUT_SECONDS
    ):
        return False
    return timeout


def _request_header_deadline(path: str, headers: Any) -> float | None:
    if path != "/api/v2/memory/search":
        return None
    raw_timeout = _header_value(headers, _AGENTIC_TIMEOUT_HEADER)
    if raw_timeout is None:
        return None
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_AGENTIC_TIMEOUT_SECONDS
    ):
        return None
    return time.monotonic() + timeout


def _remaining_request_deadline(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _RequestDeadlineExceeded
    return remaining


def _check_request_deadline(deadline: float | None) -> None:
    if deadline is not None:
        _remaining_request_deadline(deadline)


def _header_value(headers: Any, name: str) -> str | None:
    if hasattr(headers, "get"):
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        return value if isinstance(value, str) else None
    encoded_name = name.lower().encode("ascii")
    for header_name, header_value in headers:
        if header_name.lower() == encoded_name:
            try:
                return header_value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


def _valid_write_scope(payload: dict[str, Any]) -> bool:
    project_id = payload.get("project_id")
    return payload.get("app_id") == _APP_ID and is_persisted_memory_project_id(project_id)


def _valid_search_scope(payload: dict[str, Any]) -> bool:
    project_id = payload.get("project_id")
    return payload.get("app_id") == _APP_ID and is_new_stored_memory_project_id(project_id)


def _exact_keys(payload: dict[str, Any], keys: set[str]) -> bool:
    return set(payload) == keys


def _validate_add(
    payload: dict[str, Any],
    *,
    attachments_root: Path | None,
) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id", "messages"}) or not _valid_write_scope(payload):
        return "add"
    messages = payload.get("messages")
    if not _valid_session(payload.get("session_id")) or not isinstance(messages, list) or len(messages) != 1:
        return "add"
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"sender_id", "role", "timestamp", "content"}:
        return "add"
    if (
        not _valid_principal(message.get("sender_id"))
        or message.get("role") != "user"
        or not isinstance(message.get("timestamp"), int)
        or isinstance(message.get("timestamp"), bool)
    ):
        return "add"
    content = message.get("content")
    if isinstance(content, str):
        return None
    if not isinstance(content, list) or not 1 <= len(content) <= 9:
        return "add"
    for item in content:
        if not isinstance(item, dict):
            return "add"
        if item.get("type") == "text":
            if set(item) != {"type", "text"} or not isinstance(item.get("text"), str):
                return "add"
            continue
        if not _valid_pinned_attachment(item, attachments_root):
            return "add"
    return None


def _valid_pinned_attachment(item: dict[str, Any], attachments_root: Path | None) -> bool:
    if set(item) != {"type", "name", "uri", "ext"}:
        return False
    if item.get("type") not in {"image", "audio", "doc", "pdf", "html", "email"}:
        return False
    name = item.get("name")
    uri = item.get("uri")
    extension = item.get("ext")
    if (
        attachments_root is None
        or not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 512
        or not isinstance(uri, str)
        or not isinstance(extension, str)
        or not extension.isalnum()
        or len(extension) > 8
        or extension.lower() not in SUPPORTED_ATTACHMENT_EXTENSIONS
    ):
        return False
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return False
    try:
        root = attachments_root.resolve(strict=True)
        raw_path = Path(unquote(parsed.path))
        raw_info = raw_path.lstat()
        if stat.S_ISLNK(raw_info.st_mode):
            return False
        path = raw_path.resolve(strict=True)
        path.relative_to(root)
        info = path.lstat()
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _validate_flush(payload: dict[str, Any]) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id"}) or not _valid_write_scope(payload):
        return "flush"
    return None if _valid_session(payload.get("session_id")) else "flush"


def _validate_search(payload: dict[str, Any]) -> str | None:
    required = {
        "user_id",
        "app_id",
        "project_id",
        "query",
        "method",
        "top_k",
        "include_profile",
        "enable_llm_rerank",
    }
    if frozenset(payload) not in {frozenset(required), frozenset((*required, "filters"))}:
        return "search"
    if not _valid_search_scope(payload):
        return "search"
    if (
        not _valid_principal(payload.get("user_id"))
        or not isinstance(payload.get("query"), str)
        or payload.get("method") not in {"keyword", "vector", "hybrid", "agentic"}
        or not isinstance(payload.get("top_k"), int)
        or isinstance(payload.get("top_k"), bool)
        or not 1 <= payload["top_k"] <= 20
        or type(payload.get("include_profile")) is not bool
        or payload.get("enable_llm_rerank") is not False
    ):
        return "search"
    filters = payload.get("filters")
    if filters is not None and (
        not isinstance(filters, dict)
        or set(filters) != {"session_id"}
        or not _valid_session(filters.get("session_id"))
    ):
        return "search"
    return None


def _validate_get(payload: dict[str, Any]) -> str | None:
    profile_keys = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size"}
    if _exact_keys(payload, profile_keys):
        if (
            not _valid_principal(payload.get("user_id"))
            or payload.get("app_id") != _APP_ID
            or payload.get("project_id") != "default"
            or payload.get("memory_type") != "profile"
            or payload.get("page") != 1
            or payload.get("page_size") != 1
        ):
            return "get"
        return None

    episode_keys = profile_keys | {"sort_by", "sort_order"}
    if not _exact_keys(payload, episode_keys):
        return "get"
    page = payload.get("page")
    page_size = payload.get("page_size")
    if (
        not _valid_principal(payload.get("user_id"))
        or not _valid_search_scope(payload)
        or payload.get("memory_type") != "episode"
        or isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 20
        or payload.get("sort_by") != "timestamp"
        or payload.get("sort_order") != "desc"
    ):
        return "get"
    return None


def _valid_principal(value: object) -> bool:
    return is_memory_owner_id(value)


def _valid_session(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("utf-8")) <= 128
        and _SESSION_PATTERN.fullmatch(value) is not None
    )


def _processing_healthy_from_child_environment() -> bool:
    """Run fixed authenticated probes only inside the scrubbed owned child."""

    from core.memory.everos import EverOSPort, MULTIMODAL_EXPLICIT_ENV

    multimodal_kwargs: dict[str, str | None] = {}
    if os.environ.get(MULTIMODAL_EXPLICIT_ENV) == "1":
        multimodal_kwargs = {
            "multimodal_base_url": os.environ.get("EVEROS_MULTIMODAL__BASE_URL"),
            "multimodal_model": os.environ.get("EVEROS_MULTIMODAL__MODEL"),
            "multimodal_api_key": os.environ.get("EVEROS_MULTIMODAL__API_KEY"),
        }

    provider = EverOSPort(
        Path("/nonexistent-memory-sidecar.sock"),
        llm_base_url=os.environ.get("EVEROS_LLM__BASE_URL"),
        llm_model=os.environ.get("EVEROS_LLM__MODEL"),
        llm_api_key=os.environ.get("EVEROS_LLM__API_KEY"),
        embedding_base_url=os.environ.get("EVEROS_EMBEDDING__BASE_URL"),
        embedding_model=os.environ.get("EVEROS_EMBEDDING__MODEL"),
        embedding_api_key=os.environ.get("EVEROS_EMBEDDING__API_KEY"),
        rerank_base_url=os.environ.get("EVEROS_RERANK__BASE_URL"),
        rerank_model=os.environ.get("EVEROS_RERANK__MODEL"),
        rerank_api_key=os.environ.get("EVEROS_RERANK__API_KEY"),
        rerank_provider=os.environ.get("EVEROS_RERANK__PROVIDER"),
        **multimodal_kwargs,
    )
    return asyncio.run(provider.processing_healthy())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds")
    parser.add_argument("--probe-processing", action="store_true")
    args = parser.parse_args()
    if args.probe_processing:
        return 0 if _processing_healthy_from_child_environment() else 1
    if not args.uds:
        parser.error("--uds is required when serving")
    serve(Path(args.uds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
