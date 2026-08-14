"""Child-only UDS launcher for the pinned EverOS ASGI factory."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import re
import stat
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from core.memory.artifact import EVEROS_VERSION
from core.memory.project_ids import (
    is_new_stored_memory_project_id,
    is_persisted_memory_project_id,
)
from core.memory.everos_insight import install_error_scrubbers, prepare_call_recorder
from core.memory.everos_insight.patches import boundary_request
from core.memory.modality import SUPPORTED_ATTACHMENT_EXTENSIONS


_MAX_BODY_BYTES = 64 * 1024
_APP_ID = "avibe"
_PRINCIPAL_PATTERN = re.compile(r"u-[0-9a-f]{32}\Z")
_SESSION_PATTERN = re.compile(r"src--[0-9a-f]{64}--e(?:0|[1-9][0-9]*)\Z")
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
    recorder = None
    if call_log_db := os.environ.get("AVIBE_MEMORY_CALL_LOG_DB"):
        recorder = prepare_call_recorder(Path(call_log_db))

    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()
    if recorder is not None:
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def recorder_lifespan(app_instance: Any) -> Any:
            try:
                recorder.start()
            except Exception:
                logger.warning("memory_call_recorder_start_failed", exc_info=True)
            try:
                async with original_lifespan(app_instance) as state:
                    yield state
            finally:
                try:
                    await recorder.close(timeout=1.0)
                except Exception:
                    logger.warning("memory_call_recorder_close_failed", exc_info=True)

        app.router.lifespan_context = recorder_lifespan
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
        if recorder is not None and request.url.path in {
            "/api/v2/memory/add",
            "/api/v2/memory/flush",
        }:
            with boundary_request():
                return await call_next(request)
        return await call_next(request)

    config = uvicorn.Config(
        _RecorderHealthProjection(app, recorder),
        uds=str(uds),
        access_log=False,
        log_level="warning",
        log_config=None,
        timeout_graceful_shutdown=1,
    )
    uvicorn.Server(config).run()


class _RecorderHealthProjection:
    """Append recorder state to the existing EverOS health response."""

    def __init__(self, app: Any, recorder: Any | None) -> None:
        self._app = app
        self._recorder = recorder

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/health"
        ):
            await self._app(scope, receive, send)
            return

        messages: list[dict[str, Any]] = []

        async def capture(message: dict[str, Any]) -> None:
            messages.append(message)

        await self._app(scope, receive, capture)
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message.get("type") == "http.response.body"
        )
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["recorder"] = _recorder_health(self._recorder)
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        for message in messages:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"content-length"
                ]
                headers.append((b"content-length", str(len(encoded)).encode("ascii")))
                projected = dict(message)
                projected["headers"] = headers
                await send(projected)
                break
        await send({"type": "http.response.body", "body": encoded})


def _recorder_health(recorder: Any | None) -> dict[str, str | None]:
    if recorder is None:
        return {"state": "disabled", "reason": None}
    try:
        health = recorder.health
    except Exception:
        return {"state": "degraded", "reason": "writer_failures"}
    if not isinstance(health, dict):
        return {"state": "degraded", "reason": "writer_failures"}
    state = health.get("state")
    reason = health.get("reason")
    if state not in {"active", "degraded", "disabled"} or not (
        reason is None or isinstance(reason, str)
    ):
        return {"state": "degraded", "reason": "writer_failures"}
    return {"state": state, "reason": reason}


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
    if len(body) > _MAX_BODY_BYTES:
        return "body"
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
        if not _valid_workbench_attachment(item, attachments_root):
            return "add"
    return None


def _valid_workbench_attachment(item: dict[str, Any], attachments_root: Path | None) -> bool:
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
    keys = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size"}
    if not _exact_keys(payload, keys):
        return "get"
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


def _valid_principal(value: object) -> bool:
    return isinstance(value, str) and _PRINCIPAL_PATTERN.fullmatch(value) is not None


def _valid_session(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value.encode("utf-8")) <= 128
        and _SESSION_PATTERN.fullmatch(value) is not None
    )


def _processing_healthy_from_child_environment() -> bool:
    """Run fixed authenticated probes only inside the scrubbed owned child."""

    from core.memory.everos import EverOSPort

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
