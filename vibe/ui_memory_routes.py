"""The ``/api/memory/*`` UI surface.

Every route here requires an authorized local or authenticated Avibe Cloud browser
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


_PROCESSING_RECORD_CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_PROCESSING_RECORD_ENTRY_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")


def _memory_ui_user_key() -> str | None:
    """Ask ``ui_server`` for the authorized browser's Memory identity.

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


def _processing_record_list_query(
    request: FastAPIRequest,
) -> tuple[str | None, int, str | None]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if any(key not in {"cursor", "limit", "project"} for key in keys) or len(
        keys
    ) != len(set(keys)):
        raise ValueError("invalid Processing Record query")
    values = dict(items)
    cursor = values.get("cursor")
    if cursor is not None and _PROCESSING_RECORD_CURSOR_RE.fullmatch(cursor) is None:
        raise ValueError("invalid Processing Record cursor")
    raw_limit = values.get("limit", "20")
    if not raw_limit.isascii() or not raw_limit.isdecimal():
        raise ValueError("invalid Processing Record limit")
    limit = int(raw_limit)
    if not 1 <= limit <= 50:
        raise ValueError("invalid Processing Record limit")
    project = values.get("project")
    if project is not None:
        from vibe.memory_project_ids import parse_agent_search_project

        project = parse_agent_search_project(project)
    return cursor, limit, project


def _processing_record_entry_query(
    request: FastAPIRequest,
) -> tuple[str, str | None]:
    items = list(request.query_params.multi_items())
    keys = [key for key, _value in items]
    if (
        any(key not in {"memcell_id", "project"} for key in keys)
        or len(keys) != len(set(keys))
        or "memcell_id" not in keys
    ):
        raise ValueError("invalid Processing Record entry query")
    values = dict(items)
    memcell_id = values["memcell_id"]
    if _PROCESSING_RECORD_ENTRY_ID_RE.fullmatch(memcell_id) is None:
        raise ValueError("invalid Processing Record entry id")
    project = values.get("project")
    if project is not None:
        from vibe.memory_project_ids import parse_agent_search_project

        project = parse_agent_search_project(project)
    return memcell_id, project


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
    payload.pop("cloud", None)
    payload["mode"] = memory.settings_mode()
    payload["cloud_available"] = memory.cloud.memory_capability_available()
    payload["managed"] = memory.settings_mode() == "organization"
    payload["transition_notice_pending"] = memory.cloud.transition_notice_pending
    payload["capability_paused"] = (
        memory.enabled
        and memory.cloud_runtime_selected()
        and memory.runtime_source() == "unavailable"
    )
    return payload


def _memory_im_attachment_capture_available(config: V2Config) -> bool:
    from vibe.memory_contract import IM_ATTACHMENT_CAPTURE_PLATFORMS

    return bool(
        IM_ATTACHMENT_CAPTURE_PLATFORMS.intersection(config.enabled_platforms())
    )


def _memory_settings_payload() -> dict:
    config = V2Config.load()
    payload = _memory_settings_projection(config.memory)
    payload["status"] = "ok"
    payload["im_attachment_capture_available"] = (
        _memory_im_attachment_capture_available(config)
    )
    return payload


