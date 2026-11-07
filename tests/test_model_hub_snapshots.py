"""Store ownership: shared readers cannot edit; runtime writers replace only their path.

The old cache-isolation test allowed mutation of a disposable whole-config copy.
These cases instead require sharing and reject nested writes, and exercise the
health writers that formerly relied on the load's implicit copy.
"""

from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import timedelta

import pytest

from config import paths
from config.v2_config import (
    ModelHubExecutorSettings,
    ModelHubHostedToolConfig,
    ModelHubImageModelChoice,
    ModelHubModelConfig,
    ModelHubSearchServiceSettings,
    ModelHubSourceStateConfig,
    ModelHubSourceUsageConfig,
    V2Config,
)
from core.handlers.model_hub.classification import ResolutionDecision
from core.handlers.model_hub.service import V2ModelHubConfigStore
from tests.test_model_hub_resolution import _config, _service, _source
from tests.test_model_hub_api import _service as _oauth_service


def test_snapshot_graph_rejects_nested_edits_and_keeps_payloads_owned():
    config = V2Config.default()
    source = _source("src_snapshot1")
    source.models[0].reasoning_efforts = ["low", "high"]
    source.usage = ModelHubSourceUsageConfig(month_spend_cents=12)
    config.model_hub = _config([source])
    config.model_hub.hosted_tools = {
        "web_search": ModelHubHostedToolConfig(
            executor_order=(source.id,), executor_order_kinds={source.id: "model"},
            services={source.id: ModelHubSearchServiceSettings(enabled=False)},
            executors={source.id: ModelHubExecutorSettings(enabled=False)},
        ),
        "image_generate": ModelHubHostedToolConfig(
            default_image=ModelHubImageModelChoice(source.id, source.models[0].id),
        ),
    }
    config.save()
    store = V2ModelHubConfigStore()
    snapshot = store.load()
    assert store.load() is snapshot

    def cannot_mutate(node):
        if is_dataclass(node):
            for field in fields(node):
                value = getattr(node, field.name)
                with pytest.raises(FrozenInstanceError):
                    setattr(node, field.name, None)
                with pytest.raises(FrozenInstanceError):
                    delattr(node, field.name)
                cannot_mutate(value)
        elif isinstance(node, dict):
            with pytest.raises(FrozenInstanceError):
                node["injected"] = None
            with pytest.raises(FrozenInstanceError):
                node.update({"injected": None})
            with pytest.raises(FrozenInstanceError):
                node.clear()
            for value in node.values():
                cannot_mutate(value)
        elif isinstance(node, (list, tuple)):
            if isinstance(node, list):
                with pytest.raises(FrozenInstanceError):
                    node.append(None)
                with pytest.raises(FrozenInstanceError):
                    node[:] = []
                with pytest.raises(FrozenInstanceError):
                    node.clear()
            for value in node:
                cannot_mutate(value)

    cannot_mutate(snapshot)
    # List value equality is significant: an unseeded Vibey supply must stay
    # absent across unrelated writes, not become an explicit empty user choice.
    assert snapshot.vibey_supply_pending
    assert "vibey" not in snapshot.to_payload()["agents"]
    payload = snapshot.to_payload()
    payload["sources"][0]["models"][0]["reasoning_efforts"].append("edited")
    assert store.load().sources[0].models[0].reasoning_efforts == ["low", "high"]
    assert store.load() is snapshot


def test_missing_file_defaults_are_read_only_and_recreation_invalidates():
    store = V2ModelHubConfigStore()
    path = paths.get_config_path()
    path.unlink(missing_ok=True)
    missing = store.load()
    with pytest.raises(FrozenInstanceError):
        missing.enabled = False
    config = V2Config.default()
    config.model_hub.enabled = False
    config.save()
    saved = store.load()
    assert saved.enabled is False
    assert missing.enabled is True
    path.unlink()
    assert store.load().enabled is True
    assert saved.enabled is False


