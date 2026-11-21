"""Device-only JSON routes and owner-only, value-free pairing metadata."""

from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import Request
from fastapi.responses import JSONResponse

from core.handlers.mobile_health import current_identity, request_metadata
from core.mobile_health_contract import (
    MAX_BODY_BYTES, MobileError, integer, object_fields, text,
)
from storage.mobile_store import MobileStore

DEVICE_PREFIX = "/api/mobile/device/v1"
_REQUEST_ID = r"mreq_[0-9a-f]{32}"
_DEVICE_ROUTES = {
    ("POST", f"{DEVICE_PREFIX}/binding-ticket"),
    ("POST", f"{DEVICE_PREFIX}/pair"),
    ("PUT", f"{DEVICE_PREFIX}/manifest"),
    ("PUT", f"{DEVICE_PREFIX}/health/days"),
    ("POST", f"{DEVICE_PREFIX}/requests/claim"),
    ("DELETE", f"{DEVICE_PREFIX}/health"),
    ("DELETE", f"{DEVICE_PREFIX}/"),
}


def is_device_route(method: str, path: str) -> bool:
    return (method, path) in _DEVICE_ROUTES or (
        method == "POST"
        and re.fullmatch(rf"{DEVICE_PREFIX}/requests/{_REQUEST_ID}/result", path) is not None
    )


def canonical_device_route(request: Request) -> bool:
    """Do not widen the exemption through encoded slashes, dots or route aliases."""
    return is_device_route(request.method, request.scope["path"]) and (
        request.scope.get("raw_path", b"") == request.scope["path"].encode("ascii")
    )


def mobile_error_response(error: MobileError) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if error.status in {429, 503}:
        headers["Retry-After"] = "30"
    return JSONResponse({"ok": False, "code": error.code, "error": error.code},
                        status_code=error.status, headers=headers)


async def _body(request: Request, *, optional=False) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise MobileError("invalid_payload", 413)
    if optional and not body:
        return {}
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise MobileError("invalid_payload")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise MobileError("invalid_payload") from None
    if not isinstance(payload, dict):
        raise MobileError("invalid_payload")
    return payload


