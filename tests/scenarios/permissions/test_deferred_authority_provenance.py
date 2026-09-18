"""PERMISSIONS-024/027: deferred work keeps the authority it was authorized under.

A Vault request and a dispatching Show event are both decided long after the call
that created them, and both resume an Agent turn from a row rather than from the
message that woke the daemon. So the authority has to be recorded by the producer,
at the moment it is still known, and read back by the consumer.

Three properties are checked together, because any one alone is a bug:

* the producer refuses a target the caller could not start a turn in, before the
  row that would carry the work exists,
* the row it does write keeps the trusted snapshot for its deferred consumer, and
* every outward projection of that row — request payloads, the audit trail, the
  event echo and the transcript stream — hides it.

The first and second are checked against the same consumer the controller
actually runs: a reserved Delivery is carried to
``SessionTurnManager._remote_delivery_execution_denial``, the seam that decides
whether a rebuilt turn may execute. Admission and execution have to agree while
access is unchanged, and execution alone has to refuse once it is revoked.

A local caller keeps the historical shape byte for byte: no snapshot is written
and nothing is stripped.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from core.show_session_events import ShowSessionEventError, ShowSessionEventStore
from storage import message_deliveries
from storage import vault_service as vs
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_sessions, metadata as sqlite_metadata, vault_audit, vault_requests
from storage.models import message_deliveries as message_deliveries_table
from storage.models import show_session_events as show_session_events_table
from storage.resource_access_service import (
    RESOURCE_USER_CONTEXT_METADATA_KEY,
    metadata_with_resource_user_context,
)
from storage.settings_service import upsert_scope
from storage.vault_crypto import Sealed
from vibe import cli
from vibe.authorization import AuthorizationContext, invocation_authority

ORG_EDITOR_EMAIL = "editor@example.com"
# The instance id the paired test configuration binds to. A stored snapshot is
# only honoured while it still belongs to this installation, so a context that
# will be read back through the execution guard has to carry it.
PAIRED_INSTANCE_ID = "inst_123"


def _remote(role: str = "editor", *, instance_kind: str = "personal") -> AuthorizationContext:
    return AuthorizationContext(
        instance_role=role,
        subject=f"{role}-subject",
        email=f"{role}@example.com",
        instance_id=PAIRED_INSTANCE_ID,
        instance_kind=instance_kind,
        instance_access_source="email",
        organization_id="org-1",
        organization_member_id="org-member",
        organization_role="member",
        is_remote=True,
    )


def _expected_snapshot(context: AuthorizationContext) -> dict:
    return dict(metadata_with_resource_user_context({}, context)[RESOURCE_USER_CONTEXT_METADATA_KEY])


def _org_editor() -> AuthorizationContext:
    """An Organization Editor: the one role a Project policy can actually revoke.

    Personal instances hand every Editor their instance role regardless of
    policy, so a Personal caller cannot express "reachable" and "unreachable" as
    two different sessions.
    """

    return _remote("editor", instance_kind="organization")


def _bind_project(conn, project_id: str, *, revision: int, emails: list[str]) -> None:
    from storage import project_access_service

    project_access_service.apply_project_access_intent(
        conn,
        {
            "project_id": project_id,
            "revision": revision,
            "mode": "restricted",
            "organization_id": "org-1",
            "bindings": [
                {"principal_kind": "email", "principal_value": email, "access_role": "editor"}
                for email in emails
            ],
        },
    )


@pytest.fixture
def turn_targets(tmp_path):
    """One real Agent on two real sessions: one this Editor may drive, one they may not.

    Both rows are written the way the product writes them — through
    ``create_session``, with the Agent pinned — so the Agent half of the
    preflight and of the execution guard is asked about a Session that has one.
    """

    from core.vibe_agents import VibeAgentStore
    from storage import projects_service, resource_access_service
    from storage.workbench_sessions_service import create_session
    from tests.ui_server_test_helpers import _save_config

    # A stored snapshot is re-decided against the current pairing, so the
    # execution guard only answers at all on a paired installation whose durable
    # binding exists — the interactive path bootstraps it, exactly like this.
    config = _save_config(tmp_path, paired=True, instance_kind="organization")
    ensure_sqlite_state()

    from vibe import remote_access

    assert remote_access.binding_is_ready(config)
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agent(backend="codex")
        agent = store.require("codex")
    finally:
        store.close()

    engine = create_sqlite_engine()
    folder = tmp_path / "authority-project"
    folder.mkdir()
    with engine.begin() as conn:
        # Public, so the Agent ACL admits this Editor and the Project policy is
        # the only thing separating the two targets.
        resource_access_service.ensure_resource_policy(
            conn,
            resource_kind="agent",
            resource_id=agent.id,
            owner_user_id="someone-else",
            organization_id="org-1",
            access_level="public",
            group_ids=[],
        )
        project = projects_service.create_project(conn, str(folder), display_name="授权项目")
        _bind_project(conn, project["id"], revision=1, emails=[ORG_EDITOR_EMAIL])
        reachable = create_session(
            conn,
            scope_id=project["scope_id"],
            agent_backend=agent.backend,
            agent_name=agent.name,
        )
        # No scope: a standalone session belongs to no Project, so only runtime
        # management reaches it. This is the canonical forbidden target.
        unreachable = create_session(
            conn,
            scope_id=None,
            agent_backend=agent.backend,
            agent_name=agent.name,
        )
    assert reachable["agent_id"] and unreachable["agent_id"]

    return SimpleNamespace(
        engine=engine,
        agent=agent,
        project=project,
        reachable=reachable["id"],
        unreachable=unreachable["id"],
    )


def _run_cli(monkeypatch, argv: list[str], context: AuthorizationContext | None) -> int:
    """Drive the real CLI entry point under the env the host writes for a caller."""

    from core.caller_context import CALLER_CONTEXT_ENV_NAMES, CallerContext

    for name in CALLER_CONTEXT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    if context is not None:
        env = CallerContext(
            session_id="ses_cli_show",
            is_remote=True,
            resource_user_context=_expected_snapshot(context),
        ).to_env()
        for name, value in env.items():
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", ["vibe", *argv])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    return int(exited.value.code or 0)


# --- PERMISSIONS-024: Vault requests ---------------------------------------------------------


@pytest.fixture
def vault(tmp_path):
    vs.GRANT_RUNTIME_CACHE.clear()
    engine = create_sqlite_engine(tmp_path / "vault_provenance.sqlite")
    sqlite_metadata.create_all(engine)
    return engine


def _sealed() -> Sealed:
    return Sealed(ciphertext="ct", nonce="n", wrap_meta="wm")


def _request_row(conn, request_id: str) -> dict:
    return dict(
        conn.execute(select(vault_requests).where(vault_requests.c.id == request_id)).mappings().one()
    )


def _audit_requesters(conn, request_id: str) -> list:
    return [
        json.loads(value) if value else None
        for value in conn.execute(
            select(vault_audit.c.requester).where(vault_audit.c.request_id == request_id)
        ).scalars()
    ]


@pytest.mark.parametrize("caller", ["remote", "local"])
def test_permissions_024_a_vault_request_records_the_authority_that_made_it(vault, caller):
    """PERMISSIONS-024: the request row is the carrier; a local caller's row is unchanged."""
    context = _remote() if caller == "remote" else None
    with vault.begin() as conn:
        vs.create_secret(conn, name="P_KEY", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            "P_KEY",
            requester={"session_id": "ses_req", "source": "agent-cli"},
            delivery={"session_id": "ses_req"},
            user_context=context,
        )
        stored = json.loads(_request_row(conn, request["id"])["requester"])

    if caller == "local":
        # Byte for byte the historical shape: no key added, nothing to strip.
        assert stored == {"session_id": "ses_req", "source": "agent-cli"}
        assert vs.request_authorization_snapshot(_row_for(vault, request["id"])) is None
        return

    assert stored[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)
    assert stored["session_id"] == "ses_req"
    assert vs.request_authorization_snapshot(_row_for(vault, request["id"])) == _expected_snapshot(context)


