"""PERMISSIONS-023: reservation and fork preflight follow the caller's own authority.

A remote Editor's ``vibe agent run`` is deferred work: the CLI resolves that
caller's signed snapshot and the reservation/fork writers apply it before any
Session row, workspace directory or native-fork metadata exists. Only the
external backend fork is out of scope here — the snapshot serializer and parser,
scope resolution, Project ACL, Agent ACL and persistence are all real.

A local caller keeps the historical Owner semantics (no context, no change), and
Personal instances keep their signed-kind Editor bypass, so the same call that an
Organization refuses is expected to succeed there.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from core.caller_context import CallerContext, caller_resource_user_context
from core.services import sessions as sessions_service
from core.services.session_fork import SessionForkError, reserve_forked_session
from core.vibe_agents import VibeAgentAccessError, VibeAgentStore
from config import paths
from storage import project_access_service, projects_service, resource_access_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, resource_access_policies
from storage.resource_access_service import (
    RESOURCE_USER_CONTEXT_METADATA_KEY,
    metadata_with_resource_user_context,
)
from storage.workbench_sessions_service import ProjectAccessDeniedError, create_session
from tests.ui_server_test_helpers import _save_config
from vibe.authorization import AuthorizationContext, InstanceAuthorizationError

EDITOR_EMAIL = "editor@example.com"


@pytest.fixture(params=["personal", "organization"])
def reservations(request, tmp_path):
    config = _save_config(tmp_path, paired=True, instance_kind=request.param)
    ensure_sqlite_state()
    store = VibeAgentStore()
    store.ensure_builtin_default_agent(backend="codex")
    agent = store.require("codex")
    engine = create_sqlite_engine()
    open_folder = tmp_path / "open"
    closed_folder = tmp_path / "closed"
    open_folder.mkdir()
    closed_folder.mkdir()
    with engine.begin() as conn:
        open_project = projects_service.create_project(conn, str(open_folder), display_name="开放项目")
        project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": open_project["id"],
                "revision": 1,
                "mode": "restricted",
                "organization_id": "org-1",
                # An email principal: the deferred caller only matches it when
                # the stored snapshot actually carries the signed email.
                "bindings": [
                    {
                        "principal_kind": "email",
                        "principal_value": EDITOR_EMAIL,
                        "access_role": "editor",
                    }
                ],
            },
        )
        closed_project = projects_service.create_project(conn, str(closed_folder), display_name="受限项目")
        project_access_service.apply_project_access_intent(
            conn,
            {
                "project_id": closed_project["id"],
                "revision": 1,
                "mode": "restricted",
                "organization_id": "foreign-org",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": "other-group",
                        "access_role": "viewer",
                    }
                ],
            },
        )
        sources = {
            name: create_session(
                conn,
                scope_id=scope,
                agent_backend=agent.backend,
                agent_name=agent.name,
                title=f"源会话 {name}",
            )
            for name, scope in (
                ("open", open_project["scope_id"]),
                ("closed", closed_project["scope_id"]),
                ("standalone", None),
            )
        }
        for session in sources.values():
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == session["id"])
                .values(native_session_id="native-test")
            )

    def usable_agent():
        """Give the Agent an ACL an Organization Editor can select."""

        with engine.begin() as conn:
            resource_access_service.ensure_resource_policy(
                conn,
                resource_kind="agent",
                resource_id=agent.id,
                owner_user_id="someone-else",
                organization_id="org-1",
                access_level="public",
                group_ids=[],
            )

    def unusable_agent():
        """Leave the Agent policy-less, which an Organization Editor cannot select."""

        with engine.begin() as conn:
            conn.execute(
                resource_access_policies.delete().where(resource_access_policies.c.resource_id == agent.id)
            )

    def authorization(role="editor", subject="editor", drop_email=False):
        context = AuthorizationContext(
            instance_role=role,
            subject=subject,
            email=f"{subject}@example.com",
            instance_id=config.remote_access.vibe_cloud.instance_id,
            instance_kind=request.param,
            instance_access_source="email",
            organization_id="org-1",
            organization_member_id="org-member",
            organization_role="member",
            is_remote=True,
        )
        snapshot = dict(
            metadata_with_resource_user_context({}, context)[RESOURCE_USER_CONTEXT_METADATA_KEY]
        )
        if drop_email:
            snapshot.pop("email", None)
        # Exactly the CLI hop: the host writes the snapshot into the caller env
        # and the command reads its own authority back out of it.
        return caller_resource_user_context(
            CallerContext(session_id="ses-caller", is_remote=True, resource_user_context=snapshot)
        )

    def session_count():
        with engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(agent_sessions)).scalar_one()

    def native_session_ids():
        with engine.connect() as conn:
            return dict(
                conn.execute(select(agent_sessions.c.id, agent_sessions.c.native_session_id)).all()
            )

    unusable_agent()
    yield SimpleNamespace(
        kind=request.param,
        agent=agent,
        open_project=open_project,
        closed_project=closed_project,
        sources=sources,
        authorization=authorization,
        usable_agent=usable_agent,
        unusable_agent=unusable_agent,
        session_count=session_count,
        native_session_ids=native_session_ids,
        tmp_path=tmp_path,
    )
    store.close()
    engine.dispose()


def _reserve(state, *, scope_key, authorization_context, workdir=None):
    return sessions_service.reserve_agent_session(
        scope_key=scope_key,
        agent_backend=state.agent.backend,
        session_anchor=f"{scope_key}:run_test",
        agent_id=state.agent.id,
        agent_name=state.agent.name,
        workdir=workdir,
        visibility="background",
        authorization_context=authorization_context,
    )


def test_permissions_023_editor_reserves_only_a_project_and_agent_it_may_use(reservations):
    """PERMISSIONS-023: a permitted placement succeeds; a forbidden one writes nothing."""
    state = reservations
    state.usable_agent()
    editor = state.authorization()

    reserved = _reserve(state, scope_key=state.open_project["scope_id"], authorization_context=editor)
    assert reserved
    before = state.session_count()

    if state.kind == "organization":
        with pytest.raises(ProjectAccessDeniedError):
            _reserve(state, scope_key=state.closed_project["scope_id"], authorization_context=editor)
    else:
        # A Personal instance has no Organization to restrict against, so its
        # signed-kind Editor bypass is the intended answer, not a gap.
        assert _reserve(state, scope_key=state.closed_project["scope_id"], authorization_context=editor)
        before += 1
    assert state.session_count() == before


def test_permissions_023_reservation_requires_agent_selection_authority(reservations):
    """PERMISSIONS-023: an Agent the caller cannot select is refused before the row."""
    state = reservations
    state.unusable_agent()
    editor = state.authorization()
    before = state.session_count()

    if state.kind == "organization":
        with pytest.raises(VibeAgentAccessError):
            _reserve(state, scope_key=state.open_project["scope_id"], authorization_context=editor)
        assert state.session_count() == before
    else:
        assert _reserve(state, scope_key=state.open_project["scope_id"], authorization_context=editor)
        assert state.session_count() == before + 1


@pytest.mark.parametrize("caller", ["viewer", "malformed", "missing_email"])
def test_permissions_023_unauthorized_callers_never_reserve(reservations, caller):
    """PERMISSIONS-023: below Editor, malformed remote and legacy snapshots fail closed."""
    state = reservations
    state.usable_agent()
    before = state.session_count()
    if caller == "viewer":
        context, expected = state.authorization(role="viewer"), InstanceAuthorizationError
    elif caller == "malformed":
        # A remote caller with unusable provenance is anonymous remote, never local.
        context, expected = {}, InstanceAuthorizationError
    else:
        # A record written before the snapshot carried the signed email cannot
        # match an email Project binding, and no email is invented for it.
        context, expected = state.authorization(drop_email=True), ProjectAccessDeniedError

    if caller == "missing_email" and state.kind == "personal":
        assert _reserve(state, scope_key=state.open_project["scope_id"], authorization_context=context)
        assert state.session_count() == before + 1
        return
    with pytest.raises(expected):
        _reserve(state, scope_key=state.open_project["scope_id"], authorization_context=context)
    assert state.session_count() == before


def test_permissions_023_standalone_reservation_keeps_runtime_management_boundary(reservations):
    """PERMISSIONS-023: a refused standalone reservation leaves no row and no workspace."""
    state = reservations
    state.usable_agent()
    workspace = state.tmp_path / "standalone-workspace"
    before = state.session_count()

    with pytest.raises(ProjectAccessDeniedError):
        sessions_service.reserve_standalone_agent_session(
            agent_backend=state.agent.backend,
            session_anchor="standalone_test",
            agent_id=state.agent.id,
            agent_name=state.agent.name,
            workdir=str(workspace),
            authorization_context=state.authorization(),
        )
    assert state.session_count() == before
    assert not workspace.exists()

    allowed = sessions_service.reserve_standalone_agent_session(
        agent_backend=state.agent.backend,
        session_anchor="standalone_test",
        agent_id=state.agent.id,
        agent_name=state.agent.name,
        workdir=str(workspace),
        authorization_context=state.authorization(role="member"),
    )
    assert allowed
    assert workspace.exists()


def test_permissions_023_local_callers_keep_owner_reservation_semantics(reservations):
    """PERMISSIONS-023: no context is still the local Owner path, unchanged."""
    state = reservations
    state.unusable_agent()
    before = state.session_count()
    assert _reserve(state, scope_key=state.closed_project["scope_id"], authorization_context=None)
    assert sessions_service.reserve_standalone_agent_session(
        agent_backend=state.agent.backend,
        session_anchor="standalone_local",
        agent_id=state.agent.id,
        agent_name=state.agent.name,
        workdir=str(state.tmp_path / "local-workspace"),
    )
    assert state.session_count() == before + 2


def test_permissions_023_fork_of_a_permitted_source_and_agent_succeeds(reservations):
    """PERMISSIONS-023: the permitted case still forks, and into a permitted Project."""
    state = reservations
    state.usable_agent()
    editor = state.authorization()
    # ``--fork-self`` resolves to the caller's own session id, so it reaches this
    # same source-authority check rather than a separate path.
    result = reserve_forked_session(
        source_session_id=state.sources["open"]["id"],
        db_path=paths.get_sqlite_state_path(),
        authorization_context=editor,
    )
    assert result.session_id
    placed = reserve_forked_session(
        source_session_id=state.sources["open"]["id"],
        scope_id=state.open_project["scope_id"],
        db_path=paths.get_sqlite_state_path(),
        authorization_context=editor,
    )
    assert placed.session_id != result.session_id


def test_permissions_023_fork_inherited_agent_needs_selection_authority(reservations):
    """PERMISSIONS-023: without an override the fork still runs as the source's Agent."""
    state = reservations
    state.unusable_agent()
    before, natives = state.session_count(), state.native_session_ids()

    if state.kind == "organization":
        with pytest.raises(SessionForkError) as denied:
            reserve_forked_session(
                source_session_id=state.sources["open"]["id"],
                db_path=paths.get_sqlite_state_path(),
                authorization_context=state.authorization(),
            )
        assert denied.value.code == "session_fork_agent_forbidden"
        assert state.session_count() == before
        assert state.native_session_ids() == natives
    else:
        assert reserve_forked_session(
            source_session_id=state.sources["open"]["id"],
            db_path=paths.get_sqlite_state_path(),
            authorization_context=state.authorization(),
        ).session_id


