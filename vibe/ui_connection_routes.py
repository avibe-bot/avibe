"""The connection-methods page has only a read and a write. The browser never confirms."""
from __future__ import annotations

import logging

from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from config.v2_config import V2Config
from vibe import connection_runtime as runtime, connection_settings as cs
from vibe.ui_compat import request as current_request

logger = logging.getLogger(__name__)
BASE = "/api/devices/connection"


def _owner_binding():
    from vibe import ui_server

    viewer = ui_server._device_viewer()
    if viewer and viewer[2]:
        binding, subject, _, sid = viewer
        if binding and sid and subject:
            return cs.Actor(binding)
        if (not sid and not subject and not ui_server._is_direct_ingress_request()
                and ui_server._is_direct_loopback_connection()):
            # A delegated CLI request arrives locally, but must not acquire the
            # local operator exemption. This header can only narrow authority.
            delegated = current_request.headers.get("X-Vibe-Delegated-Change") == "1"
            binding = cs.binding(V2Config.load())
            if delegated and current_request.headers.get('X-Vibe-Expected-Binding') != binding:
                raise cs.ConnectionError('connection_binding_changed', 403)
            return cs.Actor(binding, local=not delegated)
    raise cs.ConnectionError("connection_owner_required", 403)


def _reply(data, code=200, *, background=None):
    return JSONResponse(data, status_code=code, headers={"Cache-Control": "no-store"}, background=background)


def register_connection_routes(app):
    async def dispatch(request, func):
        def handle():
            try:
                return func(_owner_binding())
            except cs.ConnectionError as exc:
                return _reply({"error": exc.code}, exc.status)
            except (OSError, ValueError):
                logger.warning("Connection settings unavailable", exc_info=True)
                return _reply({"error": "connection_state_unavailable"}, 503)
        return await app.dispatch_native_request(request, handle)

    @app.get(BASE)
    async def connection_state(request: Request):
        return await dispatch(request, lambda binding: _reply(runtime.view(binding)))

    @app.patch(BASE)
    async def connection_change(request: Request):
        def change(binding):
            key, new = cs.validated_change(current_request.json)
            op = runtime.prepare(binding, key, new)
            if op is None:
                return _reply(runtime.view(binding))
            # Starlette sends the final ASGI body before running this. Nothing
            # (including the setting on disk) retires a stream before that send.
            return _reply(runtime.view(binding), 202, background=BackgroundTask(runtime.apply, binding, op))
        return await dispatch(request, change)
