"""The desktop mini's legacy and stable main-session contracts."""

from __future__ import annotations

from pathlib import Path

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


def _context(principal: str):
    from vibe.authorization import AuthorizationContext, instance_owner_context

    if principal == "local":
        return instance_owner_context()
    return AuthorizationContext(instance_role="owner", is_remote=True, subject=principal.removeprefix("remote:"))


def _ensure(principal: str, agent_name: str):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import ensure_desktop_mini_session

    with create_sqlite_engine().begin() as conn:
        return ensure_desktop_mini_session(
            conn,
            agent_name=agent_name,
            title="Vibey",
            authorization_context=_context(principal),
        )


def _ensure_main(principal: str, agent_name: str):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import ensure_main_session

    with create_sqlite_engine().begin() as conn:
        return ensure_main_session(
            conn,
            agent_name=agent_name,
            title="Vibey",
            authorization_context=_context(principal),
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


def test_main_session_is_created_reused_and_recreated_without_a_project(state):
    from sqlalchemy import delete

    from config import paths
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from storage.workbench_sessions_service import MAIN_SESSION_ID

    tmp_path, vibey = state
    first = _ensure_main("local", vibey.name)
    assert first["id"] == MAIN_SESSION_ID
    assert first["scope_id"] is None
    assert first["agent_name"] == vibey.name
    assert first["agent_backend"] == "vibey"
    assert first["metadata"]["kind"] == "main"
    assert first["workdir"] == str(paths.get_vibe_remote_dir() / "main" / "local")
    assert (tmp_path / "main" / "local").is_dir()
    assert _ensure_main("local", vibey.name)["id"] == first["id"]

    with create_sqlite_engine().begin() as conn:
        conn.execute(delete(agent_sessions).where(agent_sessions.c.id == MAIN_SESSION_ID))
    rebuilt = _ensure_main("local", vibey.name)
    assert rebuilt["id"] == MAIN_SESSION_ID
    assert rebuilt["workdir"] == first["workdir"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope_id", "avibe::project::proj_invalid"),
        ("agent_id", "agent_invalid"),
        ("agent_name", "agent_invalid"),
        ("agent_backend", "claude"),
        ("agent_variant", "claude"),
        ("session_anchor", "ses_other"),
        ("workdir", "/tmp/not-the-main-workdir"),
        ("visibility", "background"),
        ("metadata_json", '{"kind":"other"}'),
    ],
)
def test_existing_main_session_must_keep_all_built_in_invariants(state, field, value):
    from sqlalchemy import update

    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from storage.workbench_sessions_service import MainSessionInvariantError

    tmp_path, vibey = state
    first = _ensure_main("local", vibey.name)
    if field == "scope_id":
        value = _project(tmp_path, "invariant-scope")["scope_id"]
    with create_sqlite_engine().begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == first["id"])
            .values(**{field: value})
        )

    with pytest.raises(MainSessionInvariantError) as exc:
        _ensure_main("local", vibey.name)
    assert exc.value.code == "main_session_invalid"


def test_archived_main_session_is_terminal_and_cannot_be_reopened(state):
    from sqlalchemy import select, update

    from core.services import sessions as workbench_sessions_service
    from storage.db import create_sqlite_engine
    from storage.models import agent_sessions
    from storage.workbench_sessions_service import MainSessionArchivedError

    _tmp_path, vibey = state
    first = _ensure_main("local", vibey.name)
    with create_sqlite_engine().begin() as conn:
        with pytest.raises(workbench_sessions_service.ReservedSessionError):
            workbench_sessions_service.update_session(
                conn,
                first["id"],
                visibility="background",
            )

    with create_sqlite_engine().begin() as conn:
        conn.execute(
            update(agent_sessions)
            .where(agent_sessions.c.id == first["id"])
            .values(status="archived")
        )

    with pytest.raises(MainSessionArchivedError):
        _ensure_main("local", vibey.name)
    with create_sqlite_engine().connect() as conn:
        assert conn.execute(
            select(agent_sessions.c.status).where(agent_sessions.c.id == first["id"])
        ).scalar_one() == "archived"

    response = app.test_client().get("/api/sessions/main")
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "main_session_archived"

    with create_sqlite_engine().begin() as conn:
        with pytest.raises(workbench_sessions_service.ReservedSessionError):
            workbench_sessions_service.archive_session(conn, first["id"])


