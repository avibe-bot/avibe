"""MODEL-HUB-MIGRATION-001: released catalogs load before strict validation."""

from __future__ import annotations

import copy
import json
import stat
from pathlib import Path

import pytest

from config import v2_config
from config.v2_config import ModelHubBackendModelConfig, ModelHubConfig, V2Config
from core.services.settings import default_config
from vibe import api


def _source(source_id, protocol, model_ids):
    example = json.loads(Path("docs/plans/model-hub-contracts/source.schema.json").read_text())["examples"][1]
    source = copy.deepcopy(example)
    source.update(id=source_id, protocol=protocol, credential_ref=f"opaque:{source_id}")
    source["models"] = [
        {"id": model_id, "origin": "manual", "reasoning_efforts": []}
        for model_id in model_ids
    ]
    return source


def _payload():
    payload = api.config_to_payload(default_config(), include_secrets=True, include_internal=True)
    payload["show_duration"] = True
    payload["migration_sentinel"] = {"keep": "配置"}
    payload["model_hub"]["enabled"] = True
    payload["model_hub"]["agents"]["opencode"].pop("models")
    return payload


def _write(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, path.read_bytes()


def _assert_migrated(path, original):
    loaded = V2Config.load(config_path=path, persist_migrations=True)
    assert loaded.load_warnings == ()
    assert loaded.recovered_sections == ()
    assert loaded.model_hub.enabled is True
    persisted = json.loads(path.read_text())
    # Compare disk JSON to disk JSON: API metadata also contains Python tuples.
    assert {k: v for k, v in persisted.items() if k != "model_hub"} == {
        k: v for k, v in json.loads(original).items() if k != "model_hub"
    }
    [backup] = path.parent.glob("config.json.bak-model-hub-migration-*")
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    after = path.read_bytes()
    reloaded = V2Config.load(config_path=path)
    assert reloaded.load_warnings == ()
    assert reloaded.model_hub.to_payload() == loaded.model_hub.to_payload()
    assert path.read_bytes() == after
    assert len(list(path.parent.glob("config.json.bak-*"))) == 1
    # The new artifact remains ordinary current-schema input, without a marker
    # that requires the migration helper to read it (also the pre-fix parser).
    assert ModelHubConfig.from_payload(persisted["model_hub"]).to_payload() == loaded.model_hub.to_payload()
    return loaded


@pytest.mark.parametrize("mode", ["direct", "hub"])
def test_legacy_empty_opencode_catalog_migrates_and_reloads(tmp_path, mode):
    payload = _payload()
    payload["model_hub"]["agents"]["opencode"]["mode"] = mode
    path, original = _write(tmp_path, payload)

    loaded = _assert_migrated(path, original)

    assert loaded.model_hub.agents["opencode"].models == []
    assert loaded.model_hub.agents["opencode"].mode == mode
    assert json.loads(path.read_text())["model_hub"]["agents"]["opencode"]["models"] == []


@pytest.mark.parametrize("protocol", ["anthropic", "openai_responses"])
@pytest.mark.parametrize("authority", ["route", "inventory"])
def test_legacy_selected_models_preserve_sources_routes_and_order(tmp_path, protocol, authority):
    """MODEL-HUB-MIGRATION-001: selected models retain their persisted routing evidence."""
    payload = _payload()
    hub = payload["model_hub"]
    agent = hub["agents"]["opencode"]
    model_ids = ["vendor/自定义模型", "model-second"]
    source_ids = ["src_migration01", "src_migration02"]
    source_model_ids = ["upstream-first", "upstream-second"] if authority == "route" else model_ids
    hub["sources"] = [_source(source_id, protocol, source_model_ids) for source_id in source_ids]
    agent["menu"] = {"view": "full", "checked": model_ids}
    agent["sources"] = {"order": list(reversed(source_ids))}
    agent["mode"] = "hub"
    if authority == "route":
        agent["routes"] = {
            model_id: {"hops": [{"source_id": source_id, "model_id": upstream} for source_id in source_ids]}
            for model_id, upstream in zip(model_ids, source_model_ids)
        }
        # A conflicting inventory is irrelevant to an explicit override.
        hub["sources"].append(_source("src_unrelated01", "openai_chat", model_ids))
        agent["sources"]["order"].insert(0, "src_unrelated01")
    expected_sources = [v2_config.ModelHubSourceConfig.from_payload(s).to_payload() for s in hub["sources"]]
    path, original = _write(tmp_path, payload)

    loaded = _assert_migrated(path, original)

    migrated = loaded.model_hub.agents["opencode"]
    assert [(m.id, m.native_protocol) for m in migrated.models] == [(model_id, protocol) for model_id in model_ids]
    assert migrated.menu.to_payload() == agent["menu"]
    assert migrated.sources.to_payload() == agent["sources"]
    assert {key: route.to_payload() for key, route in migrated.routes.items()} == agent["routes"]
    assert [source.to_payload() for source in loaded.model_hub.sources] == expected_sources


def test_legacy_explicit_catalog_keeps_row_metadata_and_modern_protocol(tmp_path):
    payload = _payload()
    hub = payload["model_hub"]
    agent = hub["agents"]["opencode"]
    legacy = ModelHubBackendModelConfig(
        id="legacy-row", display_name="保留模型", origin="provider", context_window=64000,
        reasoning_efforts=["low", "high"], native_protocol="anthropic",
    ).to_payload()
    legacy.pop("native_protocol")
    modern = ModelHubBackendModelConfig(id="modern-row", native_protocol="openai_responses").to_payload()
    agent["models"] = [legacy, modern]
    agent["menu"]["checked"] = ["legacy-row", "modern-row"]
    hub["sources"] = [_source("src_migration01", "anthropic", ["legacy-row"])]
    agent["sources"]["order"] = ["src_migration01"]
    path, original = _write(tmp_path, payload)

    loaded = _assert_migrated(path, original)

    assert [row.to_payload() for row in loaded.model_hub.agents["opencode"].models] == [
        {**legacy, "native_protocol": "anthropic"}, modern,
    ]


@pytest.mark.parametrize("problem", ["missing", "conflicting", "chat", "missing-route-source"])
def test_unrepresentable_legacy_selection_preserves_file_and_blocks_save(tmp_path, problem):
    payload = _payload()
    hub = payload["model_hub"]
    agent = hub["agents"]["opencode"]
    agent["menu"]["checked"] = ["model"]
    if problem != "missing":
        protocols = ["anthropic", "openai_responses"] if problem == "conflicting" else ["openai_chat"]
        hub["sources"] = [_source(f"src_migration0{i}", protocol, ["model"]) for i, protocol in enumerate(protocols)]
        agent["sources"]["order"] = [s["id"] for s in hub["sources"]]
    if problem == "missing-route-source":
        agent["routes"] = {"model": {"hops": [{"source_id": "src_missing001", "model_id": "model"}]}}
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert path.read_bytes() == original
    assert loaded.load_warnings
    assert "native_protocol" in " ".join(loaded.load_warnings)
    assert "repair" in " ".join(loaded.load_warnings).lower()
    assert loaded.show_duration is True
    assert api.client_config_payload(loaded)["config_recovery"]["required"] is True
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path=path)
    assert path.read_bytes() == original
    [backup] = path.parent.glob("config.json.bak-*")
    assert backup.read_bytes() == original


