from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from config.v2_config import (
    MemoryCloudCapabilities,
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
)
from vibe import model_service
from vibe import ui_memory_routes


def _key_payload(
    key: str,
    *,
    rerank_access_key: object = None,
    include_typed_keys: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": key,
        "created_at": "2026-08-17T00:00:00Z",
        "rotated": False,
        "previous_valid_until": None,
    }
    if include_typed_keys:
        payload["typed_keys"] = {"rerank": rerank_access_key}
    return payload


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
            _key_payload(key)
        )
    return model_service._resolved_memory(  # noqa: SLF001
        current,
        status=model_service._status_from_payload(payload),  # noqa: SLF001
        instance_id="instance-1",
        proxy_base_url="https://backend.example.test/v1/model",
        minted=minted,
    )


def _active_cloud_memory(
    *,
    key: str = "mak_opaque",
    rerank_access_key: str | None = None,
    access_key_revision: int | None = 1,
    revision: int = 1,
) -> MemoryConfig:
    return MemoryConfig(
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
            revision=revision,
            model_access_key=key,
            rerank_access_key=rerank_access_key,
            access_key_revision=access_key_revision,
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )


def test_model_access_key_parser_accepts_legacy_and_typed_responses() -> None:
    legacy = model_service._mint_from_payload(  # noqa: SLF001
        _key_payload("mak_opaque")
    )
    typed = model_service._mint_from_payload(  # noqa: SLF001
        _key_payload(
            "mak_opaque",
            rerank_access_key="mak_rr_dashscope_opaque",
            include_typed_keys=True,
        )
    )

    assert legacy.key == typed.key == "mak_opaque"
    assert legacy.rerank_access_key is None
    assert typed.rerank_access_key == "mak_rr_dashscope_opaque"
    assert "mak_opaque" not in repr(typed)
    assert "mak_rr_dashscope_opaque" not in repr(typed)


@pytest.mark.parametrize(
    "typed_keys",
    [
        None,
        [],
        {},
        {"rerank": None},
        {"rerank": "mak_rr_future_opaque"},
        {"rerank": "mak_rr_deepinfra_"},
        {"rerank": "mak_rr_deepinfra_different"},
    ],
)
def test_model_access_key_parser_keeps_base_key_for_any_unusable_typed_shape(
    typed_keys: object,
) -> None:
    payload = _key_payload("mak_opaque")
    payload["typed_keys"] = typed_keys

    minted = model_service._mint_from_payload(payload)  # noqa: SLF001

    assert minted.key == "mak_opaque"
    assert minted.rerank_access_key is None


@pytest.mark.parametrize(
    (
        "current_key",
        "current_rerank",
        "current_key_revision",
        "status_revision",
        "minted_key",
        "minted_rerank",
    ),
    [
        ("mak_opaque", None, 1, 2, "mak_opaque", "mak_rr_deepinfra_opaque"),
        (
            "mak_opaque",
            "mak_rr_deepinfra_opaque",
            1,
            2,
            "mak_opaque",
            None,
        ),
        (
            "mak_old",
            "mak_rr_deepinfra_old",
            1,
            2,
            "mak_new",
            "mak_rr_deepinfra_new",
        ),
        (
            "mak_opaque",
            "mak_rr_deepinfra_opaque",
            1,
            2,
            "mak_opaque",
            "mak_rr_dashscope_opaque",
        ),
        ("mak_opaque", None, None, 1, "mak_opaque", "mak_rr_dashscope_opaque"),
    ],
)
def test_key_bundle_changes_use_one_processing_reconciliation_without_embedding_change(
    monkeypatch: pytest.MonkeyPatch,
    current_key: str,
    current_rerank: str | None,
    current_key_revision: int | None,
    status_revision: int,
    minted_key: str,
    minted_rerank: str | None,
) -> None:
    state = {
        "memory": _active_cloud_memory(
            key=current_key,
            rerank_access_key=current_rerank,
            access_key_revision=current_key_revision,
            revision=1,
        )
    }
    prior_embedding_identity = state["memory"].runtime_embedding_identity()
    prior_processing = state["memory"].runtime_processing()
    requests: list[str] = []
    reconciled: list[MemoryConfig] = []

    class _Cloud:
        @staticmethod
        def runtime_credentials() -> tuple[str, str, str]:
            return "https://backend.example.test", "instance-1", "device-secret"

    config = SimpleNamespace(
        remote_access=SimpleNamespace(vibe_cloud=_Cloud()),
    )

    def load() -> SimpleNamespace:
        return SimpleNamespace(memory=deepcopy(state["memory"]))

    def request(_config, _credentials, _method, suffix):
        requests.append(suffix)
        if suffix == "model-service":
            return _status(revision=status_revision)
        assert suffix == model_service.MODEL_ACCESS_KEY_SUFFIX
        return _key_payload(
            minted_key,
            rerank_access_key=minted_rerank,
            include_typed_keys=minted_rerank is not None,
        )

    def persist(_expected, candidate):
        state["memory"] = deepcopy(candidate)
        return SimpleNamespace(memory=deepcopy(candidate))

    def reconcile(candidate):
        reconciled.append(deepcopy(candidate))
        return True

    monkeypatch.setattr(model_service.V2Config, "load", load)
    monkeypatch.setattr(model_service, "_paired_device_request", request)
    monkeypatch.setattr(model_service, "_persist_candidate", persist)
    monkeypatch.setattr(model_service, "_reconcile_candidate", reconcile)

    result = model_service.sync_model_service_once(config)

    assert result["ok"] is True
    assert requests == ["model-service", model_service.MODEL_ACCESS_KEY_SUFFIX]
    assert len(reconciled) == 1
    assert state["memory"].cloud.model_access_key == minted_key
    assert state["memory"].cloud.rerank_access_key == minted_rerank
    assert state["memory"].cloud.access_key_revision == status_revision
    assert state["memory"].runtime_processing() != prior_processing
    assert state["memory"].runtime_embedding_identity() == prior_embedding_identity
    assert state["memory"].cloud.applied_embedding_identity == "emb-v1"
    assert state["memory"].cloud.transition_notice_pending is False


