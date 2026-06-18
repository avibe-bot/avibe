"""Tests for avibe project find-or-create / archive-recovery semantics.

Projects are keyed by their resolved absolute folder path. Creating or
"opening" a project for a folder we already track must reuse the existing
scope (no duplicates) and revive it if it was archived — that is how a
project is restored after archiving, without a dedicated unarchive endpoint.
"""

from __future__ import annotations

import pytest

from core.vibe_agents import VibeAgentStore
from storage import projects_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import scope_settings, scopes


@pytest.fixture
def engine():
    # conftest's autouse fixture points VIBE_REMOTE_HOME at a per-test tmp dir,
    # so this initialises and connects to an isolated SQLite state file.
    ensure_sqlite_state()
    return create_sqlite_engine()


def _ensure_agent(name: str, backend: str) -> None:
    store = VibeAgentStore()
    try:
        if store.get(name) is None:
            store.create(name=name, backend=backend)
    finally:
        store.close()


def test_create_project_is_idempotent_by_path(engine, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    resolved = str(folder.resolve())

    with engine.begin() as conn:
        first = projects_service.create_project(conn, str(folder), display_name="My Project")
    with engine.begin() as conn:
        second = projects_service.create_project(conn, str(folder), display_name="Different Name")
        projects = projects_service.list_projects(conn, include_archived=True)

    # Same scope reused, not a fresh duplicate.
    assert first["id"] == second["id"]
    assert first["scope_id"] == second["scope_id"]
    # Reuse keeps the original name; the second call's display_name is ignored.
    assert second["display_name"] == "My Project"
    # Exactly one project tracks this path.
    same_path = [p for p in projects if p["folder_path"] == resolved]
    assert len(same_path) == 1


def test_reopening_archived_path_revives_it(engine, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()

    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder), display_name="Kept Name")
        projects_service.archive_project(conn, created["id"])

    # Archived projects drop out of the default list.
    with engine.connect() as conn:
        assert all(p["id"] != created["id"] for p in projects_service.list_projects(conn))

    # Re-opening the same folder restores the same project (revived, name kept).
    with engine.begin() as conn:
        revived = projects_service.create_project(conn, str(folder), display_name="Ignored On Reuse")

    assert revived["id"] == created["id"]
    assert revived["archived"] is False
    assert revived["display_name"] == "Kept Name"
    with engine.connect() as conn:
        assert any(p["id"] == created["id"] for p in projects_service.list_projects(conn))


def test_different_paths_make_distinct_projects(engine, tmp_path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()

    with engine.begin() as conn:
        a = projects_service.create_project(conn, str(a_dir))
        b = projects_service.create_project(conn, str(b_dir))

    assert a["id"] != b["id"]
    assert a["scope_id"] != b["scope_id"]


def test_path_lookup_ignores_non_project_scopes(engine, tmp_path):
    """An IM-channel scope sharing the same workdir must never be matched."""
    folder = tmp_path / "shared"
    folder.mkdir()
    workdir = str(folder.resolve())

    # Seed a Slack channel scope pointing at the same directory.
    with engine.begin() as conn:
        ts = "2026-01-01T00:00:00Z"
        conn.execute(
            scopes.insert().values(
                id="slack::channel::C1",
                platform="slack",
                scope_type="channel",
                native_id="C1",
                parent_scope_id=None,
                display_name="chan",
                native_type="channel",
                is_private=0,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=ts,
                last_seen_at=ts,
                updated_at=ts,
            )
        )
        conn.execute(
            scope_settings.insert().values(
                scope_id="slack::channel::C1",
                enabled=1,
                role=None,
                workdir=workdir,
                agent_name=None,
                agent_backend=None,
                agent_variant=None,
                model=None,
                reasoning_effort=None,
                require_mention=None,
                settings_version=1,
                settings_json="{}",
                created_at=ts,
                updated_at=ts,
            )
        )

    with engine.begin() as conn:
        # The channel sharing the path is not a project match...
        assert projects_service._find_project_by_workdir(conn, workdir) is None
        # ...so creating a project for it mints a real avibe project scope.
        proj = projects_service.create_project(conn, workdir)

    assert proj["scope_id"].startswith("avibe::project::")


def test_archive_folderless_project_hides_it(engine):
    """A legacy project scope with no scope_settings row must still archive.

    Such projects (created before the folder was mandatory) have no settings
    row, so the old UPDATE-only archive was a no-op and they stayed visible.
    archive_project now inserts a disabled row so they drop out of the list.
    """
    ts = "2026-01-01T00:00:00Z"
    scope_id = "avibe::project::proj_folderless"
    with engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id=scope_id,
                platform="avibe",
                scope_type="project",
                native_id="proj_folderless",
                parent_scope_id=None,
                display_name="legacy",
                native_type="project",
                is_private=1,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=ts,
                last_seen_at=ts,
                updated_at=ts,
            )
        )

    # No scope_settings row → shown in the default list (treated as active).
    with engine.connect() as conn:
        assert any(p["id"] == "proj_folderless" for p in projects_service.list_projects(conn))

    with engine.begin() as conn:
        result = projects_service.archive_project(conn, "proj_folderless")
    assert result["archived"] is True

    # Now hidden by default, but still present when archived are included.
    with engine.connect() as conn:
        assert all(p["id"] != "proj_folderless" for p in projects_service.list_projects(conn))
        assert any(
            p["id"] == "proj_folderless"
            for p in projects_service.list_projects(conn, include_archived=True)
        )


