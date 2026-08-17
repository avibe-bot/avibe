from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from config.v2_config import (
    AgentsConfig,
    MemoryCloudCapabilities,
    MemoryCloudConfig,
    MemoryConfig,
    MemoryEndpointConfig,
    MemoryProcessingConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
    memory_config_to_payload,
)
from core.memory import process as memory_process
from core.memory import runtime as memory_runtime
from core.memory.everos import MULTIMODAL_EXPLICIT_ENV
from vibe import model_service, ui_memory_routes


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "memory" / "released_mode_initialization.json"


def _status(
    *,
    scope: str = "platform",
    chat: bool = True,
    embedding: bool = True,
    multimodal: bool = False,
    identity: str | None = "emb-v1",
    revision: int = 1,
) -> dict:
    return {
        "mode": scope,
        "capabilities": {
            "asr": False,
            "chat": chat,
            "embedding": embedding,
            "multimodal": multimodal,
        },
        "embedding_identity": identity,
        "quota": {"enforced": False},
        "revision": revision,
    }


def _mint(
    key: str = "mak_first",
    *,
    rotated: bool = False,
    previous_valid_until: str | None = None,
) -> dict:
    return {
        "key": key,
        "created_at": "2026-08-17T00:00:00Z",
        "rotated": rotated,
        "previous_valid_until": previous_valid_until,
    }


def _paired_config(memory: MemoryConfig | None = None) -> V2Config:
    config = V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        memory=memory or MemoryConfig(),
    )
    cloud = config.remote_access.vibe_cloud
    cloud.enabled = True
    cloud.backend_url = "https://backend.example.test"
    cloud.instance_id = "instance-1"
    cloud.instance_secret = "device-secret"
    return config


def _manual_memory() -> MemoryConfig:
    return MemoryConfig(
        enabled=True,
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
    minted: dict | None = None,
) -> MemoryConfig:
    return model_service._resolved_memory(  # noqa: SLF001
        current,
        status=model_service._status_from_payload(payload),  # noqa: SLF001
        instance_id="instance-1",
        proxy_base_url="https://backend.example.test/v1/model",
        minted=(
            model_service._mint_from_payload(minted)  # noqa: SLF001
            if minted is not None
            else None
        ),
    )


def test_released_memory_shapes_initialize_mode_without_reinterpreting_manual_config(
    tmp_path: Path,
) -> None:
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for fixture in fixtures:
        config_path = tmp_path / f"{fixture['name']}.json"
        config_path.write_text(json.dumps(fixture["config"]), encoding="utf-8")
        loaded = V2Config.load(config_path).memory
        assert loaded.mode is None, fixture["name"]
        assert (
            model_service._memory_mode_after_initialization(loaded)  # noqa: SLF001
            == fixture["expected_mode"]
        ), fixture["name"]


def test_fresh_paired_install_gets_zero_config_cloud_memory_with_write_only_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config().save()
    requests: list[tuple[str, str]] = []
    reconciled: list[MemoryConfig] = []

    def device_request(_config: V2Config, method: str, suffix: str) -> dict:
        requests.append((method, suffix))
        return _status(multimodal=False) if method == "GET" else _mint()

    def reconcile(candidate: MemoryConfig) -> bool:
        reconciled.append(deepcopy(candidate))
        model_service._clear_apply_pending(candidate)  # noqa: SLF001
        return True

    monkeypatch.setattr(model_service, "_device_request", device_request)
    monkeypatch.setattr(model_service, "_reconcile_candidate", reconcile)

    assert model_service.sync_model_service_once()["ok"] is True

    memory = V2Config.load().memory
    runtime = memory.runtime_processing()
    assert requests == [
        ("GET", "model-service"),
        ("POST", "model-access-key"),
    ]
    assert memory.mode == "platform"
    assert memory.settings_mode() == "platform"
    assert memory.enabled is True
    assert memory.cloud.model_access_key == "mak_first"
    assert memory.cloud.embedding_identity == "emb-v1"
    assert runtime.llm.base_url == "https://backend.example.test/v1/model"
    assert runtime.llm.model == "avibe-cloud-chat"
    assert runtime.embedding.model == "avibe-cloud-embedding-emb-v1"
    assert runtime.llm.api_key == "mak_first"
    assert runtime.multimodal is None
    assert memory.effective_multimodal_available() is True
    assert reconciled
    projected = memory_config_to_payload(memory)
    assert projected["cloud"]["model_access_key"] is None
    assert projected["cloud"]["has_model_access_key"] is True


