"""Paired-device Cloud Model Service resolution for local Memory."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import logging
import threading
from typing import Any, Mapping

from config.v2_config import (
    MemoryCloudCapabilities,
    MemoryConfig,
    V2Config,
    memory_config_to_payload,
)


logger = logging.getLogger(__name__)

MODEL_SERVICE_POLL_SECONDS = 60
_SYNC_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_REFRESH_EVENT = threading.Event()
_WORKER_STARTED = False


class ModelServiceResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelServiceStatus:
    scope: str
    capabilities: MemoryCloudCapabilities
    embedding_identity: str | None
    quota_enforced: bool
    revision: int

    def memory_available(self) -> bool:
        return self.capabilities.memory_available() and bool(self.embedding_identity)


@dataclass(frozen=True)
class ModelAccessKeyMint:
    key: str
    created_at: str
    rotated: bool
    previous_valid_until: str | None


def _iso8601(value: object, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ModelServiceResolutionError("model_service_invalid_response")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelServiceResolutionError("model_service_invalid_response") from exc
    if parsed.tzinfo is None:
        raise ModelServiceResolutionError("model_service_invalid_response")
    return candidate


def _status_from_payload(payload: object) -> ModelServiceStatus:
    if not isinstance(payload, Mapping):
        raise ModelServiceResolutionError("model_service_invalid_response")
    scope = payload.get("mode")
    capabilities = payload.get("capabilities")
    embedding_identity = payload.get("embedding_identity")
    quota = payload.get("quota")
    revision = payload.get("revision")
    if scope not in {"organization", "platform"}:
        raise ModelServiceResolutionError("model_service_invalid_response")
    if not isinstance(capabilities, Mapping):
        raise ModelServiceResolutionError("model_service_invalid_response")
    required_capabilities = {"asr", "chat", "embedding", "multimodal"}
    if not required_capabilities.issubset(capabilities) or any(
        not isinstance(capabilities.get(name), bool) for name in required_capabilities
    ):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if embedding_identity is not None and (not isinstance(embedding_identity, str) or not embedding_identity.strip()):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if bool(capabilities["embedding"]) != bool(embedding_identity):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if not isinstance(quota, Mapping) or not isinstance(quota.get("enforced"), bool):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ModelServiceResolutionError("model_service_invalid_response")
    return ModelServiceStatus(
        scope=str(scope),
        capabilities=MemoryCloudCapabilities(
            asr=capabilities["asr"],
            chat=capabilities["chat"],
            embedding=capabilities["embedding"],
            multimodal=capabilities["multimodal"],
        ),
        embedding_identity=embedding_identity.strip() if embedding_identity else None,
        quota_enforced=quota["enforced"],
        revision=revision,
    )


def _mint_from_payload(payload: object) -> ModelAccessKeyMint:
    if not isinstance(payload, Mapping) or set(payload) != {
        "key",
        "created_at",
        "rotated",
        "previous_valid_until",
    }:
        raise ModelServiceResolutionError("model_access_key_invalid_response")
    key = payload.get("key")
    rotated = payload.get("rotated")
    previous_valid_until = payload.get("previous_valid_until")
    if (
        not isinstance(key, str)
        or not key.startswith("mak_")
        or not isinstance(rotated, bool)
        or (not rotated and previous_valid_until is not None)
        or (rotated and previous_valid_until is None)
    ):
        raise ModelServiceResolutionError("model_access_key_invalid_response")
    created_at = _iso8601(payload.get("created_at"))
    previous = _iso8601(previous_valid_until, nullable=True)
    assert isinstance(created_at, str)
    return ModelAccessKeyMint(
        key=key,
        created_at=created_at,
        rotated=rotated,
        previous_valid_until=previous,
    )


def _device_request(
    config: V2Config,
    method: str,
    suffix: str,
) -> dict[str, Any]:
    from vibe import remote_access

    return remote_access._device_json_request(config, method, suffix)  # noqa: SLF001


def _memory_mode_after_initialization(memory: MemoryConfig) -> str:
    if memory.mode is not None:
        return memory.mode
    return "custom" if memory.custom_processing_complete() else "platform"


def _status_needs_model_key(memory: MemoryConfig, status: ModelServiceStatus) -> bool:
    if not status.memory_available():
        return False
    if status.scope == "organization":
        if memory.cloud.organization_attached:
            return True
        if memory.cloud.transition_notice_pending:
            return False
        return not (_memory_mode_after_initialization(memory) == "custom" and memory.custom_processing_complete())
    return _memory_mode_after_initialization(memory) == "platform"


def _resolved_memory(
    current: MemoryConfig,
    *,
    status: ModelServiceStatus,
    instance_id: str,
    proxy_base_url: str,
    minted: ModelAccessKeyMint | None,
) -> MemoryConfig:
    candidate = deepcopy(current)
    previous = deepcopy(current)
    first_resolution = candidate.cloud.source_instance_id != instance_id
    if first_resolution:
        candidate.cloud.model_access_key = None
        candidate.cloud.organization_attached = False
        candidate.cloud.transition_notice_pending = False
        candidate.cloud.applied_embedding_identity = None
    if candidate.mode is None:
        candidate.mode = _memory_mode_after_initialization(candidate)
        if candidate.mode == "platform" and status.memory_available():
            candidate.enabled = True

    candidate.cloud.scope = status.scope  # type: ignore[assignment]
    candidate.cloud.capabilities = status.capabilities
    candidate.cloud.embedding_identity = status.embedding_identity
    candidate.cloud.revision = status.revision
    candidate.cloud.quota_enforced = status.quota_enforced
    candidate.cloud.proxy_base_url = proxy_base_url
    candidate.cloud.source_instance_id = instance_id
    if minted is not None:
        candidate.cloud.model_access_key = minted.key

    if status.scope == "organization":
        if status.memory_available():
            if candidate.cloud.organization_attached:
                first_managed_activation = (
                    candidate.cloud.applied_embedding_identity is None
                )
                if (
                    candidate.cloud.applied_embedding_identity is not None
                    and candidate.cloud.applied_embedding_identity != status.embedding_identity
                ):
                    candidate.recovery_intent = "rebuild"
                candidate.cloud.applied_embedding_identity = status.embedding_identity
                if first_managed_activation:
                    candidate.enabled = True
            elif candidate.cloud.transition_notice_pending:
                candidate.recovery_intent = "rebuild"
            elif candidate.mode == "custom" and previous.custom_processing_complete():
                candidate.cloud.transition_notice_pending = True
                candidate.recovery_intent = "rebuild"
            else:
                if (
                    previous.cloud.scope == "platform"
                    and previous.cloud.applied_embedding_identity is not None
                    and previous.cloud.applied_embedding_identity != status.embedding_identity
                ):
                    candidate.recovery_intent = "rebuild"
                candidate.cloud.organization_attached = True
                candidate.cloud.applied_embedding_identity = status.embedding_identity
                candidate.enabled = True
        elif candidate.cloud.organization_attached:
            # Keep the last applied identity while paused so re-enable can
            # distinguish an unchanged provider from one that needs rebuild.
            candidate.cloud.transition_notice_pending = False
        elif not (
            candidate.mode == "custom" and previous.custom_processing_complete()
        ):
            # Scope binding remains managed even with no Memory pair. Only a
            # complete configuration that was actively custom is grandfathered;
            # every other installation pauses instead of falling back.
            candidate.cloud.organization_attached = True
            candidate.cloud.applied_embedding_identity = (
                previous.cloud.applied_embedding_identity
            )
    else:
        was_organization_cloud = previous.cloud.scope == "organization" and previous.cloud.organization_attached
        candidate.cloud.organization_attached = False
        candidate.cloud.transition_notice_pending = False
        if candidate.mode == "platform" and status.memory_available():
            if (
                (was_organization_cloud or previous.cloud.scope == "platform")
                and previous.cloud.applied_embedding_identity is not None
                and previous.cloud.applied_embedding_identity != status.embedding_identity
            ):
                candidate.recovery_intent = "rebuild"
            candidate.cloud.applied_embedding_identity = status.embedding_identity
        elif was_organization_cloud:
            candidate.recovery_intent = "rebuild"
            candidate.cloud.applied_embedding_identity = None

    candidate.cloud.runtime_apply_pending = current.cloud.runtime_apply_pending or candidate != current
    candidate.validate()
    return candidate


def _persist_candidate(current: MemoryConfig, candidate: MemoryConfig) -> V2Config:
    from vibe import api

    return api.save_memory_config(
        memory_config_to_payload(candidate, include_secrets=True),
        recovery_intent=candidate.recovery_intent,
        expected=current,
    )


def _clear_apply_pending(expected: MemoryConfig) -> None:
    from config.v2_config import atomic_update_memory

    def clear(current: MemoryConfig) -> MemoryConfig:
        if current != expected:
            return current
        current.cloud.runtime_apply_pending = False
        return current

    atomic_update_memory(clear)


def _reconcile_candidate(candidate: MemoryConfig) -> bool:
    from vibe import internal_client
    from vibe.async_bridge import run_coroutine_blocking

    try:
        result = run_coroutine_blocking(internal_client.reconcile_memory())
    except Exception:
        return False
    body = result.get("body") if isinstance(result, dict) else None
    if not isinstance(body, dict):
        return False
    if body.get("ok") is True:
        _clear_apply_pending(candidate)
        return True
    if candidate.recovery_intent == "rebuild" and body.get("error") == "memory_embedding_rebuild_required":
        _clear_apply_pending(candidate)
        return True
    return False


def sync_model_service_once(config: V2Config | None = None) -> dict[str, Any]:
    """Refresh one paired instance's model status and hot-apply Memory changes."""

    if not _SYNC_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": "already_running"}
    try:
        config = config or V2Config.load()
        credentials = config.remote_access.vibe_cloud.runtime_credentials()
        if credentials is None:
            return {"ok": False, "configured": False}
        backend_url, instance_id, _device_secret = credentials
        status = _status_from_payload(_device_request(config, "GET", "model-service"))
        current = V2Config.load().memory
        current_key = current.cloud.model_access_key if current.cloud.source_instance_id == instance_id else None
        minted = None
        if _status_needs_model_key(current, status) and not current_key:
            minted = _mint_from_payload(_device_request(config, "POST", "model-access-key"))
        candidate = _resolved_memory(
            current,
            status=status,
            instance_id=instance_id,
            proxy_base_url=f"{backend_url.rstrip('/')}/v1/model",
            minted=minted,
        )
        if candidate == current and not current.cloud.runtime_apply_pending:
            return {"ok": True, "configured": True, "changed": False}
        if candidate == current:
            applied = _reconcile_candidate(current)
            return {
                "ok": applied,
                "configured": True,
                "changed": False,
                "apply_pending": not applied,
            }
        saved = _persist_candidate(current, candidate).memory
        applied = _reconcile_candidate(saved)
        return {
            "ok": applied,
            "configured": True,
            "changed": True,
            "apply_pending": not applied,
        }
    finally:
        _SYNC_LOCK.release()


