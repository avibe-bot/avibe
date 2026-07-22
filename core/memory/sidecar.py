"""Child-only UDS launcher for the pinned EverOS ASGI factory."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any


_MAX_BODY_BYTES = 64 * 1024
_APP_ID = "avibe"
_PROJECT_ID = "personal"


def serve(uds: Path, owner_id: str) -> None:
    if version("everos") != "1.1.3":
        raise RuntimeError("everos version mismatch")
    if uds.exists() or not uds.parent.is_dir():
        raise RuntimeError("invalid sidecar socket path")
    if not owner_id:
        raise RuntimeError("missing sidecar owner")
    os.umask(0o077)

    from starlette.responses import JSONResponse
    import uvicorn

    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()

    @app.middleware("http")
    async def guard(request: Any, call_next: Any) -> Any:
        body = await request.body()
        if _request_rejection(request.method, request.url.path, body, owner_id) is not None:
            return JSONResponse({"detail": "memory_request_rejected"}, status_code=403)
        return await call_next(request)

    config = uvicorn.Config(app, uds=str(uds), access_log=False, log_level="warning", log_config=None)
    uvicorn.Server(config).run()


def _request_rejection(method: str, path: str, body: bytes, owner_id: str) -> str | None:
    if method == "GET" and path == "/health":
        return None
    if method != "POST" or path not in {
        "/api/v1/memory/add",
        "/api/v1/memory/flush",
        "/api/v1/memory/search",
        "/api/v1/memory/get",
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
    if path == "/api/v1/memory/add":
        return _validate_add(payload, owner_id)
    if path == "/api/v1/memory/flush":
        return _validate_flush(payload)
    if path == "/api/v1/memory/search":
        return _validate_search(payload, owner_id)
    return _validate_get(payload, owner_id)


def _valid_scope(payload: dict[str, Any]) -> bool:
    return payload.get("app_id") == _APP_ID and payload.get("project_id") == _PROJECT_ID


def _exact_keys(payload: dict[str, Any], keys: set[str]) -> bool:
    return set(payload) == keys


def _validate_add(payload: dict[str, Any], owner_id: str) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id", "messages"}) or not _valid_scope(payload):
        return "add"
    messages = payload.get("messages")
    if not isinstance(payload.get("session_id"), str) or not isinstance(messages, list) or len(messages) != 1:
        return "add"
    message = messages[0]
    if not isinstance(message, dict) or set(message) != {"sender_id", "role", "timestamp", "content"}:
        return "add"
    if (
        message.get("sender_id") != owner_id
        or message.get("role") != "user"
        or not isinstance(message.get("timestamp"), int)
        or isinstance(message.get("timestamp"), bool)
        or not isinstance(message.get("content"), str)
    ):
        return "add"
    return None


def _validate_flush(payload: dict[str, Any]) -> str | None:
    if not _exact_keys(payload, {"session_id", "app_id", "project_id"}) or not _valid_scope(payload):
        return "flush"
    return None if isinstance(payload.get("session_id"), str) else "flush"


def _validate_search(payload: dict[str, Any], owner_id: str) -> str | None:
    keys = {"user_id", "app_id", "project_id", "query", "method", "top_k", "include_profile", "enable_llm_rerank"}
    if not _exact_keys(payload, keys) or not _valid_scope(payload):
        return "search"
    if (
        payload.get("user_id") != owner_id
        or not isinstance(payload.get("query"), str)
        or payload.get("method") != "hybrid"
        or not isinstance(payload.get("top_k"), int)
        or isinstance(payload.get("top_k"), bool)
        or not 1 <= payload["top_k"] <= 20
        or payload.get("include_profile") is not True
        or payload.get("enable_llm_rerank") is not False
    ):
        return "search"
    return None


def _validate_get(payload: dict[str, Any], owner_id: str) -> str | None:
    keys = {"user_id", "app_id", "project_id", "memory_type", "page", "page_size", "sort_by", "sort_order"}
    if not _exact_keys(payload, keys) or not _valid_scope(payload):
        return "get"
    if (
        payload.get("user_id") != owner_id
        or payload.get("memory_type") not in {"profile", "episode"}
        or payload.get("page") != 1
        or payload.get("page_size") != 20
        or payload.get("sort_by") != "timestamp"
        or payload.get("sort_order") != "desc"
    ):
        return "get"
    return None


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
    )
    return asyncio.run(provider.processing_healthy())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds")
    parser.add_argument("--owner-id")
    parser.add_argument("--probe-processing", action="store_true")
    args = parser.parse_args()
    if args.probe_processing:
        return 0 if _processing_healthy_from_child_environment() else 1
    if not args.uds or not args.owner_id:
        parser.error("--uds and --owner-id are required when serving")
    serve(Path(args.uds), args.owner_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