def test_unpairing_clears_cloud_runtime_and_reconciles_the_running_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
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
    config = _paired_config(current)
    config.remote_access.vibe_cloud.enabled = False
    config.save()
    reconciled: list[MemoryConfig] = []

    def reconcile(candidate: MemoryConfig) -> bool:
        reconciled.append(deepcopy(candidate))
        model_service._clear_apply_pending(candidate)  # noqa: SLF001
        return True

    monkeypatch.setattr(model_service, "_reconcile_candidate", reconcile)
    monkeypatch.setattr(
        model_service,
        "_device_request",
        lambda *_args: pytest.fail("unpaired sync must not call the backend"),
    )

    result = model_service.sync_model_service_once()
    memory = V2Config.load().memory

    assert result == {
        "ok": True,
        "configured": False,
        "changed": True,
        "apply_pending": False,
    }
    assert reconciled and reconciled[0].runtime_source() == "unavailable"
    assert memory.enabled is True
    assert memory.cloud.scope == "platform"
    assert memory.cloud.model_access_key is None
    assert memory.cloud.proxy_base_url is None
    assert memory.cloud.source_instance_id == "instance-1"
    assert memory.cloud.applied_embedding_identity == "emb-v1"
    assert memory.cloud.organization_attached is False


def test_managed_scope_is_persisted_before_first_key_mint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _paired_config().save()

    def device_request(_config: V2Config, method: str, _suffix: str) -> dict:
        if method == "GET":
            return _status(scope="organization")
        persisted = V2Config.load().memory
        assert persisted.settings_mode() == "organization"
        assert persisted.cloud.organization_attached is True
        assert persisted.cloud.model_access_key is None
        raise model_service.ModelServiceResolutionError("mint_temporarily_unavailable")

    monkeypatch.setattr(model_service, "_device_request", device_request)

    with pytest.raises(
        model_service.ModelServiceResolutionError,
        match="mint_temporarily_unavailable",
    ):
        model_service.sync_model_service_once()

    memory = V2Config.load().memory
    assert memory.settings_mode() == "organization"
    assert memory.cloud.organization_attached is True
    assert memory.cloud.model_access_key is None
    assert memory.enabled is False
    assert memory.runtime_source() == "unavailable"


def test_metadata_only_status_refresh_does_not_reconcile_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            revision=1,
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )
    _paired_config(current).save()
    payload = _status(revision=2)
    payload["capabilities"]["asr"] = True
    payload["quota"]["enforced"] = True
    monkeypatch.setattr(model_service, "_device_request", lambda *_args: payload)
    monkeypatch.setattr(
        model_service,
        "_reconcile_candidate",
        lambda _candidate: pytest.fail("metadata-only refresh must not reconcile"),
    )

    result = model_service.sync_model_service_once()
    memory = V2Config.load().memory

    assert result == {"ok": True, "configured": True, "changed": True}
    assert memory.cloud.revision == 2
    assert memory.cloud.capabilities.asr is True
    assert memory.cloud.quota_enforced is True
    assert memory.cloud.runtime_apply_pending is False


def test_enterprise_attachment_pauses_custom_until_acknowledged() -> None:
    current = _manual_memory()
    status = model_service._status_from_payload(  # noqa: SLF001
        _status(scope="organization")
    )
    assert model_service._status_needs_model_key(current, status) is False  # noqa: SLF001
    candidate = _resolved(
        current,
        _status(scope="organization"),
    )

    assert candidate.settings_mode() == "organization"
    assert candidate.cloud.transition_notice_pending is True
    assert candidate.cloud.organization_attached is False
    assert candidate.cloud.model_access_key is None
    assert candidate.recovery_intent == "rebuild"
    assert candidate.runtime_source() == "custom"
    assert candidate.runtime_embedding_identity() == (
        "custom",
        "https://embedding.example.test/v1",
        "embedding-v1",
    )

    ready_to_acknowledge = deepcopy(candidate)
    ready_to_acknowledge.cloud.model_access_key = "mak_first"
    current = _paired_config(ready_to_acknowledge)
    target, confirm_rebuild = ui_memory_routes._memory_settings_patch(  # noqa: SLF001
        current,
        {"acknowledge_transition": True, "confirm_rebuild": True},
    )
    acknowledged = ui_memory_routes._memory_candidate_config(  # noqa: SLF001
        current,
        target,
    ).memory
    assert confirm_rebuild is True
    assert acknowledged.cloud.transition_notice_pending is False
    assert acknowledged.cloud.organization_attached is True
    assert acknowledged.runtime_source() == "cloud"
    assert acknowledged.runtime_embedding_identity() == ("cloud", "emb-v1", None)
    assert (
        ui_memory_routes._memory_embedding_configuration_changed(  # noqa: SLF001
            current,
            _paired_config(acknowledged),
        )
        is True
    )