def rotate_model_access_key(config: V2Config | None = None) -> dict[str, Any]:
    """Rotate mak and apply it through the same sidecar settings ladder."""

    with _SYNC_LOCK:
        config = config or V2Config.load()
        credentials = config.remote_access.vibe_cloud.runtime_credentials()
        if credentials is None:
            raise ModelServiceResolutionError("model_service_not_configured")
        _backend_url, instance_id, _device_secret = credentials
        current = V2Config.load().memory
        if current.cloud.source_instance_id != instance_id or not current.cloud_runtime_selected():
            raise ModelServiceResolutionError("model_service_not_configured")
        minted = _mint_from_payload(_device_request(config, "POST", "model-access-key"))
        candidate = deepcopy(current)
        candidate.cloud.model_access_key = minted.key
        candidate.cloud.runtime_apply_pending = True
        candidate.validate()
        saved = _persist_candidate(current, candidate).memory
        if not _reconcile_candidate(saved):
            rollback = deepcopy(current)
            rollback.cloud.runtime_apply_pending = False
            _persist_candidate(saved, rollback)
            _reconcile_candidate(rollback)
            raise ModelServiceResolutionError("model_access_key_apply_failed")
        return {
            "ok": True,
            "rotated": minted.rotated,
            "created_at": minted.created_at,
            "previous_valid_until": minted.previous_valid_until,
        }


