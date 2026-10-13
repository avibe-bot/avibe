"""The desktop mini's Vibey conversation: created on first use, then reused.

Interim rule until sessions get a built-in ``main`` type
(docs/plans/2026-10-12-desktop-mini-main-agent.md §10.1).
"""

from __future__ import annotations

import pytest

from storage.importer import ensure_sqlite_state
from tests.ui_server_test_helpers import csrf_headers
from vibe.ui_server import app


@pytest.fixture()
def state(monkeypatch, tmp_path):
    from core.vibe_agents import VibeAgentStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents([])
        vibey = store.get_builtin_default_agent_for_backend("vibey")
    finally:
        store.close()
    assert vibey is not None
    yield tmp_path, vibey


def _project(tmp_path, name: str):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    folder = tmp_path / name
    folder.mkdir()
    with create_sqlite_engine().begin() as conn:
        return create_project(conn, str(folder), display_name=name)


def _ensure(owner_key: str, agent_name: str):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import ensure_desktop_mini_session
    from vibe.authorization import instance_owner_context

    with create_sqlite_engine().begin() as conn:
        return ensure_desktop_mini_session(
            conn,
            owner_key=owner_key,
            agent_name=agent_name,
            title="Vibey",
            authorization_context=instance_owner_context(),
        )


def _touch(project_id: str, when: str) -> None:
    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import scopes

    with create_sqlite_engine().begin() as conn:
        conn.execute(update(scopes).where(scopes.c.native_id == project_id).values(last_seen_at=when))


def test_first_use_creates_a_vibey_session_in_the_most_recent_project_then_reuses_it(state):
    tmp_path, vibey = state
    older = _project(tmp_path, "older")
    recent = _project(tmp_path, "recent")
    _touch(older["id"], "2026-01-01T00:00:00Z")
    _touch(recent["id"], "2026-10-01T00:00:00Z")

    first = _ensure("local", vibey.name)
    assert first["project_id"] == recent["id"]
    assert first["agent_name"] == vibey.name
    assert first["agent_backend"] == "vibey"
    assert first["visibility"] == "foreground"

    # Recency moving later does not move the conversation.
    _touch(older["id"], "2026-12-01T00:00:00Z")
    assert _ensure("local", vibey.name)["id"] == first["id"]


def test_each_principal_gets_its_own_session(state):
    tmp_path, vibey = state
    _project(tmp_path, "p")
    mine = _ensure("local", vibey.name)
    theirs = _ensure("remote:someone", vibey.name)
    assert mine["id"] != theirs["id"]
    assert _ensure("remote:someone", vibey.name)["id"] == theirs["id"]


def test_an_archived_session_is_replaced(state):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import archive_session

    tmp_path, vibey = state
    _project(tmp_path, "p")
    first = _ensure("local", vibey.name)
    with create_sqlite_engine().begin() as conn:
        archive_session(conn, first["id"])
    second = _ensure("local", vibey.name)
    assert second["id"] != first["id"]
    assert second["status"] == "active"


def test_without_a_chat_project_there_is_no_session(state):
    from storage.workbench_sessions_service import NoChatProjectError

    _tmp_path, vibey = state
    with pytest.raises(NoChatProjectError):
        _ensure("local", vibey.name)


def test_route_returns_the_session_with_its_chat_grant(state):
    tmp_path, _vibey = state
    _project(tmp_path, "p")
    client = app.test_client()
    first = client.post("/api/desktop-mini/session", json={}, headers=csrf_headers(client))
    assert first.status_code == 200, first.get_json()
    body = first.get_json()
    assert body["agent_backend"] == "vibey"
    assert body["capabilities"] == {"can_chat": True}
    again = client.post("/api/desktop-mini/session", json={}, headers=csrf_headers(client))
    assert again.get_json()["id"] == body["id"]


def test_route_explains_a_missing_project(state):
    client = app.test_client()
    response = client.post("/api/desktop-mini/session", json={}, headers=csrf_headers(client))
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "no_chat_project"


def test_route_is_an_editor_write():
    from vibe.authorization import required_instance_role

    assert required_instance_role("POST", "/api/desktop-mini/session") == "editor"


def test_client_metadata_cannot_claim_someone_elses_conversation(state):
    """Ownership is server-owned: a session carrying a look-alike marker is never handed out."""
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import create_session

    tmp_path, vibey = state
    project = _project(tmp_path, "shared")
    with create_sqlite_engine().begin() as conn:
        planted = create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend="",
            agent_name=vibey.name,
            metadata={"desktop_mini_owner": "local", "desktop_mini_session": "local"},
        )
    assert _ensure("local", vibey.name)["id"] != planted["id"]


def test_unrelated_malformed_metadata_does_not_break_the_lookup(state):
    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from storage.workbench_sessions_service import create_session

    tmp_path, vibey = state
    project = _project(tmp_path, "p")
    with create_sqlite_engine().begin() as conn:
        junk = create_session(conn, scope_id=project["scope_id"], agent_backend="", agent_name=vibey.name)
        conn.execute(update(agent_sessions).where(agent_sessions.c.id == junk["id"]).values(metadata_json="{not json"))
    first = _ensure("local", vibey.name)
    assert _ensure("local", vibey.name)["id"] == first["id"]
