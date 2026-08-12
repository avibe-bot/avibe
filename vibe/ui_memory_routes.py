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
import math
import re
from typing import Any, Awaitable, Callable, Generic, TypeVar

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


def _memory_repair_available() -> bool:
    from core.memory.artifact import get_memory_artifact_manager

    try:
        return get_memory_artifact_manager().sync_capability()
    except Exception:
        return False


def _memory_settings_payload() -> dict:
    # Tag the response, not `memory_config_to_payload` itself: the same helper
    # feeds the persisted config, which must stay free of result envelopes.
    memory = V2Config.load().memory
    payload = _memory_settings_projection(memory)
    payload["status"] = "ok"
    # Read-only projection while a durable rebuild marker is pending.
    payload["rebuild_required"] = memory.recovery_intent == "rebuild"
    payload["factory_reset_required"] = memory.recovery_intent == "factory_reset"
    payload["repair_available"] = (
        memory.recovery_intent is None and _memory_repair_available()
    )
 
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
    """Return whether the normalized vector-space identity would change."""

    current_embedding = current.memory.processing.embedding
    candidate_embedding = candidate.memory.processing.embedding

    def normalized(value: str | None) -> str | None:
        stripped = (value or "").strip()
        return stripped or None

    return (
        normalized(current_embedding.base_url)
        != normalized(candidate_embedding.base_url)
        or normalized(current_embedding.model)
        != normalized(candidate_embedding.model)
    )


def _memory_api_key_only_patch(patch_payload: object) -> bool:
    """Return whether this patch changes only one or both provider API keys."""

    if not isinstance(patch_payload, dict) or set(patch_payload) != {"processing"}:
        return False
    processing = patch_payload.get("processing")
    if not isinstance(processing, dict) or not processing:
        return False
    if not set(processing).issubset({"llm", "embedding"}):
        return False
    return all(
        isinstance(endpoint, dict) and set(endpoint) == {"api_key"}
        for endpoint in processing.values()
    )


def _memory_factory_reset_repair_patch(patch_payload: object) -> bool:
    """Return whether a patch only repairs processing endpoint settings.

    A factory-reset marker fences activation until the roots are gone.  Once
    activation itself fails, the operator must still be able to correct the
    endpoint URL, model, or credential before retrying that fenced operation.
    Keep this escape hatch narrow: enabled state and rebuild confirmation are
    separate lifecycle controls and must never be smuggled through it.
    """

    if not isinstance(patch_payload, dict) or set(patch_payload) != {"processing"}:
        return False
    processing = patch_payload.get("processing")
    if not isinstance(processing, dict) or not processing:
        return False
    if not set(processing).issubset({"llm", "embedding"}):
        return False
    return all(
        isinstance(endpoint, dict)
        and bool(endpoint)
        and set(endpoint).issubset({"base_url", "model", "api_key"})
        for endpoint in processing.values()
    )


def _memory_closed_error(payload: dict, *, fallback: str) -> str:
    from core.memory.types import is_memory_error_code

    value = payload.get("error")
    return value if is_memory_error_code(value) else fallback


def _memory_preflight_projection(payload: dict) -> dict:
    diagnostic = payload.get("diagnostic")
    if not isinstance(diagnostic, dict):
        return {}
    allowed = {"side", "http_status", "provider_error_code", "message"}
    return {key: diagnostic[key] for key in allowed if key in diagnostic}


