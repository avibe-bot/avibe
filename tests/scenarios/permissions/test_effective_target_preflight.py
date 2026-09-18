"""PERMISSIONS-028: deferred work admits its effective target before it is written.

A Run row, a callback route and a stored Task/Watch definition are all replayed
later under the authority recorded when the row was written. Passing the command's
role floor only says the caller may create work of that kind; it does not say they
may put it in *this* Session. Without that second question the CLI wrote the row
first and discovered the refusal at dispatch, where nobody is waiting for it, and a
caller who cannot chat in a Project — or cannot use the Agent that Session runs on
— could occupy the target anyway.

These cases drive the real ``main`` dispatch with the caller env the host writes,
against a real pairing, real Projects, real Agent ACLs and the real stores. A
refusal has to leave the counts and the stored row exactly as they were; being
refused later at execution is not the property under test.

Saved automation control is deliberately outside this gate: running, pausing or
resuming a stored definition steers work that already carries its own authority
and must not restamp itself to whoever asked.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from config import paths
from config.v2_config import V2Config
from core.caller_context import CALLER_CONTEXT_ENV_NAMES, CallerContext
from core.scheduled_tasks import ScheduledTaskStore
from core.vibe_agents import VibeAgentStore
from core.watches import ManagedWatchStore
from storage import project_access_service, projects_service, resource_access_service
from storage.db import create_sqlite_engine
from storage.importer import ensure_sqlite_state
from storage.models import agent_runs, agent_sessions, resource_access_policies, scopes
from storage.sessions_service import SQLiteSessionsService
from storage.workbench_sessions_service import create_session
from vibe import cli

from tests.scenarios.permissions.test_cli_invocation_authority import (
    _INSTANCE_ID,
    _pair_instance,
    _snapshot,
)

EDITOR_EMAIL = "editor@example.com"
IM_SCOPE_KEY = "slack::channel::C123"
IM_ANCHOR = "slack_C123"
#: A well-formed legacy key with no Scope and no Session behind it yet. Each case
#: suffixes it so one case's placement never becomes the next one's existing row.
FRESH_SCOPE_KEY = "slack::channel::C_NEW"


@pytest.fixture(params=["personal", "organization"])
def targets(request, tmp_path):
    """One instance holding every kind of target a deferred command can name."""

    kind = request.param
    _pair_instance(instance_kind=kind)
    ensure_sqlite_state()

    # Legacy scope keys are an IM shape, and a task/watch target is refused on an
    # unenabled platform before any authorization runs. Enable the transport the
    # keys below name so those cases reach the gate under test; no credential is
    # configured and nothing connects.
    config = V2Config.load()
    if "slack" not in config.platforms.enabled:
        config.platforms.enabled.append("slack")
    config.save()

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
        sessions = {
            name: create_session(
                conn,
                scope_id=scope,
                agent_backend=agent.backend,
                agent_name=agent.name,
                title=f"目标会话 {name}",
            )["id"]
            for name, scope in (
                ("open", open_project["scope_id"]),
                ("closed", closed_project["scope_id"]),
                ("standalone", None),
            )
        }

    # The legacy target: an IM thread, resolved by ``(scope, anchor)`` exactly the
    # way an inbound message resolves it.
    service = SQLiteSessionsService(paths.get_sqlite_state_path())
    try:
        sessions["legacy"] = service.reserve_agent_session(
            scope_key=IM_SCOPE_KEY,
            agent_backend=agent.backend,
            session_anchor=IM_ANCHOR,
            agent_name=agent.name,
        )
    finally:
        service.close()

    def usable_agent():
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
        with engine.begin() as conn:
            conn.execute(
                resource_access_policies.delete().where(
                    resource_access_policies.c.resource_id == agent.id
                )
            )

    def counts():
        with engine.connect() as conn:
            return {
                "sessions": conn.execute(
                    select(func.count()).select_from(agent_sessions)
                ).scalar_one(),
                # A placement check has to resolve the Scope it is asked about, and
                # resolving a legacy key can create one. Counting Scopes is how a
                # refusal proves it left nothing behind.
                "scopes": conn.execute(select(func.count()).select_from(scopes)).scalar_one(),
                "runs": conn.execute(select(func.count()).select_from(agent_runs)).scalar_one(),
                "tasks": len(ScheduledTaskStore().list_tasks()),
                "watches": len(ManagedWatchStore().list_watches()),
            }

    def task_rows():
        return {item.id: asdict(item) for item in ScheduledTaskStore().list_tasks()}

    def watch_rows():
        return {item.id: asdict(item) for item in ManagedWatchStore().list_watches()}

    usable_agent()
    yield SimpleNamespace(
        kind=kind,
        agent=agent,
        sessions=sessions,
        open_project=open_project,
        closed_project=closed_project,
        usable_agent=usable_agent,
        unusable_agent=unusable_agent,
        counts=counts,
        task_rows=task_rows,
        watch_rows=watch_rows,
        tmp_path=tmp_path,
    )
    store.close()
    engine.dispose()


@pytest.fixture
def run_cli(monkeypatch, capsys):
    """Run the real entry point as a caller, and report its exit code and refusal."""

    # No waiter process runs in a hermetic test, so startup would never be
    # confirmed. Observe the definition the command actually stored instead of
    # waiting for it: the write still has to have happened for this to answer.
    monkeypatch.setattr(
        cli,
        "_wait_for_watch_startup",
        lambda store, runtime_store, watch_id, **kwargs: (
            store.get_watch(watch_id),
            runtime_store.load().get("watches", {}).get(watch_id),
        ),
    )

    def run(argv, *, role="editor", kind="organization", caller_session=None):
        for name in CALLER_CONTEXT_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        if role is not None:
            env = CallerContext(
                session_id=caller_session or "ses_cli_caller",
                is_remote=True,
                resource_user_context=_snapshot(
                    role, instance_kind=kind, instance_id=_INSTANCE_ID
                ),
            ).to_env()
            for name, value in env.items():
                monkeypatch.setenv(name, value)
        monkeypatch.setattr(sys, "argv", ["vibe", *argv])
        capsys.readouterr()
        with pytest.raises(SystemExit) as exited:
            cli.main()
        captured = capsys.readouterr()
        payload = None
        if captured.err.strip():
            try:
                payload = json.loads(captured.err.strip())
            except json.JSONDecodeError:
                payload = None
        return int(exited.value.code or 0), payload

    return run


def _editor_email_snapshot_matches_open_project() -> bool:
    """The signed Editor snapshot carries the email the open Project binds."""

    return _snapshot("editor")["email"] == EDITOR_EMAIL


def _message_task_argv(session_flag, value, *, name="t"):
    return [
        "task",
        "add",
        "--name",
        name,
        session_flag,
        value,
        "--cron",
        "0 9 * * *",
        "--message",
        "hello",
    ]


def _watch_argv(session_flag, value, *, name="w"):
    return [
        "watch",
        "add",
        "--name",
        name,
        session_flag,
        value,
        "--message",
        "hello",
        "--",
        "true",
    ]


def test_permissions_028_the_open_project_binds_the_signed_editor():
    """The fixture's positive control is the caller the Project actually names."""

    assert _editor_email_snapshot_matches_open_project()