@pytest.mark.parametrize("nonempty", [False, True])
def test_modern_opencode_catalog_is_unchanged_without_backup(tmp_path, nonempty):
    payload = _payload()
    agent = payload["model_hub"]["agents"]["opencode"]
    agent["models"] = [ModelHubBackendModelConfig(id="model", native_protocol="anthropic").to_payload()] if nonempty else []
    agent["menu"]["checked"] = ["model"] if nonempty else []
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.load_warnings == ()
    assert path.read_bytes() == original
    assert list(path.parent.glob("config.json.bak-*")) == []
    assert loaded.model_hub.to_payload() == payload["model_hub"]


@pytest.mark.parametrize("retired", ["mappings", "source-policy"])
def test_legacy_opencode_migration_keeps_retired_field_validation(tmp_path, retired):
    payload = _payload()
    agent = payload["model_hub"]["agents"]["opencode"]
    if retired == "mappings":
        agent["mappings"] = []
    else:
        agent["sources"]["policy"] = "follow"
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.recovered_sections == ("model_hub",)
    assert loaded.load_warnings
    assert path.read_bytes() == original


def test_legacy_opencode_migration_respects_read_only_load(tmp_path):
    payload = _payload()
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path, persist_migrations=False)

    assert loaded.load_warnings == ()
    assert loaded.model_hub.enabled is True
    assert loaded.model_hub.agents["opencode"].models == []
    assert path.read_bytes() == original
    assert list(path.parent.glob("config.json.bak-*")) == []


