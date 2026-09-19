"""One-time gateway runtime default upgrade, without native custody changes."""

from __future__ import annotations

import asyncio
import copy
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from config import v2_config
from config.v2_config import MODEL_HUB_BACKENDS, ModelHubConfig, V2Config, update_config_fields


def legacy_payload(tmp_path, *, enabled=False):
    fixture_path = tmp_path / "fixture-default.json"
    V2Config.default().save(config_path=fixture_path)
    payload = read_config(fixture_path)
    payload["model_hub"].pop("runtime_default_applied")
    payload["model_hub"]["enabled"] = enabled
    for backend in MODEL_HUB_BACKENDS:
        payload["model_hub"]["agents"][backend]["mode"] = "direct"
    # Synthetic state proves the upgrade does not clear credentials or change
    # launch auth mode. No native credential stores are ever opened.
    payload["agents"]["claude"].update(auth_mode="api_key", api_key="fixture-key", base_url="https://relay.invalid")
    payload["agents"]["codex"].update(auth_mode="oauth", api_key=None)
    payload["runtime"]["default_cwd"] = "/fixture/旧工作区"
    payload["unrelated_sentinel"] = {"keep": "配置"}
    return payload


def write_config(tmp_path, payload):
    path = tmp_path / "升级目录" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, path.read_bytes()


def read_config(path):
    return json.loads(path.read_text(encoding="utf-8"))


def assert_no_native_change(original, upgraded):
    assert upgraded["agents"] == original["agents"]
    assert upgraded["model_hub"]["agents"] == original["model_hub"]["agents"]
    assert upgraded["model_hub"]["sources"] == original["model_hub"]["sources"]
    assert {key: value for key, value in upgraded.items() if key != "model_hub"} == {
        key: value for key, value in original.items() if key != "model_hub"
    }