def _memory_settings_patch(
    current: V2Config,
    patch_payload: object,
) -> tuple[dict, bool]:
    """Merge one write-only settings patch and extract accepted-loss authority."""

    from config.v2_config import memory_config_to_payload

    allowed_fields = {
        "enabled",
        "processing",
        "mode",
        "acknowledge_transition",
        "confirm_loss",
    }
    if not isinstance(patch_payload, dict) or not set(patch_payload).issubset(
        allowed_fields
    ):
        raise ValueError("invalid_memory_patch")
    confirm_loss = patch_payload.get("confirm_loss", False)
    if not isinstance(confirm_loss, bool):
        raise ValueError("invalid_memory_patch")
    if "acknowledge_transition" in patch_payload and not set(
        patch_payload
    ).issubset({"acknowledge_transition", "confirm_loss"}):
        raise ValueError("invalid_memory_patch")

    target = memory_config_to_payload(current.memory, include_secrets=True)
    endpoints = ("llm", "embedding", "rerank", "multimodal")
    for endpoint in endpoints:
        existing = target["processing"].get(endpoint)
        if isinstance(existing, dict):
            existing.pop("has_api_key", None)

    adopt_cloud_identity = False
    if "enabled" in patch_payload:
        if not isinstance(patch_payload["enabled"], bool):
            raise ValueError("invalid_memory_patch")
        target["enabled"] = patch_payload["enabled"]
    if "mode" in patch_payload:
        mode = patch_payload["mode"]
        if (
            mode not in {"platform", "custom"}
            or current.memory.cloud.scope == "organization"
        ):
            raise ValueError("invalid_memory_patch")
        if mode == "platform" and not current.memory.cloud.memory_capability_available():
            raise ValueError("memory_capability_unavailable")
        target["mode"] = mode
        adopt_cloud_identity = mode == "platform"
    if "acknowledge_transition" in patch_payload:
        cloud_scope = current.memory.cloud.scope
        if (
            patch_payload["acknowledge_transition"] is not True
            or not current.memory.cloud.transition_notice_pending
            or cloud_scope not in {"organization", "platform"}
            or not current.memory.cloud.memory_capability_available()
        ):
            raise ValueError("invalid_memory_patch")
        if cloud_scope == "organization":
            target["cloud"]["organization_attached"] = True
        target["cloud"]["transition_notice_pending"] = False
        adopt_cloud_identity = True
    if adopt_cloud_identity:
        target["cloud"]["applied_embedding_identity"] = (
            current.memory.cloud.embedding_identity
        )

    processing_patch = patch_payload.get("processing")
    if processing_patch is not None:
        target_mode = target.get("mode")
        if target_mode == "platform" or (
            current.memory.settings_mode() != "custom"
            and target_mode != "custom"
        ):
            raise ValueError("invalid_memory_patch")
        if not isinstance(processing_patch, dict) or not set(
            processing_patch
        ).issubset(endpoints):
            raise ValueError("invalid_memory_patch")
        for endpoint, endpoint_patch in processing_patch.items():
            allowed = {"base_url", "model", "api_key"}
            if endpoint == "rerank":
                allowed.add("provider")
            if not isinstance(endpoint_patch, dict) or not set(
                endpoint_patch
            ).issubset(allowed):
                raise ValueError("invalid_memory_patch")
            target["processing"].setdefault(
                endpoint,
                {"base_url": None, "model": None, "api_key": None},
            ).update(endpoint_patch)
            if (
                endpoint in {"rerank", "multimodal"}
                and set(endpoint_patch) == {"api_key"}
                and endpoint_patch["api_key"] in {None, ""}
            ):
                cleared = {"base_url": None, "model": None, "api_key": None}
                if endpoint == "rerank":
                    cleared["provider"] = None
                target["processing"][endpoint] = cleared

    required_key_cleared = any(
        isinstance(endpoint_patch, dict)
        and "api_key" in endpoint_patch
        and endpoint_patch["api_key"] in {None, ""}
        for endpoint, endpoint_patch in (processing_patch or {}).items()
        if endpoint in {"llm", "embedding"}
    )
    if required_key_cleared and target["enabled"]:
        raise ValueError("memory_key_clear_while_enabled")
    return target, confirm_loss


def _memory_candidate_config(current: V2Config, memory_payload: dict) -> V2Config:
    from vibe.api import config_to_payload

    full_payload = config_to_payload(current, include_secrets=True)
    full_payload["memory"] = memory_payload
    return V2Config.from_payload(full_payload)


def _memory_embedding_configuration_changed(
    current: V2Config,
    candidate: V2Config,
) -> bool:
    if (
        current.memory.runtime_embedding_identity()
        != candidate.memory.runtime_embedding_identity()
    ):
        return True
    # A released organization runtime is fenced on its last applied cloud
    # identity while the saved settings still point at custom processing. The
    # acknowledgement therefore keeps the visible runtime tuple unchanged, but
    # must still take the destructive reconfigure path when it advances that
    # applied identity and clears the notice.
    return bool(
        current.memory.cloud.transition_notice_pending
        and not candidate.memory.cloud.transition_notice_pending
        and current.memory.cloud.applied_embedding_identity
        != candidate.memory.cloud.applied_embedding_identity
    )