@pytest.mark.parametrize(
    "command",
    ["agent_run", "hook_send", "task_add", "watch_add"],
)
def test_permissions_028_every_producer_refuses_an_unreachable_target(targets, run_cli, command):
    """PERMISSIONS-028: no producer writes work into a Session its caller cannot drive."""

    state = targets
    standalone = state.sessions["standalone"]
    before = state.counts()

    argv = {
        "agent_run": ["agent", "run", "--session-id", standalone, "--message", "hi", "--async", "--no-callback"],
        "hook_send": ["hook", "send", "--session-id", standalone, "--message", "hi"],
        "task_add": _message_task_argv("--session-id", standalone),
        "watch_add": _watch_argv("--session-id", standalone),
    }[command]

    code, payload = run_cli(argv, kind=state.kind)

    # A standalone Session belongs to no Project, so it is runtime-management
    # work on either instance kind -- the one negative that does not depend on
    # a Personal instance's signed-kind Editor bypass.
    assert code == 1
    assert payload["code"] == "session_not_found"
    assert state.counts() == before


def test_permissions_028_a_legacy_key_names_the_same_session(targets, run_cli):
    """PERMISSIONS-028: the deprecated scope key resolves to the row it would speak in."""

    state = targets
    before = state.counts()

    code, payload = run_cli(
        ["hook", "send", "--session-key", IM_SCOPE_KEY, "--message", "hi"],
        kind=state.kind,
    )
    assert code == 1
    assert payload["code"] == "session_not_found"
    assert payload["details"]["session_id"] == state.sessions["legacy"]
    assert state.counts() == before

    # A key naming a thread that does not exist yet is a creation, not a
    # refusal: it is admitted as a placement instead (see below), so a local
    # caller opening new IM work is unaffected by the gate.
    fresh = run_cli(
        ["hook", "send", "--session-key", "slack::channel::C999", "--message", "hi"],
        role=None,
    )
    assert fresh[0] == 0


