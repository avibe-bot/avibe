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


def test_the_migration_runs_under_the_lock_and_before_the_controller_exists() -> None:
    """The order `main()` still owns, after readiness moved out of it.

    The lock is taken first because the migration has to run under it, so the
    lock says only that a process got as far as trying. The migration then runs
    before anything is constructed against the schema it produces.

    This test used to end by pinning `mark_service_instance_started()` between
    the controller and `run()`, on the reasoning that building the controller was
    the last thing a new release could break structurally. That reasoning was
    wrong: `run()` starts the checkpoint service, the dispatch server and the IM
    runtime, any of which a new release can fail, and each of which it catches
    and returns from -- so a release that never served was being announced as
    serving. Readiness now belongs to `run()`, and the tests for it are in
    `tests/test_service_readiness.py`. What remains here is the ordering that is
    genuinely `main()`'s, asserted structurally because the failure mode is a
    later edit moving a step for a plausible-sounding reason.
    """

    tree = ast.parse(textwrap.dedent(inspect.getsource(service_main.main)))
    lines = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
        lines.setdefault(name, node.lineno)

    assert lines["acquire_service_instance_lock"] < lines["prepare_sqlite_state"]
    assert lines["prepare_sqlite_state"] < lines["Controller"]
    assert lines["Controller"] < lines["run"]