def test_duplicate_path_pick_prefers_active_then_recent(engine, tmp_path):
    """Legacy duplicates for one path resolve deterministically: active first."""
    folder = tmp_path / "dup"
    folder.mkdir()
    workdir = str(folder.resolve())

    def _insert_project(scope_id: str, *, enabled: int, last_seen: str) -> None:
        with engine.begin() as conn:
            conn.execute(
                scopes.insert().values(
                    id=scope_id,
                    platform="avibe",
                    scope_type="project",
                    native_id=scope_id.split("::")[-1],
                    parent_scope_id=None,
                    display_name="dup",
                    native_type="project",
                    is_private=1,
                    supports_threads=1,
                    metadata_json="{}",
                    first_seen_at="2026-01-01T00:00:00Z",
                    last_seen_at=last_seen,
                    updated_at=last_seen,
                )
            )
            conn.execute(
                scope_settings.insert().values(
                    scope_id=scope_id,
                    enabled=enabled,
                    role=None,
                    workdir=workdir,
                    agent_name=None,
                    agent_backend=None,
                    agent_variant=None,
                    model=None,
                    reasoning_effort=None,
                    require_mention=None,
                    settings_version=1,
                    settings_json="{}",
                    created_at="2026-01-01T00:00:00Z",
                    updated_at=last_seen,
                )
            )

    # An archived row seen more recently, and an active row seen earlier.
    _insert_project("avibe::project::proj_archived", enabled=0, last_seen="2026-05-02T00:00:00Z")
    _insert_project("avibe::project::proj_active", enabled=1, last_seen="2026-05-01T00:00:00Z")

    with engine.begin() as conn:
        found = projects_service._find_project_by_workdir(conn, workdir)

    # Active wins over the more-recent archived row.
    assert found is not None
    assert found["scope_id"] == "avibe::project::proj_active"


# --- Per-project default Agent ----------------------------------------------


def test_new_project_has_no_default_agent(engine, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder))
    assert created["default_agent"] is None


def test_update_project_sets_and_reads_default_agent(engine, tmp_path):
    _ensure_agent("claude", "claude")
    folder = tmp_path / "proj"
    folder.mkdir()
    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder))
        updated = projects_service.update_project(
            conn,
            created["id"],
            agent_name="claude",
            agent_variant="claude",
            model="opus",
            reasoning_effort="high",
        )
    assert updated["default_agent"] == {
        "agent_backend": "claude",
        "agent_name": "claude",
        "agent_variant": "claude",
        "model": "opus",
        "reasoning_effort": "high",
    }
    # The default survives a fresh read and appears in the list payload too.
    with engine.connect() as conn:
        got = projects_service.get_project(conn, created["id"])
        listed = next(p for p in projects_service.list_projects(conn) if p["id"] == created["id"])
    assert got["default_agent"]["model"] == "opus"
    assert listed["default_agent"]["agent_backend"] == "claude"


def test_update_project_clears_default_agent(engine, tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder))
        projects_service.update_project(conn, created["id"], agent_name="codex", model="gpt-5-codex")
        # Sending explicit Nones clears the default back to "follow global default".
        cleared = projects_service.update_project(
            conn,
            created["id"],
            agent_name=None,
            agent_variant=None,
            model=None,
            reasoning_effort=None,
        )
    assert cleared["default_agent"] is None


def test_rename_leaves_default_agent_untouched(engine, tmp_path):
    _ensure_agent("claude", "claude")
    folder = tmp_path / "proj"
    folder.mkdir()
    with engine.begin() as conn:
        created = projects_service.create_project(conn, str(folder))
        projects_service.update_project(conn, created["id"], agent_name="claude", model="opus")
        # Omitted default-Agent fields must not be wiped by an unrelated update.
        renamed = projects_service.update_project(conn, created["id"], display_name="Renamed")
    assert renamed["display_name"] == "Renamed"
    assert renamed["default_agent"]["agent_backend"] == "claude"
    assert renamed["default_agent"]["model"] == "opus"


def test_set_default_agent_on_folderless_project_inserts_row(engine):
    """A legacy project with no scope_settings row still accepts a default Agent."""
    _ensure_agent("opencode", "opencode")
    ts = "2026-01-01T00:00:00Z"
    scope_id = "avibe::project::proj_folderless_default"
    with engine.begin() as conn:
        conn.execute(
            scopes.insert().values(
                id=scope_id,
                platform="avibe",
                scope_type="project",
                native_id="proj_folderless_default",
                parent_scope_id=None,
                display_name="legacy",
                native_type="project",
                is_private=1,
                supports_threads=1,
                metadata_json="{}",
                first_seen_at=ts,
                last_seen_at=ts,
                updated_at=ts,
            )
        )
        # No scope_settings row yet → update must INSERT one carrying the default.
        updated = projects_service.update_project(
            conn, "proj_folderless_default", agent_name="opencode", model="grok-code"
        )
    assert updated["default_agent"]["agent_backend"] == "opencode"
    assert updated["default_agent"]["model"] == "grok-code"
