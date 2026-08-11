"""The ``/api/memory/*`` UI surface.

Every route here requires a trusted local or authenticated Avibe Cloud browser
and answers with the closed Memory result envelope (``{"status": "ok", ...}`` /
``{"status": "failed", "error":
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
import re
from typing import Any, Callable

from fastapi import Request as FastAPIRequest

from config.v2_config import V2Config
from vibe.ui_compat import Response, jsonify


_MEMORY_LOG_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_MEMORY_LOG_ENTRY_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def _memory_ui_user_key() -> str | None:
    """Ask ``ui_server`` for the trusted browser's Memory identity.

    Late-bound on purpose: the policy belongs with the other request-locality
    helpers in ``ui_server``, and resolving it per call keeps it patchable there.
    """

    from vibe import ui_server

    return ui_server.memory_ui_user_key()


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


def _memory_log_list_query(request: FastAPIRequest) -> tuple[str | None, int]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if any(key not in {"cursor", "limit"} for key in keys) or len(keys) != len(set(keys)):
        raise ValueError("invalid memory log query")
    values = dict(items)
    cursor = values.get("cursor")
    if cursor is not None and _MEMORY_LOG_CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError("invalid memory log cursor")
    raw_limit = values.get("limit", "20")
    if not raw_limit.isascii() or not raw_limit.isdecimal():
        raise ValueError("invalid memory log limit")
    limit = int(raw_limit)
    if not 1 <= limit <= 50:
        raise ValueError("invalid memory log limit")
    return cursor, limit


def _memory_log_entry_query(request: FastAPIRequest) -> str:
    items = list(request.query_params.multi_items())
    if len(items) != 1 or items[0][0] != "memcell_id":
        raise ValueError("invalid memory log entry query")
    memcell_id = items[0][1]
    if _MEMORY_LOG_ENTRY_ID_RE.fullmatch(memcell_id) is None:
        raise ValueError("invalid memory log entry id")
    return memcell_id


async def _memory_internal_result(call: Callable[[], Any]) -> tuple[dict, int]:
    from vibe import internal_client

    try:
        result = await call()
    except internal_client.InternalServerUnavailable:
        return {"status": "failed", "error": "memory_sidecar_unavailable"}, 503
    body = result.get("body") or {}
    if not isinstance(body, dict):
        body = {"status": "failed", "error": "memory_provider_response_invalid"}
    return body, result.get("status_code", 503)


async def _memory_internal_response(call: Callable[[], Any]) -> Response:
    body, status_code = await _memory_internal_result(call)
    return _memory_response(body, status_code=status_code)


def _memory_settings_projection(memory: object) -> dict:
    from config.v2_config import memory_config_to_payload

    payload = memory_config_to_payload(memory)
    payload.pop("diagnostics", None)
    return payload


def _memory_settings_payload() -> dict:
    # Tag the response, not `memory_config_to_payload` itself: the same helper
    # feeds the persisted config, which must stay free of result envelopes.
    memory = V2Config.load().memory
    payload = _memory_settings_projection(memory)
    payload["status"] = "ok"
    # Read-only projection while a durable rebuild marker is pending.
    payload["rebuild_required"] = bool(memory.embedding_change_pending)
    return payload


def _memory_settings_patch(current: V2Config, patch_payload: object) -> tuple[dict, bool]:
    """Merge one write-only Memory settings PATCH.

    Returns ``(target_payload, confirm_rebuild)``. ``confirm_rebuild`` is accepted
    only as an exact boolean and never becomes part of the persisted candidate.
    """

    from config.v2_config import memory_config_to_payload

    if not isinstance(patch_payload, dict) or not set(patch_payload).issubset(
        {"enabled", "processing", "confirm_rebuild"}
    ):
        raise ValueError("invalid_memory_patch")
    confirm_rebuild = patch_payload.get("confirm_rebuild", False)
    if not isinstance(confirm_rebuild, bool):
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
    return target, confirm_rebuild


def _memory_candidate_config(current: V2Config, memory_payload: dict) -> V2Config:
    from vibe.api import config_to_payload

    full_payload = config_to_payload(current, include_secrets=True)
    full_payload["memory"] = memory_payload
    return V2Config.from_payload(full_payload)


def _memory_embedding_configuration_changed(current: V2Config, candidate: V2Config) -> bool:
    """Return whether an already-established vector-space identity would change.

    First-time configuration of empty embedding fields is ordinary setup, not a
    rebuild. Only a change away from a previously set base_url/model requires
    confirm_rebuild.
    """

    current_embedding = current.memory.processing.embedding
    candidate_embedding = candidate.memory.processing.embedding
    current_base = (current_embedding.base_url or "").strip()
    current_model = (current_embedding.model or "").strip()
    if not current_base and not current_model:
        return False
    return (
        current_base != (candidate_embedding.base_url or "").strip()
        or current_model != (candidate_embedding.model or "").strip()
    )


def _memory_closed_error(payload: dict, *, fallback: str) -> str:
    from core.memory.types import is_memory_error_code

    value = payload.get("error")
    return value if is_memory_error_code(value) else fallback


_settings_write_lock: asyncio.Lock | None = None
_settings_write_lock_loop: asyncio.AbstractEventLoop | None = None
_restart_request_task: asyncio.Task[tuple[dict, int]] | None = None
_restart_request_task_loop: asyncio.AbstractEventLoop | None = None
_rebuild_request_task: asyncio.Task[tuple[dict, int]] | None = None
_rebuild_request_task_loop: asyncio.AbstractEventLoop | None = None


def _memory_settings_write_lock() -> asyncio.Lock:
    """Return this process' Memory settings write lock, bound to the live loop.

    Created lazily instead of at import time: ``asyncio.Lock`` binds itself to
    the first loop that awaits it, while the UI app object outlives individual
    loops (in-process restart, test clients), so a module-level lock would start
    raising once a new loop took over.
    """

    global _settings_write_lock, _settings_write_lock_loop

    loop = asyncio.get_running_loop()
    if _settings_write_lock is None or _settings_write_lock_loop is not loop:
        _settings_write_lock = asyncio.Lock()
        _settings_write_lock_loop = loop
    return _settings_write_lock


async def _run_memory_restart_request() -> tuple[dict, int]:
    from vibe import internal_client

    async with _memory_settings_write_lock():
        return await _memory_internal_result(internal_client.memory_restart)


def _memory_restart_request_task() -> asyncio.Task[tuple[dict, int]]:
    """Create or join the UI process' loop-scoped restart request owner."""

    global _restart_request_task, _restart_request_task_loop

    loop = asyncio.get_running_loop()
    if _restart_request_task_loop is not loop:
        _restart_request_task = None
        _restart_request_task_loop = loop
    if _restart_request_task is None:
        task = loop.create_task(_run_memory_restart_request())
        _restart_request_task = task

        def clear_finished(finished: asyncio.Task[tuple[dict, int]]) -> None:
            global _restart_request_task

            if _restart_request_task is finished:
                _restart_request_task = None

        task.add_done_callback(clear_finished)
    return _restart_request_task