def test_fresh_install_stamps_default_and_keeps_hub_backend_modes(tmp_path):
    config = V2Config.default()
    assert config.model_hub.enabled is True
    assert config.model_hub.runtime_default_applied is True
    assert all(agent.mode == "hub" for agent in config.model_hub.agents.values())
    path = tmp_path / "config.json"
    config.save(config_path=path)
    original = path.read_bytes()
    loaded = V2Config.load(config_path=path)
    assert loaded.model_hub.to_payload() == config.model_hub.to_payload()
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("config.json.bak-*"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("marker", ["absent", False])
def test_old_install_promotes_once_and_backs_up_exact_snapshot(tmp_path, enabled, marker):
    original = legacy_payload(tmp_path, enabled=enabled)
    if marker is False:
        original["model_hub"]["runtime_default_applied"] = False
    path, raw = write_config(tmp_path, original)

    loaded = V2Config.load(config_path=path)

    assert loaded.load_warnings == ()
    assert loaded.model_hub.enabled is True
    assert loaded.model_hub.runtime_default_applied is True
    upgraded = read_config(path)
    assert upgraded["model_hub"]["enabled"] is True
    assert upgraded["model_hub"]["runtime_default_applied"] is True
    assert_no_native_change(original, upgraded)
    [backup] = path.parent.glob("config.json.bak-model-hub-migration-*")
    assert backup.read_bytes() == raw
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    persisted = path.read_bytes()
    assert V2Config.load(config_path=path).model_hub.to_payload() == loaded.model_hub.to_payload()
    assert path.read_bytes() == persisted
    assert len(list(path.parent.glob("config.json.bak-*"))) == 1


@pytest.mark.parametrize("already_applied", [False, True])
def test_default_upgrade_composes_with_existing_shape_migration(tmp_path, already_applied):
    payload = legacy_payload(tmp_path)
    if already_applied:
        payload["model_hub"]["runtime_default_applied"] = True
    payload["model_hub"]["priority_order"] = []
    path, original = write_config(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.load_warnings == ()
    assert loaded.model_hub.enabled is (not already_applied)
    assert loaded.model_hub.runtime_default_applied is True
    assert "priority_order" not in read_config(path)["model_hub"]
    assert read_config(path)["agents"] == payload["agents"]
    [backup] = path.parent.glob("config.json.bak-model-hub-migration-*")
    assert backup.read_bytes() == original


@pytest.mark.parametrize("shape", ["missing", "null", "empty", "missing-backend"])
def test_legacy_omissions_never_consent_to_native_takeover(tmp_path, shape):
    original = legacy_payload(tmp_path)
    if shape == "missing":
        original.pop("model_hub")
    elif shape == "null":
        original["model_hub"] = None
    elif shape == "empty":
        original["model_hub"] = {}
    else:
        original["model_hub"]["agents"].pop("codex")
        original["model_hub"]["agents"]["claude"]["mode"] = "hub"
    path, _ = write_config(tmp_path, original)

    loaded = V2Config.load(config_path=path)

    assert loaded.load_warnings == ()
    assert loaded.model_hub.enabled is True
    assert loaded.model_hub.runtime_default_applied is True
    expected = {"claude": "hub", "codex": "direct", "opencode": "direct"} if shape == "missing-backend" else {
        name: "direct" for name in MODEL_HUB_BACKENDS
    }
    assert {name: agent.mode for name, agent in loaded.model_hub.agents.items()} == expected
    assert read_config(path)["agents"] == original["agents"]


def test_runtime_stop_after_upgrade_survives_subsequent_loads(tmp_path):
    path, _ = write_config(tmp_path, legacy_payload(tmp_path))
    V2Config.load(config_path=path)
    # This is the real narrow config transaction used by the Model Hub store.
    stopped = update_config_fields(
        lambda cfg: setattr(cfg.model_hub, "enabled", False),
        config_path=path,
    )
    assert stopped.model_hub.runtime_default_applied is True
    persisted = path.read_bytes()
    for _ in range(3):
        loaded = V2Config.load(config_path=path)
        assert loaded.model_hub.enabled is False
        assert loaded.model_hub.runtime_default_applied is True
        assert all(agent.mode == "direct" for agent in loaded.model_hub.agents.values())
        assert path.read_bytes() == persisted
    assert len(list(path.parent.glob("config.json.bak-*"))) == 1


def test_service_runtime_stop_round_trips_real_store_with_fake_runtime(tmp_path, monkeypatch):
    from core.handlers.model_hub.adapter import EngineHealth, EngineStatus
    from core.handlers.model_hub.events import BoundedEventLog
    from core.handlers.model_hub.service import ModelHubService, V2ModelHubConfigStore

    payload = legacy_payload(tmp_path)
    path, _ = write_config(tmp_path, payload)
    monkeypatch.setattr(v2_config.paths, "get_config_path", lambda: path)
    stop = AsyncMock(return_value=EngineStatus(
        health=EngineHealth.NOT_STARTED,
        installed_version=None,
        verified=False,
        listen_host="127.0.0.1",
        listen_port=None,
        last_check_iso=None,
    ))
    service = ModelHubService(
        store=V2ModelHubConfigStore(),
        adapter=SimpleNamespace(stop_runtime=stop),
        events=BoundedEventLog(tmp_path / "events.json"),
    )

    runtime = asyncio.run(service.runtime_stop())

    stop.assert_awaited_once()
    assert runtime["enabled"] is False
    assert runtime["status"]["health"] == "not_started"
    persisted = path.read_bytes()
    for _ in range(3):
        loaded = V2Config.load(config_path=path)
        assert loaded.load_warnings == ()
        assert loaded.model_hub.enabled is False
        assert loaded.model_hub.runtime_default_applied is True
        assert path.read_bytes() == persisted
    assert read_config(path)["agents"] == payload["agents"]
    assert read_config(path)["model_hub"]["agents"] == payload["model_hub"]["agents"]


def test_new_explicit_stop_is_not_reinterpreted_as_old_default(tmp_path):
    config = V2Config.default()
    config.model_hub.enabled = False
    path = tmp_path / "config.json"
    config.save(config_path=path)
    raw = path.read_bytes()
    assert V2Config.load(config_path=path).model_hub.enabled is False
    assert path.read_bytes() == raw
    assert not list(tmp_path.glob("config.json.bak-*"))


def test_write_parser_does_not_perform_the_disk_upgrade():
    parsed = ModelHubConfig.from_payload({"enabled": False})
    assert parsed.enabled is False
    assert parsed.runtime_default_applied is True
    assert parsed.to_payload()["runtime_default_applied"] is True


@pytest.mark.parametrize("marker", [None, 0, 1, "true", [], {}])
def test_invalid_marker_is_strict_and_disk_recovery_does_not_activate(tmp_path, marker):
    payload = legacy_payload(tmp_path)
    payload["model_hub"]["runtime_default_applied"] = marker
    with pytest.raises(ValueError, match="runtime_default_applied"):
        ModelHubConfig.from_payload(payload["model_hub"])
    path, original = write_config(tmp_path, payload)

    loaded = V2Config.load(config_path=path)

    assert loaded.model_hub.enabled is False
    assert all(agent.mode == "direct" for agent in loaded.model_hub.agents.values())
    assert "model_hub" in loaded.recovered_sections
    assert loaded.load_warnings
    assert path.read_bytes() == original
    [backup] = path.parent.glob("config.json.bak-recovery-*")
    assert backup.read_bytes() == original


@pytest.mark.parametrize("bad_hub", [
    "not-an-object",
    {"enabled": "false"},
    {"enabled": False, "sources": "invalid"},
    {"enabled": False, "agents": {"claude": {"mode": "hub"}}},
])
def test_malformed_optional_section_is_not_upgraded_or_rewritten(tmp_path, bad_hub):
    payload = legacy_payload(tmp_path)
    payload["model_hub"] = bad_hub
    path, original = write_config(tmp_path, payload)
    loaded = V2Config.load(config_path=path)
    assert loaded.model_hub.enabled is False
    assert all(agent.mode == "direct" for agent in loaded.model_hub.agents.values())
    assert loaded.load_warnings and not loaded.whole_config_recovery
    assert path.read_bytes() == original


@pytest.mark.parametrize("raw", [b'{"mode":', b"\xff\xfe", b"[]"])
def test_whole_config_recovery_does_not_apply_fresh_install_defaults(tmp_path, raw):
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    loaded = V2Config.load(config_path=path)
    assert loaded.whole_config_recovery
    assert loaded.model_hub.enabled is False
    assert all(agent.mode == "direct" for agent in loaded.model_hub.agents.values())
    assert loaded.load_warnings
    assert path.read_bytes() == raw


@pytest.mark.parametrize("shape", ["false", "true", "missing-enabled", "missing-hub"])
def test_unrelated_recovery_defers_old_default_upgrade(tmp_path, shape):
    payload = legacy_payload(tmp_path)
    if shape == "true":
        payload["model_hub"]["enabled"] = True
    elif shape == "missing-enabled":
        payload["model_hub"].pop("enabled")
    elif shape == "missing-hub":
        payload.pop("model_hub")
    payload["runtime"] = "invalid"
    path, original = write_config(tmp_path, payload)
    loaded = V2Config.load(config_path=path)
    assert loaded.model_hub.enabled is (shape == "true")
    assert loaded.load_warnings
    assert path.read_bytes() == original


def test_read_only_preview_does_not_persist_or_create_upgrade_backup(tmp_path):
    path, original = write_config(tmp_path, legacy_payload(tmp_path))
    loaded = V2Config.load(config_path=path, persist_migrations=False)
    assert loaded.model_hub.enabled is True
    assert loaded.model_hub.runtime_default_applied is True
    assert path.read_bytes() == original
    assert not list(path.parent.glob("config.json.bak-*"))


def test_both_fields_are_written_in_one_compare_and_swap(tmp_path, monkeypatch):
    path, original = write_config(tmp_path, legacy_payload(tmp_path))
    write = v2_config._write_config_payload_if_unchanged
    calls = []

    def capture(target, upgraded, expected_raw):
        assert target.read_bytes() == original
        assert upgraded["model_hub"]["enabled"] is True
        assert upgraded["model_hub"]["runtime_default_applied"] is True
        calls.append(copy.deepcopy(upgraded))
        return write(target, upgraded, expected_raw)

    monkeypatch.setattr(v2_config, "_write_config_payload_if_unchanged", capture)
    V2Config.load(config_path=path)
    assert len(calls) == 1
    assert read_config(path) == calls[0]


@pytest.mark.parametrize("failure", ["backup", "replace"])
@pytest.mark.parametrize("shape", ["false", "missing-enabled", "missing-hub"])
def test_failed_persistence_does_not_activate_or_stamp_the_upgrade(tmp_path, monkeypatch, failure, shape):
    payload = legacy_payload(tmp_path)
    if shape == "missing-enabled":
        payload["model_hub"].pop("enabled")
    elif shape == "missing-hub":
        payload.pop("model_hub")
    path, original = write_config(tmp_path, payload)
    if failure == "backup":
        monkeypatch.setattr(v2_config, "_backup_config_file", lambda *a, **kw: None)
    else:
        monkeypatch.setattr(v2_config, "_persist_migrated_config_payload", Mock(side_effect=OSError("fixture refusal")))
    loaded = V2Config.load(config_path=path)
    assert loaded.model_hub.enabled is False
    assert loaded.model_hub.runtime_default_applied is False
    assert loaded.load_warnings
    assert path.read_bytes() == original


def test_concurrent_new_stop_wins_over_upgrade_snapshot(tmp_path, monkeypatch):
    payload = legacy_payload(tmp_path)
    path, _ = write_config(tmp_path, payload)
    winner = copy.deepcopy(payload)
    winner["model_hub"].update(enabled=False, runtime_default_applied=True)
    winner["show_duration"] = True
    persist = v2_config._persist_migrated_config_payload
    called = False

    def concurrent_save(target, expected_raw, upgraded):
        nonlocal called
        assert not called
        called = True
        target.write_text(json.dumps(winner), encoding="utf-8")
        return persist(target, expected_raw, upgraded)

    monkeypatch.setattr(v2_config, "_persist_migrated_config_payload", concurrent_save)
    loaded = V2Config.load(config_path=path)
    assert loaded.model_hub.enabled is False
    assert loaded.model_hub.runtime_default_applied is True
    assert loaded.show_duration is True
    assert loaded.load_warnings and "file changed" in " ".join(loaded.load_warnings)
    assert read_config(path) == winner
    assert not list(path.parent.glob("config.json.bak-*"))