def _row_for(engine, request_id: str) -> dict:
    with engine.connect() as conn:
        return _request_row(conn, request_id)


def test_permissions_024_a_caller_cannot_forge_the_recorded_authority(vault):
    """PERMISSIONS-024: a requester-supplied snapshot is dropped, not trusted."""
    context = _remote()
    forged = {"vibe_instance_role": "owner", "sub": "attacker"}
    with vault.begin() as conn:
        vs.create_secret(conn, name="F_KEY", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            "F_KEY",
            requester={"session_id": "ses_forge", RESOURCE_USER_CONTEXT_METADATA_KEY: forged},
            user_context=context,
        )

    assert vs.request_authorization_snapshot(_row_for(vault, request["id"])) == _expected_snapshot(context)


def test_permissions_024_outward_request_views_hide_the_recorded_authority(vault):
    """PERMISSIONS-024: the snapshot is for the deferred consumer, not for any reader."""
    context = _remote()
    with vault.begin() as conn:
        vs.create_secret(conn, name="V_KEY", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            "V_KEY",
            requester={"session_id": "ses_view"},
            user_context=context,
        )
        fetched = vs.get_request(conn, request["id"])
        listed = vs.list_requests(conn)
        audited = _audit_requesters(conn, request["id"])

    for payload in (request, fetched, *[item for item in listed if item["id"] == request["id"]]):
        assert payload["requester"] == {"session_id": "ses_view"}
    # The audit trail is a non-secret summary and passes through one chokepoint,
    # so no event of any type can persist the snapshot to a log surface.
    assert audited
    for entry in audited:
        assert RESOURCE_USER_CONTEXT_METADATA_KEY not in (entry or {})
    # The row itself still holds it.
    assert vs.request_authorization_snapshot(_row_for(vault, request["id"])) == _expected_snapshot(context)