async def _run_memory_rebuild_request() -> tuple[dict, int]:
    # Intentionally not under the settings write lock: confirmed settings saves
    # already hold that lock and then join this retained task. Controller-side
    # rebuild ownership serializes the destructive work.
    from vibe import internal_client

    return await _memory_internal_result(internal_client.memory_rebuild)


def _memory_rebuild_request_task() -> asyncio.Task[tuple[dict, int]]:
    """Create or join the UI process' loop-scoped rebuild request owner."""

    global _rebuild_request_task, _rebuild_request_task_loop

    loop = asyncio.get_running_loop()
    if _rebuild_request_task_loop is not loop:
        _rebuild_request_task = None
        _rebuild_request_task_loop = loop
    if _rebuild_request_task is None:
        task = loop.create_task(_run_memory_rebuild_request())
        _rebuild_request_task = task

        def clear_finished(finished: asyncio.Task[tuple[dict, int]]) -> None:
            global _rebuild_request_task

            if _rebuild_request_task is finished:
                _rebuild_request_task = None

        task.add_done_callback(clear_finished)
    return _rebuild_request_task


def _settings_ok_payload(memory, runtime_payload: dict | None = None) -> dict:
    payload = _memory_settings_projection(memory)
    payload["status"] = "ok"
    payload["rebuild_required"] = bool(getattr(memory, "embedding_change_pending", False))
    if runtime_payload is not None:
        payload["runtime"] = runtime_payload
    return payload


