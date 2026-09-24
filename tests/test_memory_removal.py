"""Focused compatibility checks for the removed Memory backend."""

import json
from pathlib import Path

import pytest

import config.v2_config as v2_config
from config.v2_config import V2Config, config_write_transaction


def test_obsolete_memory_config_is_ignored_on_round_trip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    payload = {
        "mode": "self_host",
        "version": "v2",
        "memory": {"enabled": True, "malformed": object()},
        "runtime": {"default_cwd": str(tmp_path / "work")},
        "agents": {"opencode": {}, "claude": {}, "codex": {}, "avault": {}},
        "slack": {"bot_token": "", "app_token": ""},
    }
    config = V2Config.from_payload(payload)
    config.save(path)
    saved = json.loads(path.read_text())
    assert "memory" not in saved
    assert saved["runtime"]["default_cwd"] == str(tmp_path / "work")


def test_config_write_transaction_loads_test_owned_snapshot(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    config = V2Config.default()
    config.save(path)
    with config_write_transaction(path) as snapshot:
        assert snapshot.runtime.default_cwd == config.runtime.default_cwd

@pytest.mark.parametrize("obsolete", [True, None, "legacy", [], {"enabled": True}])
def test_json_obsolete_memory_shapes_are_ignored(obsolete, tmp_path):
    payload = {
        "mode": "self_host",
        "version": "v2",
        "memory": obsolete,
        "runtime": {"default_cwd": str(tmp_path / "work")},
        "agents": {"opencode": {}, "claude": {}, "codex": {}, "avault": {}},
        "slack": {"bot_token": "", "app_token": ""},
    }
    config = V2Config.from_payload(json.loads(json.dumps(payload)))
    assert config.runtime.default_cwd == str(tmp_path / "work")


def _old_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    V2Config.default().save(path)
    payload = json.loads(path.read_text())
    payload["memory"] = {"enabled": True, "retained": {"legacy": 1}}
    payload["legacy_delivery"] = {"format": "old"}
    path.write_text(json.dumps(payload))
    return path, payload


@pytest.mark.parametrize("minimal_hub", [False, True])
def test_retired_memory_removed_only_after_successful_startup_migration(tmp_path, monkeypatch, minimal_hub):
    """MUC-001: startup persistence removes the key without losing other keys or data."""
    path, payload = _old_config(tmp_path, monkeypatch)
    if minimal_hub:
        payload["model_hub"] = {"enabled": False, "runtime_default_applied": True}
        path.write_text(json.dumps(payload))
    data = tmp_path / "home/memory/sentinel.bin"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"\x00legacy\xff")
    V2Config.load(config_path=path)
    assert json.loads(path.read_text()) == payload
    loaded = V2Config.load(config_path=path, persist_migrations=True)
    saved = json.loads(path.read_text())
    assert not loaded.load_warnings
    assert "memory" not in saved
    assert saved["legacy_delivery"] == payload["legacy_delivery"]
    assert saved["model_hub"] == payload["model_hub"]
    assert data.read_bytes() == b"\x00legacy\xff"
    assert any(path.parent.glob("config.json.bak-model-hub-migration-*"))


def test_retired_memory_migration_does_not_overwrite_concurrent_writer(tmp_path, monkeypatch):
    """MUC-002: a competing write wins; the next startup may retry."""
    path, payload = _old_config(tmp_path, monkeypatch)
    original = v2_config._write_config_payload_if_unchanged

    def competing_write(target, replacement, expected):
        updated = json.loads(target.read_text())
        updated["legacy_delivery"]["format"] = "new"
        updated["legacy_delivery"]["revision"] = updated["legacy_delivery"].get("revision", 0) + 1
        target.write_text(json.dumps(updated))
        return original(target, replacement, expected)

    monkeypatch.setattr(v2_config, "_write_config_payload_if_unchanged", competing_write)
    V2Config.load(config_path=path, persist_migrations=True)
    saved = json.loads(path.read_text())
    assert saved["memory"] == payload["memory"]
    assert saved["legacy_delivery"]["format"] == "new"
    monkeypatch.setattr(v2_config, "_write_config_payload_if_unchanged", original)
    V2Config.load(config_path=path, persist_migrations=True)
    saved = json.loads(path.read_text())
    assert "memory" not in saved
    assert saved["legacy_delivery"]["format"] == "new"


def test_retired_memory_malformed_config_keeps_original_for_recovery(tmp_path, monkeypatch):
    """MUC-003: recovery must not silently strip the old config."""
    path, payload = _old_config(tmp_path, monkeypatch)
    payload["model_hub"] = {"sources": 42}
    path.write_text(json.dumps(payload))
    loaded = V2Config.load(config_path=path, persist_migrations=True)
    assert loaded.load_warnings
    assert json.loads(path.read_text()) == payload
