"""Project-default Agent inheritance at workbench session creation.

A new session in a project adopts the project's default Agent (set via Project
Settings) unless the caller pins one explicitly. With no project default the
session is created agent-less so dispatch falls back to the global default
Vibe Agent — preserving the pre-existing "plain Workbench chat" behavior.
"""

from __future__ import annotations

import pytest

from core.vibe_agents import VibeAgentStore
from storage import projects_service, workbench_sessions_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.settings_service import upsert_scope


@pytest.fixture
def engine():
    # conftest's autouse fixture isolates VIBE_REMOTE_HOME per test.
    ensure_sqlite_state()
    return create_sqlite_engine()


def _project_with_default(engine, tmp_path, **default):
    folder = tmp_path / "proj"
    folder.mkdir()
    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder))
        if default:
            projects_service.update_project(conn, created["id"], **default)
    return created


def _ensure_agent(name: str, backend: str) -> None:
    store = VibeAgentStore()
    try:
        if store.get(name) is None:
            store.create(name=name, backend=backend)
    finally:
        store.close()


def test_session_inherits_project_default_when_unspecified(engine, tmp_path):
    _ensure_agent("claude", "claude")
    created = _project_with_default(
        engine,
        tmp_path,
        agent_name="claude",
        agent_variant="claude",
        model="opus",
        reasoning_effort="high",
    )
    with engine.begin() as conn:
        # The composer creates with an empty backend when the user picks nothing.
        session = workbench_sessions_service.create_session(
            conn, scope_id=created["scope_id"], agent_backend=""
        )
    assert session["agent_backend"] == "claude"
    assert session["agent_name"] == "claude"
    assert session["agent_variant"] == "claude"
    assert session["model"] == "opus"
    assert session["reasoning_effort"] == "high"


def test_explicit_backend_overrides_project_default(engine, tmp_path):
    _ensure_agent("claude", "claude")
    created = _project_with_default(
        engine,
        tmp_path,
        agent_name="claude",
        model="opus",
        reasoning_effort="high",
    )
    with engine.begin() as conn:
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=created["scope_id"],
            agent_backend="codex",
            model="gpt-5-codex",
        )
    # An explicit pick wins outright; the project default is not mixed in.
    assert session["agent_backend"] == "codex"
    assert session["model"] == "gpt-5-codex"
    assert session["reasoning_effort"] is None


def test_explicit_agent_name_derives_backend_on_create(engine, tmp_path):
    _ensure_agent("reviewer", "codex")
    created = _project_with_default(engine, tmp_path)
    with engine.begin() as conn:
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=created["scope_id"],
            agent_backend="",
            agent_name="reviewer",
        )

    assert session["agent_name"] == "reviewer"
    assert session["agent_backend"] == "codex"
    assert session["agent_variant"] == "codex"


def test_agent_name_update_derives_backend(engine, tmp_path):
    _ensure_agent("reviewer", "codex")
    created = _project_with_default(engine, tmp_path)
    with engine.begin() as conn:
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=created["scope_id"],
            agent_backend="",
        )
        updated = workbench_sessions_service.update_session(
            conn,
            session["id"],
            agent_name="reviewer",
        )

    assert updated["agent_name"] == "reviewer"
    assert updated["agent_backend"] == "codex"


def test_agent_name_update_resets_stale_variant(engine, tmp_path):
    _ensure_agent("claude-helper", "claude")
    _ensure_agent("reviewer", "codex")
    created = _project_with_default(engine, tmp_path)
    with engine.begin() as conn:
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=created["scope_id"],
            agent_name="claude-helper",
            agent_backend="claude",
            agent_variant="claude-subagent",
        )
        updated = workbench_sessions_service.update_session(
            conn,
            session["id"],
            agent_name="reviewer",
        )

    assert updated["agent_name"] == "reviewer"
    assert updated["agent_backend"] == "codex"
    assert updated["agent_variant"] == "codex"


def test_session_without_project_default_stays_agentless(engine, tmp_path):
    created = _project_with_default(engine, tmp_path)  # no default configured
    with engine.begin() as conn:
        session = workbench_sessions_service.create_session(
            conn, scope_id=created["scope_id"], agent_backend=""
        )
    # Empty backend + empty model/effort → dispatch resolves the global default.
    assert (session["agent_backend"] or "") == ""
    assert session["model"] is None
    assert session["reasoning_effort"] is None


def test_folderless_project_session_snapshots_process_cwd(engine, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_folderless",
            now="2026-06-04T05:00:00Z",
        )
        session = workbench_sessions_service.create_session(
            conn,
            scope_id=scope_id,
            agent_backend="",
        )

    assert session["workdir"] == str(tmp_path)