async def _apply_memory_settings_patch(
    patch_payload: object,
) -> Response:
    """Persist one Memory settings patch, reconcile it, or roll the save back.

    The whole read -> save -> reconcile -> rollback sequence runs under one
    process lock. Reconciliation awaits the controller, so two overlapping tabs
    would otherwise interleave: the later request could persist and reconcile
    successfully, and then a late-failing earlier request would restore its own
    stale snapshot over it and reconcile that instead - after its caller had
    already been told the newer settings were saved. Serializing also makes the
    second request read the first one's persisted result as its baseline.
    """

    from vibe import api, internal_client
    from config.v2_config import memory_config_to_payload

    async with _memory_settings_write_lock():
        try:
            current = await asyncio.to_thread(V2Config.load)
            target_payload, confirm_rebuild = _memory_settings_patch(current, patch_payload)
            candidate = _memory_candidate_config(current, target_payload)
            identity_changed = _memory_embedding_configuration_changed(current, candidate)
            pending_marker = bool(current.memory.embedding_change_pending)
        except (TypeError, ValueError):
            return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)

        # Unconfirmed identity change never writes — including on empty roots —
        # so a check-then-save race cannot quietly accept a new vector space.
        if identity_changed and not confirm_rebuild:
            return _memory_response(
                {
                    "status": "failed",
                    "error": "memory_embedding_rebuild_required",
                },
                status_code=409,
            )

        # Credential-only updates under an existing marker update the candidate
        # only. Operational fields (especially enabled) still reconcile so a
        # disable cannot leave a live runtime admitting captures.
        enabled_changed = bool(candidate.memory.enabled) != bool(current.memory.enabled)
        if pending_marker and not identity_changed and not enabled_changed:
            try:
                saved = await asyncio.to_thread(
                    api.save_memory_config,
                    target_payload,
                    embedding_change_pending=True,
                    expected=current.memory,
                )
            except api.MemoryConfigStaleWrite:
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
            except ValueError:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            except Exception:
                return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)
            return _memory_response(_settings_ok_payload(saved.memory))

        embedding_change_pending = pending_marker or identity_changed
        try:
            # Persist a durable marker before asking the controller to inspect
            # the root. If Avibe exits in this interval, startup must re-run
            # the same guarded inspection instead of treating the candidate as
            # its own embedding baseline.
            saved = await asyncio.to_thread(
                api.save_memory_config,
                target_payload,
                embedding_change_pending=embedding_change_pending,
                expected=current.memory,
            )
        except api.MemoryConfigStaleWrite:
            return _memory_response(
                {"status": "failed", "error": "memory_operation_in_progress"},
                status_code=409,
            )
        except ValueError:
            return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
        except Exception:
            return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)

        # Confirmed identity change: candidate+marker are durable. Schedule the
        # same rebuild path as Retry; never roll the confirmed config back.
        if identity_changed and confirm_rebuild:
            body, status_code = await asyncio.shield(_memory_rebuild_request_task())
            runtime_payload = body if isinstance(body, dict) else {}
            latest = await asyncio.to_thread(V2Config.load)
            payload = _settings_ok_payload(latest.memory, runtime_payload)
            if status_code != 200 or runtime_payload.get("ok") is not True:
                error = _memory_closed_error(
                    runtime_payload,
                    fallback="memory_rebuild_failed",
                )
                payload["status"] = "failed"
                payload["error"] = error
                # Keep the durable marker projection from latest config. Do not
                # re-arm Retry after settlement if only activation failed later.
                return _memory_response(payload, status_code=status_code if status_code >= 400 else 409)
            return _memory_response(payload)

        response = await _memory_internal_response(internal_client.reconcile_memory)
        runtime_payload = _memory_response_body(response)
        if response.status_code != 200 or runtime_payload.get("ok") is not True:
            closed_error = _memory_closed_error(
                runtime_payload,
                fallback="memory_sidecar_unavailable",
            )
            # Pending-marker reconcile that only needs rebuild is not a rollback.
            if closed_error == "memory_embedding_rebuild_required":
                payload = _settings_ok_payload(saved.memory, runtime_payload)
                payload["rebuild_required"] = True
                return _memory_response(payload)
            # Ordinary non-identity saves must not outrun the controller's closed
            # compatibility decision, including while memory is disabled.
            try:
                rollback_payload = memory_config_to_payload(
                    current.memory,
                    include_secrets=True,
                )
                await asyncio.to_thread(
                    api.save_memory_config,
                    rollback_payload,
                    embedding_change_pending=current.memory.embedding_change_pending,
                    expected=saved.memory,
                )
                await _memory_internal_response(internal_client.reconcile_memory)
            except Exception:
                pass
            return _memory_response(
                {
                    "status": "failed",
                    "error": closed_error,
                },
                status_code=409,
            )
        if response.status_code >= 500:
            return response
        latest = await asyncio.to_thread(V2Config.load)
        return _memory_response(_settings_ok_payload(latest.memory, runtime_payload))


