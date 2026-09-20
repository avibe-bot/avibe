from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from config.v2_config import (
    MemoryCloudCapabilities,
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
)
from vibe import model_service
from vibe import ui_memory_routes


def _status(
    *,
    scope: str = "platform",
    chat: bool = True,
    embedding: bool = True,
    identity: str | None = "emb-v1",
    revision: int = 1,
) -> dict:
    return {
        "mode": scope,
        "capabilities": {
            "asr": False,
            "chat": chat,
            "embedding": embedding,
            "multimodal": False,
            "memory_llm": chat,
        },
        "memory_llm_source": "chat_fallback",
        "embedding_identity": identity,
        "quota": {"enforced": False},
        "revision": revision,
    }


def _manual_memory() -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
        mode="custom",
        processing=MemoryProcessingConfig(
            llm=MemoryEndpointConfig(
                "https://llm.example.test/v1",
                "chat-v1",
                "llm-secret",
            ),
            embedding=MemoryEndpointConfig(
                "https://embedding.example.test/v1",
                "embedding-v1",
                "embedding-secret",
            ),
        ),
    )


def _resolved(
    current: MemoryConfig,
    payload: dict,
    *,
    key: str | None = None,
) -> MemoryConfig:
    minted = None
    if key is not None:
        minted = model_service._mint_from_payload(  # noqa: SLF001
            {
                "key": key,
                "created_at": "2026-08-17T00:00:00Z",
                "rotated": False,
                "previous_valid_until": None,
            }
        )
    return model_service._resolved_memory(  # noqa: SLF001
        current,
        status=model_service._status_from_payload(payload),  # noqa: SLF001
        instance_id="instance-1",
        proxy_base_url="https://backend.example.test/v1/model",
        minted=minted,
    )


def test_enterprise_attachment_pauses_custom_without_recovery_marker() -> None:
    candidate = _resolved(
        _manual_memory(),
        _status(scope="organization"),
    )

    assert candidate.settings_mode() == "organization"
    assert candidate.cloud.transition_notice_pending is True
    assert candidate.cloud.organization_attached is False
    assert candidate.runtime_source() == "custom"
    assert candidate.runtime_embedding_identity() == (
        "custom",
        "https://embedding.example.test/v1",
        "embedding-v1",
    )
    assert not hasattr(candidate, "recovery_intent")
    assert not hasattr(candidate.cloud, "transition_rebuild_owned")


def test_cloud_identity_change_keeps_last_applied_runtime_identity() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    changed = _resolved(current, _status(identity="emb-v2", revision=2))

    assert changed.cloud.embedding_identity == "emb-v2"
    assert changed.cloud.applied_embedding_identity == "emb-v1"
    assert changed.cloud.transition_notice_pending is True
    assert changed.runtime_embedding_identity() == ("cloud", "emb-v1", None)
    assert changed.cloud.runtime_apply_pending is True


def test_cloud_identity_notice_alone_requires_live_reconciliation() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(
                chat=True,
                embedding=True,
                memory_llm=True,
            ),
            memory_llm_source="chat_fallback",
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            revision=1,
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    changed = _resolved(current, _status(identity="emb-v2", revision=1))

    assert changed.runtime_processing() == current.runtime_processing()
    assert changed.cloud.transition_notice_pending is True
    assert changed.cloud.runtime_apply_pending is True


def test_scope_release_acknowledgement_compares_applied_cloud_identity() -> None:
    organization = _resolved(
        _manual_memory(),
        _status(scope="organization", identity="emb-org"),
    )
    organization.cloud.model_access_key = "mak_org"
    organization.cloud.organization_attached = True
    organization.cloud.applied_embedding_identity = "emb-org"
    organization.cloud.transition_notice_pending = False

    released = _resolved(
        organization,
        _status(scope="platform", identity="emb-platform", revision=2),
    )
    acknowledged = deepcopy(released)
    acknowledged.cloud.transition_notice_pending = False
    acknowledged.cloud.applied_embedding_identity = "emb-platform"

    assert (
        released.runtime_embedding_identity()
        == acknowledged.runtime_embedding_identity()
    )
    assert ui_memory_routes._memory_embedding_configuration_changed(  # noqa: SLF001
        SimpleNamespace(memory=released),
        SimpleNamespace(memory=acknowledged),
    ) is True


def test_same_cloud_identity_resumes_without_destructive_authority() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(),
            embedding_identity=None,
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    resumed = _resolved(current, _status(identity="emb-v1", revision=2))

    assert resumed.runtime_source() == "cloud"
    assert resumed.cloud.transition_notice_pending is False
    assert resumed.cloud.applied_embedding_identity == "emb-v1"


def test_provider_capability_fault_pauses_without_marking_repair() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    unavailable = _resolved(
        current,
        _status(chat=False, embedding=False, identity=None, revision=2),
    )

    assert unavailable.runtime_source() == "unavailable"
    assert unavailable.cloud.applied_embedding_identity == "emb-v1"
    assert unavailable.legacy_needs_repair is False


def test_unpairing_fences_cloud_egress_and_retains_identity_baseline() -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    fenced = model_service._fenced_cloud_memory(current)  # noqa: SLF001

    assert fenced.runtime_source() == "unavailable"
    assert fenced.cloud.model_access_key is None
    assert fenced.cloud.applied_embedding_identity == "emb-v1"
    assert fenced.cloud.runtime_apply_pending is True


def test_first_cloud_activation_records_identity_without_recovery_state() -> None:
    current = MemoryConfig(enabled=False, mode="platform")

    activated = _resolved(current, _status(identity="emb-v1"), key="mak_first")

    assert activated.enabled is True
    assert activated.cloud.applied_embedding_identity == "emb-v1"
    assert activated.runtime_source() == "cloud"
    assert activated.legacy_needs_repair is False