def _memory_preflight_required(
    current: V2Config,
    candidate: V2Config,
) -> bool:
    return bool(
        candidate.memory.enabled
        and (
            current.memory.runtime_processing()
            != candidate.memory.runtime_processing()
            or current.memory.runtime_source()
            != candidate.memory.runtime_source()
        )
    )


def _memory_preflight_projection(payload: dict) -> dict:
    diagnostic = payload.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return {}
    allowed = {"side", "http_status", "provider_error_code", "message"}
    return {key: diagnostic[key] for key in allowed if key in diagnostic}


def _memory_closed_error(payload: dict, *, fallback: str) -> str:
    from vibe.memory_contract import is_memory_error_code

    value = payload.get("error")
    return value if is_memory_error_code(value) else fallback


_MEMORY_SETTINGS_LOCKS: dict[int, asyncio.Lock] = {}


def _memory_settings_write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = id(loop)
    lock = _MEMORY_SETTINGS_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _MEMORY_SETTINGS_LOCKS[key] = lock
    return lock


async def _settings_ok_payload(
    config: V2Config,
    runtime_payload: dict | None = None,
) -> dict:
    payload = _memory_settings_projection(config.memory)
    payload["status"] = "ok"
    payload["im_attachment_capture_available"] = (
        _memory_im_attachment_capture_available(config)
    )
    if runtime_payload is not None:
        payload["runtime"] = runtime_payload
    return payload


def _memory_operation_response(payload: dict, status_code: int) -> Response:
    allowed = {
        "ok",
        "operation",
        "state",
        "error",
        "result",
        "data_deleted",
        "data_remaining",
        "roots",
    }
    public = {key: payload[key] for key in allowed if key in payload}
    if payload.get("ok") is not True and "error" not in public:
        public["error"] = "memory_sidecar_unavailable"
    return _memory_response(public, status_code=status_code)