def test_permissions_028_the_callers_own_session_is_an_effective_target(targets, run_cli):
    """PERMISSIONS-028: a target Avibe defaulted from the caller env is still checked."""

    state = targets
    before = state.counts()

    # ``task add`` with no target means "here", so the Session comes from the
    # caller environment rather than the argument -- and is just as much an
    # effective target as one that was typed.
    code, payload = run_cli(
        ["task", "add", "--name", "t", "--cron", "0 9 * * *", "--message", "hello"],
        kind=state.kind,
        caller_session=state.sessions["standalone"],
    )
    assert code == 1
    assert payload["code"] == "session_not_found"
    assert state.counts() == before


def test_permissions_028_a_callback_is_admitted_before_the_run_is_reserved(targets, run_cli):
    """PERMISSIONS-028: the callback route is recorded on the row, so it is checked first."""

    state = targets
    before = state.counts()

    code, payload = run_cli(
        [
            "agent",
            "run",
            "--agent",
            state.agent.name,
            "--message",
            "hi",
            "--async",
            "--callback-session-id",
            state.sessions["standalone"],
        ],
        kind=state.kind,
    )
    assert code == 1
    assert payload["code"] == "session_not_found"
    # Nothing reserved: the refusal landed before the new Session, not after it.
    assert state.counts() == before


def test_permissions_028_a_forbidden_project_target_is_refused(targets, run_cli):
    """PERMISSIONS-028: Project chat access decides, with the Personal bypass intact."""

    state = targets
    before = state.counts()
    code, payload = run_cli(
        _message_task_argv("--session-id", state.sessions["closed"]),
        kind=state.kind,
    )

    if state.kind == "organization":
        assert code == 1
        assert payload["code"] == "session_not_found"
        assert state.counts() == before
    else:
        # A Personal instance has no Organization to restrict against; its
        # Editor bypass is the intended answer here, not a gap.
        assert code == 0
        assert state.counts()["tasks"] == before["tasks"] + 1


def test_permissions_028_an_unusable_agent_refuses_the_turn(targets, run_cli):
    """PERMISSIONS-028: the Session's own Agent is the one the caller must be able to use."""

    state = targets
    state.unusable_agent()
    before = state.counts()

    code, payload = run_cli(
        ["agent", "run", "--session-id", state.sessions["open"], "--message", "hi", "--async", "--no-callback"],
        kind=state.kind,
    )

    if state.kind == "organization":
        assert code == 1
        assert payload["code"] == "agent_access_forbidden"
        assert state.counts() == before
    else:
        assert code == 0


