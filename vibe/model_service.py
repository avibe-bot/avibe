"""Paired-device Cloud Model Service resolution for local Memory."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
import threading
from typing import Any, Mapping
import urllib.error
import urllib.request

from config.v2_config import (
    MemoryCloudCapabilities,
    MemoryCloudLlmSource,
    MemoryConfig,
    V2Config,
    managed_rerank_projection,
    memory_config_to_payload,
)


logger = logging.getLogger(__name__)

MODEL_SERVICE_POLL_SECONDS = 60
MODEL_SERVICE_REFRESH_PATH = "/api/model-service/refresh"
MODEL_ACCESS_KEY_SUFFIX = "model-access-key?include_typed_keys=1"
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
    memory_llm_source: MemoryCloudLlmSource
    embedding_identity: str | None
    quota_enforced: bool
    revision: int

    def memory_available(self) -> bool:
        return self.capabilities.memory_available() and bool(self.embedding_identity)


@dataclass(frozen=True)
class ModelAccessKeyMint:
    key: str = field(repr=False)
    rerank_access_key: str | None = field(repr=False)
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
    memory_llm_source = payload.get("memory_llm_source")
    embedding_identity = payload.get("embedding_identity")
    quota = payload.get("quota")
    revision = payload.get("revision")
    if scope not in {"organization", "platform"}:
        raise ModelServiceResolutionError("model_service_invalid_response")
    if not isinstance(capabilities, Mapping):
        raise ModelServiceResolutionError("model_service_invalid_response")
    required_capabilities = {"asr", "chat", "embedding", "multimodal", "memory_llm"}
    if not required_capabilities.issubset(capabilities) or any(
        not isinstance(capabilities.get(name), bool) for name in required_capabilities
    ):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if embedding_identity is not None and (not isinstance(embedding_identity, str) or not embedding_identity.strip()):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if bool(capabilities["embedding"]) != bool(embedding_identity):
        raise ModelServiceResolutionError("model_service_invalid_response")
    if memory_llm_source not in {"dedicated", "chat_fallback"}:
        raise ModelServiceResolutionError("model_service_invalid_response")
    if memory_llm_source == "dedicated" and not capabilities["memory_llm"]:
        raise ModelServiceResolutionError("model_service_invalid_response")
    if memory_llm_source == "chat_fallback" and capabilities["memory_llm"] != capabilities["chat"]:
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
            memory_llm=capabilities["memory_llm"],
        ),
        memory_llm_source=memory_llm_source,
        embedding_identity=embedding_identity.strip() if embedding_identity else None,
        quota_enforced=quota["enforced"],
        revision=revision,
    )


def _mint_from_payload(payload: object) -> ModelAccessKeyMint:
    legacy_fields = {
        "key",
        "created_at",
        "rotated",
        "previous_valid_until",
    }
    if not isinstance(payload, Mapping) or set(payload) not in {
        frozenset(legacy_fields),
        frozenset((*legacy_fields, "typed_keys")),
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
    typed_keys = payload.get("typed_keys")
    typed_rerank = typed_keys.get("rerank") if isinstance(typed_keys, Mapping) else None
    rerank_access_key = (
        typed_rerank
        if managed_rerank_projection(key, typed_rerank) is not None
        else None
    )
    return ModelAccessKeyMint(
        key=key,
        rerank_access_key=rerank_access_key,
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


def _runtime_state_signature(memory: MemoryConfig) -> tuple[object, ...]:
    """Return only fields whose change requires sidecar reconciliation."""

    return (
        memory.enabled,
        memory.runtime_source(),
        memory.runtime_processing(),
        memory.effective_multimodal_available(),
        memory.cloud.transition_notice_pending,
    )


def _cancel_organization_transition(memory: MemoryConfig) -> bool:
    """Cancel a pending organization transition notice."""

    if not memory.cloud.transition_notice_pending:
        return False
    memory.cloud.transition_notice_pending = False
    return True


def _cloud_embedding_identity_changed(previous: MemoryConfig, identity: str) -> bool:
    baseline = previous.cloud.applied_embedding_identity
    return baseline is not None and baseline != identity


def _adopt_cloud_embedding_identity(
    candidate: MemoryConfig,
    previous: MemoryConfig,
    identity: str,
) -> bool:
    """Record one cloud target while checking the prior runtime baseline."""

    baseline = previous.cloud.applied_embedding_identity
    first_activation = baseline is None
    if previous.cloud_runtime_selected() and _cloud_embedding_identity_changed(previous, identity):
        candidate.cloud.transition_notice_pending = True
    else:
        candidate.cloud.applied_embedding_identity = identity
    return first_activation


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
        _cancel_organization_transition(candidate)
        candidate.cloud.model_access_key = None
        candidate.cloud.rerank_access_key = None
        candidate.cloud.access_key_revision = None
        candidate.cloud.organization_attached = False
    if candidate.mode is None:
        candidate.mode = _memory_mode_after_initialization(candidate)

    candidate.cloud.scope = status.scope  # type: ignore[assignment]
    candidate.cloud.capabilities = status.capabilities
    candidate.cloud.memory_llm_source = status.memory_llm_source
    candidate.cloud.embedding_identity = status.embedding_identity
    candidate.cloud.revision = status.revision
    candidate.cloud.quota_enforced = status.quota_enforced
    candidate.cloud.proxy_base_url = proxy_base_url
    candidate.cloud.source_instance_id = instance_id
    if minted is not None:
        candidate.cloud.model_access_key = minted.key
        candidate.cloud.rerank_access_key = minted.rerank_access_key
        candidate.cloud.access_key_revision = status.revision

    if status.scope == "organization":
        if status.memory_available():
            assert status.embedding_identity is not None
            if candidate.cloud.organization_attached:
                if candidate.cloud.model_access_key:
                    first_managed_activation = _adopt_cloud_embedding_identity(
                        candidate,
                        previous,
                        status.embedding_identity,
                    )
                    if first_managed_activation:
                        candidate.enabled = True
            elif candidate.cloud.transition_notice_pending:
                pass
            elif candidate.mode == "custom" and previous.custom_processing_complete():
                candidate.cloud.transition_notice_pending = True
            else:
                candidate.cloud.organization_attached = True
                if candidate.cloud.model_access_key:
                    _adopt_cloud_embedding_identity(
                        candidate,
                        previous,
                        status.embedding_identity,
                    )
                    candidate.enabled = True
        else:
            _cancel_organization_transition(candidate)
            if not candidate.cloud.organization_attached and not (
                candidate.mode == "custom" and previous.custom_processing_complete()
            ):
                # Scope binding remains managed even with no Memory pair. Only a
                # complete configuration that was actively custom is grandfathered;
                # every other installation pauses instead of falling back. Attached
                # runtimes keep their identity baseline for a checked resume.
                candidate.cloud.organization_attached = True
                candidate.cloud.applied_embedding_identity = previous.cloud.applied_embedding_identity
    else:
        was_organization_cloud = previous.cloud.scope == "organization" and previous.cloud.organization_attached
        _cancel_organization_transition(candidate)
        candidate.cloud.organization_attached = False
        if candidate.mode == "platform" and status.memory_available() and candidate.cloud.model_access_key:
            assert status.embedding_identity is not None
            first_platform_activation = _adopt_cloud_embedding_identity(
                candidate,
                previous,
                status.embedding_identity,
            )
            if first_platform_activation:
                candidate.enabled = True
        elif was_organization_cloud:
            if candidate.mode != "platform":
                candidate.cloud.transition_notice_pending = True
            elif status.memory_available():
                assert status.embedding_identity is not None
                if _cloud_embedding_identity_changed(previous, status.embedding_identity):
                    candidate.cloud.transition_notice_pending = True
            # The applied identity is also the durable "not fresh" baseline.
            # Keep it across an unavailable release so later capability recovery
            # can compare identities without overriding an explicit user opt-out.

    candidate.cloud.runtime_apply_pending = (
        current.cloud.runtime_apply_pending
        or _runtime_state_signature(candidate) != _runtime_state_signature(current)
    )
    candidate.validate()
    return candidate


def _fenced_cloud_memory(current: MemoryConfig) -> MemoryConfig:
    """Fence cloud egress while retaining scope and the identity baseline."""

    cloud = current.cloud
    had_cloud_state = bool(
        cloud.scope
        or cloud.model_access_key
        or cloud.rerank_access_key
        or cloud.access_key_revision is not None
        or cloud.proxy_base_url
        or cloud.source_instance_id
        or cloud.organization_attached
        or cloud.transition_notice_pending
    )
    if not had_cloud_state:
        return current

    candidate = deepcopy(current)
    candidate.cloud.capabilities = MemoryCloudCapabilities()
    candidate.cloud.memory_llm_source = None
    candidate.cloud.embedding_identity = None
    candidate.cloud.revision = None
    candidate.cloud.quota_enforced = False
    candidate.cloud.model_access_key = None
    candidate.cloud.rerank_access_key = None
    candidate.cloud.access_key_revision = None
    candidate.cloud.proxy_base_url = None
    _cancel_organization_transition(candidate)
    candidate.cloud.runtime_apply_pending = (
        current.cloud.runtime_apply_pending
        or _runtime_state_signature(candidate) != _runtime_state_signature(current)
    )
    candidate.validate()
    return candidate


def _fence_replaced_pairing(current: MemoryConfig) -> tuple[MemoryConfig, bool]:
    """Durably stop an old instance runtime before contacting its replacement."""

    candidate = _fenced_cloud_memory(current)
    changed = candidate != current
    if changed:
        candidate = _persist_candidate(current, candidate).memory
    if candidate.cloud.runtime_apply_pending:
        if not _reconcile_candidate(candidate):
            raise ModelServiceResolutionError("model_service_pairing_fence_failed")
        candidate = V2Config.load().memory
    return candidate, changed


def _guard_pairing_authority(credentials: tuple[str, str, str]) -> None:
    """Fence stale cloud egress whenever the request authority has changed."""

    live = V2Config.load()
    if live.remote_access.vibe_cloud.runtime_credentials() == credentials:
        return
    _fence_replaced_pairing(live.memory)
    request_model_service_refresh()
    raise ModelServiceResolutionError("model_service_pairing_changed")


def _paired_device_request(
    config: V2Config,
    credentials: tuple[str, str, str],
    method: str,
    suffix: str,
) -> dict[str, Any]:
    """Run one device request only while its captured pairing owns egress."""

    _guard_pairing_authority(credentials)
    try:
        payload = _device_request(config, method, suffix)
    except Exception:
        _guard_pairing_authority(credentials)
        raise
    _guard_pairing_authority(credentials)
    return payload


def _persist_candidate(current: MemoryConfig, candidate: MemoryConfig) -> V2Config:
    """Persist a candidate with any required sidecar apply durably marked."""

    from vibe import api

    candidate = deepcopy(candidate)
    candidate.cloud.runtime_apply_pending = (
        current.cloud.runtime_apply_pending
        or _runtime_state_signature(candidate) != _runtime_state_signature(current)
    )
    return api.save_memory_config(
        memory_config_to_payload(candidate, include_secrets=True),
        expected=current,
    )


def clear_runtime_apply_pending(expected: MemoryConfig) -> V2Config:
    """Clear pending only when *expected* is still the applied configuration."""

    from config.v2_config import atomic_update_memory

    def clear(current: MemoryConfig) -> MemoryConfig:
        if current != expected:
            return current
        current.cloud.runtime_apply_pending = False
        return current

    return atomic_update_memory(clear)


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
        clear_runtime_apply_pending(candidate)
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
            current = V2Config.load().memory
            candidate = _fenced_cloud_memory(current)
            if candidate == current and not current.cloud.runtime_apply_pending:
                return {"ok": False, "configured": False, "changed": False}
            changed = candidate != current
            if changed:
                candidate = _persist_candidate(current, candidate).memory
            applied = _reconcile_candidate(candidate)
            return {
                "ok": applied,
                "configured": False,
                "changed": changed,
                "apply_pending": not applied,
            }
        backend_url, instance_id, _device_secret = credentials
        changed = False
        current = V2Config.load().memory
        if (
            current.cloud.source_instance_id
            and current.cloud.source_instance_id != instance_id
        ):
            current, changed = _fence_replaced_pairing(current)
        status = _status_from_payload(
            _paired_device_request(config, credentials, "GET", "model-service")
        )
        current = V2Config.load().memory
        candidate = _resolved_memory(
            current,
            status=status,
            instance_id=instance_id,
            proxy_base_url=f"{backend_url.rstrip('/')}/v1/model",
            minted=None,
        )
        status_changed = candidate != current
        if status_changed:
            current = _persist_candidate(current, candidate).memory
            changed = True

        current_key = current.cloud.model_access_key
        if _status_needs_model_key(current, status) and (
            not current_key
            or current.cloud.access_key_revision != status.revision
        ):
            try:
                minted = _mint_from_payload(
                    _paired_device_request(
                        config,
                        credentials,
                        "POST",
                        MODEL_ACCESS_KEY_SUFFIX,
                    )
                )
            except Exception:
                if current.cloud.runtime_apply_pending:
                    _reconcile_candidate(current)
                raise
            candidate = _resolved_memory(
                current,
                status=status,
                instance_id=instance_id,
                proxy_base_url=f"{backend_url.rstrip('/')}/v1/model",
                minted=minted,
            )
            if candidate != current:
                current = _persist_candidate(current, candidate).memory
                changed = True

        if not current.cloud.runtime_apply_pending:
            return {"ok": True, "configured": True, "changed": changed}
        applied = _reconcile_candidate(current)
        return {
            "ok": applied,
            "configured": True,
            "changed": changed,
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
        minted = _mint_from_payload(
            _paired_device_request(
                config,
                credentials,
                "POST",
                MODEL_ACCESS_KEY_SUFFIX,
            )
        )
        candidate = deepcopy(current)
        candidate.cloud.model_access_key = minted.key
        candidate.cloud.rerank_access_key = minted.rerank_access_key
        candidate.cloud.access_key_revision = current.cloud.revision
        candidate.cloud.runtime_apply_pending = True
        candidate.validate()
        saved = _persist_candidate(current, candidate).memory
        if not _reconcile_candidate(saved):
            rollback = deepcopy(current)
            rollback.cloud.runtime_apply_pending = False
            rollback = _persist_candidate(saved, rollback).memory
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
        if (
            current.cloud.model_access_key
            and current.cloud.access_key_revision is not None
            and current.cloud.access_key_revision == current.cloud.revision
        ):
            return V2Config.load()
        minted = _mint_from_payload(
            _paired_device_request(
                config,
                credentials,
                "POST",
                MODEL_ACCESS_KEY_SUFFIX,
            )
        )
        candidate = deepcopy(current)
        candidate.cloud.model_access_key = minted.key
        candidate.cloud.rerank_access_key = minted.rerank_access_key
        candidate.cloud.access_key_revision = current.cloud.revision
        candidate.validate()
        return _persist_candidate(current, candidate)


def _model_service_ui_origins(config: V2Config) -> tuple[str, ...]:
    """Reuse the tunnel's canonical route to the local UI process."""

    from vibe import remote_access

    return (remote_access.origin_service_for_pairing(config),)