def test_permissions_023_fork_override_agent_needs_selection_authority(reservations):
    """PERMISSIONS-023: an override Agent is checked the same way, not instead."""
    state = reservations
    if state.kind != "organization":
        pytest.skip("a Personal instance grants Editors Agent use by signed instance kind")
    state.unusable_agent()
    before = state.session_count()
    with pytest.raises(SessionForkError) as denied:
        reserve_forked_session(
            source_session_id=state.sources["open"]["id"],
            agent_name=state.agent.name,
            db_path=paths.get_sqlite_state_path(),
            authorization_context=state.authorization(),
        )
    assert denied.value.code == "session_fork_agent_forbidden"
    assert state.session_count() == before


def test_permissions_023_fork_checks_source_and_destination_independently(reservations):
    """PERMISSIONS-023: a permitted destination cannot rescue a forbidden source."""
    state = reservations
    if state.kind != "organization":
        pytest.skip("a Personal instance Editor reaches every Project by signed instance kind")
    state.usable_agent()
    editor = state.authorization()
    before, natives = state.session_count(), state.native_session_ids()

    with pytest.raises(SessionForkError) as unreachable_source:
        reserve_forked_session(
            source_session_id=state.sources["closed"]["id"],
            scope_id=state.open_project["scope_id"],
            db_path=paths.get_sqlite_state_path(),
            authorization_context=editor,
        )
    assert "source_not_found" in str(unreachable_source.value)

    with pytest.raises(SessionForkError) as unreachable_destination:
        reserve_forked_session(
            source_session_id=state.sources["open"]["id"],
            scope_id=state.closed_project["scope_id"],
            db_path=paths.get_sqlite_state_path(),
            authorization_context=editor,
        )
    assert "destination_not_permitted" in str(unreachable_destination.value)

    with pytest.raises(SessionForkError):
        reserve_forked_session(
            source_session_id=state.sources["standalone"]["id"],
            db_path=paths.get_sqlite_state_path(),
            authorization_context=editor,
        )
    assert state.session_count() == before
    assert state.native_session_ids() == natives


@pytest.mark.parametrize("source", ["missing", "archived"])
def test_permissions_023_fork_preserves_existing_source_answers(reservations, source):
    """PERMISSIONS-023: real resource checks keep their existing meaning."""
    state = reservations
    state.usable_agent()
    engine = create_sqlite_engine()
    source_session_id = "missing-session"
    if source == "archived":
        source_session_id = state.sources["open"]["id"]
        with engine.begin() as conn:
            conn.execute(
                agent_sessions.update()
                .where(agent_sessions.c.id == source_session_id)
                .values(status="archived")
            )
    engine.dispose()
    before = state.session_count()
    for context in (state.authorization(), None):
        with pytest.raises(SessionForkError) as denied:
            reserve_forked_session(
                source_session_id=source_session_id,
                db_path=paths.get_sqlite_state_path(),
                authorization_context=context,
            )
        if context is None:
            # The local Owner path reaches the real resource checks, so their
            # existing wording is what a caller still gets.
            assert source_session_id in str(denied.value)
    assert state.session_count() == before
