"""Child-only EverOS ASGI launcher.

The parent harness never imports EverOS. This module is executed only with the
locked environment's Python after process ownership and environment isolation
have been established.
"""

from __future__ import annotations

import argparse
import ipaddress
import importlib
import os
import socket
from contextvars import ContextVar
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .constants import EVEROS_VERSION
from .metrics import append_egress_metric, append_request_metric, classify_request_path
from .request_guard import validate_request

_REQUEST_PHASE: ContextVar[str] = ContextVar("memory_poc_request_phase", default="unattributed")


def _install_request_counter(metrics_path: Path, *, egress_path: Path | None = None) -> None:
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
        if egress_path is not None:
            append_egress_metric(egress_path, hostname=getattr(request.url, "host", None))
        try:
            response = await original_async_send(self, request, *args, **kwargs)
        except Exception:  # noqa: BLE001
            append_request_metric(metrics_path, kind=kind, phase=_REQUEST_PHASE.get())
            raise
        append_request_metric(
            metrics_path,
            kind=kind,
            usage=usage_from_response(response),
            phase=_REQUEST_PHASE.get(),
        )
        return response

    def tracked_sync_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
        kind = classify_request_path(str(request.url.path))
        if egress_path is not None:
            append_egress_metric(egress_path, hostname=getattr(request.url, "host", None))
        try:
            response = original_sync_send(self, request, *args, **kwargs)
        except Exception:  # noqa: BLE001
            append_request_metric(metrics_path, kind=kind, phase=_REQUEST_PHASE.get())
            raise
        append_request_metric(
            metrics_path,
            kind=kind,
            usage=usage_from_response(response),
            phase=_REQUEST_PHASE.get(),
        )
        return response

    httpx.AsyncClient.send = tracked_async_send
    httpx.Client.send = tracked_sync_send
    if egress_path is not None:
        _install_egress_counter(egress_path)


def _install_egress_counter(egress_path: Path) -> None:
    """Record network destinations without retaining URLs, ports, or IP values."""
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    resolved_ip_literals: set[str] = set()

    def tracked_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        append_egress_metric(egress_path, hostname=host if isinstance(host, str) else None)
        result = original_getaddrinfo(host, *args, **kwargs)
        if isinstance(host, str) and not _is_ip_literal(host):
            for item in result:
                if isinstance(item, tuple) and len(item) >= 5 and isinstance(item[4], tuple) and item[4]:
                    address = item[4][0]
                    if isinstance(address, str) and _is_ip_literal(address):
                        resolved_ip_literals.add(address)
        return result

    def record_socket_address(address: Any) -> None:
        if not isinstance(address, tuple) or not address or not isinstance(address[0], str):
            return
        host = address[0]
        if _is_ip_literal(host) and host in resolved_ip_literals:
            return
        append_egress_metric(egress_path, hostname=host)

    def tracked_connect(self: Any, address: Any) -> Any:
        record_socket_address(address)
        return original_connect(self, address)

    def tracked_connect_ex(self: Any, address: Any) -> Any:
        record_socket_address(address)
        return original_connect_ex(self, address)

    socket.getaddrinfo = tracked_getaddrinfo
    socket.socket.connect = tracked_connect
    socket.socket.connect_ex = tracked_connect_ex


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return False
    return True


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
        egress_value = os.environ.get("MEMORY_POC_EGRESS_METRICS")
        _install_request_counter(Path(metrics_value), egress_path=Path(egress_value) if egress_value else None)

    from starlette.responses import JSONResponse
    import uvicorn

    factory_module = importlib.import_module("everos.entrypoints.api.app")
    create_app = getattr(factory_module, "create_app")
    app = create_app()

    @app.middleware("http")
    async def guard(request: Any, call_next: Any) -> Any:
        phase = request.headers.get("x-memory-poc-phase", "unattributed")
        rejection = validate_request(request.method, request.url.path, await request.body(), owner_id=owner_id, phase=phase)
        if rejection is not None:
            return JSONResponse({"detail": "memory_poc_request_rejected"}, status_code=403)
        token = _REQUEST_PHASE.set(phase if phase in {"ingestion", "read", "health", "research"} else "unattributed")
        try:
            return await call_next(request)
        finally:
            _REQUEST_PHASE.reset(token)

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