def _memory_rebuild_result(
    payload: dict,
    status_code: int,
) -> tuple[dict, int]:
    """Normalize the internal response into one final public rebuild outcome."""

    from core.memory.types import is_memory_error_code

    protocol_failure = {
        "ok": False,
        "error": "memory_rebuild_failed",
        "result": "failed",
    }
    if status_code == 202 or status_code < 200 or 300 <= status_code < 400:
        return protocol_failure, 503

    error = payload.get("error")
    if payload.get("status") == "failed" and status_code >= 500:
        if isinstance(error, str) and is_memory_error_code(error):
            return {"ok": False, "error": error, "result": "failed"}, 503
        return protocol_failure, 503

    result = payload.get("result")
    if result not in {
        "completed",
        "completed_empty",
        "root_busy",
        "interrupted",
        "timed_out",
        "failed",
    }:
        return protocol_failure, 503

    public = {"ok": payload.get("ok"), "result": result}
    state = payload.get("state")
    if state in {"ready", "disabled"}:
        public["state"] = state

    if payload.get("ok") is True:
        if status_code != 200 or result not in {"completed", "completed_empty"}:
            return protocol_failure, 503
        return public, 200

    if payload.get("ok") is not False:
        return protocol_failure, 503
    if not isinstance(error, str) or not is_memory_error_code(error):
        return protocol_failure, 503
    public["error"] = error
    return public, 503 if status_code >= 500 else 409


_FACTORY_RESET_ROOTS = frozenset({"memory", "state/memory"})
_FACTORY_RESET_SUCCESS_KEYS = frozenset(
    {"ok", "result", "data_deleted", "data_remaining", "roots"}
)


def _memory_factory_reset_result(payload: dict, status_code: int) -> tuple[dict, int]:
    """Normalize the exact final factory-reset contract without leaking internals."""

    from core.memory.types import is_memory_error_code

    # The Controller reports an in-flight operation before deletion starts, so
    # there is intentionally no root envelope to project yet. Preserve only
    # this exact closed conflict shape; every extra or missing field fails
    # closed below without leaking backend details.
    if status_code == 409 and payload == {
        "ok": False,
        "error": "memory_operation_in_progress",
        "result": "failed",
    }:
        return dict(payload), 409

    # Successful responses are closed and must not carry backend-only fields.
    # Failure responses may carry diagnostic fields, which are projected below
    # only when they are part of the public contract.
    if payload.get("ok") is True and not set(payload).issubset(_FACTORY_RESET_SUCCESS_KEYS):
        return {"ok": False, "error": "memory_factory_reset_failed", "result": "failed"}, 503

    roots = payload.get("roots")
    valid_roots = (
        isinstance(roots, list)
        and len(roots) == 2
        and {item.get("path") for item in roots if isinstance(item, dict)} == _FACTORY_RESET_ROOTS
        and all(
            isinstance(item, dict)
            and set(item).issubset({"path", "existed", "deleted", "error"})
            and item.get("path") in _FACTORY_RESET_ROOTS
            and isinstance(item.get("existed"), bool)
            and isinstance(item.get("deleted"), bool)
            and ("error" not in item or isinstance(item["error"], str))
            for item in roots
        )
    )
    if not valid_roots or not isinstance(payload.get("data_deleted"), bool) or not isinstance(
        payload.get("data_remaining"), bool
    ):
        return {"ok": False, "error": "memory_factory_reset_failed", "result": "failed"}, 503
    clean_roots = [
        {
            key: item[key]
            for key in ("path", "existed", "deleted", "error")
            if key in item
        }
        for item in roots
    ]
    if payload.get("ok") is True:
        if status_code != 200 or payload.get("result") != "completed":
            return {"ok": False, "error": "memory_factory_reset_failed", "result": "failed"}, 503
        return {
            "ok": True,
            "result": "completed",
            "data_deleted": payload["data_deleted"],
            "data_remaining": payload["data_remaining"],
            "roots": clean_roots,
        }, 200
    error = payload.get("error")
    result = payload.get("result")
    if result not in {"partial", "deleted_activation_failed", "failed"}:
        result = "failed"
    if not isinstance(error, str) or not is_memory_error_code(error):
        error = "memory_factory_reset_failed"
    public = {
        "ok": False,
        "result": result,
        "error": error,
        "data_deleted": payload["data_deleted"],
        "data_remaining": payload["data_remaining"],
        "roots": clean_roots,
    }
    if isinstance(payload.get("reason"), str):
        public["reason"] = payload["reason"]
    return public, 409 if error == "memory_operation_in_progress" else 503