def test_permissions_028_a_permitted_editor_still_queues_the_turn(targets, run_cli):
    """PERMISSIONS-028: the gate refuses what a caller may not reach, nothing more."""

    state = targets
    before = state.counts()

    code, payload = run_cli(
        ["agent", "run", "--session-id", state.sessions["open"], "--message", "hi", "--async", "--no-callback"],
        kind=state.kind,
    )
    assert code == 0, payload
    assert state.counts()["runs"] == before["runs"] + 1

    tasks_before = state.counts()
    assert (
        run_cli(_message_task_argv("--session-id", state.sessions["open"]), kind=state.kind)[0] == 0
    )
    assert state.counts()["tasks"] == tasks_before["tasks"] + 1


@pytest.mark.parametrize("role", ["member", "owner", None])
def test_permissions_028_runtime_management_and_local_callers_are_unchanged(targets, run_cli, role):
    """PERMISSIONS-028: Member, Owner and local callers keep the reach they had."""

    state = targets
    before = state.counts()

    code, payload = run_cli(
        ["agent", "run", "--session-id", state.sessions["standalone"], "--message", "hi", "--async", "--no-callback"],
        role=role,
        kind=state.kind,
    )
    assert code == 0, payload
    assert state.counts()["runs"] == before["runs"] + 1


def test_permissions_028_an_update_reauthorizes_its_effective_target(targets, run_cli):
    """PERMISSIONS-028: retargeting rewrites the definition under the invoker."""

    state = targets
    assert run_cli(_message_task_argv("--session-id", state.sessions["open"], name="keep"), kind=state.kind)[0] == 0
    rows = state.task_rows()
    task_id = next(iter(rows))
    before = state.counts()

    code, payload = run_cli(
        ["task", "update", task_id, "--session-id", state.sessions["standalone"]],
        kind=state.kind,
    )
    assert code == 1
    assert payload["code"] == "session_not_found"
    # The whole stored definition, not just its target: a refused edit writes nothing.
    assert state.task_rows() == rows
    assert state.counts() == before


def test_permissions_028_an_inherited_update_target_is_checked_too(targets, run_cli):
    """PERMISSIONS-028: an edit that never names the Session still speaks in it."""

    state = targets
    seeded, seeded_payload = run_cli(_watch_argv("--session-id", state.sessions["standalone"]), role=None)
    assert seeded == 0, seeded_payload
    rows = state.watch_rows()
    watch_id = next(iter(rows))

    code, payload = run_cli(["watch", "update", watch_id, "--name", "renamed"], kind=state.kind)
    assert code == 1
    assert payload["code"] == "session_not_found"
    assert state.watch_rows() == rows


@pytest.mark.parametrize("kind_of_work", ["task", "watch"])
@pytest.mark.parametrize("placement", ["--create-session", "--create-session-per-run"])
def test_permissions_028_a_permitted_destination_still_takes_the_work(
    targets, run_cli, kind_of_work, placement
):
    """PERMISSIONS-028: the placement gate refuses a destination, not the shape of one."""

    state = targets
    argv = (
        ["task", "add", "--name", "p", "--cron", "0 9 * * *", "--message", "hello"]
        if kind_of_work == "task"
        else ["watch", "add", "--name", "p", "--message", "hello", "--", "true"]
    )
    code, payload = run_cli(
        [*argv, placement, "--scope-id", state.open_project["scope_id"]],
        kind=state.kind,
        # A real Session for the caller env: ``watch add`` reads it for delivery,
        # and an invented id fails there long before the gate under test.
        caller_session=state.sessions["open"],
    )
    assert code == 0, payload
    rows = state.task_rows() if kind_of_work == "task" else state.watch_rows()
    assert len(rows) == 1