def ensure_model_access_key(config: V2Config | None = None) -> V2Config:
    """Mint the first local mak immediately before a cloud-mode switch."""

    with _SYNC_LOCK:
        config = config or V2Config.load()
        credentials = config.remote_access.vibe_cloud.runtime_credentials()
        if credentials is None:
            raise ModelServiceResolutionError("model_service_not_configured")
        _backend_url, instance_id, _device_secret = credentials
        current = V2Config.load().memory
        if (
            current.cloud.source_instance_id != instance_id
            or not current.cloud.memory_capability_available()
            or current.cloud.scope not in {"platform", "organization"}
        ):
            raise ModelServiceResolutionError("model_service_not_configured")
        if current.cloud.model_access_key:
            return V2Config.load()
        minted = _mint_from_payload(_device_request(config, "POST", "model-access-key"))
        candidate = deepcopy(current)
        candidate.cloud.model_access_key = minted.key
        candidate.validate()
        return _persist_candidate(current, candidate)


def request_model_service_refresh() -> None:
    _REFRESH_EVENT.set()


def _worker_loop(initial_config: V2Config | None) -> None:
    config = initial_config
    while True:
        try:
            sync_model_service_once(config)
        except Exception as exc:
            # Resolution failures can originate in a remote response. Keep the
            # periodic worker observable without echoing that response into logs.
            logger.warning(
                "Cloud Model Service sync failed (%s)",
                type(exc).__name__,
            )
        config = None
        if _REFRESH_EVENT.wait(MODEL_SERVICE_POLL_SECONDS):
            _REFRESH_EVENT.clear()


def start_model_service_polling(config: V2Config | None = None) -> None:
    global _WORKER_STARTED

    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        worker = threading.Thread(
            target=_worker_loop,
            args=(config,),
            name="vibe-model-service-sync",
            daemon=True,
        )
        _WORKER_STARTED = True
        try:
            worker.start()
        except Exception:
            _WORKER_STARTED = False
            raise
