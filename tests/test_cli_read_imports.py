"""Read commands execute without loading Agent backends or the Web server.

The static-entry guard stops before dispatch. These subprocesses use real,
initialized, test-owned state so eager package initializers in a config/store
read cannot hide behind an already-imported pytest process or an early error.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = r'''
import contextlib
import io
import json
import sys
from vibe import cli

out, err = io.StringIO(), io.StringIO()
sys.argv = ["vibe", *json.loads(sys.argv[1])]
code = 0
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
forbidden = (
    "claude_agent_sdk", "mcp", "modules.agents.claude",
    "modules.agents.codex", "modules.agents.opencode", "modules.agents.vibey",
    "core.handlers.session_handler", "core.handlers.message_handler",
    "core.handlers.model_hub.service", "vibe.model_hub_runtime",
    "vibe.ui_server", "vibe.api", "vibe.ui_compat", "fastapi",
)
heavy = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + ".") for prefix in forbidden
))
print(json.dumps({"code": code, "out": out.getvalue(), "err": err.getvalue(), "heavy": heavy}))
'''


@pytest.fixture
def read_command_env(tmp_path, monkeypatch, sqlite_db_factory):
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AVIBE_", "VIBE_"))}
    for name in (
        "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
        "XDG_STATE_HOME", "AVIBE_HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    ):
        value = str(tmp_path / name.lower())
        env[name] = value
        monkeypatch.setenv(name, value)
    env["PYTHONPATH"] = str(_ROOT)
    env["AVIBE_SESSION_ID"] = "ses-cli-read"
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    from config import paths
    from config.v2_config import V2Config
    from storage.db import create_sqlite_engine
    from storage.models import agent_runs, agent_sessions, run_definitions
    from core.show_pages import ShowPageStore

    skill = Path(env["HOME"]) / ".agents" / "skills" / "cli-read"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: cli-read\ndescription: Read-command import fixture.\n---\nRead only.\n")
    V2Config.default().save(paths.get_config_path())
    sqlite_db_factory(paths.get_sqlite_state_path())
    engine = create_sqlite_engine()
    now = "2026-11-07T00:00:00Z"
    try:
        with engine.begin() as conn:
            conn.execute(agent_sessions.insert().values(
                id="ses-cli-read", agent_backend="vibey", agent_variant="vibey",
                session_anchor="ses-cli-read", native_session_id="ses-cli-read", status="active",
                metadata_json="{}", created_at=now, updated_at=now,
            ))
            conn.execute(agent_runs.insert().values(
                id="read-run", run_type="agent_run", status="queued", session_id="ses-cli-read",
                metadata_json="{}", created_at=now, updated_at=now,
            ))
            for kind in ("scheduled", "watch"):
                conn.execute(run_definitions.insert().values(
                    id=f"read-{kind}", definition_type=kind, name=f"Read {kind}",
                    session_id="ses-cli-read", enabled=1, schedule_type="cron",
                    cron="0 9 * * *", timezone="UTC", mode="forever",
                    metadata_json="{}", created_at=now, updated_at=now,
                ))
    finally:
        engine.dispose()
    store = ShowPageStore()
    try:
        store.ensure("ses-cli-read")
    finally:
        store.close()
    paths.get_show_page_dir("ses-cli-read").mkdir(parents=True)
    return env


@pytest.mark.parametrize("argv,kind", [
    (["vault", "list"], "vault_secrets"),
    (["watch", "list"], "run_definitions"),
    (["task", "list"], "run_definitions"),
    (["harness", "status"], "harness_status"),
    (["show", "status"], "show_page"),
    (["agent", "list"], "agents"),
    (["session", "queue", "list"], "session_queue"),
    (["data", "query", "--sql", "select 1"], "data_query"),
    (["skill", "list"], "skills"),
    (["runs", "list"], "agent_runs"),
])
def test_read_command_does_not_import_backends_or_servers(tmp_path, read_command_env, argv, kind):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(argv)],
        cwd=tmp_path, env=read_command_env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["code"] == 0, observed["err"]
    payload = json.loads(observed["out"])
    assert payload["ok"] is True
    assert payload["kind"] == kind
    if kind == "skills":
        assert "cli-read" in {skill["name"] for skill in payload["skills"]}
    assert observed["heavy"] == []