@pytest.mark.parametrize(
    "event",
    ["scope_release", "capability_removal", "unpair"],
)
def test_canceling_enterprise_attachment_resumes_the_preserved_custom_runtime(
    event: str,
) -> None:
    pending = _resolved(
        _manual_memory(),
        _status(scope="organization"),
    )

    if event == "scope_release":
        canceled = _resolved(
            pending,
            _status(scope="platform", revision=2),
        )
    elif event == "capability_removal":
        canceled = _resolved(
            pending,
            _status(
                scope="organization",
                chat=False,
                embedding=False,
                identity=None,
                revision=2,
            ),
        )
    else:
        canceled = model_service._unpaired_memory(pending)  # noqa: SLF001

    assert canceled.cloud.transition_notice_pending is False
    assert canceled.cloud.organization_attached is False
    assert canceled.recovery_intent is None
    assert canceled.runtime_source() == "custom"
    assert canceled.cloud.runtime_apply_pending is True


def test_canceling_enterprise_attachment_preserves_an_unrelated_recovery_fence() -> None:
    pending = _resolved(
        _manual_memory(),
        _status(scope="organization"),
    )
    pending.recovery_intent = "factory_reset"

    canceled = _resolved(
        pending,
        _status(scope="platform", revision=2),
    )

    assert canceled.cloud.transition_notice_pending is False
    assert canceled.recovery_intent == "factory_reset"


def test_enterprise_transition_acknowledgement_cannot_edit_custom_endpoints() -> None:
    pending = _resolved(
        _manual_memory(),
        _status(scope="organization"),
    )
    pending.cloud.model_access_key = "mak_first"

    with pytest.raises(ValueError, match="invalid_memory_patch"):
        ui_memory_routes._memory_settings_patch(  # noqa: SLF001
            _paired_config(pending),
            {
                "acknowledge_transition": True,
                "confirm_rebuild": True,
                "processing": {
                    "embedding": {"model": "unauthorized-new-model"},
                },
            },
        )


def test_org_without_memory_pair_does_not_transition_released_custom_config() -> None:
    current = _manual_memory()
    candidate = _resolved(
        current,
        _status(
            scope="organization",
            chat=True,
            embedding=False,
            identity=None,
        ),
    )

    assert candidate.settings_mode() == "custom"
    assert candidate.runtime_source() == "custom"
    assert candidate.cloud.transition_notice_pending is False
    assert candidate.cloud.organization_attached is False
    assert candidate.recovery_intent is None

    fresh = _resolved(
        MemoryConfig(),
        _status(
            scope="organization",
            chat=False,
            embedding=False,
            identity=None,
        ),
    )
    assert fresh.enabled is False
    assert fresh.settings_mode() == "organization"
    assert fresh.cloud.organization_attached is True
    assert fresh.runtime_source() == "unavailable"


def test_enterprise_without_memory_pair_pauses_an_active_platform_instance() -> None:
    platform = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-platform",
            applied_embedding_identity="emb-platform",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    paused = _resolved(
        platform,
        _status(
            scope="organization",
            chat=False,
            embedding=False,
            identity=None,
            revision=2,
        ),
    )

    assert paused.settings_mode() == "organization"
    assert paused.cloud.organization_attached is True
    assert paused.cloud.applied_embedding_identity == "emb-platform"
    assert paused.enabled is True
    assert paused.runtime_source() == "unavailable"


def test_fresh_managed_instance_enables_when_organization_adds_memory_pair() -> None:
    waiting = _resolved(
        MemoryConfig(),
        _status(
            scope="organization",
            chat=False,
            embedding=False,
            identity=None,
        ),
    )

    activated = _resolved(
        waiting,
        _status(scope="organization", identity="emb-org", revision=2),
        minted=_mint(),
    )

    assert activated.enabled is True
    assert activated.cloud.organization_attached is True
    assert activated.cloud.applied_embedding_identity == "emb-org"
    assert activated.runtime_source() == "cloud"
    assert activated.recovery_intent is None