@pytest.mark.parametrize("role", ["editor", "member", None])
def test_permissions_028_per_run_work_with_no_scope_is_a_stream_of_standalone_sessions(
    targets, run_cli, role
):
    """PERMISSIONS-028: creating unplaced Sessions repeatedly needs the reach to create one.

    A ``create_per_run`` definition with no Scope reserves a standalone Session on
    every fire, which is the reach an Editor is refused when they name one directly.
    Deciding it at creation is the same answer, delivered where the caller is -- and
    on both instance kinds, because an unplaced Session is runtime-management work
    that no Personal Project bypass applies to.
    """

    state = targets
    before = state.counts()
    code, payload = run_cli(
        ["task", "add", "--name", "p", "--cron", "0 9 * * *", "--message", "hello", "--create-session-per-run"],
        role=role,
        kind=state.kind,
    )

    if role == "editor":
        assert code == 1
        assert payload["code"] == "project_access_denied"
        assert state.counts() == before
    else:
        assert code == 0, payload
        assert state.counts()["tasks"] == before["tasks"] + 1


@pytest.mark.parametrize("kind_of_work", ["task", "watch"])
def test_permissions_028_a_replacement_target_is_the_one_that_is_admitted(
    targets, run_cli, kind_of_work
):
    """PERMISSIONS-028: an edit that replaces the Session is judged on where it lands.

    The definition will never speak in the old row again, so holding the caller to
    it would refuse an Editor for a Session their own edit is removing. What has to
    be admitted is the destination they chose -- and it is, by the reservation
    writer, before the replacement Session exists.
    """

    state = targets
    seeded = (
        _message_task_argv("--session-id", state.sessions["standalone"], name="move")
        if kind_of_work == "task"
        else _watch_argv("--session-id", state.sessions["standalone"], name="move")
    )
    assert run_cli(seeded, role=None)[0] == 0
    rows = state.task_rows() if kind_of_work == "task" else state.watch_rows()
    definition_id = next(iter(rows))

    code, payload = run_cli(
        [
            kind_of_work,
            "update",
            definition_id,
            "--create-session",
            "--scope-id",
            state.open_project["scope_id"],
        ],
        kind=state.kind,
    )
    assert code == 0, payload

    after = (state.task_rows() if kind_of_work == "task" else state.watch_rows())[definition_id]
    assert after["session_policy"] == "create_once"
    assert after["session_id"] not in (None, "", state.sessions["standalone"])


@pytest.mark.parametrize("kind_of_work", ["task", "watch"])
@pytest.mark.parametrize("placement", ["--create-session", "--create-session-per-run"])
def test_permissions_028_a_replacement_cannot_land_where_the_caller_cannot_chat(
    targets, run_cli, kind_of_work, placement
):
    """PERMISSIONS-028: replacing the target does not buy a Project the caller lacks.

    Both placements are covered because they refuse in different places: one
    reusable Session is reserved during the edit, while one Session per run is only
    described by it -- and a description that nothing admits would hand every future
    fire a Project this caller may not chat in.
    """

    state = targets
    seeded = (
        _message_task_argv("--session-id", state.sessions["open"], name="move")
        if kind_of_work == "task"
        else _watch_argv("--session-id", state.sessions["open"], name="move")
    )
    assert run_cli(seeded, role=None)[0] == 0
    rows = state.task_rows() if kind_of_work == "task" else state.watch_rows()
    definition_id = next(iter(rows))
    before = state.counts()

    code, payload = run_cli(
        [
            kind_of_work,
            "update",
            definition_id,
            placement,
            "--scope-id",
            state.closed_project["scope_id"],
        ],
        kind=state.kind,
    )

    if state.kind == "organization":
        assert code == 1
        assert payload["code"] == "project_access_denied"
        # Neither the replacement Session nor the edited definition was written.
        assert state.counts() == before
        assert (state.task_rows() if kind_of_work == "task" else state.watch_rows()) == rows
    else:
        # Same Personal-instance bypass as every other Project negative here.
        assert code == 0, payload