def test_permissions_024_auto_resume_runs_under_the_requesting_caller(monkeypatch, tmp_path):
    """PERMISSIONS-024: the callback turn carries the original snapshot, not the daemon's own."""
    from types import SimpleNamespace

    from core import scheduled_tasks as st

    ensure_sqlite_state()
    context = _remote()
    engine = create_sqlite_engine()
    sqlite_metadata.create_all(engine)
    with engine.begin() as conn:
        vs.create_secret(conn, name="R_KEY", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            "R_KEY",
            requester={"session_id": "ses_resume"},
            delivery={"session_id": "ses_resume"},
            user_context=context,
        )
        option = request["card"]["grant_options"][0]
        vs.create_grant(
            conn,
            member_names=option["member_snapshot"],
            source_selector=option["source_selector"],
            purpose=option["purpose"],
            request_id=request["id"],
            cache_ready=True,
        )
        row = _request_row(conn, request["id"])

    target = SimpleNamespace(
        session_key=SimpleNamespace(to_key=lambda: "avibe::scope::ses_resume"),
        agent_name="codex",
        agent_id="aid",
        agent_backend="codex",
        model=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(st, "resolve_session_id_target", lambda session_id: target)
    request_store = st.TaskExecutionStore(tmp_path / "task_requests")
    service = st.ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=st.ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    assert service._process_vault_callback_sync(row) == "sent"
    [callback] = request_store.list_pending()
    assert callback.metadata[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)
    assert callback.metadata["vault_request_type"] == "access"


def test_permissions_024_a_local_requests_auto_resume_stays_local(monkeypatch, tmp_path):
    """PERMISSIONS-024: with no recorded authority the callback metadata is unchanged."""
    from types import SimpleNamespace

    from core import scheduled_tasks as st

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    sqlite_metadata.create_all(engine)
    with engine.begin() as conn:
        vs.create_secret(conn, name="L_KEY", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            "L_KEY",
            requester={"session_id": "ses_local"},
            delivery={"session_id": "ses_local"},
        )
        option = request["card"]["grant_options"][0]
        vs.create_grant(
            conn,
            member_names=option["member_snapshot"],
            source_selector=option["source_selector"],
            purpose=option["purpose"],
            request_id=request["id"],
            cache_ready=True,
        )
        row = _request_row(conn, request["id"])

    target = SimpleNamespace(
        session_key=SimpleNamespace(to_key=lambda: "avibe::scope::ses_local"),
        agent_name="codex",
        agent_id="aid",
        agent_backend="codex",
        model=None,
        reasoning_effort=None,
    )
    monkeypatch.setattr(st, "resolve_session_id_target", lambda session_id: target)
    request_store = st.TaskExecutionStore(tmp_path / "task_requests")
    service = st.ScheduledTaskService(
        controller=SimpleNamespace(platform_settings_managers={}),
        store=st.ScheduledTaskStore(tmp_path / "scheduled_tasks.json"),
        request_store=request_store,
    )

    assert service._process_vault_callback_sync(row) == "sent"
    [callback] = request_store.list_pending()
    assert RESOURCE_USER_CONTEXT_METADATA_KEY not in callback.metadata


@pytest.mark.parametrize("request_kind", ["access", "provision"])
def test_permissions_024_a_request_cannot_name_a_session_its_caller_cannot_resume(
    monkeypatch, turn_targets, request_kind
):
    """PERMISSIONS-024: the session an approval would resume is an effect, checked first.

    The approval card is the first thing a request composes, so failing it proves
    the refusal lands before anything is composed, asked of a person, or stored —
    not merely rolled back afterwards. ``create_sign_request`` reaches the same
    seam with the same arguments; only its keypair setup differs.
    """

    def composed(*args, **kwargs):
        pytest.fail("the request was composed before its target was checked")

    monkeypatch.setattr(vs, "approval_card", composed)
    monkeypatch.setattr(vs, "_secure_input_card", composed)
    context = _org_editor()
    with turn_targets.engine.begin() as conn:
        vs.create_secret(conn, name="TARGET_KEY", sealed=_sealed(), protection="protected")
    with turn_targets.engine.connect() as conn:
        audit_before = conn.execute(select(func.count()).select_from(vault_audit)).scalar_one()

    with pytest.raises(vs.VaultSecretAccessError):
        with turn_targets.engine.begin() as conn:
            if request_kind == "access":
                vs.create_access_request(
                    conn,
                    "TARGET_KEY",
                    requester={"session_id": turn_targets.unreachable},
                    delivery={"session_id": turn_targets.unreachable},
                    user_context=context,
                )
            else:
                vs.create_provision_request(
                    conn,
                    "MISSING_KEY",
                    requester={"session_id": turn_targets.unreachable},
                    user_context=context,
                )

    with turn_targets.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(vault_requests)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(vault_audit)).scalar_one() == audit_before


