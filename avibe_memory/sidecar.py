"""Child-only UDS launcher for the pinned EverOS ASGI factory."""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib
import json
import logging
import math
import os
import re
import stat
from importlib.metadata import version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from avibe_memory.artifact import EVEROS_VERSION
from vibe.memory_project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
)
from avibe_memory.secret_scrubber import install_error_scrubbers
from avibe_memory.modality import SUPPORTED_ATTACHMENT_EXTENSIONS
from avibe_memory.store import is_memory_owner_id
from avibe_memory.types import (
    MAX_AGENTIC_TIMEOUT_SECONDS,
    MAX_MEMORY_LIST_PAGE_SIZE,
    MAX_MEMORY_SEARCH_RESULTS,
)


_APP_ID = "avibe"
_AGENTIC_TIMEOUT_HEADER = "X-Avibe-Memory-Agentic-Timeout-Seconds"
_AGENTIC_ROUND_HEADER = "X-Avibe-Memory-Agentic-Round"
_SESSION_PATTERN = re.compile(r"src--[0-9a-f]{64}--e(?:0|[1-9][0-9]*)\Z")
_AGENTIC_ROUND_STATE: contextvars.ContextVar[dict[str, str] | None] = (
    contextvars.ContextVar("avibe_memory_agentic_round", default=None)
)
logger = logging.getLogger(__name__)


def serve(uds: Path) -> None:
    if version("everos") != EVEROS_VERSION:
        raise RuntimeError("everos version mismatch")
    if uds.exists() or not uds.parent.is_dir():
        raise RuntimeError("invalid sidecar socket path")
    os.umask(0o077)

    from starlette.responses import JSONResponse
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

    @app.middleware("http")
    async def guard(request: Any, call_next: Any) -> Any:
        body = await request.body()
        rejection = _request_rejection(
            request.method,
            request.url.path,
            body,
            attachments_root=attachments_root,
        )
        if rejection is not None:
            return JSONResponse({"detail": "memory_request_rejected"}, status_code=403)
        return await call_next(request)

    config = uvicorn.Config(
        _AgenticDeadlineProjection(app),
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
    """Own and cancel the downstream ASGI task for bounded agentic search."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/api/v2/memory/search"
        ):
            await self._app(scope, receive, send)
            return

        request_messages, body = await _buffer_request(receive)
        message_index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal message_index
            if message_index < len(request_messages):
                message = request_messages[message_index]
                message_index += 1
                return message
            return await receive()

        agentic_timeout = _agentic_request_timeout(
            scope.get("path", ""),
            body,
            scope.get("headers", []),
        )
        if agentic_timeout is False:
            await _send_json_error(send, status=403, detail="memory_request_rejected")
            return
        if agentic_timeout is None:
            await self._app(scope, replay_receive, send)
            return

        response_messages: list[dict[str, Any]] = []

        async def capture_send(message: dict[str, Any]) -> None:
            response_messages.append(message)

        round_state: dict[str, str] = {}
        token = _AGENTIC_ROUND_STATE.set(round_state)
        try:
            await asyncio.wait_for(
                self._app(scope, replay_receive, capture_send),
                timeout=agentic_timeout,
            )
        except asyncio.TimeoutError:
            await _send_json_error(
                send,
                status=504,
                detail="memory_request_timed_out",
                agentic_round=round_state.get("round"),
            )
            return
        finally:
            _AGENTIC_ROUND_STATE.reset(token)

        round_value = round_state.get("round")
        if round_value in {"round1", "round2"}:
            _append_response_header(
                response_messages,
                _AGENTIC_ROUND_HEADER,
                round_value,
            )
        for message in response_messages:
            await send(message)


async def _buffer_request(receive: Any) -> tuple[list[dict[str, Any]], bytes]:
    messages: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") == "http.disconnect":
            break
        if message.get("type") != "http.request":
            continue
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return messages, b"".join(chunks)


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


def _append_response_header(
    messages: list[dict[str, Any]],
    name: str,
    value: str,
) -> None:
    encoded_name = name.lower().encode("ascii")
    encoded_value = value.encode("ascii")
    for index, message in enumerate(messages):
        if message.get("type") != "http.response.start":
            continue
        projected = dict(message)
        projected["headers"] = [
            (header_name, header_value)
            for header_name, header_value in message.get("headers", [])
            if header_name.lower() != encoded_name
        ]
        projected["headers"].append((encoded_name, encoded_value))
        messages[index] = projected
        return


def _request_rejection(
    method: str,
    path: str,
    body: bytes,
    *,
    attachments_root: Path | None = None,
) -> str | None:
    if method == "GET" and path == "/health":
        return None
    if method != "POST" or path not in {
        "/api/v2/memory/add",
        "/api/v2/memory/flush",
        "/api/v2/memory/search",
        "/api/v2/memory/get",
    }:
        return "route"
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return "json"
    if not isinstance(payload, dict):
        return "shape"
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
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("method") != "agentic":
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
    required_fields = {"sender_id", "role", "timestamp", "content"}
    if (
        not isinstance(message, dict)
        or not required_fields <= set(message) <= required_fields | {"sender_name"}
    ):
        return "add"
    if "sender_name" in message and (
        not isinstance(message["sender_name"], str)
        or not message["sender_name"].strip()
        or len(message["sender_name"]) > 128
    ):
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
        or not 1 <= payload["top_k"] <= MAX_MEMORY_SEARCH_RESULTS
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
        or not 1 <= page_size <= MAX_MEMORY_LIST_PAGE_SIZE
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

    from avibe_memory.everos import EverOSPort, MULTIMODAL_EXPLICIT_ENV

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