def register_mobile_routes(app) -> None:
    async def dispatch_device(request: Request):
        async def handler():
            try:
                if not canonical_device_route(request):
                    raise MobileError("device_unauthorized", 401)
                store = await asyncio.to_thread(MobileStore)
                payload = None
                device = None
                if request.url.path != f"{DEVICE_PREFIX}/pair":
                    authorization = request.headers.getlist("authorization")
                    if len(authorization) != 1 or not re.fullmatch(
                        r"Bearer [A-Za-z0-9_-]{43}", authorization[0],
                    ):
                        raise MobileError("device_unauthorized", 401)
                    # First reject unknown credentials without a control-plane call.
                    device = await asyncio.to_thread(store.lookup_credential, authorization[0][7:])
                else:
                    payload = await _body(request)
                    await asyncio.to_thread(store.precheck_pairing, payload)
                identity = await asyncio.to_thread(current_identity, store)
                if device is not None:
                    device = await asyncio.to_thread(store.authenticate, authorization[0][7:], identity)
                if payload is None:
                    payload = await _body(request, optional=request.method == "DELETE")
                path = request.scope["path"]
                if path == f"{DEVICE_PREFIX}/pair":
                    from storage.device_health_registry import pair
                    result = await asyncio.to_thread(pair, store, payload, identity)
                elif path == f"{DEVICE_PREFIX}/binding-ticket":
                    from config.v2_config import V2Config
                    from storage import device_health_tickets
                    from vibe.remote_access import device_binding

                    object_fields(payload, set())
                    def issue_ticket():
                        config = V2Config.load()
                        if config.remote_access.vibe_cloud.instance_id != identity['id']:
                            raise MobileError('instance_not_eligible', 403)
                        return device_health_tickets.issue(store, device, device_binding(config))
                    result = await asyncio.to_thread(issue_ticket)
                elif path == f"{DEVICE_PREFIX}/manifest":
                    result = await asyncio.to_thread(store.manifest, device, payload)
                elif path == f"{DEVICE_PREFIX}/health/days":
                    result = await asyncio.to_thread(store.upload, device, payload)
                elif path == f"{DEVICE_PREFIX}/requests/claim":
                    object_fields(payload, {"wait_seconds", "max"}, {"request_id"})
                    seconds = integer(payload["wait_seconds"], 0, 25)
                    maximum = integer(payload["max"], 1, 10)
                    request_id = payload.get("request_id")
                    if request_id is not None and re.fullmatch(_REQUEST_ID, text(request_id)) is None:
                        raise MobileError("invalid_payload")
                    deadline = time.monotonic() + seconds
                    await asyncio.to_thread(store.set_polling, device["id"], seconds)
                    try:
                        while True:
                            result = await asyncio.to_thread(
                                store.claim, device["id"], maximum=maximum, request_id=request_id,
                            )
                            if result["requests"] or time.monotonic() >= deadline:
                                break
                            await asyncio.sleep(min(0.2, max(0, deadline - time.monotonic())))
                    finally:
                        await asyncio.to_thread(store.clear_polling, device["id"])
                elif path.endswith("/result"):
                    result = await asyncio.to_thread(store.result, device, path.split("/")[-2], payload)
                else:
                    object_fields(payload, set())
                    result = await asyncio.to_thread(store.revoke, device_id=device["id"], caller="device")
                    if path == f"{DEVICE_PREFIX}/":
                        result = {}
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except MobileError as error:
                return mobile_error_response(error)

        return await app.dispatch_native_request(request, handler, parse_json=False)

    for method, path in sorted(_DEVICE_ROUTES):
        app.add_api_route(path, dispatch_device, methods=[method], include_in_schema=False)
    app.add_api_route(
        f"{DEVICE_PREFIX}/requests/{{request_id}}/result", dispatch_device,
        methods=["POST"], include_in_schema=False,
    )

    async def pairing(request: Request):
        async def handler():
            from vibe.ui_compat import g
            try:
                context = getattr(g, "authorization_context", None)
                if context is None or not context.is_instance_owner:
                    raise MobileError("instance_access_forbidden", 403)
                payload = await _body(request)
                object_fields(payload, {"request_id"})
                store = await asyncio.to_thread(MobileStore)
                identity = await asyncio.to_thread(current_identity, store)
                if context.is_remote and (
                    not context.email or context.email.casefold() != identity["owner"]
                    or context.instance_id != identity["id"]
                ):
                    raise MobileError("instance_not_eligible", 403)
                from storage.device_health_registry import issue_pairing
                from vibe.ui_server import _device_viewer
                result = await asyncio.to_thread(issue_pairing, store, text(payload['request_id']), identity, _device_viewer())
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            except MobileError as error:
                return mobile_error_response(error)
        return await app.dispatch_native_request(request, handler, parse_json=False)

    async def metadata(request: Request, request_id: str):
        async def handler():
            from vibe.ui_compat import g
            try:
                context = getattr(g, "authorization_context", None)
                if context is None or not context.is_instance_owner:
                    raise MobileError("instance_access_forbidden", 403)
                store = await asyncio.to_thread(MobileStore)
                identity = await asyncio.to_thread(current_identity, store)
                if context.is_remote and (
                    not context.email or context.email.casefold() != identity["owner"]
                    or context.instance_id != identity["id"]
                ):
                    raise MobileError("instance_not_eligible", 403)
                result = await asyncio.to_thread(store.request_for_identity, request_id, identity)
                return JSONResponse(request_metadata(result), headers={"Cache-Control": "no-store"})
            except MobileError as error:
                return mobile_error_response(error)
        return await app.dispatch_native_request(request, handler, parse_json=False)

    app.add_api_route("/api/mobile/pairings", pairing, methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/mobile/requests/{request_id}", metadata, methods=["GET"], include_in_schema=False)