@pytest.mark.parametrize("transition", ["cooldown", "recovered", "blocked"])
@pytest.mark.asyncio
async def test_health_writer_replaces_only_the_changed_source(tmp_path, monkeypatch, transition):
    config = V2Config.default()
    config.model_hub = _config([_source("src_changed1"), _source("src_untouched1")])
    service, _, _ = _service(tmp_path, config.model_hub)
    if transition == "recovered":
        config.model_hub.sources[0].state = ModelHubSourceStateConfig(
            status="cooldown", retry_at=(service.now() + timedelta(seconds=60)).isoformat(),
            detail_key="models.source.cooldown.server_error",
        )
    config.save()
    service.store = V2ModelHubConfigStore()
    before = service.store.load()
    source = before.sources[0]
    service.recovery.reconcile(before)
    generation = service._reserve_settlement_generation(source.id, source=source)
    original_state = source.state.to_payload()
    saved_edits = []
    save = service._save_runtime_config

    async def capture(previous, updated):
        saved_edits.append(updated)
        assert previous is before
        assert updated.sources[0] is not source
        assert updated.sources[0].models is source.models
        assert updated.sources[1] is before.sources[1]
        assert updated.agents is before.agents
        assert updated.vibey_supply_pending is before.vibey_supply_pending
        return await save(previous, updated)

    monkeypatch.setattr(service, "_save_runtime_config", capture)
    if transition == "cooldown":
        await service._write_cooldown_locked(
            before, source,
            ResolutionDecision(action="fallback", reason="server_error", cooldown_seconds=60),
            agent="claude", model_id="claude-opus-4-6",
        )
        expected = "cooldown"
    elif transition == "recovered":
        await service._record_recovery_success(
            source.id, generation, backend="claude", model_id="claude-opus-4-6",
        )
        expected = "standby"
    else:
        await service._write_source_blocker_locked(
            before, source, backend="claude", model_id="claude-opus-4-6",
            detail_key="models.source.needs_action.credential_revoked", reason="credential_revoked",
        )
        expected = "needs_action"
    assert len(saved_edits) == 1
    assert source.state.to_payload() == original_state
    after = service.store.load()
    assert after.sources[0].state.status == expected
    assert V2ModelHubConfigStore().load().sources[0].state.status == expected
    saved_edits[0].sources[0].display_name = "unsaved edit after commit"
    assert after.sources[0].display_name == "src_changed1"
    assert "vibey" not in after.to_payload()["agents"]
    with pytest.raises(FrozenInstanceError):
        after.sources[0].state.status = "active"


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.asyncio
async def test_native_reauth_commits_and_restores_without_mutating_readers(tmp_path, restore):
    service, _, adapter = _oauth_service(tmp_path)
    source = _source("src_native0001", kind="subscription", channel="native_cli", credential_ref=None)
    source.models.append(ModelHubModelConfig(id="manual-model", provenance="manual"))
    source.account_label = "fixture account"
    config = V2Config.default()
    config.model_hub = _config([source])
    config.save()
    service.store = V2ModelHubConfigStore()
    reader = service.store.load()
    before = reader.to_payload()

    async def start_reauth(source_id, vendor, *, on_irreversible_start):
        rollback = on_irreversible_start()
        current = service.store.load().sources[0]
        assert current.state.status == "needs_action"
        assert current.state.detail_key == "models.source.needs_action.oauth_expired"
        assert current.account_label is None
        assert [model.id for model in current.models] == ["manual-model"]
        assert reader.to_payload() == before
        if restore:
            rollback()
        return adapter._flow(source_id, "oaf_snapshot1")

    adapter.start_reauth = start_reauth
    await service.reauth_source(source.id, {"acknowledge_irreversible": True})
    assert reader.to_payload() == before
    current = V2ModelHubConfigStore().load()
    if restore:
        assert current.to_payload() == before
    else:
        assert current.sources[0].state.status == "needs_action"
        assert [model.id for model in current.sources[0].models] == ["manual-model"]