def test_main_session_is_owner_only(state):
    from vibe.authorization import AuthorizationContext, InstanceAuthorizationError
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import ensure_main_session

    _tmp_path, vibey = state
    with create_sqlite_engine().begin() as conn:
        with pytest.raises(InstanceAuthorizationError):
            ensure_main_session(
                conn,
                agent_name=vibey.name,
                authorization_context=AuthorizationContext(
                    instance_role="editor",
                    is_remote=True,
                    subject="member",
                ),
            )


def test_main_workdir_is_prepared_before_writer_lock(state, monkeypatch):
    from storage import workbench_sessions_service

    _tmp_path, vibey = state
    lock_acquired = False
    original_reserve = workbench_sessions_service.reserve_write_lock
    original_mkdir = Path.mkdir

    def reserve(conn):
        nonlocal lock_acquired
        original_reserve(conn)
        lock_acquired = True

    def mkdir(path, *args, **kwargs):
        assert not lock_acquired
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(workbench_sessions_service, "reserve_write_lock", reserve)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    _ensure_main("local", vibey.name)


def test_main_route_returns_the_stable_session_and_can_chat_without_a_project(state):
    from storage.workbench_sessions_service import MAIN_SESSION_ID

    client = app.test_client()
    response = client.get("/api/sessions/main")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["session"]["id"] == MAIN_SESSION_ID
    assert body["session"]["scope_id"] is None
    assert body["capabilities"] == {"can_chat": True}


def test_main_workdir_keeps_a_remote_principal_inside_avibe_home(state):
    from config import paths

    _tmp_path, vibey = state
    session = _ensure_main("remote:../outside", vibey.name)
    workdir = Path(session["workdir"])
    assert workdir.parent == paths.get_vibe_remote_dir() / "main"
    assert workdir.name == "remote_.._outside"


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


def test_owner_comes_from_the_authorization_context():
    from vibe.authorization import AuthorizationContext, instance_owner_context
    from storage.workbench_sessions_service import desktop_mini_owner_key

    assert desktop_mini_owner_key(instance_owner_context()) == "local"
    assert desktop_mini_owner_key(AuthorizationContext(instance_role="editor", is_remote=True, subject="u1")) == "remote:u1"
    # A remote caller without a subject is nobody, never the local owner.
    assert desktop_mini_owner_key(AuthorizationContext(instance_role="editor", is_remote=True)) is None


def test_a_remote_caller_without_a_subject_is_refused(state):
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import ProjectAccessDeniedError, ensure_desktop_mini_session
    from vibe.authorization import AuthorizationContext

    tmp_path, vibey = state
    _project(tmp_path, "p")
    _ensure("local", vibey.name)
    with pytest.raises(ProjectAccessDeniedError), create_sqlite_engine().begin() as conn:
        ensure_desktop_mini_session(
            conn,
            agent_name=vibey.name,
            authorization_context=AuthorizationContext(instance_role="owner", is_remote=True),
        )


def test_a_recorded_session_whose_agent_the_caller_lost_is_not_reused(state, monkeypatch):
    from core import vibe_agents

    tmp_path, vibey = state
    _project(tmp_path, "p")
    _ensure("local", vibey.name)

    def refuse(*_args, **_kwargs):
        raise vibe_agents.VibeAgentAccessError("Agent access is not permitted.")

    monkeypatch.setattr(vibe_agents, "ensure_agent_selection_access", refuse)
    # Not reused; a new one is refused by the same check instead of being handed out.
    with pytest.raises(vibe_agents.VibeAgentAccessError):
        _ensure("local", vibey.name)


def test_a_session_moved_to_another_agent_is_not_reused(state):
    from core.vibe_agents import VibeAgentStore
    from storage.db import create_sqlite_engine
    from storage.workbench_sessions_service import update_session
    from vibe.authorization import instance_owner_context

    tmp_path, vibey = state
    _project(tmp_path, "p")
    first = _ensure("local", vibey.name)
    store = VibeAgentStore()
    try:
        store.create(name="reviewer", backend="vibey")
    finally:
        store.close()
    with create_sqlite_engine().begin() as conn:
        update_session(conn, first["id"], agent_name="reviewer", authorization_context=instance_owner_context())
    second = _ensure("local", vibey.name)
    assert second["id"] != first["id"]
    assert second["agent_name"] == vibey.name
    assert _ensure("local", vibey.name)["id"] == second["id"]
