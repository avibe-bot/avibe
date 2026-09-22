"""Focused compatibility checks for the removed Memory backend."""

import json
from pathlib import Path

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