_REPAIR_HEALTH_KEYS = frozenset(
    {
        "healthy",
        "reasons",
        "pending",
        "failed_permanent",
        "failed_retryable",
        "drain_consecutive_failures",
        "unrecoverable_total",
        "optimize_failure_streak",
        "prune_stale_seconds",
    }
)
_REPAIR_COUNT_KEYS = _REPAIR_HEALTH_KEYS - {
    "healthy",
    "reasons",
    "prune_stale_seconds",
}


def _memory_repair_health(value: object) -> dict[str, object] | None:
    """Validate and copy only the existing bounded cascade projection."""

    if not isinstance(value, dict) or set(value) != _REPAIR_HEALTH_KEYS:
        return None
    if type(value.get("healthy")) is not bool:
        return None
    reasons = value.get("reasons")
    if (
        not isinstance(reasons, list)
        or len(reasons) > 8
        or any(not isinstance(item, str) or len(item.encode("utf-8")) > 64 for item in reasons)
    ):
        return None
    if any(
        type(value.get(key)) is not int or not 0 <= value[key] <= 2**53
        for key in _REPAIR_COUNT_KEYS
    ):
        return None
    stale = value.get("prune_stale_seconds")
    if (
        isinstance(stale, bool)
        or not isinstance(stale, (int, float))
        or not math.isfinite(float(stale))
        or not 0 <= float(stale) <= 10**12
    ):
        return None
    return {
        "healthy": value["healthy"],
        "reasons": list(reasons),
        **{key: value[key] for key in sorted(_REPAIR_COUNT_KEYS)},
        "prune_stale_seconds": float(stale),
    }


def _memory_repair_result(payload: dict, status_code: int) -> tuple[dict, int]:
    """Normalize one exact final Repair response without leaking internals."""

    protocol_failure = {
        "ok": False,
        "error": "memory_repair_failed",
        "result": "failed",
    }
    if status_code not in {200, 409, 503}:
        return protocol_failure, 503

    if payload.get("ok") is True:
        if set(payload) != {"ok", "result", "health"} or status_code != 200:
            return protocol_failure, 503
        health = _memory_repair_health(payload.get("health"))
        result = payload.get("result")
        if health is None or result not in {"completed", "completed_with_warnings"}:
            return protocol_failure, 503
        if (result == "completed") is not (health["healthy"] is True):
            return protocol_failure, 503
        return {"ok": True, "result": result, "health": health}, 200

    if set(payload) != {"ok", "error", "result"} or payload.get("ok") is not False:
        return protocol_failure, 503
    error = payload.get("error")
    result = payload.get("result")
    expected_status = {
        "memory_disabled": 409,
        "memory_operation_in_progress": 409,
        "memory_runtime_unsupported": 409,
        "memory_store_unavailable": 503,
        "memory_sidecar_unavailable": 503,
        "memory_repair_failed": 503,
        "memory_embedding_unavailable": 409,
        "memory_llm_unavailable": 409,
    }.get(error)
    if expected_status is None or expected_status != status_code:
        return protocol_failure, 503
    if error == "memory_repair_failed":
        if result not in {"interrupted", "timed_out", "failed"}:
            return protocol_failure, 503
    elif result != "failed":
        return protocol_failure, 503
    return {"ok": False, "error": error, "result": result}, status_code


_settings_write_lock: asyncio.Lock | None = None
_settings_write_lock_loop: asyncio.AbstractEventLoop | None = None
_RetainedResult = TypeVar("_RetainedResult")


class _MemoryRetainedRequestOwner(Generic[_RetainedResult]):
    """Own one loop-affine request task that duplicate callers can join."""

    def __init__(self) -> None:
        self._task: asyncio.Task[_RetainedResult] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def current_task(self) -> asyncio.Task[_RetainedResult] | None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._task = None
            self._loop = loop
        return self._task

    def running(self) -> bool:
        task = self.current_task()
        return task is not None and not task.done()

    def create_or_join(
        self,
        factory: Callable[[], Awaitable[_RetainedResult]],
    ) -> asyncio.Task[_RetainedResult]:
        loop = asyncio.get_running_loop()
        task = self.current_task()
        if task is None:
            task = loop.create_task(factory())
            self._task = task
            task.add_done_callback(self._clear_finished)
        return task

    def _clear_finished(self, finished: asyncio.Task[_RetainedResult]) -> None:
        if self._task is finished:
            self._task = None