def _notify_ui_model_service_refresh(config: V2Config) -> bool:
    """Wake the UI-owned worker when pairing changes in another process."""

    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token
    from vibe import runtime

    try:
        ui_pid = int(runtime.read_status().get("ui_pid"))
    except (TypeError, ValueError):
        return False
    if ui_pid == os.getpid():
        return False

    headers = {"X-Vibe-Show-Client": "cli"}
    for origin in _model_service_ui_origins(config):
        try:
            status_request = urllib.request.Request(
                f"{origin}/status",
                method="GET",
                headers=headers,
            )
            with urllib.request.urlopen(status_request, timeout=0.75) as response:
                status = json.loads(response.read().decode("utf-8"))
            if int(status.get("ui_pid")) != ui_pid:
                continue
            refresh_request = urllib.request.Request(
                f"{origin}{MODEL_SERVICE_REFRESH_PATH}",
                data=b"{}",
                method="POST",
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token(),
                },
            )
            with urllib.request.urlopen(refresh_request, timeout=0.75) as response:
                return response.status == 200
        except (OSError, TypeError, ValueError, json.JSONDecodeError, urllib.error.URLError):
            continue
    return False


def request_model_service_refresh() -> None:
    _REFRESH_EVENT.set()
    try:
        _notify_ui_model_service_refresh(V2Config.load())
    except Exception:
        # The in-process event remains authoritative when the UI is this process;
        # a stopped or unavailable UI has no sleeping worker to wake.
        logger.debug("Cloud Model Service UI refresh notification was unavailable", exc_info=True)


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