async def _apply_memory_settings_patch(
    patch_payload: object,
    *,
    user_key: str,
) -> Response:
    """Apply one settings change without durable recovery workflow state."""

    from config.v2_config import memory_config_to_payload
    from vibe import api, internal_client, model_service

    async with _memory_settings_write_lock():
        try:
            current = await asyncio.to_thread(V2Config.load)
            target_payload, confirm_loss = _memory_settings_patch(
                current,
                patch_payload,
            )
            candidate = _memory_candidate_config(current, target_payload)
            identity_changed = _memory_embedding_configuration_changed(
                current,
                candidate,
            )

            needs_cloud_key = (
                isinstance(patch_payload, dict)
                and (
                    patch_payload.get("mode") == "platform"
                    or patch_payload.get("acknowledge_transition") is True
                )
                and (
                    not current.memory.cloud.model_access_key
                    or current.memory.cloud.access_key_revision is None
                    or current.memory.cloud.access_key_revision
                    != current.memory.cloud.revision
                )
            )
            if needs_cloud_key:
                await asyncio.to_thread(model_service.ensure_model_access_key, current)
                current = await asyncio.to_thread(V2Config.load)
                target_payload, confirm_loss = _memory_settings_patch(
                    current,
                    patch_payload,
                )
                candidate = _memory_candidate_config(current, target_payload)
                identity_changed = _memory_embedding_configuration_changed(
                    current,
                    candidate,
                )
        except ValueError as exc:
            error = (
                "memory_capability_unavailable"
                if str(exc) == "memory_capability_unavailable"
                else "memory_invalid_input"
            )
            return _memory_response(
                {"status": "failed", "error": error},
                status_code=409 if error == "memory_capability_unavailable" else 400,
            )
        except Exception:
            return _memory_response(
                {"status": "failed", "error": "memory_store_unavailable"},
                status_code=503,
            )

        if identity_changed and confirm_loss is not True:
            return _memory_response(
                {
                    "status": "failed",
                    "error": "memory_loss_confirmation_required",
                },
                status_code=409,
            )

        if _memory_preflight_required(current, candidate):
            preflight, preflight_status = await _memory_internal_result(
                lambda: internal_client.memory_preflight(
                    payload={
                        "memory": memory_config_to_payload(
                            candidate.memory,
                            include_secrets=True,
                        )
                    },
                    user_key=user_key,
                )
            )
            if preflight.get("ok") is not True:
                failure = {
                    "status": "failed",
                    "error": _memory_closed_error(
                        preflight,
                        fallback="memory_processing_failed",
                    ),
                }
                diagnostic = _memory_preflight_projection(preflight)
                if diagnostic:
                    failure["diagnostic"] = diagnostic
                return _memory_response(
                    failure,
                    status_code=preflight_status if preflight_status >= 400 else 409,
                )

        if identity_changed:
            body, status_code = await _memory_internal_result(
                lambda: internal_client.memory_reconfigure(
                    confirm_loss=confirm_loss,
                    memory=memory_config_to_payload(
                        candidate.memory,
                        include_secrets=True,
                    ),
                    expected_memory=memory_config_to_payload(
                        current.memory,
                        include_secrets=True,
                    ),
                    user_key=user_key,
                )
            )
            if body.get("ok") is not True:
                return _memory_operation_response(body, status_code)
            latest = await asyncio.to_thread(
                model_service.clear_runtime_apply_pending,
                candidate.memory,
            )
            return _memory_response(await _settings_ok_payload(latest, body))

        try:
            saved = await asyncio.to_thread(
                api.save_memory_config,
                target_payload,
                expected=current.memory,
            )
        except (api.MemoryConfigStaleWrite, api.MemoryOperationBusy):
            return _memory_response(
                {"status": "failed", "error": "memory_operation_in_progress"},
                status_code=409,
            )
        except ValueError:
            return _memory_response(
                {"status": "failed", "error": "memory_invalid_input"},
                status_code=400,
            )
        except Exception:
            return _memory_response(
                {"status": "failed", "error": "memory_store_unavailable"},
                status_code=503,
            )

        response = await _memory_internal_response(internal_client.reconcile_memory)
        runtime_payload = _memory_response_body(response)
        if response.status_code != 200 or runtime_payload.get("ok") is not True:
            error = _memory_closed_error(
                runtime_payload,
                fallback="memory_sidecar_unavailable",
            )
            try:
                await asyncio.to_thread(
                    api.save_memory_config,
                    memory_config_to_payload(
                        current.memory,
                        include_secrets=True,
                    ),
                    expected=saved.memory,
                )
                await _memory_internal_response(internal_client.reconcile_memory)
            except Exception:
                pass
            return _memory_response(
                {"status": "failed", "error": error},
                status_code=409 if response.status_code < 500 else response.status_code,
            )
        latest = await asyncio.to_thread(
            model_service.clear_runtime_apply_pending,
            saved.memory,
        )
        return _memory_response(
            await _settings_ok_payload(latest, runtime_payload)
        )


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
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                patch_payload = await starlette_request.json()
            except (TypeError, ValueError):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            return await _apply_memory_settings_patch(
                patch_payload,
                user_key=user_key,
            )

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

    @app.get("/api/memory/processing-record/entries", include_in_schema=False)
    async def memory_processing_record_entries_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                cursor, limit, project = _processing_record_list_query(
                    starlette_request
                )
            except ValueError:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_processing_record_entries(
                    cursor=cursor,
                    limit=limit,
                    project=project,
                    user_key=user_key,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/processing-record/entry", include_in_schema=False)
    async def memory_processing_record_entry_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                memcell_id, project = _processing_record_entry_query(
                    starlette_request
                )
            except ValueError:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_processing_record_entry(
                    memcell_id,
                    project=project,
                    user_key=user_key,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.get("/api/memory/projects", include_in_schema=False)
    async def memory_projects_get(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_projects(user_key=user_key)
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
            if (
                not isinstance(payload, dict)
                or not {"query", "policy"}.issubset(payload)
                or set(payload) - {"query", "policy", "project"}
            ):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            query = payload.get("query")
            if not isinstance(query, str):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe.memory_contract import RecallPolicy

            try:
                policy = RecallPolicy.from_payload(payload.get("policy"))
            except (TypeError, ValueError):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            if policy.mode == "agentic":
                return _memory_response(
                    {
                        "status": "failed",
                        "error": "memory_capability_unavailable",
                    },
                    status_code=503,
                )
            project = payload.get("project")
            if project is not None and not isinstance(project, str):
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_search(
                    query,
                    policy.payload(),
                    user_key=user_key,
                    project=project,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/list", include_in_schema=False)
    async def memory_list_post(starlette_request: FastAPIRequest):
        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if not isinstance(payload, dict) or set(payload) - {
                "project",
                "page",
                "cursor",
                "limit",
                "origin",
            }:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            project = payload.get("project")
            page = payload.get("page")
            cursor = payload.get("cursor")
            limit = payload.get("limit", 20)
            origin = payload.get("origin")
            from core.memory_loader import MEMORY_LIST_CURSOR_MAX_BYTES
            from vibe.memory_contract import MAX_MEMORY_LIST_PAGE_SIZE

            cursor_bytes: int | None = None
            if isinstance(cursor, str):
                try:
                    cursor_bytes = len(cursor.encode("utf-8"))
                except UnicodeEncodeError:
                    pass

            if (
                (project is not None and not isinstance(project, str))
                or (origin is not None and origin not in ("user", "agent"))
                or isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= MAX_MEMORY_LIST_PAGE_SIZE
                or (
                    page is not None
                    and (
                        isinstance(page, bool)
                        or not isinstance(page, int)
                        or page < 1
                    )
                )
                or (
                    cursor is not None
                    and (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor_bytes is None
                        or cursor_bytes > MEMORY_LIST_CURSOR_MAX_BYTES
                    )
                )
                or (project == "all" and page is not None)
                or (project != "all" and cursor is not None)
            ):
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"},
                    status_code=400,
                )
            from vibe import internal_client

            return await _memory_internal_response(
                lambda: internal_client.memory_list(
                    user_key=user_key,
                    project=project,
                    page=page,
                    cursor=cursor,
                    limit=limit,
                    origin=origin,
                )
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/runtime/wake", include_in_schema=False)
    async def memory_runtime_wake_post(starlette_request: FastAPIRequest):
        async def handler():
            if _memory_ui_user_key() is None:
                return _memory_forbidden_response()
            from vibe import internal_client

            body, status_code = await _memory_internal_result(
                internal_client.memory_wake
            )
            return _memory_operation_response(body, status_code)

        return await app.dispatch_native_request(starlette_request, handler)

    async def confirmed_data_operation(
        request: FastAPIRequest,
        *,
        operation: str,
    ) -> Response:
        user_key = _memory_ui_user_key()
        if user_key is None:
            return _memory_forbidden_response()
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if payload != {"confirm_loss": True}:
            return _memory_response(
                {
                    "ok": False,
                    "operation": operation,
                    "error": "memory_loss_confirmation_required",
                    "result": "unchanged",
                },
                status_code=400,
            )
        from vibe import internal_client

        if operation == "repair":
            call = lambda: internal_client.memory_repair(
                confirm_loss=True,
                user_key=user_key,
            )
        else:
            call = lambda: internal_client.memory_delete_data(
                confirm_loss=True,
                user_key=user_key,
            )
        body, status_code = await _memory_internal_result(call)
        return _memory_operation_response(body, status_code)

    @app.post("/api/memory/repair", include_in_schema=False)
    async def memory_repair_post(starlette_request: FastAPIRequest):
        async def handler():
            return await confirmed_data_operation(
                starlette_request,
                operation="repair",
            )

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/delete-data", include_in_schema=False)
    async def memory_delete_data_post(starlette_request: FastAPIRequest):
        async def handler():
            return await confirmed_data_operation(
                starlette_request,
                operation="delete_data",
            )

        return await app.dispatch_native_request(starlette_request, handler)