_restart_request_owner = _MemoryRetainedRequestOwner[tuple[dict, int]]()
_rebuild_request_owner = _MemoryRetainedRequestOwner[tuple[dict, int]]()
_repair_request_owner = _MemoryRetainedRequestOwner[tuple[dict, int]]()
_factory_reset_request_owner = _MemoryRetainedRequestOwner[tuple[dict, int]]()


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


def _memory_rebuild_request_running() -> bool:
    return _rebuild_request_owner.running()


def _memory_factory_reset_request_running() -> bool:
    return _factory_reset_request_owner.running()


async def _run_memory_restart_request() -> tuple[dict, int]:
    from vibe import internal_client

    if _memory_mutation_request_running():
        return {"status": "failed", "error": "memory_operation_in_progress"}, 409
    async with _memory_settings_write_lock():
        if _memory_mutation_request_running() or _memory_factory_reset_request_running():
            return {"status": "failed", "error": "memory_operation_in_progress"}, 409
        return await _memory_internal_result(internal_client.memory_restart)


def _memory_repair_request_running() -> bool:
    return _repair_request_owner.running()


def _memory_mutation_request_running() -> bool:
    return _memory_rebuild_request_running() or _memory_repair_request_running()


def _memory_restart_request_task() -> asyncio.Task[tuple[dict, int]]:
    return _restart_request_owner.create_or_join(_run_memory_restart_request)


async def _run_memory_rebuild_request(user_key: str) -> tuple[dict, int]:
    # Intentionally not under the settings write lock: confirmed settings saves
    # already hold that lock and then join this retained task. Controller-side
    # rebuild ownership serializes the destructive work.
    from vibe import internal_client

    body, status_code = await _memory_internal_result(
        lambda: internal_client.memory_rebuild(user_key=user_key)
    )
    return _memory_rebuild_result(body, status_code)


def _memory_rebuild_request_task(*, user_key: str) -> asyncio.Task[tuple[dict, int]]:
    return _rebuild_request_owner.create_or_join(
        lambda: _run_memory_rebuild_request(user_key)
    )


async def _run_memory_factory_reset_request(user_key: str) -> tuple[dict, int]:
    from vibe import internal_client

    body, status_code = await _memory_internal_result(
        lambda: internal_client.memory_factory_reset(user_key=user_key)
    )
    return _memory_factory_reset_result(body, status_code)


def _memory_factory_reset_request_task(*, user_key: str) -> asyncio.Task[tuple[dict, int]]:
    return _factory_reset_request_owner.create_or_join(
        lambda: _run_memory_factory_reset_request(user_key)
    )


async def _run_memory_repair_request(user_key: str) -> tuple[dict, int]:
    from vibe import internal_client

    body, status_code = await _memory_internal_result(
        lambda: internal_client.memory_repair(user_key=user_key)
    )
    return _memory_repair_result(body, status_code)


def _memory_repair_request_task(*, user_key: str) -> asyncio.Task[tuple[dict, int]]:
    return _repair_request_owner.create_or_join(
        lambda: _run_memory_repair_request(user_key)
    )


async def _settings_ok_payload(memory, runtime_payload: dict | None = None) -> dict:
    from core.memory.blocking import run_blocking

    payload = _memory_settings_projection(memory)
    payload["status"] = "ok"
    payload["rebuild_required"] = getattr(memory, "recovery_intent", None) == "rebuild"
    payload["factory_reset_required"] = getattr(memory, "recovery_intent", None) == "factory_reset"
    payload["repair_available"] = (
        getattr(memory, "recovery_intent", None) is None
        and await run_blocking(_memory_repair_available)
    )
    if runtime_payload is not None:
        payload["runtime"] = runtime_payload
    return payload