@pytest.mark.parametrize("caller", ["editor", "member", "local"])
def test_permissions_024_a_reachable_target_still_records_the_request(turn_targets, caller):
    """PERMISSIONS-024: the check narrows nothing a caller could already reach.

    The Editor is bound to the Project; runtime management reaches every session
    without one; and a local caller is unchanged, target and shape both.
    """

    context = None if caller == "local" else _remote(caller, instance_kind="organization")
    session_id = turn_targets.unreachable if caller != "editor" else turn_targets.reachable
    with turn_targets.engine.begin() as conn:
        vs.create_secret(conn, name=f"OK_KEY_{caller}", sealed=_sealed(), protection="protected")
        request = vs.create_access_request(
            conn,
            f"OK_KEY_{caller}",
            requester={"session_id": session_id},
            delivery={"session_id": session_id},
            user_context=context,
        )
        stored = json.loads(_request_row(conn, request["id"])["requester"])

    assert stored["session_id"] == session_id
    if caller == "local":
        assert stored == {"session_id": session_id}
    else:
        assert stored[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)


def test_permissions_024_a_request_that_names_no_session_is_unchanged(turn_targets):
    """PERMISSIONS-024: a request resuming nothing keeps working; this is not a shape rule."""
    context = _org_editor()
    with turn_targets.engine.begin() as conn:
        vs.create_secret(conn, name="FREE_KEY", sealed=_sealed(), protection="protected")
        anonymous = vs.create_access_request(
            conn, "FREE_KEY", requester={"source": "agent-cli"}, user_context=context
        )
        # An id no session answers to resumes nothing either, so it stays as
        # useful as it has always been.
        unknown = vs.create_access_request(
            conn, "FREE_KEY", requester={"session_id": "ses_never_existed"}, user_context=context
        )

    for request in (anonymous, unknown):
        assert vs.request_authorization_snapshot(_row_for(turn_targets.engine, request["id"])) == (
            _expected_snapshot(context)
        )