def test_fresh_platform_instance_enables_when_memory_capabilities_recover() -> None:
    waiting = _resolved(
        MemoryConfig(),
        _status(
            scope="platform",
            chat=False,
            embedding=False,
            identity=None,
        ),
    )

    activated = _resolved(
        waiting,
        _status(scope="platform", identity="emb-platform", revision=2),
        minted=_mint(),
    )

    assert waiting.mode == "platform"
    assert waiting.enabled is False
    assert activated.enabled is True
    assert activated.cloud.applied_embedding_identity == "emb-platform"
    assert activated.runtime_source() == "cloud"
    assert activated.recovery_intent is None


def test_capability_removal_pauses_and_resume_checks_last_applied_identity() -> None:
    attached = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="organization",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
            organization_attached=True,
        ),
    )
    removed = _resolved(
        attached,
        _status(
            scope="organization",
            chat=False,
            embedding=True,
            identity="emb-v1",
            revision=2,
        ),
    )
    assert removed.runtime_source() == "unavailable"
    assert removed.runtime_embedding_identity() == ("cloud", "emb-v1", None)
    assert removed.recovery_intent is None

    same_identity = _resolved(
        removed,
        _status(scope="organization", identity="emb-v1", revision=3),
    )
    assert same_identity.runtime_source() == "cloud"
    assert same_identity.recovery_intent is None

    changed_identity = _resolved(
        removed,
        _status(scope="organization", identity="emb-v2", revision=4),
    )
    assert changed_identity.runtime_source() == "cloud"
    assert changed_identity.recovery_intent == "rebuild"


def test_platform_to_organization_upstream_change_is_an_identity_change() -> None:
    platform = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-platform",
            applied_embedding_identity="emb-platform",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )

    organization = _resolved(
        platform,
        _status(scope="organization", identity="emb-organization", revision=2),
    )

    assert organization.cloud.organization_attached is True
    assert organization.runtime_embedding_identity() == (
        "cloud",
        "emb-organization",
        None,
    )
    assert organization.recovery_intent == "rebuild"


@pytest.mark.parametrize("old_scope", ["platform", "organization"])
@pytest.mark.parametrize("new_scope", ["platform", "organization"])
@pytest.mark.parametrize(
    ("new_identity", "expected_recovery"),
    [("emb-old", None), ("emb-new", "rebuild")],
)
def test_pairing_a_different_instance_preserves_the_cloud_identity_baseline(
    old_scope: str,
    new_scope: str,
    new_identity: str,
    expected_recovery: str | None,
) -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope=old_scope,
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-old",
            applied_embedding_identity="emb-old",
            model_access_key="mak_old",
            proxy_base_url="https://old.example.test/v1/model",
            source_instance_id="instance-old",
            organization_attached=old_scope == "organization",
        ),
    )

    repaired = _resolved(
        current,
        _status(scope=new_scope, identity=new_identity, revision=2),
        minted=_mint("mak_new"),
    )

    assert repaired.cloud.applied_embedding_identity == new_identity
    assert repaired.recovery_intent == expected_recovery
    assert repaired.runtime_source() == "cloud"


def test_platform_mode_acknowledgement_records_the_confirmed_cloud_identity() -> None:
    memory = _manual_memory()
    memory.mode = "custom"
    memory.cloud = MemoryCloudConfig(
        scope="platform",
        capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
        embedding_identity="emb-cloud",
        applied_embedding_identity="emb-previous-cloud",
        model_access_key="mak_first",
        proxy_base_url="https://backend.example.test/v1/model",
        source_instance_id="instance-1",
    )
    current = _paired_config(memory)

    target, confirm_rebuild = ui_memory_routes._memory_settings_patch(  # noqa: SLF001
        current,
        {"mode": "platform", "confirm_rebuild": True},
    )
    candidate = ui_memory_routes._memory_candidate_config(  # noqa: SLF001
        current,
        target,
    )

    assert confirm_rebuild is True
    assert candidate.memory.cloud.applied_embedding_identity == "emb-cloud"
    assert (
        ui_memory_routes._memory_embedding_configuration_changed(  # noqa: SLF001
            current,
            candidate,
        )
        is True
    )