@pytest.mark.parametrize("command", ["hook_send", "task_add", "watch_add"])
def test_permissions_028_a_fresh_legacy_target_is_a_placement_not_a_blank(
    targets, run_cli, command
):
    """PERMISSIONS-028: a key whose thread nobody has opened yet is still a target.

    There is no Session to admit the caller TO, but the work being written says
    where one will be created on the first dispatch -- the reservation writer's
    question, asked one step earlier. Read as "no target, no question", the same
    Editor who is refused the IM thread that exists was handed the one that does
    not, and every future fire with it.
    """

    state = targets
    key = f"{FRESH_SCOPE_KEY}_{command}"
    before = state.counts()

    argv = {
        "hook_send": ["hook", "send", "--session-key", key, "--message", "hi"],
        "task_add": _message_task_argv("--session-key", key),
        "watch_add": _watch_argv("--session-key", key),
    }[command]
    code, payload = run_cli(argv, kind=state.kind)

    # An IM scope is not a Project, so no Personal Project bypass reaches this --
    # the same answer the identical key gets once its Session exists.
    assert code == 1
    assert payload["code"] == "project_access_denied"
    # Scopes included: resolving the key to ask the question must not leave the
    # row the question was about.
    assert state.counts() == before


@pytest.mark.parametrize("role", ["member", "owner", None])
def test_permissions_028_fresh_legacy_work_still_opens_for_the_callers_that_could(
    targets, run_cli, role
):
    """PERMISSIONS-028: Member, Owner and a local caller keep opening new IM work."""

    state = targets
    before = state.counts()

    code, payload = run_cli(
        ["hook", "send", "--session-key", f"{FRESH_SCOPE_KEY}_{role}", "--message", "hi"],
        role=role,
        kind=state.kind,
    )
    assert code == 0, payload
    assert state.counts()["runs"] == before["runs"] + 1


@pytest.mark.parametrize("kind_of_work", ["task", "watch"])
def test_permissions_028_an_update_onto_a_fresh_legacy_target_is_admitted_too(
    targets, run_cli, kind_of_work
):
    """PERMISSIONS-028: an edit may not repoint saved work at a Scope it cannot reach.

    The definition keeps speaking in the target this edit names, so the target is
    reauthorized -- and a Session that does not exist yet is reauthorized as the
    placement it is, not skipped for having no row.
    """

    state = targets
    seeded = (
        _message_task_argv("--session-id", state.sessions["standalone"], name="repoint")
        if kind_of_work == "task"
        else _watch_argv("--session-id", state.sessions["standalone"], name="repoint")
    )
    assert run_cli(seeded, role=None)[0] == 0
    rows = state.task_rows() if kind_of_work == "task" else state.watch_rows()
    definition_id = next(iter(rows))
    before = state.counts()

    code, payload = run_cli(
        [
            kind_of_work,
            "update",
            definition_id,
            "--session-key",
            f"{FRESH_SCOPE_KEY}_update_{kind_of_work}",
        ],
        kind=state.kind,
    )
    assert code == 1
    assert payload["code"] == "project_access_denied"
    assert (state.task_rows() if kind_of_work == "task" else state.watch_rows()) == rows
    assert state.counts() == before


def test_permissions_028_saved_control_keeps_the_definitions_own_authority(targets, run_cli):
    """PERMISSIONS-028: pausing saved work is admission, not a reauthorization."""

    state = targets
    assert run_cli(_message_task_argv("--session-id", state.sessions["standalone"]), role=None)[0] == 0
    rows = state.task_rows()
    task_id = next(iter(rows))

    # An Editor may steer a definition they could not have created: the target was
    # admitted when it was written, and control must not restamp it to the invoker.
    assert run_cli(["task", "pause", task_id], kind=state.kind)[0] == 0
    paused = state.task_rows()[task_id]
    assert paused["session_id"] == rows[task_id]["session_id"]
    assert paused["metadata"] == rows[task_id]["metadata"]

    assert run_cli(["task", "resume", task_id], kind=state.kind)[0] == 0
    assert state.task_rows()[task_id]["metadata"] == rows[task_id]["metadata"]