# --- PERMISSIONS-027: Show events ------------------------------------------------------------


@pytest.fixture
def show_session():
    from storage import messages_service

    ensure_sqlite_state()
    engine = create_sqlite_engine()
    now = messages_service._utc_now_iso()
    with engine.begin() as conn:
        scope_id = upsert_scope(
            conn,
            platform="avibe",
            scope_type="project",
            native_id="proj_show_authority",
            now=now,
        )
        conn.execute(
            agent_sessions.insert().values(
                id="ses_show_authority",
                scope_id=scope_id,
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor_show_authority",
                native_session_id="",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    return "ses_show_authority"


def _dispatching_payload(event_id: str = "show_evt_authority") -> dict:
    return {
        "id": event_id,
        "type": "human.annotation.created",
        "annotation": {"intent": "comment", "comment": "Run this for me.", "dispatch": True},
    }


def _stored_delivery_metadata(delivery_id: str) -> dict:
    with create_sqlite_engine().connect() as conn:
        row = message_deliveries.get_delivery(conn, delivery_id)
    assert row is not None
    return message_deliveries.delivery_payload(row)["metadata"]


@pytest.mark.parametrize("caller", ["remote", "local"])
def test_permissions_027_a_dispatching_show_event_records_its_author(show_session, caller):
    """PERMISSIONS-027: the reservation carries the authority; the outward payload does not."""
    context = _remote() if caller == "remote" else None
    store = ShowSessionEventStore()
    try:
        event = store.append(
            show_session,
            _dispatching_payload(),
            reserve_dispatch=True,
            authorization_context=context,
        )
    finally:
        store.close()

    stored = _stored_delivery_metadata(event["delivery_id"])
    if caller == "local":
        assert RESOURCE_USER_CONTEXT_METADATA_KEY not in stored
    else:
        assert stored[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)
    # Every audience of the event reads the projection, not the row.
    assert RESOURCE_USER_CONTEXT_METADATA_KEY not in event["delivery"]["metadata"]


def test_permissions_027_a_non_dispatching_event_hides_the_author_from_the_transcript(show_session):
    """PERMISSIONS-027: the transcript message an event returns and streams is projected too."""
    context = _remote()
    store = ShowSessionEventStore()
    try:
        event = store.append(
            show_session,
            {
                "id": "show_evt_transcript",
                "type": "human.annotation.created",
                "annotation": {"intent": "comment", "comment": "Just a note."},
            },
            authorization_context=context,
        )
        listed = store.list(show_session)
    finally:
        store.close()

    assert RESOURCE_USER_CONTEXT_METADATA_KEY not in event["message"]["metadata"]
    for item in listed["events"]:
        message = item.get("message")
        if isinstance(message, dict) and isinstance(message.get("metadata"), dict):
            assert RESOURCE_USER_CONTEXT_METADATA_KEY not in message["metadata"]

    from storage.models import messages

    with create_sqlite_engine().connect() as conn:
        stored = json.loads(
            conn.execute(
                select(messages.c.metadata_json).where(messages.c.id == event["message_id"])
            ).scalar_one()
        )
    assert stored[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)


def test_permissions_027_the_cli_reserves_a_dispatching_event_under_its_own_caller(show_session):
    """PERMISSIONS-027: the live-UI hop cannot relabel a remote caller as this machine."""
    context = _remote()
    payload = _dispatching_payload("show_evt_cli")

    with invocation_authority(context):
        cli._reserve_dispatching_show_event(show_session, payload)

    store = ShowSessionEventStore()
    try:
        # The POST that follows reaches a separate process with no caller env, so it
        # resolves as this machine's Owner — and must replay the reservation rather
        # than write a second, differently-authorized one.
        replayed = store.append(show_session, payload, reserve_dispatch=True)
    finally:
        store.close()

    assert replayed["id"] == "show_evt_cli"
    assert replayed["delivery"], "the replay still hands the live path a delivery to dispatch"
    stored = _stored_delivery_metadata(replayed["delivery_id"])
    assert stored[RESOURCE_USER_CONTEXT_METADATA_KEY] == _expected_snapshot(context)
    assert RESOURCE_USER_CONTEXT_METADATA_KEY not in replayed["delivery"]["metadata"]


def test_permissions_027_the_cli_reserves_nothing_for_an_event_that_starts_no_turn(show_session):
    """PERMISSIONS-027: only deferred work is pre-reserved; ordinary events stay single-write."""
    quiet = {
        "id": "show_evt_quiet",
        "type": "human.annotation.created",
        "annotation": {"intent": "comment", "comment": "No turn, please."},
    }

    with invocation_authority(_remote()):
        cli._reserve_dispatching_show_event(show_session, quiet)

    with create_sqlite_engine().connect() as conn:
        assert conn.execute(select(show_session_events_table.c.id)).scalars().all() == []


# --- PERMISSIONS-027: admission, the stored row, and the execution guard ----------------------


def _reserve_dispatch(session_id: str, event_id: str, context: AuthorizationContext | None) -> dict:
    store = ShowSessionEventStore()
    try:
        return store.append(
            session_id,
            _dispatching_payload(event_id),
            reserve_dispatch=True,
            authorization_context=context,
        )
    finally:
        store.close()


def test_permissions_027_an_admitted_reservation_is_still_admitted_at_execution(turn_targets):
    """PERMISSIONS-027: the preflight answers what the execution guard will answer.

    Admission that disagreed with execution would be either an escalation or a
    turn that queues and then quietly dies, so the same Delivery is carried from
    the producer to the seam the controller consults before it runs.
    """

    from core.session_turns import SessionTurnManager

    context = _org_editor()
    event = _reserve_dispatch(turn_targets.reachable, "show_evt_chain", context)

    with turn_targets.engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, event["delivery_id"])
        assert message_deliveries.delivery_has_remote_resource_context(delivery)
        assert SessionTurnManager._remote_delivery_execution_denial(conn, delivery) is None


def test_permissions_027_revoking_access_after_the_reservation_stops_the_turn(turn_targets):
    """PERMISSIONS-027: the recorded snapshot is re-decided, not trusted as a past approval."""
    from core.session_turns import SessionTurnManager

    event = _reserve_dispatch(turn_targets.reachable, "show_evt_revoked", _org_editor())

    with turn_targets.engine.begin() as conn:
        _bind_project(
            conn,
            turn_targets.project["id"],
            revision=2,
            emails=["someone-else@example.com"],
        )

    with turn_targets.engine.connect() as conn:
        delivery = message_deliveries.get_delivery(conn, event["delivery_id"])
        assert (
            SessionTurnManager._remote_delivery_execution_denial(conn, delivery)
            == "remote_project_access_forbidden"
        )


def test_permissions_027_an_unreachable_session_is_refused_before_the_reservation(turn_targets):
    """PERMISSIONS-027: a forbidden target costs no row, no Delivery and no queued turn.

    The refusal reads as "not found", which is what chat access answers
    everywhere else: someone who cannot reach a session should not learn from the
    refusal that it exists.
    """

    with pytest.raises(ShowSessionEventError) as refused:
        _reserve_dispatch(turn_targets.unreachable, "show_evt_refused", _org_editor())
    assert refused.value.code == "session_not_found"

    with turn_targets.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(show_session_events_table)).scalar_one() == 0
        assert (
            conn.execute(
                select(func.count())
                .select_from(message_deliveries_table)
                .where(message_deliveries_table.c.session_id == turn_targets.unreachable)
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("caller", ["local", "member", "owner"])
def test_permissions_027_local_and_management_callers_keep_the_same_targets(turn_targets, caller):
    """PERMISSIONS-027: the new refusal narrows nobody who could already reach the session."""
    context = None if caller == "local" else _remote(caller, instance_kind="organization")
    event = _reserve_dispatch(turn_targets.unreachable, f"show_evt_{caller}", context)

    stored = _stored_delivery_metadata(event["delivery_id"])
    assert (RESOURCE_USER_CONTEXT_METADATA_KEY in stored) is (caller != "local")
    assert RESOURCE_USER_CONTEXT_METADATA_KEY not in event["delivery"]["metadata"]


def test_permissions_027_the_cli_fails_the_command_when_its_reservation_is_refused(
    monkeypatch, turn_targets
):
    """PERMISSIONS-027: a refused reservation ends the command instead of falling through.

    Both fall-through paths resolve as this machine's Owner, so either one would
    have turned a refusal into an escalation. Neither may run.
    """

    from vibe import ui_server

    wrote: list[str] = []
    monkeypatch.setattr(cli, "_post_show_event_to_live_ui", lambda *a, **k: wrote.append("live"))
    monkeypatch.setattr(ui_server, "record_local_show_event", lambda *a, **k: wrote.append("local"))

    exit_code = _run_cli(
        monkeypatch,
        [
            "show",
            "event",
            "--session-id",
            turn_targets.unreachable,
            "--dispatch",
            "--event-json",
            json.dumps(_dispatching_payload("show_evt_cli_refused")),
            "--json",
        ],
        _org_editor(),
    )

    assert exit_code == 1
    assert wrote == []
    with turn_targets.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(show_session_events_table)).scalar_one() == 0


def test_permissions_027_an_offline_cli_dispatch_still_runs_as_its_caller(monkeypatch, turn_targets):
    """PERMISSIONS-027: with no live UI the local fallback replays the reservation.

    The fallback resolves no caller at all, so without the pre-reservation it
    would write this turn as the host Owner. One Delivery exists afterwards, and
    it belongs to the Editor who typed the command.
    """

    from vibe import ui_server

    context = _org_editor()
    monkeypatch.setattr(cli, "_post_show_event_to_live_ui", lambda *a, **k: None)

    async def settled(event_payload):
        return ui_server._ShowEventDispatchOutcome.ACCEPTED

    monkeypatch.setattr(ui_server, "_run_show_event_dispatch", settled)

    exit_code = _run_cli(
        monkeypatch,
        [
            "show",
            "event",
            "--session-id",
            turn_targets.reachable,
            "--dispatch",
            "--event-json",
            json.dumps(_dispatching_payload("show_evt_cli_offline")),
            "--json",
        ],
        context,
    )

    assert exit_code == 0
    with turn_targets.engine.connect() as conn:
        delivery_ids = conn.execute(
            select(message_deliveries_table.c.id).where(
                message_deliveries_table.c.session_id == turn_targets.reachable
            )
        ).scalars().all()
    assert len(delivery_ids) == 1
    assert _stored_delivery_metadata(delivery_ids[0])[RESOURCE_USER_CONTEXT_METADATA_KEY] == (
        _expected_snapshot(context)
    )
