from __future__ import annotations

import ast
import inspect
import textwrap
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

    def fake_ensure_sqlite_state(*, primary_platform: str):
        calls.append(primary_platform)
        return SimpleNamespace(imported=True, db_path=Path("/tmp/vibe.sqlite"), backup_path=Path("/tmp/backup"))

    monkeypatch.setattr(service_main, "ensure_sqlite_state", fake_ensure_sqlite_state)

    report = service_main.prepare_sqlite_state(_config())

    assert calls == ["slack"]
    assert report.imported is True


def test_startup_is_reported_only_after_everything_that_can_break_it() -> None:
    """Where the mark sits is the whole meaning of the mark.

    The lock is taken first because the migration has to run under it, so the
    lock says only that a process got as far as trying. What the mark says is
    that the database migrated and the controller built -- the last things a new
    release can break structurally, and therefore the first moment "the upgrade
    worked" is a statement about anything.

    Asserted structurally, because the failure mode is a later edit moving it
    earlier for a plausible-sounding reason and nothing noticing until an
    upgrade fails and is recorded as a success.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(service_main.main)))
    lines = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        lines.setdefault(name, node.lineno)

    assert lines["acquire_service_instance_lock"] < lines["prepare_sqlite_state"]
    assert lines["prepare_sqlite_state"] < lines["mark_service_instance_started"]
    assert lines["Controller"] < lines["mark_service_instance_started"]
    assert lines["mark_service_instance_started"] < lines["run"]