def test_legacy_opencode_migration_requires_backup_before_persist(monkeypatch, tmp_path):
    payload = _payload()
    path, original = _write(tmp_path, payload)
    monkeypatch.setattr(v2_config, "_backup_config_file", lambda *args, **kwargs: None)

    loaded = V2Config.load(config_path=path, persist_migrations=True)

    assert loaded.model_hub.enabled is True
    assert "could not be backed up" in " ".join(loaded.load_warnings)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path=path)


def test_legacy_opencode_migration_does_not_replace_concurrent_save(monkeypatch, tmp_path):
    payload = _payload()
    path, original = _write(tmp_path, payload)
    winner = copy.deepcopy(payload)
    winner["model_hub"]["agents"]["opencode"]["models"] = []
    winner["show_duration"] = False
    winning_bytes = json.dumps(winner).encode()
    write = v2_config._write_config_payload_if_unchanged

    def concurrent_write(config_path, migrated_payload, expected_raw):
        config_path.write_bytes(winning_bytes)
        return write(config_path, migrated_payload, expected_raw)

    monkeypatch.setattr(v2_config, "_write_config_payload_if_unchanged", concurrent_write)

    loaded = V2Config.load(config_path=path, persist_migrations=True)

    assert loaded.show_duration is False
    assert "before replacement" in " ".join(loaded.load_warnings)
    assert path.read_bytes() == winning_bytes
    [backup] = path.parent.glob("config.json.bak-*")
    assert backup.read_bytes() == original


@pytest.mark.parametrize("models", [None, {}, [None], [{"id": "model", "native_protocol": None}]])
def test_explicit_malformed_catalog_is_not_reinterpreted(tmp_path, models):
    payload = _payload()
    hub = payload["model_hub"]
    agent = hub["agents"]["opencode"]
    agent["models"] = models
    agent["menu"]["checked"] = ["model"]
    hub["sources"] = [_source("src_migration01", "anthropic", ["model"])]
    agent["sources"]["order"] = ["src_migration01"]
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.recovered_sections == ("model_hub",)
    assert loaded.load_warnings
    assert path.read_bytes() == original


def test_one_ambiguous_row_prevents_partial_catalog_migration(tmp_path):
    payload = _payload()
    hub = payload["model_hub"]
    agent = hub["agents"]["opencode"]
    agent["menu"]["checked"] = ["known", "unknown"]
    hub["sources"] = [_source("src_migration01", "anthropic", ["known"])]
    agent["sources"]["order"] = ["src_migration01"]
    path, original = _write(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.load_warnings
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="recovery warnings"):
        loaded.save(config_path=path)


@pytest.mark.parametrize("catalog_present", [False, True])
def test_legacy_catalog_remains_invalid_at_the_strict_write_boundary(catalog_present):
    payload = _payload()["model_hub"]
    if catalog_present:
        payload["agents"]["opencode"]["models"] = [{"id": "model"}]
    with pytest.raises(ValueError, match="models.*required"):
        ModelHubConfig.from_payload(payload)