@pytest.mark.parametrize(
    "first_mint_outcome",
    ["timeout", "upstream_error", "malformed"],
)
def test_stale_typed_rerank_is_fenced_until_matching_revision_mint(
    first_mint_outcome: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {
        "memory": _active_cloud_memory(
            rerank_access_key="mak_rr_deepinfra_opaque",
            access_key_revision=1,
            revision=1,
        )
    }
    before = deepcopy(state["memory"])
    requests: list[str] = []
    reconciled: list[MemoryConfig] = []
    mint_attempts = 0

    class _Cloud:
        @staticmethod
        def runtime_credentials() -> tuple[str, str, str]:
            return "https://backend.example.test", "instance-1", "device-secret"

    config = SimpleNamespace(
        remote_access=SimpleNamespace(vibe_cloud=_Cloud()),
    )

    def load() -> SimpleNamespace:
        return SimpleNamespace(memory=deepcopy(state["memory"]))

    def request(_config, _credentials, _method, suffix):
        nonlocal mint_attempts
        requests.append(suffix)
        if suffix == "model-service":
            return _status(revision=2)
        assert suffix == model_service.MODEL_ACCESS_KEY_SUFFIX
        mint_attempts += 1
        if mint_attempts == 1:
            if first_mint_outcome == "timeout":
                raise TimeoutError("mint timed out")
            if first_mint_outcome == "upstream_error":
                raise model_service.ModelServiceResolutionError(
                    "cloud_request_failed"
                )
            return {"key": "malformed"}
        return _key_payload(
            "mak_opaque",
            rerank_access_key="mak_rr_dashscope_opaque",
            include_typed_keys=True,
        )

    def persist(_expected, candidate):
        state["memory"] = deepcopy(candidate)
        return SimpleNamespace(memory=deepcopy(candidate))

    def reconcile(candidate):
        reconciled.append(deepcopy(candidate))
        applied = deepcopy(candidate)
        applied.cloud.runtime_apply_pending = False
        state["memory"] = applied
        return True

    monkeypatch.setattr(model_service.V2Config, "load", load)
    monkeypatch.setattr(model_service, "_paired_device_request", request)
    monkeypatch.setattr(model_service, "_persist_candidate", persist)
    monkeypatch.setattr(model_service, "_reconcile_candidate", reconcile)

    with pytest.raises((TimeoutError, model_service.ModelServiceResolutionError)):
        model_service.sync_model_service_once(config)

    stale = deepcopy(state["memory"])
    stale_processing = stale.runtime_processing()
    assert stale.cloud.revision == 2
    assert stale.cloud.access_key_revision == 1
    assert stale.cloud.model_access_key == "mak_opaque"
    assert stale.cloud.rerank_access_key == "mak_rr_deepinfra_opaque"
    assert stale.runtime_source() == "cloud"
    assert stale_processing.llm.complete() is True
    assert stale_processing.embedding.complete() is True
    assert stale_processing.rerank is None
    assert stale.runtime_embedding_identity() == before.runtime_embedding_identity()
    assert ui_memory_routes._memory_embedding_configuration_changed(  # noqa: SLF001
        SimpleNamespace(memory=before),
        SimpleNamespace(memory=stale),
    ) is False
    assert len(reconciled) == 1
    assert reconciled[0].runtime_processing().rerank is None

    result = model_service.sync_model_service_once(config)

    restored = state["memory"]
    rerank = restored.runtime_processing().rerank
    assert result["ok"] is True
    assert restored.cloud.revision == 2
    assert restored.cloud.access_key_revision == 2
    assert rerank is not None
    assert rerank.provider == "dashscope"
    assert rerank.api_key == "mak_rr_dashscope_opaque"
    assert restored.runtime_embedding_identity() == before.runtime_embedding_identity()
    assert ui_memory_routes._memory_embedding_configuration_changed(  # noqa: SLF001
        SimpleNamespace(memory=stale),
        SimpleNamespace(memory=restored),
    ) is False
    assert len(reconciled) == 2
    assert reconciled[1].runtime_processing().rerank == rerank
    assert requests == [
        "model-service",
        model_service.MODEL_ACCESS_KEY_SUFFIX,
        "model-service",
        model_service.MODEL_ACCESS_KEY_SUFFIX,
    ]


@pytest.mark.parametrize("operation", ["ensure", "rotate"])
def test_explicit_key_bundle_paths_use_the_typed_opt_in_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    state = {
        "memory": _active_cloud_memory(
            access_key_revision=None if operation == "ensure" else 4,
            revision=4,
        )
    }
    requests: list[tuple[str, str]] = []
    reconciled: list[MemoryConfig] = []

    class _Cloud:
        @staticmethod
        def runtime_credentials() -> tuple[str, str, str]:
            return "https://backend.example.test", "instance-1", "device-secret"

    config = SimpleNamespace(
        remote_access=SimpleNamespace(vibe_cloud=_Cloud()),
    )

    def load() -> SimpleNamespace:
        return SimpleNamespace(memory=deepcopy(state["memory"]))

    def request(_config, _credentials, method, suffix):
        requests.append((method, suffix))
        return _key_payload(
            "mak_rotated",
            rerank_access_key="mak_rr_deepinfra_rotated",
            include_typed_keys=True,
        )

    def persist(_expected, candidate):
        state["memory"] = deepcopy(candidate)
        return SimpleNamespace(memory=deepcopy(candidate))

    def reconcile(candidate):
        reconciled.append(deepcopy(candidate))
        return True

    monkeypatch.setattr(model_service.V2Config, "load", load)
    monkeypatch.setattr(model_service, "_paired_device_request", request)
    monkeypatch.setattr(model_service, "_persist_candidate", persist)
    monkeypatch.setattr(model_service, "_reconcile_candidate", reconcile)

    if operation == "ensure":
        result = model_service.ensure_model_access_key(config)
        assert result.memory == state["memory"]
        assert reconciled == []
    else:
        result = model_service.rotate_model_access_key(config)
        assert result["ok"] is True
        assert len(reconciled) == 1

    assert requests == [("POST", model_service.MODEL_ACCESS_KEY_SUFFIX)]
    assert state["memory"].cloud.model_access_key == "mak_rotated"
    assert state["memory"].cloud.rerank_access_key == (
        "mak_rr_deepinfra_rotated"
    )
    assert state["memory"].cloud.access_key_revision == 4


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
            rerank_access_key="mak_rr_deepinfra_first",
            access_key_revision=1,
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
    assert fenced.cloud.rerank_access_key is None
    assert fenced.cloud.access_key_revision is None
    assert fenced.cloud.applied_embedding_identity == "emb-v1"
    assert fenced.cloud.runtime_apply_pending is True


def test_first_cloud_activation_records_identity_without_recovery_state() -> None:
    current = MemoryConfig(enabled=False, mode="platform")

    activated = _resolved(current, _status(identity="emb-v1"), key="mak_first")

    assert activated.enabled is True
    assert activated.cloud.applied_embedding_identity == "emb-v1"
    assert activated.runtime_source() == "cloud"
    assert activated.legacy_needs_repair is False
