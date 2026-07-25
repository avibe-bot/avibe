"""The ``/api/memory/*`` UI surface.

Every route here is direct-loopback only and answers with the closed Memory
result envelope (``{"status": "ok", ...}`` / ``{"status": "failed", "error":
<closed code>}``) plus ``Cache-Control: no-store``. Keeping them in one module
means the admission check, the envelope and the no-store header are stated once
per route group instead of being scattered through ``vibe/ui_server.py``.

Registration is a function rather than a FastAPI ``APIRouter`` because the UI
app is a ``CompatApp`` whose ``before_request`` / ``after_request`` hooks are
reached through ``dispatch_native_request``; an ``APIRouter`` would bypass them.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from fastapi import Request as FastAPIRequest

from config.v2_config import V2Config
from vibe.ui_compat import Response, jsonify


def _direct_loopback_memory_request() -> bool:
    """Ask ``ui_server`` for the strict Memory admission verdict.

    Late-bound on purpose: the predicate belongs with the other request-locality
    helpers in ``ui_server``, and resolving it per call keeps it patchable there.
    """

    from vibe import ui_server

    return ui_server.is_direct_loopback_memory_request()


def _memory_response(payload: dict, *, status_code: int = 200) -> Response:
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


def _memory_forbidden_response() -> Response:
    return _memory_response({"status": "failed", "error": "memory_disabled"}, status_code=403)


def _memory_response_body(response: Response) -> dict:
    try:
        value = json.loads(response.body)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _memory_internal_response(call: Callable[[], Any]) -> Response:
    from vibe import internal_client

    try:
        result = await call()
    except internal_client.InternalServerUnavailable:
        return _memory_response(
            {"status": "failed", "error": "memory_sidecar_unavailable"},
            status_code=503,
        )
    body = result.get("body") or {}
    if not isinstance(body, dict):
        body = {"status": "failed", "error": "memory_provider_response_invalid"}
    return _memory_response(body, status_code=result.get("status_code", 503))


def _memory_settings_payload() -> dict:
    from config.v2_config import memory_config_to_payload

    # Tag the response, not `memory_config_to_payload` itself: the same helper
    # feeds the persisted config, which must stay free of result envelopes.
    return {**memory_config_to_payload(V2Config.load().memory), "status": "ok"}


def _memory_settings_patch(current: V2Config, patch_payload: object) -> dict:
    """Merge one write-only Memory settings PATCH."""

    from config.v2_config import memory_config_to_payload

    if not isinstance(patch_payload, dict) or not set(patch_payload).issubset({"enabled", "processing"}):
        raise ValueError("invalid_memory_patch")
    target = memory_config_to_payload(current.memory, include_secrets=True)
    for endpoint in ("llm", "embedding"):
        target["processing"][endpoint].pop("has_api_key", None)
    if "enabled" in patch_payload:
        if not isinstance(patch_payload["enabled"], bool):
            raise ValueError("invalid_memory_patch")
        target["enabled"] = patch_payload["enabled"]
    processing_patch = patch_payload.get("processing")
    if processing_patch is not None:
        if not isinstance(processing_patch, dict) or not set(processing_patch).issubset({"llm", "embedding"}):
            raise ValueError("invalid_memory_patch")
        for endpoint in ("llm", "embedding"):
            endpoint_patch = processing_patch.get(endpoint)
            if endpoint_patch is None:
                continue
            if not isinstance(endpoint_patch, dict) or not set(endpoint_patch).issubset({"base_url", "model", "api_key"}):
                raise ValueError("invalid_memory_patch")
            target["processing"][endpoint].update(endpoint_patch)

    explicit_key_clear = any(
        endpoint_patch.get("api_key") in {None, ""}
        for endpoint_patch in (processing_patch or {}).values()
        if isinstance(endpoint_patch, dict) and "api_key" in endpoint_patch
    )
    if explicit_key_clear and target["enabled"]:
        raise ValueError("memory_key_clear_while_enabled")
    return target


def _memory_candidate_config(current: V2Config, memory_payload: dict) -> V2Config:
    from vibe.api import config_to_payload

    full_payload = config_to_payload(current, include_secrets=True)
    full_payload["memory"] = memory_payload
    return V2Config.from_payload(full_payload)


def _memory_embedding_configuration_changed(current: V2Config, candidate: V2Config) -> bool:
    """Return whether the persisted vector-space identity would change."""

    current_embedding = current.memory.processing.embedding
    candidate_embedding = candidate.memory.processing.embedding
    return (
        current_embedding.base_url != candidate_embedding.base_url
        or current_embedding.model != candidate_embedding.model
    )


def _memory_closed_error(payload: dict, *, fallback: str) -> str:
    from core.memory.types import is_memory_error_code

    value = payload.get("error")
    return value if is_memory_error_code(value) else fallback


def register_memory_routes(app) -> None:
    """Attach the Memory routes to the UI app."""

    @app.get("/api/memory/settings", include_in_schema=False)
    async def memory_settings_get(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            try:
                return _memory_response(await asyncio.to_thread(_memory_settings_payload))
            except Exception:
                return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.patch("/api/memory/settings", include_in_schema=False)
    async def memory_settings_patch(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            try:
                patch_payload = await starlette_request.json()
                current = await asyncio.to_thread(V2Config.load)
                target_payload = _memory_settings_patch(current, patch_payload)
                candidate = _memory_candidate_config(current, target_payload)
                embedding_change_pending = (
                    current.memory.embedding_change_pending
                    or _memory_embedding_configuration_changed(current, candidate)
                )
            except (TypeError, ValueError):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)

            from vibe import internal_client

            from vibe import api

            try:
                # Persist a durable marker before asking the controller to inspect
                # the root. If Avibe exits in this interval, startup must re-run
                # the same guarded inspection instead of treating the candidate as
                # its own embedding baseline.
                saved = await asyncio.to_thread(
                    api.save_memory_config,
                    target_payload,
                    embedding_change_pending=embedding_change_pending,
                )
            except ValueError:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            except Exception:
                return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)
            response = await _memory_internal_response(internal_client.reconcile_memory)
            runtime_payload = _memory_response_body(response)
            if response.status_code != 200 or runtime_payload.get("ok") is not True:
                # Persisted settings must not outrun the controller's closed
                # compatibility decision, including while memory is disabled.
                try:
                    from config.v2_config import memory_config_to_payload

                    await asyncio.to_thread(
                        api.save_memory_config,
                        memory_config_to_payload(current.memory, include_secrets=True),
                        embedding_change_pending=current.memory.embedding_change_pending,
                    )
                    await _memory_internal_response(internal_client.reconcile_memory)
                except Exception:
                    pass
                return _memory_response(
                    {
                        "status": "failed",
                        "error": _memory_closed_error(runtime_payload, fallback="memory_sidecar_unavailable"),
                    },
                    status_code=409,
                )
            if response.status_code >= 500:
                return response
            from config.v2_config import memory_config_to_payload

            payload = memory_config_to_payload(saved.memory)
            payload["runtime"] = runtime_payload
            payload["status"] = "ok"
            return _memory_response(payload)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/status", include_in_schema=False)
    async def memory_status_get(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(internal_client.memory_status)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/failures", include_in_schema=False)
    async def memory_failures_get(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(internal_client.memory_failures)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/profile", include_in_schema=False)
    async def memory_profile_get(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_profile(user_key="avibe:local")
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/search", include_in_schema=False)
    async def memory_search_post(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict) or not set(payload).issubset({"query", "limit"}):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            query = payload.get("query")
            limit = payload.get("limit", 8)
            if not isinstance(query, str) or not isinstance(limit, int) or isinstance(limit, bool):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_search(query, limit, user_key="avibe:local")
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/clear", include_in_schema=False)
    async def memory_clear_post(starlette_request: FastAPIRequest):
        async def handler():
            if not _direct_loopback_memory_request():
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if payload != {"confirm": True}:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe import internal_client

            return await _memory_internal_response(internal_client.memory_clear)

        return await app.dispatch_native_request(starlette_request, handler)