async def _apply_memory_settings_patch(
    patch_payload: object,
    *,
    user_key: str,
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

    if _memory_mutation_request_running() or _memory_factory_reset_request_running():
        return _memory_response(
            {"status": "failed", "error": "memory_operation_in_progress"},
            status_code=409,
        )

    async with _memory_settings_write_lock():
        if _memory_mutation_request_running() or _memory_factory_reset_request_running():
            return _memory_response(
                {"status": "failed", "error": "memory_operation_in_progress"},
                status_code=409,
            )
        try:
            current = await asyncio.to_thread(V2Config.load)
            target_payload, confirm_rebuild = _memory_settings_patch(current, patch_payload)
            candidate = _memory_candidate_config(current, target_payload)
            identity_changed = _memory_embedding_configuration_changed(current, candidate)
            pending_marker = current.memory.recovery_intent == "rebuild"
            pending_factory_reset = current.memory.recovery_intent == "factory_reset"
        except (TypeError, ValueError):
            return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)

        factory_reset_repair = (
            pending_factory_reset and _memory_factory_reset_repair_patch(patch_payload)
        )

        # Unconfirmed identity change never writes — including on empty roots —
        # so a check-then-save race cannot quietly accept a new vector space.
        # A factory-reset repair is the one deliberate exception: the reset
        # marker guarantees the next retry will wipe any remaining old root
        # before activating the corrected endpoint.
        if identity_changed and not confirm_rebuild and not factory_reset_repair:
            return _memory_response(
                {
                    "status": "failed",
                    "error": "memory_embedding_rebuild_required",
                },
                status_code=409,
            )

        if pending_factory_reset and not factory_reset_repair:
            return _memory_response(
                {"status": "failed", "error": "memory_operation_in_progress"},
                status_code=409,
            )

        if factory_reset_repair:
            try:
                saved = await asyncio.to_thread(
                    api.save_memory_config,
                    target_payload,
                    recovery_intent="factory_reset",
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
            # Do not reconcile here: the durable reset marker remains the
            # authority and Retry must perform the fenced deletion/activation.
            return _memory_response(await _settings_ok_payload(saved.memory))

        # An exact credential-only update under an existing marker updates the
        # candidate without touching the fenced runtime. Every broader patch
        # keeps the ordinary reconcile/rollback contract.
        if pending_marker and _memory_api_key_only_patch(patch_payload):
            try:
                saved = await asyncio.to_thread(
                    api.save_memory_config,
                    target_payload,
                    recovery_intent="rebuild",
                    expected=current.memory,
                )
            except (api.MemoryConfigStaleWrite, api.MemoryOperationBusy):
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
            except ValueError:
                return _memory_response({"status": "failed", "error": "memory_invalid_input"}, status_code=400)
            except Exception:
                return _memory_response({"status": "failed", "error": "memory_store_unavailable"}, status_code=503)
            return _memory_response(await _settings_ok_payload(saved.memory))

        recovery_intent = "rebuild" if pending_marker or identity_changed else None

        if identity_changed and confirm_rebuild:
            from config.v2_config import memory_config_to_payload
            preflight = await _memory_internal_result(
                lambda: internal_client.memory_preflight(
                    payload={"memory": memory_config_to_payload(candidate, include_secrets=True)}
                )
            )
            if preflight[0].get("ok") is not True:
                failure = dict(preflight[0])
                failure["status"] = "failed"
                failure["diagnostic"] = _memory_preflight_projection(preflight[0])
                return _memory_response(failure, status_code=409)

        try:
            # Persist a durable marker before asking the controller to inspect
            # the root. If Avibe exits in this interval, startup must re-run
            # the same guarded inspection instead of treating the candidate as
            # its own embedding baseline.
            saved = await asyncio.to_thread(
                api.save_memory_config,
                target_payload,
                recovery_intent=recovery_intent,
                expected=current.memory,
            )
        except (api.MemoryConfigStaleWrite, api.MemoryOperationBusy):
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
            body, status_code = await asyncio.shield(
                _memory_rebuild_request_task(user_key=user_key)
            )
            runtime_payload = body if isinstance(body, dict) else {}
            latest = await asyncio.to_thread(V2Config.load)
            payload = await _settings_ok_payload(latest.memory, runtime_payload)
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
                payload = await _settings_ok_payload(saved.memory, runtime_payload)
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
                    recovery_intent=current.memory.recovery_intent,
                    expected=saved.memory,
                )
                await _memory_internal_response(internal_client.reconcile_memory)
            except api.MemoryOperationBusy:
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
            except api.MemoryConfigStaleWrite:
                pass
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
        return _memory_response(await _settings_ok_payload(latest.memory, runtime_payload))


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
            user_key = _memory_ui_user_key()
            if user_key is None:
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
            if _memory_factory_reset_request_running() or _memory_repair_request_running():
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
            task = _rebuild_request_owner.current_task()
            if task is None:
                async with _memory_settings_write_lock():
                    if _memory_factory_reset_request_running() or _memory_repair_request_running():
                        return _memory_response(
                            {
                                "status": "failed",
                                "error": "memory_operation_in_progress",
                            },
                            status_code=409,
                        )
                    task = _rebuild_request_owner.current_task()
                    if task is None:
                        task = _memory_rebuild_request_task(user_key=user_key)
            body, status_code = await asyncio.shield(task)
            return _memory_response(body, status_code=status_code)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/runtime/factory-reset", include_in_schema=False)
    async def memory_runtime_factory_reset_post(starlette_request: FastAPIRequest):
        """Await one retained Controller-owned factory reset or its retry."""

        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
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
            if _memory_rebuild_request_running() or _memory_repair_request_running():
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
            task = _factory_reset_request_owner.current_task()
            if task is None:
                async with _memory_settings_write_lock():
                    if _memory_rebuild_request_running() or _memory_repair_request_running():
                        return _memory_response(
                            {"status": "failed", "error": "memory_operation_in_progress"},
                            status_code=409,
                        )
                    task = _factory_reset_request_owner.current_task()
                    if task is None:
                        task = _memory_factory_reset_request_task(user_key=user_key)
            body, status_code = await asyncio.shield(task)
            return _memory_response(body, status_code=status_code)

        return await app.dispatch_native_request(starlette_request, handler)

    @app.post("/api/memory/runtime/repair", include_in_schema=False)
    async def memory_runtime_repair_post(starlette_request: FastAPIRequest):
        """Wait for one retained live cascade sync and its final health."""

        async def handler():
            user_key = _memory_ui_user_key()
            if user_key is None:
                return _memory_forbidden_response()
            try:
                payload = await starlette_request.json()
            except Exception:
                payload = None
            if payload != {"confirm": True}:
                return _memory_response(
                    {"status": "failed", "error": "memory_invalid_input"}, status_code=400
                )
            if _memory_rebuild_request_running() or _memory_factory_reset_request_running():
                return _memory_response(
                    {"ok": False, "error": "memory_operation_in_progress", "result": "failed"},
                    status_code=409,
                )
            task = _repair_request_owner.current_task()
            if task is None:
                async with _memory_settings_write_lock():
                    if _memory_rebuild_request_running() or _memory_factory_reset_request_running():
                        return _memory_response(
                            {"ok": False, "error": "memory_operation_in_progress", "result": "failed"},
                            status_code=409,
                        )
                    task = _repair_request_owner.current_task()
                    if task is None:
                        task = _memory_repair_request_task(user_key=user_key)
            body, status_code = await asyncio.shield(task)
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
            if _memory_mutation_request_running() or _memory_factory_reset_request_running():
                return _memory_response(
                    {"status": "failed", "error": "memory_operation_in_progress"},
                    status_code=409,
                )
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
        if _memory_mutation_request_running() or _memory_factory_reset_request_running():
            return _memory_response(
                {"status": "failed", "error": "memory_operation_in_progress"},
                status_code=409,
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