def register_memory_routes(app) -> None:
    """Attach the Memory routes to the UI app."""

    @app.get("/api/memory/settings", include_in_schema=False)
    async def memory_settings_get(starlette_request: FastAPIRequest):
        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            try:
                return _memory_response(
                    await asyncio.to_thread(_memory_settings_payload)
                )
            except Exception:
                return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.patch("/api/memory/settings", include_in_schema=False)
    async def memory_settings_patch(starlette_request: FastAPIRequest):
        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            try:
                patch_payload = await starlette_request.json()
            except (TypeError, ValueError):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            return await _apply_memory_settings_patch(patch_payload)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/status", include_in_schema=False)
    async def memory_status_get(starlette_request: FastAPIRequest):
        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(internal_client.memory_status)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/processing-record", include_in_schema=False)
    async def memory_processing_record_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_processing_record(user_key=user_key)
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/failures", include_in_schema=False)
    async def memory_failures_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_failures(user_key=user_key)
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/maintenance", include_in_schema=False)
    async def memory_maintenance_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_maintenance(user_key=user_key)
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/profile", include_in_schema=False)
    async def memory_profile_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_profile(user_key=user_key)
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/log", include_in_schema=False)
    async def memory_log_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                cursor, limit = _memory_log_list_query(starlette_request)
            except ValueError:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_log(
                    cursor=cursor,
                    limit=limit,
                    user_key=user_key,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/log/entry", include_in_schema=False)
    async def memory_log_entry_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                memcell_id = _memory_log_entry_query(starlette_request)
            except ValueError:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_log_entry(
                    memcell_id,
                    user_key=user_key,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/search", include_in_schema=False)
    async def memory_search_post(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict) or set(payload) != {"query", "policy"}:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            query = payload.get("query")
            if not isinstance(query, str):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from core.memory.types import RecallPolicy

            try:
                policy = RecallPolicy.from_payload(payload.get("policy"))
            except (TypeError, ValueError):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_search(
                    query,
                    policy.payload(),
                    user_key=user_key,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/runtime/restart", include_in_schema=False)
    async def memory_runtime_restart_post(starlette_request: FastAPIRequest):
        """Restart the live sidecar without changing persisted settings."""

        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            body, status_code = await asyncio.shield(_memory_restart_request_task())
            # The retained task shares only the internal transport result. Each
            # request needs its own Response so request-local after hooks cannot
            # mix remote-session cookies across concurrent callers.
            return _memory_response(body, status_code=status_code)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/runtime/rebuild", include_in_schema=False)
    async def memory_runtime_rebuild_post(starlette_request: FastAPIRequest):
        """Wait for one retained Memory rebuild; requires a pending marker."""

        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if payload != {"confirm": True}:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            body, status_code = await asyncio.shield(_memory_rebuild_request_task())
            return _memory_response(body, status_code=status_code)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/clear", include_in_schema=False)
    async def memory_clear_post(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if payload != {"confirm": True}:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_clear(user_key=user_key)
            )

        return await app.dispatch_native_request(starlette_request, handler)

    async def memory_clear_recovery_response(
        starlette_request: FastAPIRequest,
        *,
        action: str,
    ) -> Response:
        user_key = _memory_ui_user_key()
        if user_key is None:
            return _memory_forbidden_response()
        try:
            payload = await starlette_request.json()
        except Exception:
            payload = None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"operation_id"}
            or not isinstance(payload.get("operation_id"), str)
            or not 1 <= len(payload["operation_id"]) <= 128
        ):
            return _memory_response(
                {"status": "failed", "error": "memory_invalid_input"},
                status_code=400,
            )
        from vibe import internal_client

        return await _memory_internal_response(
            lambda: internal_client.memory_clear_recovery(
                payload["operation_id"],
                action=action,
                user_key=user_key,
            )
        )

    @app.post("/api/memory/clear/resume", include_in_schema=False)
    async def memory_clear_resume_post(starlette_request: FastAPIRequest):
        async def handler():
            return await memory_clear_recovery_response(
                starlette_request,
                action="resume",
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/clear/abort", include_in_schema=False)
    async def memory_clear_abort_post(starlette_request: FastAPIRequest):
        async def handler():
            return await memory_clear_recovery_response(
                starlette_request,
                action="abort",
            )

        return await app.dispatch_native_request(starlette_request, handler)