@pytest.mark.parametrize("dedicated_multimodal", [False, True])
def test_cloud_aliases_and_mak_flow_through_existing_everos_environment(
    dedicated_multimodal: bool,
    tmp_path: Path,
) -> None:
    memory = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(
                chat=True,
                embedding=True,
                multimodal=dedicated_multimodal,
            ),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_first",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )
    settings = memory_runtime._process_settings(memory)  # noqa: SLF001
    environment = memory_process._memory_child_environment(  # noqa: SLF001
        python=Path("/usr/bin/python3"),
        memory_dir=tmp_path / "memory",
        provider_root=tmp_path / "provider",
        attachments_root=tmp_path / "attachments",
        settings=settings,
        role=None,
    )

    assert environment["EVEROS_LLM__BASE_URL"] == ("https://backend.example.test/v1/model")
    assert environment["EVEROS_LLM__MODEL"] == "avibe-cloud-chat"
    assert environment["EVEROS_LLM__API_KEY"] == "mak_first"
    assert environment["EVEROS_EMBEDDING__MODEL"] == ("avibe-cloud-embedding-emb-v1")
    assert environment["EVEROS_EMBEDDING__API_KEY"] == "mak_first"
    assert memory.effective_multimodal_available() is True
    if dedicated_multimodal:
        assert environment["EVEROS_MULTIMODAL__BASE_URL"] == ("https://backend.example.test/v1/model/mm")
        assert environment["EVEROS_MULTIMODAL__MODEL"] == ("avibe-cloud-multimodal")
        assert MULTIMODAL_EXPLICIT_ENV in environment
    else:
        assert environment["EVEROS_MULTIMODAL__BASE_URL"] == (environment["EVEROS_LLM__BASE_URL"])
        assert environment["EVEROS_MULTIMODAL__MODEL"] == (environment["EVEROS_LLM__MODEL"])
        assert MULTIMODAL_EXPLICIT_ENV not in environment


def test_mak_rotation_does_not_change_embedding_identity_and_rolls_back_on_apply_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = MemoryConfig(
        enabled=True,
        mode="platform",
        cloud=MemoryCloudConfig(
            scope="platform",
            capabilities=MemoryCloudCapabilities(chat=True, embedding=True),
            embedding_identity="emb-v1",
            applied_embedding_identity="emb-v1",
            model_access_key="mak_old",
            proxy_base_url="https://backend.example.test/v1/model",
            source_instance_id="instance-1",
        ),
    )
    config = _paired_config()
    snapshots = [deepcopy(current)]

    monkeypatch.setattr(
        V2Config,
        "load",
        classmethod(lambda cls: _paired_config(deepcopy(snapshots[-1]))),
    )
    monkeypatch.setattr(
        model_service,
        "_device_request",
        lambda *_args: _mint(
            "mak_new",
            rotated=True,
            previous_valid_until="2026-08-17T00:05:00Z",
        ),
    )

    def persist(_expected: MemoryConfig, candidate: MemoryConfig) -> V2Config:
        snapshots.append(deepcopy(candidate))
        return _paired_config(deepcopy(candidate))

    reconcile_results = iter([False, True])
    monkeypatch.setattr(model_service, "_persist_candidate", persist)
    monkeypatch.setattr(
        model_service,
        "_reconcile_candidate",
        lambda _candidate: next(reconcile_results),
    )

    with pytest.raises(
        model_service.ModelServiceResolutionError,
        match="model_access_key_apply_failed",
    ):
        model_service.rotate_model_access_key(config)

    assert snapshots[0].runtime_embedding_identity() == (
        "cloud",
        "emb-v1",
        None,
    )
    assert snapshots[1].runtime_embedding_identity() == (
        "cloud",
        "emb-v1",
        None,
    )
    assert snapshots[1].cloud.model_access_key == "mak_new"
    assert snapshots[-1].cloud.model_access_key == "mak_old"


@pytest.mark.parametrize(
    "payload",
    [
        _mint() | {"unexpected": True},
        _mint(rotated=True, previous_valid_until=None),
        _mint(rotated=False, previous_valid_until="2026-08-17T00:05:00Z"),
        _mint() | {"created_at": "2026-08-17T00:00:00"},
    ],
)
def test_mak_response_requires_the_frozen_one_time_shape(payload: dict) -> None:
    with pytest.raises(model_service.ModelServiceResolutionError):
        model_service._mint_from_payload(payload)  # noqa: SLF001
