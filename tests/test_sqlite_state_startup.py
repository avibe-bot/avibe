from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main as service_main
from config.v2_config import (
    AgentsConfig,
    OpenCodeConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)


def _config() -> V2Config:
    return V2Config(
        platform="slack",
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="", app_token=""),
        runtime=RuntimeConfig(default_cwd="/tmp"),
        agents=AgentsConfig(opencode=OpenCodeConfig(enabled=True, cli_path="opencode")),
    )


def test_prepare_sqlite_state_uses_config_primary_platform(monkeypatch) -> None:
    calls = []

    def fake_repair_sqlite_state(*, primary_platform: str, include_counts: bool):
        calls.append((primary_platform, include_counts))
        return SimpleNamespace(imported=True, db_path=Path("/tmp/vibe.sqlite"), backup_path=Path("/tmp/backup"))

    monkeypatch.setattr("storage.importer.repair_sqlite_state", fake_repair_sqlite_state)

    report = service_main.prepare_sqlite_state(_config())

    assert calls == [("slack", False)]
    assert report.imported is True

