"""Child-only EverOS ASGI launcher.

The parent harness never imports EverOS. This module is executed only with the
locked environment's Python after process ownership and environment isolation
have been established.
"""

from __future__ import annotations

import argparse
import importlib
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .constants import EVEROS_VERSION
from .metrics import append_request_metric, classify_request_path
from .request_guard import validate_request


def _install_request_counter(metrics_path: Path) -> None:
    import httpx

    original_async_send = httpx.AsyncClient.send
    original_sync_send = httpx.Client.send

    def usage_from_response(response: Any) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            return payload["usage"]
        return None

    async def tracked_async_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        kind = classify_request_path(str(request.url.path))
        try:
            response = await original_async_send(self, request, *args, **kwargs)
        except Exception:  # noqa: BLE001
            append_request_metric(metrics_path, kind=kind)
            raise
        append_request_metric(metrics_path, kind=kind, usage=usage_from_response(response))
        return response

    def tracked_sync_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        kind = classify_request_path(str(request.url.path))
        try:
            response = original_sync_send(self, request, *args, **kwargs)
        except Exception:  # noqa: BLE001
            append_request_metric(metrics_path, kind=kind)
            raise
        append_request_metric(metrics_path, kind=kind, usage=usage_from_response(response))
        return response

    httpx.AsyncClient.send = tracked_async_send
    httpx.Client.send = tracked_sync_send


def serve(uds: Path) -> None:
    if version("everos") != EVEROS_VERSION:
        raise RuntimeError("everos_version_mismatch")
    if uds.exists():
        raise RuntimeError("socket_path_already_exists")
    if not uds.parent.is_dir():
        raise RuntimeError("socket_directory_missing")
    owner_id = os.environ.get("MEMORY_POC_OWNER_ID")
    if not owner_id:
        raise RuntimeError("fixed_owner_missing")
    os.umask(0o077)
    metrics_value = os.environ.get("MEMORY_POC_REQUEST_METRICS")
    if metrics_value:
        _install_request_counter(Path(metrics_value))

    from starlette.responses import JSONResponse
    import uvicorn

    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()

    @app.middleware("http")
    async def guard(request: Any, call_next: Any) -> Any:
        rejection = validate_request(request.method, request.url.path, await request.body(), owner_id=owner_id)
        if rejection is not None:
            return JSONResponse({"detail": "memory_poc_request_rejected"}, status_code=403)
        return await call_next(request)

    config = uvicorn.Config(app, uds=str(uds), access_log=False, log_level="warning", log_config=None)
    uvicorn.Server(config).run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uds", required=True)
    args = parser.parse_args()
    serve(Path(args.uds))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
