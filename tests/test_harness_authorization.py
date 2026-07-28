from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from core.caller_context import AVIBE_AUTHORIZATION_PRINCIPAL_ENV
from core.services import agent_graph
from core.scheduled_tasks import (
    AgentRunExecutionResult,
    ScheduledTaskService,
    ScheduledTaskStore,
    TaskExecutionRequest,
    TaskExecutionStore,
)
from core.watches import (
    ManagedWatchService,
    ManagedWatchStore,
    WatchRuntimeStateStore,
)
from storage import (
    harness_authorization_service as harness_auth,
    project_access_service,
    projects_service,
    resource_access_service,
)
from storage.background import SQLiteBackgroundTaskStore
from storage.db import create_sqlite_engine
from storage.migrations import run_migrations
from storage.models import (
    agent_runs,
    agent_sessions,
    harness_principal_entitlements,
    run_definitions,
)
from vibe.authorization import AuthorizationContext, trusted_local_context


ORG_ID = "org-harness"
GROUP_ID = "grp_harness"
OWNER_SUBJECT = "owner-harness"
RAW_SENTINEL = "RAW-HARNESS-SENTINEL-1058"


def _context(role: str, *, matching: bool = True) -> AuthorizationContext:
    owner = role == "owner"
    return AuthorizationContext(
        instance_role=role,
        subject=OWNER_SUBJECT if owner else f"{role}-harness",
        email=f"{role}@example.com",
        instance_id="instance-harness",
        instance_access_source="owner" if owner else "organization_group",
        organization_id=ORG_ID if matching else "org-other",
        organization_member_id=f"member-{role}",
        organization_role="owner" if owner else "member",
        group_ids=frozenset({GROUP_ID}),
        membership_version="membership-v1",
        claims_issued_at=int(time.time()),
        is_remote=True,
    )


def _definition_payload(
    definition_id: str,
    definition_type: str,
    project_id: str,
) -> dict[str, Any]:
    now = "2026-07-28T00:00:00+00:00"
    common = {
        "id": definition_id,
        "name": f"Authorized {definition_type}",
        "project_id": project_id,
        "session_key": "",
        "enabled": True,
        "created_at": now,
        "updated_at": now,
        "metadata": {},
    }
    if definition_type == "scheduled":
        return {
            **common,
            "prompt": f"private prompt {definition_id}",
            "schedule_type": "cron",
            "cron": "0 * * * *",
            "timezone": "UTC",
        }
    return {
        **common,
        "command": ["sh", "-c", "exit 75"],
        "message": f"private message {definition_id}",
        "mode": "forever",
        "timeout_seconds": 30,
        "lifetime_timeout_seconds": 300,
        "retry_exit_codes": [75],
        "retry_delay_seconds": 1,
    }


@dataclass
class HarnessFixture:
    store: SQLiteBackgroundTaskStore
    project_id: str
    definitions: dict[str, str]

    @property
    def engine(self):
        return self.store.engine

    def make_run(
        self,
        run_id: str,
        *,
        definition_id: str | None = None,
        status: str = "succeeded",
        dependencies: list[dict[str, str]] | None = None,
        raw_sentinel: bool = False,
        session_id: str | None = None,
        activation_context: AuthorizationContext | None = None,
    ) -> dict[str, Any]:
        now = "2026-07-28T00:01:00+00:00"
        payload: dict[str, Any] = {
            "id": run_id,
            "request_type": "scheduled" if definition_id else "agent_run",
            "task_id": definition_id,
            "project_id": self.project_id,
            "session_id": session_id,
            "status": status,
            "prompt": RAW_SENTINEL if raw_sentinel else f"private prompt {run_id}",
            "message": RAW_SENTINEL if raw_sentinel else f"private message {run_id}",
            "result_text": RAW_SENTINEL if raw_sentinel else f"raw result {run_id}",
            "stdout": RAW_SENTINEL if raw_sentinel else f"raw stdout {run_id}",
            "stderr": RAW_SENTINEL if raw_sentinel else None,
            "error": RAW_SENTINEL if raw_sentinel else None,
            "created_at": now,
            "updated_at": now,
            "started_at": now if status == "running" else None,
            "completed_at": now if status in {"succeeded", "failed", "canceled"} else None,
            "metadata": {
                "harness_resources": dependencies or [],
                **(
                    {
                        "harness_activation_principal": {
                            "principal_type": "remote",
                            "instance_id": activation_context.instance_id,
                            "subject": activation_context.subject,
                            "organization_member_id": activation_context.organization_member_id,
                            "membership_version": activation_context.membership_version,
                        }
                    }
                    if activation_context is not None
                    else {}
                ),
            },
        }
        self.store.enqueue_run(payload)
        run = self.store.get_run(run_id)
        assert run is not None
        return run

    def make_safe(self, run_id: str, text: str = "member-safe result") -> None:
        assert harness_auth.record_member_safe_output(
            run_id,
            {"text": text, "status": "complete"},
            engine=self.engine,
        )

    def set_policy(
        self,
        resource_kind: str,
        resource_id: str,
        access_level: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            return resource_access_service.apply_control_plane_intent(
                connection,
                organization_id=ORG_ID,
                resource_kind=resource_kind,
                resource_id=resource_id,
                revision=revision,
                access_level=access_level,
                group_ids=[],
            )


@pytest.fixture
def harness_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    db = tmp_path / "state" / "vibe.sqlite"
    db.parent.mkdir(parents=True)
    run_migrations(db)
    engine = create_sqlite_engine(db)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as connection:
        project = projects_service.create_project(
            connection,
            str(project_dir),
            display_name="Harness Project",
        )
        applied = project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": project["id"],
                "organization_id": ORG_ID,
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": GROUP_ID,
                        "access_role": "editor",
                    }
                ],
            },
        )
        assert applied.outcome == "applied", applied
    engine.dispose()

    store = SQLiteBackgroundTaskStore(db)
    for role in ("owner", "editor", "viewer"):
        harness_auth.mirror_remote_principal(
            _context(role),
            {
                "vibe_instance_authorization_revision": 1,
                "claims_issued_at": int(time.time()),
            },
            engine=store.engine,
        )
    definitions = {"scheduled": "task-authz", "watch": "watch-authz"}
    owner = _context("owner")
    for definition_type, definition_id in definitions.items():
        payload = harness_auth.prepare_definition_payload(
            _definition_payload(definition_id, definition_type, project["id"]),
            definition_type=definition_type,
            user_context=owner,
            engine=store.engine,
        )
        if definition_type == "scheduled":
            store.upsert_scheduled_task(payload)
        else:
            store.upsert_watch(payload)
        harness_auth.register_definition(
            definition_id,
            user_context=owner,
            engine=store.engine,
        )
        with store.engine.begin() as connection:
            resource_access_service.apply_control_plane_intent(
                connection,
                organization_id=ORG_ID,
                resource_kind=(
                    "harness_task" if definition_type == "scheduled" else "harness_watch"
                ),
                resource_id=definition_id,
                revision=1,
                access_level="public",
                group_ids=[],
            )
    fixture = HarnessFixture(store, project["id"], definitions)
    try:
        yield fixture
    finally:
        store.close()


@pytest.mark.parametrize("definition_type", ["scheduled", "watch"])
@pytest.mark.parametrize(
    ("role", "matching", "read", "lifecycle", "manage"),
    [
        ("viewer", True, True, False, False),
        ("editor", True, True, True, False),
        ("owner", True, True, True, True),
        ("viewer", False, False, False, False),
    ],
)
def test_task_watch_owner_editor_viewer_no_match_matrix(
    harness_fixture: HarnessFixture,
    definition_type: str,
    role: str,
    matching: bool,
    read: bool,
    lifecycle: bool,
    manage: bool,
) -> None:
    context = _context(role, matching=matching)
    definition_id = harness_fixture.definitions[definition_type]
    definition = (
        harness_fixture.store.get_scheduled_task(definition_id)
        if definition_type == "scheduled"
        else harness_fixture.store.get_watch(definition_id)
    )
    assert definition is not None
    with harness_fixture.engine.connect() as connection:
        for operation, expected in (
            ("list", read),
            ("detail", read),
            ("run", lifecycle),
            ("pause", lifecycle),
            ("resume", lifecycle),
            ("update", manage),
            ("delete", manage),
        ):
            if expected:
                harness_auth.authorize_definition(
                    context,
                    definition,
                    operation,
                    connection=connection,
                )
            else:
                with pytest.raises(harness_auth.HarnessAuthorizationError):
                    harness_auth.authorize_definition(
                        context,
                        definition,
                        operation,
                        connection=connection,
                    )


@pytest.mark.parametrize("definition_type", ["scheduled", "watch"])
@pytest.mark.parametrize("role", ["viewer", "editor", "owner"])
def test_task_watch_create_is_owner_only(
    harness_fixture: HarnessFixture,
    definition_type: str,
    role: str,
) -> None:
    payload = _definition_payload(
        f"create-{definition_type}-{role}",
        definition_type,
        harness_fixture.project_id,
    )
    if role == "owner":
        prepared = harness_auth.prepare_definition_payload(
            payload,
            definition_type=definition_type,
            user_context=_context(role),
            engine=harness_fixture.engine,
        )
        assert prepared["project_id"] == harness_fixture.project_id
        return

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_owner_required",
    ):
        harness_auth.prepare_definition_payload(
            payload,
            definition_type=definition_type,
            user_context=_context(role),
            engine=harness_fixture.engine,
        )


def test_manual_run_rejects_suspended_definition_and_preserves_session_key(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    editor = _context("editor")
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == task_id)
            .values(legacy_session_key="slack::channel::authorized")
        )

    authorized = harness_auth.authorize_manual_run(
        editor,
        task_id,
        engine=harness_fixture.engine,
    )
    assert authorized["session_key"] == "slack::channel::authorized"

    harness_auth.suspend_definition(task_id, engine=harness_fixture.engine)
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_definition_suspended",
    ):
        harness_auth.authorize_manual_run(
            editor,
            task_id,
            engine=harness_fixture.engine,
        )

    with pytest.raises(harness_auth.HarnessAuthorizationError) as denied:
        harness_auth.authorize_manual_run(
            _context("editor", matching=False),
            task_id,
            engine=harness_fixture.engine,
        )
    assert denied.value.hidden is True
    assert denied.value.code != "harness_definition_suspended"


def test_suspended_definition_quarantines_queued_run_before_execution(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    harness_fixture.make_run(
        "queued-suspended-definition",
        definition_id=task_id,
        status="queued",
        activation_context=_context("editor"),
    )
    harness_auth.suspend_definition(task_id, engine=harness_fixture.engine)

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_definition_suspended",
    ):
        harness_auth.revalidate_run_for_execution(
            "queued-suspended-definition",
            engine=harness_fixture.engine,
        )

    run = harness_fixture.store.get_run("queued-suspended-definition")
    assert run is not None
    assert run["output_quarantined"] is True


def test_owner_definition_list_keeps_configuration_member_list_redacts_it(
    harness_fixture: HarnessFixture,
) -> None:
    task = harness_fixture.store.get_scheduled_task(
        harness_fixture.definitions["scheduled"]
    )
    assert task is not None
    with harness_fixture.engine.connect() as connection:
        owner = harness_auth.serialize_definition(
            _context("owner"),
            task,
            connection=connection,
        )
        member = harness_auth.serialize_definition(
            _context("viewer"),
            task,
            connection=connection,
        )

    assert owner["schedule_type"] == "cron"
    assert owner["cron"] == "0 * * * *"
    assert owner["prompt"] == f"private prompt {task['id']}"
    assert owner["redacted"] is False
    assert "schedule_type" not in member
    assert "cron" not in member
    assert "prompt" not in member
    assert member["redacted"] is True


@pytest.mark.parametrize("definition_backed", [False, True])
@pytest.mark.parametrize(
    ("role", "matching", "read", "cancel", "raw"),
    [
        ("viewer", True, True, False, False),
        ("editor", True, True, True, False),
        ("owner", True, True, True, True),
        ("viewer", False, False, False, False),
    ],
)
def test_direct_and_definition_run_owner_editor_viewer_no_match_matrix(
    harness_fixture: HarnessFixture,
    definition_backed: bool,
    role: str,
    matching: bool,
    read: bool,
    cancel: bool,
    raw: bool,
) -> None:
    suffix = "definition" if definition_backed else "direct"
    run_id = f"run-matrix-{suffix}"
    definition_id = harness_fixture.definitions["scheduled"] if definition_backed else None
    if harness_fixture.store.get_run(run_id) is None:
        harness_fixture.make_run(run_id, definition_id=definition_id)
        harness_fixture.make_safe(run_id)
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    context = _context(role, matching=matching)
    with harness_fixture.engine.connect() as connection:
        for operation, expected in (
            ("list", read),
            ("detail", read),
            ("output", read),
            ("cancel", cancel),
            ("logs", raw),
            ("raw", raw),
        ):
            if expected:
                harness_auth.authorize_run(context, run, operation, connection=connection)
            else:
                with pytest.raises(harness_auth.HarnessAuthorizationError):
                    harness_auth.authorize_run(context, run, operation, connection=connection)
        if read:
            projected = harness_auth.serialize_run(context, run, connection=connection)
            if role == "owner":
                assert projected["result_text"] == f"raw result {run_id}"
            else:
                assert projected["member_safe"]["text"] == "member-safe result"
                assert projected["result_text"] == "member-safe result"


@pytest.mark.parametrize("resource_kind", ["agent", "skill", "vault_secret"])
def test_referenced_resource_acl_is_required_without_owner_fallback(
    harness_fixture: HarnessFixture,
    resource_kind: str,
) -> None:
    resource_id = f"{resource_kind}-dependency"
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind=resource_kind,
            resource_id=resource_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="public",
        )
    run_id = f"run-{resource_kind}"
    harness_fixture.make_run(
        run_id,
        dependencies=[{"resource_kind": resource_kind, "resource_id": resource_id}],
    )
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    editor = _context("editor")
    owner = _context("owner")
    task_id = harness_fixture.definitions["scheduled"]
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == task_id)
            .values(
                metadata_json=json.dumps(
                    {
                        "harness_resources": [
                            {
                                "resource_kind": resource_kind,
                                "resource_id": resource_id,
                            }
                        ]
                    }
                )
            )
        )
    harness_auth.refresh_definition_dependencies(
        task_id,
        engine=harness_fixture.engine,
    )
    harness_auth.authorize_manual_run(editor, task_id, engine=harness_fixture.engine)
    with harness_fixture.engine.connect() as connection:
        harness_auth.authorize_run(editor, run, "detail", connection=connection)
        owner_projection = harness_auth.serialize_run(owner, run, connection=connection)
        if resource_kind == "vault_secret":
            assert owner_projection["redaction"]["reason"] == "vault_resource_used"
            assert "result_text" not in owner_projection

    harness_fixture.set_policy(resource_kind, resource_id, "private", revision=1)
    completed = harness_fixture.store.get_run(run_id)
    assert completed is not None
    assert completed["status"] == "succeeded"
    with pytest.raises(harness_auth.HarnessAuthorizationError):
        harness_auth.authorize_manual_run(
            editor,
            task_id,
            engine=harness_fixture.engine,
        )
    with harness_fixture.engine.connect() as connection:
        harness_auth.authorize_run(editor, completed, "detail", connection=connection)
        projection = harness_auth.serialize_run(editor, completed, connection=connection)
        expected_reason = (
            "vault_resource_used"
            if resource_kind == "vault_secret"
            else "dependency_access_revoked"
        )
        assert projection["redaction"]["reason"] == expected_reason
        assert "result_text" not in projection
        harness_auth.authorize_run(owner, completed, "detail", connection=connection)


def test_revocation_cancels_queued_and_active_task_agent_watch_runs(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    watch_id = harness_fixture.definitions["watch"]
    editor = _context("editor")
    for definition_id in (task_id, watch_id):
        harness_auth.set_definition_enabled(
            editor,
            definition_id,
            False,
            engine=harness_fixture.engine,
        )
        harness_auth.set_definition_enabled(
            editor,
            definition_id,
            True,
            engine=harness_fixture.engine,
        )
    harness_fixture.make_run(
        "queued-task",
        definition_id=task_id,
        status="queued",
        activation_context=editor,
    )
    harness_fixture.make_run(
        "running-task",
        definition_id=task_id,
        status="running",
        activation_context=editor,
    )
    harness_fixture.make_run(
        "running-watch",
        definition_id=watch_id,
        status="running",
        activation_context=editor,
    )
    harness_fixture.make_run(
        "running-owner-task",
        definition_id=task_id,
        status="running",
        activation_context=_context("owner"),
    )

    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id="active-agent",
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="public",
        )
    harness_fixture.make_run(
        "running-agent",
        status="running",
        dependencies=[{"resource_kind": "agent", "resource_id": "active-agent"}],
        activation_context=editor,
    )
    harness_fixture.make_run(
        "running-owner-agent",
        status="running",
        dependencies=[{"resource_kind": "agent", "resource_id": "active-agent"}],
        activation_context=_context("owner"),
    )

    harness_fixture.set_policy("harness_task", task_id, "private", revision=2)
    harness_fixture.set_policy("harness_watch", watch_id, "private", revision=2)
    harness_fixture.set_policy("agent", "active-agent", "private", revision=1)

    for run_id in ("queued-task", "running-task", "running-watch", "running-agent"):
        run = harness_fixture.store.get_run(run_id)
        assert run is not None
        assert run["status"] == "canceled"
        assert run["cancel_requested"] is True
        assert run["output_quarantined"] is True
        assert run["safe_error_code"] == "authorization_revoked"

    for run_id in ("running-owner-agent",):
        run = harness_fixture.store.get_run(run_id)
        assert run is not None
        assert run["status"] == "running"
        assert run["output_quarantined"] is False

    owner_task = harness_fixture.store.get_run("running-owner-task")
    assert owner_task is not None
    assert owner_task["status"] == "canceled"
    assert owner_task["output_quarantined"] is True

    task = harness_fixture.store.get_scheduled_task(task_id)
    watch = harness_fixture.store.get_watch(watch_id)
    assert task is not None and task["authorization_state"] == "suspended_authorization"
    assert watch is not None and watch["authorization_state"] == "suspended_authorization"


@pytest.mark.parametrize("execution_kind", ["task", "agent"])
def test_active_task_and_agent_execution_are_interrupted_on_revocation(
    tmp_path,
    monkeypatch,
    execution_kind: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_store = ScheduledTaskStore()
    request_store = TaskExecutionStore()
    if execution_kind == "task":
        definition = task_store.add_task(
            name="Revoked active task",
            session_key="slack::channel::C123",
            prompt="wait for revocation",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="UTC",
        )
        request = request_store.enqueue_task_run(definition.id, task=definition)
    else:
        request = request_store.enqueue_agent_run(
            session_key="slack::channel::C123",
            message="wait for revocation",
        )
    claimed = request_store.claim(request.id)
    assert claimed is not None
    service = ScheduledTaskService(
        controller=SimpleNamespace(),
        store=task_store,
        request_store=request_store,
    )
    started = asyncio.Event()
    interrupted = asyncio.Event()

    async def block_execution(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    if execution_kind == "task":
        monkeypatch.setattr(service, "_execute_task", block_execution)
    else:
        monkeypatch.setattr(service, "_execute_agent_run", block_execution)

    rechecks = 0

    def revalidate(run_id, *, engine):
        nonlocal rechecks
        rechecks += 1
        if rechecks > 1:
            harness_auth.quarantine_run(run_id, engine=engine)
            raise harness_auth.HarnessAuthorizationError("authorization_revoked")
        return trusted_local_context()

    monkeypatch.setattr(harness_auth, "revalidate_run_for_execution", revalidate)

    async def exercise() -> None:
        execution = asyncio.create_task(service._execute_claimed_request(claimed))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.wait_for(execution, timeout=3)
        await asyncio.wait_for(interrupted.wait(), timeout=1)

    asyncio.run(exercise())
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["output_quarantined"] is True


@pytest.mark.parametrize("execution_kind", ["task", "agent"])
def test_manual_cancel_interrupts_active_task_and_agent_execution(
    tmp_path,
    monkeypatch,
    execution_kind: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_store = ScheduledTaskStore()
    request_store = TaskExecutionStore()
    if execution_kind == "task":
        definition = task_store.add_task(
            name="Canceled active task",
            session_key="slack::channel::C123",
            prompt="wait for cancellation",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="UTC",
        )
        request = request_store.enqueue_task_run(definition.id, task=definition)
    else:
        request = request_store.enqueue_agent_run(
            session_key="slack::channel::C123",
            message="wait for cancellation",
        )
    claimed = request_store.claim(request.id)
    assert claimed is not None
    service = ScheduledTaskService(
        controller=SimpleNamespace(),
        store=task_store,
        request_store=request_store,
    )
    started = asyncio.Event()
    interrupted = asyncio.Event()

    async def block_execution(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    if execution_kind == "task":
        monkeypatch.setattr(service, "_execute_task", block_execution)
    else:
        monkeypatch.setattr(service, "_execute_agent_run", block_execution)

    async def exercise() -> None:
        execution = asyncio.create_task(service._execute_claimed_request(claimed))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert harness_auth.cancel_run(
            trusted_local_context(),
            request.id,
            engine=request_store._sqlite.engine,
        )
        await asyncio.wait_for(execution, timeout=3)
        await asyncio.wait_for(interrupted.wait(), timeout=1)

    asyncio.run(exercise())
    stored = request_store.get_run(request.id)
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["cancel_requested"] is True
    assert stored["output_quarantined"] is True


def test_resume_keeps_incomplete_definition_suspended(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    editor = _context("editor")
    harness_auth.set_definition_enabled(
        editor,
        task_id,
        False,
        engine=harness_fixture.engine,
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == task_id)
            .values(
                metadata_json=json.dumps(
                    {
                        "harness_resources": [
                            {
                                "resource_kind": "unsupported",
                                "resource_id": "unknown-resource",
                            }
                        ]
                    }
                )
            )
        )
    harness_auth.refresh_definition_dependencies(
        task_id,
        engine=harness_fixture.engine,
    )

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_dependency_attribution_incomplete",
    ):
        harness_auth.set_definition_enabled(
            editor,
            task_id,
            True,
            engine=harness_fixture.engine,
        )

    stored = harness_fixture.store.get_scheduled_task(task_id)
    assert stored is not None
    assert stored["enabled"] is False
    assert stored["authorization_state"] == "suspended_authorization"
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_definition_suspended",
    ):
        harness_auth.revalidate_definition_for_execution(
            task_id,
            engine=harness_fixture.engine,
        )


def test_active_watch_worker_is_stopped_on_revocation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    watch_store = ManagedWatchStore()
    request_store = TaskExecutionStore()
    runtime_store = WatchRuntimeStateStore(tmp_path / "watch-runtime.json")
    watch = watch_store.add_watch(
        name="Revoked active watch",
        session_key="slack::channel::C123",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        shell_command=None,
        prefix="wait",
        cwd=None,
        mode="forever",
        timeout_seconds=30,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0.1,
        post_to=None,
        deliver_key=None,
    )
    service = ManagedWatchService(
        controller=SimpleNamespace(),
        store=watch_store,
        request_store=request_store,
        runtime_store=runtime_store,
    )
    rechecks = 0

    def revalidate(definition_id, *, engine):
        nonlocal rechecks
        rechecks += 1
        if rechecks > 1:
            harness_auth.suspend_definition(definition_id, engine=engine)
            raise harness_auth.HarnessAuthorizationError("authorization_revoked")
        return trusted_local_context()

    monkeypatch.setattr(harness_auth, "revalidate_definition_for_execution", revalidate)

    async def exercise() -> None:
        service.start()
        if service._startup_task is not None:
            await service._startup_task
        for _ in range(100):
            if watch.id in service._active_pids:
                break
            await asyncio.sleep(0.02)
        assert watch.id in service._active_pids
        for _ in range(150):
            if watch.id not in service._active_pids:
                break
            await asyncio.sleep(0.02)
        assert watch.id not in service._active_pids
        await service.stop()

    asyncio.run(exercise())
    stored = watch_store.get_watch(watch.id)
    assert stored is not None
    assert stored.authorization_state == "suspended_authorization"


def test_remote_entitlement_mirror_fails_closed_when_stale(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    context = _context("owner")
    now = int(time.time())
    harness_auth.mirror_remote_principal(
        context,
        {"vibe_instance_authorization_revision": 7, "claims_issued_at": now},
        engine=harness_fixture.engine,
        now=now,
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(harness_principal_entitlements).values(fresh_until=now - 1)
        )
    monkeypatch.setattr(
        harness_auth,
        "_refresh_entitlement_from_device_revision",
        lambda *_args, **_kwargs: False,
    )
    task_id = harness_fixture.definitions["scheduled"]
    harness_fixture.make_run("stale-entitlement-run", definition_id=task_id, status="queued")
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_entitlement_stale",
    ):
        harness_auth.execution_context(
            "stale-entitlement-run",
            engine=harness_fixture.engine,
            now=now,
        )


def test_remote_agent_cli_definition_creation_keeps_current_editor_principal(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    principal = {
        "principal_type": "remote",
        "instance_id": editor.instance_id,
        "subject": editor.subject,
        "organization_member_id": editor.organization_member_id,
        "membership_version": editor.membership_version,
    }
    monkeypatch.setenv("AVIBE_SESSION_ID", "remote-agent-session")
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        json.dumps(principal),
    )
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)

    resolved = resource_access_service.resolve_resource_access_context()
    assert resolved.is_remote is True
    assert resolved.instance_role == "editor"

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_owner_required",
    ):
        ScheduledTaskStore().add_task(
            name="Remote editor task",
            session_key="slack::channel::C123",
            prompt="must remain owner-only",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="UTC",
        )
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_owner_required",
    ):
        ManagedWatchStore().add_watch(
            name="Remote editor watch",
            session_key="slack::channel::C123",
            command=["true"],
            shell_command=None,
            prefix="must remain owner-only",
            cwd=None,
            mode="once",
            timeout_seconds=30,
            lifetime_timeout_seconds=300,
            retry_exit_codes=[75],
            retry_delay_seconds=1,
            post_to=None,
            deliver_key=None,
        )


def test_malformed_agent_principal_env_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)
    monkeypatch.setenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, "not-json")

    context = resource_access_service.resolve_resource_access_context()

    assert context.is_remote is True
    assert context.is_trusted_local is False


def test_stale_agent_principal_env_fails_closed(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(harness_principal_entitlements)
            .where(harness_principal_entitlements.c.instance_id == editor.instance_id)
            .where(harness_principal_entitlements.c.subject == editor.subject)
            .values(fresh_until=0)
        )
    monkeypatch.setattr(
        harness_auth,
        "_refresh_entitlement_from_device_revision",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        json.dumps(
            {
                "principal_type": "remote",
                "instance_id": editor.instance_id,
                "subject": editor.subject,
            }
        ),
    )

    context = resource_access_service.resolve_resource_access_context()

    assert context.is_remote is True
    assert context.has_role("viewer") is False
    assert context.is_trusted_local is False


def test_remote_entitlement_revision_change_fails_closed_immediately(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    context = _context("editor")
    run_id = "changed-entitlement-revision"
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=context,
    )
    monkeypatch.setattr(
        harness_auth,
        "_current_device_authorization_revision",
        lambda *, now: 2,
    )

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_entitlement_revision_changed",
    ):
        harness_auth.execution_context(
            run_id,
            engine=harness_fixture.engine,
        )


def test_callback_run_inherits_completed_parent_execution_principal(
    harness_fixture: HarnessFixture,
) -> None:
    editor = _context("editor")
    parent_id = "remote-callback-parent"
    child_id = "remote-callback-child"
    harness_fixture.make_run(
        parent_id,
        status="succeeded",
        activation_context=editor,
    )
    now = "2026-07-28T00:02:00+00:00"
    harness_fixture.store.enqueue_run(
        {
            "id": child_id,
            "request_type": "agent_run",
            "source_kind": "callback",
            "source_actor": parent_id,
            "parent_run_id": parent_id,
            "project_id": harness_fixture.project_id,
            "message": "callback result",
            "status": "queued",
            "created_at": now,
            "updated_at": now,
            "metadata": {"callback_parent_run_id": parent_id},
        }
    )

    child = harness_fixture.store.get_run(child_id)
    assert child is not None
    principal = child["authorization_provenance"]["execution_principal"]
    assert principal["principal_type"] == "remote"
    assert principal["subject"] == editor.subject
    assert principal["instance_id"] == editor.instance_id
    execution_context = harness_auth.execution_context(
        child_id,
        engine=harness_fixture.engine,
    )
    assert execution_context.is_remote
    assert execution_context.subject == editor.subject


def test_remote_entitlement_mirror_rejects_older_revision_or_claims(
    harness_fixture: HarnessFixture,
) -> None:
    current = _context("editor")
    claims_issued_at = int(time.time()) + 100
    older = AuthorizationContext(
        instance_role="viewer",
        subject=current.subject,
        email=current.email,
        instance_id=current.instance_id,
        instance_access_source=current.instance_access_source,
        organization_id=current.organization_id,
        organization_member_id=current.organization_member_id,
        organization_role=current.organization_role,
        group_ids=frozenset(),
        membership_version="membership-old",
        claims_issued_at=claims_issued_at - 1,
        is_remote=True,
    )
    harness_auth.mirror_remote_principal(
        current,
        {
            "vibe_instance_authorization_revision": 9,
            "claims_issued_at": claims_issued_at,
        },
        engine=harness_fixture.engine,
        now=claims_issued_at + 100,
    )
    harness_auth.mirror_remote_principal(
        older,
        {
            "vibe_instance_authorization_revision": 8,
            "claims_issued_at": claims_issued_at + 50,
        },
        engine=harness_fixture.engine,
        now=claims_issued_at + 200,
    )
    harness_auth.mirror_remote_principal(
        older,
        {
            "vibe_instance_authorization_revision": 9,
            "claims_issued_at": claims_issued_at - 1,
        },
        engine=harness_fixture.engine,
        now=claims_issued_at + 300,
    )

    with harness_fixture.engine.connect() as connection:
        entitlement = connection.execute(
            select(harness_principal_entitlements).where(
                harness_principal_entitlements.c.instance_id == current.instance_id,
                harness_principal_entitlements.c.subject == current.subject,
            )
        ).mappings().one()
    assert entitlement["authorization_revision"] == 9
    assert entitlement["claims_issued_at"] == claims_issued_at
    assert entitlement["instance_role"] == "editor"
    assert json.loads(entitlement["group_ids_json"]) == [GROUP_ID]
    assert entitlement["membership_version"] == "membership-v1"
    assert entitlement["fresh_until"] == claims_issued_at + 400


def test_project_acl_change_quarantines_dependent_runs(
    harness_fixture: HarnessFixture,
) -> None:
    editor = _context("editor")
    task_id = harness_fixture.definitions["scheduled"]
    external_path = harness_fixture.store.db_path.parent.parent / "external-project"
    external_path.mkdir()
    now = "2026-07-28T00:02:00+00:00"
    with harness_fixture.engine.begin() as connection:
        external_project = projects_service.create_project(
            connection,
            str(external_path),
            display_name="External Harness Project",
        )
        applied = project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": external_project["id"],
                "organization_id": ORG_ID,
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": GROUP_ID,
                        "access_role": "editor",
                    }
                ],
            },
        )
        assert applied.changed is True
        connection.execute(
            agent_sessions.insert().values(
                id="external-project-session",
                scope_id=project_access_service.project_scope_id(external_project["id"]),
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor-external-project-session",
                native_session_id="",
                title="External Project Session",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    harness_fixture.make_run(
        "project-task-run",
        definition_id=task_id,
        status="queued",
        activation_context=editor,
    )
    harness_fixture.make_run(
        "project-agent-run",
        status="running",
        activation_context=editor,
    )
    harness_fixture.make_run(
        "project-session-run",
        status="running",
        dependencies=[
            {
                "resource_kind": "session",
                "resource_id": "external-project-session",
            }
        ],
        activation_context=editor,
    )

    with harness_fixture.engine.begin() as connection:
        applied = project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": external_project["id"],
                "organization_id": ORG_ID,
                "revision": 2,
                "mode": "restricted",
                "bindings": [],
            },
        )
    assert applied.changed is True
    session_run = harness_fixture.store.get_run("project-session-run")
    assert session_run is not None
    assert session_run["status"] == "canceled"
    assert session_run["output_quarantined"] is True
    for run_id in ("project-task-run", "project-agent-run"):
        run = harness_fixture.store.get_run(run_id)
        assert run is not None
        assert run["status"] in {"queued", "running"}

    with harness_fixture.engine.begin() as connection:
        applied = project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": harness_fixture.project_id,
                "organization_id": ORG_ID,
                "revision": 2,
                "mode": "restricted",
                "bindings": [],
            },
        )
    assert applied.changed is True

    for run_id in ("project-task-run", "project-agent-run"):
        run = harness_fixture.store.get_run(run_id)
        assert run is not None
        assert run["status"] == "canceled"
        assert run["cancel_requested"] is True
        assert run["output_quarantined"] is True
        assert run["safe_error_code"] == "authorization_revoked"


def test_vault_tainted_sentinels_are_absent_from_list_event_sse_and_direct_id(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from storage import background, messages_service
    from vibe import ui_server
    from vibe.ui_compat import g

    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id="vault-sentinel",
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
    harness_fixture.make_run(
        "vault-sentinel-run",
        dependencies=[
            {"resource_kind": "vault_secret", "resource_id": "vault-sentinel"}
        ],
        raw_sentinel=True,
    )
    monkeypatch.setattr(
        background,
        "SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )
    owner = trusted_local_context()

    with ui_server.app.test_request_context("/api/harness/runs"):
        g.authorization_context = owner
        response = ui_server.harness_runs_list()
        assert RAW_SENTINEL not in response.body.decode()

    with ui_server.app.test_request_context("/api/harness/runs/vault-sentinel-run"):
        g.authorization_context = owner
        response = ui_server.harness_run_detail("vault-sentinel-run")
        assert RAW_SENTINEL not in response.body.decode()

    event_payload = ui_server._workbench_event_payload_for_context(
        owner,
        "runs.updated",
        json.dumps(
            {
                "data": {
                    "run_id": "vault-sentinel-run",
                    "status": "succeeded",
                    "result_text": RAW_SENTINEL,
                }
            }
        ),
    )
    assert RAW_SENTINEL not in event_payload

    sse_payload = ui_server._workbench_event_payload_for_context(
        owner,
        "message.new",
        json.dumps(
            {"data": {
                "id": "message-vault-run",
                "source": "harness",
                "text": RAW_SENTINEL,
                "metadata": {"harness_run_id": "vault-sentinel-run"},
            }}
        ),
    )
    assert RAW_SENTINEL not in sse_payload
    assert json.loads(sse_payload)["data"]["text"] == ""

    inbox_event = ui_server._workbench_event_payload_for_context(
        owner,
        "inbox.session.updated",
        json.dumps(
            {
                "data": {
                    "session_id": "session-vault-run",
                    "preview_text": RAW_SENTINEL,
                    "_harness_originated": True,
                    "_harness_run_id": "vault-sentinel-run",
                }
            }
        ),
    )
    assert RAW_SENTINEL not in inbox_event
    assert json.loads(inbox_event)["data"]["preview_text"] == ""

    monkeypatch.setattr(
        messages_service,
        "list_inbox_sessions",
        lambda *_args, **_kwargs: {
            "sessions": [
                {
                    "session_id": "session-vault-run",
                    "preview_text": RAW_SENTINEL,
                    "_harness_originated": True,
                    "_harness_run_id": "vault-sentinel-run",
                }
            ],
            "next_cursor": None,
        },
    )
    monkeypatch.setattr(
        messages_service,
        "unread_counts_by_session",
        lambda *_args, **_kwargs: {},
    )
    with ui_server.app.test_request_context("/api/inbox"):
        g.authorization_context = owner
        response = ui_server.inbox_list()
        body = response.body.decode()
    assert RAW_SENTINEL not in body
    assert json.loads(body)["sessions"][0]["preview_text"] == ""


def test_run_list_uses_database_authorization_scope_and_pagination(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from storage import background
    from vibe import ui_server
    from vibe.ui_compat import g

    editor = _context("editor")
    harness_fixture.make_run("visible-paged-run", activation_context=editor)
    harness_fixture.make_run(
        "hidden-definition-run",
        definition_id=harness_fixture.definitions["scheduled"],
        activation_context=editor,
    )
    harness_fixture.set_policy(
        "harness_task",
        harness_fixture.definitions["scheduled"],
        "private",
        revision=2,
    )
    harness_fixture.store.enqueue_run(
        {
            "id": "hidden-other-project-run",
            "request_type": "agent_run",
            "project_id": "project-without-access",
            "status": "succeeded",
            "created_at": "2026-07-28T00:02:00+00:00",
            "updated_at": "2026-07-28T00:02:00+00:00",
            "metadata": {
                "harness_activation_principal": {
                    "principal_type": "remote",
                    "instance_id": editor.instance_id,
                    "subject": editor.subject,
                    "organization_member_id": editor.organization_member_id,
                    "membership_version": editor.membership_version,
                }
            },
        }
    )
    monkeypatch.setattr(
        background,
        "SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )

    def reject_unbounded_list(*args, **kwargs):
        raise AssertionError("Harness route must not scan list_runs()")

    monkeypatch.setattr(SQLiteBackgroundTaskStore, "list_runs", reject_unbounded_list)
    with ui_server.app.test_request_context("/api/harness/runs?page=1&limit=1"):
        g.authorization_context = editor
        g.remote_session_payload = {
            "vibe_instance_authorization_revision": 1,
            "claims_issued_at": int(time.time()),
        }
        response = ui_server.harness_runs_list()

    payload = json.loads(response.body)
    assert response.status_code == 200
    assert [run["id"] for run in payload["runs"]] == ["visible-paged-run"]
    assert payload["total"] == 1
    assert payload["has_more"] is False


def test_non_run_bootstrap_does_not_build_run_page(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from storage import background
    from vibe import ui_server
    from vibe.ui_compat import g

    monkeypatch.setattr(
        background,
        "SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )

    def reject_run_page(*args, **kwargs):
        raise AssertionError("task bootstrap must not build a Run page")

    monkeypatch.setattr(ui_server, "_harness_run_page", reject_run_page)
    with ui_server.app.test_request_context("/api/harness/bootstrap?tab=tasks"):
        g.authorization_context = _context("editor")
        g.remote_session_payload = {
            "vibe_instance_authorization_revision": 1,
            "claims_issued_at": int(time.time()),
        }
        response = ui_server.harness_bootstrap()

    assert response.status_code == 200
    assert json.loads(response.body)["tab"] == "tasks"


def _replace_run_prompt_provenance(
    harness_fixture: HarnessFixture,
    run_id: str,
    prompt: str,
) -> None:
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(prompt=prompt)
        )
        provenance = harness_auth.prepare_run_authorization(
            connection,
            {
                "id": run_id,
                "request_type": "agent_run",
                "project_id": harness_fixture.project_id,
                "prompt": prompt,
                "metadata": {},
            },
            activation_context=trusted_local_context(),
        )
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(authorization_provenance_json=json.dumps(provenance))
        )


def test_member_safe_classifier_rejects_owner_only_input(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "unsafe-member-output"
    harness_fixture.make_run(run_id)

    assert not harness_auth.record_member_safe_output(
        run_id,
        {"text": f"private prompt {run_id}", "status": "complete"},
        engine=harness_fixture.engine,
    )
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    assert run["output_classification"] == "unsafe"
    assert run["member_safe"] is None


def test_member_safe_classifier_allows_prompt_vocabulary(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "ordinary-member-output"
    harness_fixture.make_run(run_id)
    _replace_run_prompt_provenance(
        harness_fixture,
        run_id,
        "summarize the quarterly sales report",
    )

    assert harness_auth.record_member_safe_output(
        run_id,
        {"text": "The sales report shows quarterly growth.", "status": "complete"},
        engine=harness_fixture.engine,
    )


def test_member_safe_classifier_allows_single_word_prompt_in_answer(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "single-word-member-output"
    harness_fixture.make_run(run_id)
    _replace_run_prompt_provenance(harness_fixture, run_id, "status")

    assert harness_auth.record_member_safe_output(
        run_id,
        {"text": "Current status is healthy.", "status": "complete"},
        engine=harness_fixture.engine,
    )


def test_member_safe_classifier_rejects_uppercase_exact_prompt(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "uppercase-exact-output"
    harness_fixture.make_run(run_id)
    _replace_run_prompt_provenance(harness_fixture, run_id, "SECRET")

    assert not harness_auth.record_member_safe_output(
        run_id,
        {"text": "SECRET", "status": "complete"},
        engine=harness_fixture.engine,
    )


def test_transcript_keeps_trigger_distinct_from_member_safe_output(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "distinct-transcript-output"
    harness_fixture.make_run(run_id)
    harness_fixture.make_safe(run_id, "member-safe final")
    messages = [
        {
            "id": "trigger",
            "author": "harness",
            "source": "harness",
            "type": "harness",
            "text": "private trigger",
            "content": {"text": "private trigger"},
            "metadata": {"harness_run_id": run_id},
        },
        {
            "id": "notify",
            "author": "agent",
            "source": "agent",
            "type": "notify",
            "text": "raw progress",
            "content": {"text": "raw progress"},
            "metadata": {"harness_run_id": run_id},
        },
        {
            "id": "result",
            "author": "agent",
            "source": "agent",
            "type": "result",
            "text": "raw final",
            "content": {"text": "raw final"},
            "metadata": {"harness_run_id": run_id},
        },
    ]

    with harness_fixture.engine.connect() as connection:
        member_rows = harness_auth.project_transcript_messages(
            _context("viewer"),
            messages,
            connection=connection,
        )
        owner_rows = harness_auth.project_transcript_messages(
            trusted_local_context(),
            messages,
            connection=connection,
        )

    assert [row["text"] for row in member_rows] == [
        "",
        "",
        "member-safe final",
    ]
    assert member_rows[0]["content"]["redaction"]["reason"] == (
        "owner_only_harness_input"
    )
    assert [row["text"] for row in owner_rows] == [
        "private trigger",
        "raw progress",
        "raw final",
    ]


def test_member_safe_classifier_rejects_decoded_multiline_input(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "decoded-multiline-output"
    prompt = 'private first line\n"quoted" path\\secret'
    harness_fixture.make_run(run_id)
    _replace_run_prompt_provenance(harness_fixture, run_id, prompt)

    assert not harness_auth.record_member_safe_output(
        run_id,
        {"text": f"Echoed input:\n{prompt}", "status": "complete"},
        engine=harness_fixture.engine,
    )


def test_member_safe_classifier_rejects_file_attachments(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "file-attachment-output"
    harness_fixture.make_run(run_id)

    assert not harness_auth.record_member_safe_output(
        run_id,
        {
            "text": "Sensitive attachment: [report](file:///tmp/private-report.txt)",
            "status": "complete",
        },
        engine=harness_fixture.engine,
    )
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    assert run["output_classification"] == "unsafe"
    assert run["member_safe"] is None
    assert not harness_auth.can_emit_run_output(
        run_id,
        engine=harness_fixture.engine,
    )


def test_coalesced_agent_runs_split_different_execution_principals(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    primary_context = _context("editor")
    secondary_context = AuthorizationContext(
        instance_role="editor",
        subject="editor-secondary-harness",
        email="editor-secondary@example.com",
        instance_id=primary_context.instance_id,
        instance_access_source=primary_context.instance_access_source,
        organization_id=primary_context.organization_id,
        organization_member_id="member-editor-secondary",
        organization_role=primary_context.organization_role,
        group_ids=primary_context.group_ids,
        membership_version="membership-secondary-v1",
        claims_issued_at=int(time.time()),
        is_remote=True,
    )
    harness_auth.mirror_remote_principal(
        secondary_context,
        {
            "vibe_instance_authorization_revision": 1,
            "claims_issued_at": int(time.time()),
        },
        engine=harness_fixture.engine,
    )
    now = "2026-07-28T00:03:00+00:00"
    session_id = "coalesced-principal-session"

    def principal(context: AuthorizationContext) -> dict[str, str]:
        return {
            "principal_type": "remote",
            "instance_id": str(context.instance_id),
            "subject": str(context.subject),
            "organization_member_id": str(context.organization_member_id),
            "membership_version": str(context.membership_version),
        }

    test_root = harness_fixture.store.db_path.parent.parent
    request_store = TaskExecutionStore(test_root / "coalesced-requests")
    request_store._sqlite = harness_fixture.store
    task_store = ScheduledTaskStore(test_root / "coalesced-tasks.json")
    task_store._sqlite = harness_fixture.store
    task_store.load()
    monkeypatch.setattr(
        "core.scheduled_tasks.SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=project_access_service.project_scope_id(
                    harness_fixture.project_id
                ),
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor-coalesced-principal-session",
                native_session_id="",
                title="Coalesced Principal Session",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    requests = [
        request_store.enqueue_agent_run(
            session_id=session_id,
            message=message,
            metadata={
                "harness_activation_principal": principal(context),
                "workbench_queue_holds_run": True,
            },
        )
        for message, context in (
            ("primary principal message", primary_context),
            ("secondary principal message", secondary_context),
        )
    ]
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    run_ids = [request.id for request in requests]
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    primary_row = sqlite_store.get_run(run_ids[0])
    assert primary_row is not None
    request = TaskExecutionRequest.from_dict(primary_row)
    submitted: list[str] = []

    async def execute_agent_run(**kwargs):
        submitted.append(str(kwargs["message"]))
        return AgentRunExecutionResult(error=None, complete_on_return=True)

    service = ScheduledTaskService(
        controller=SimpleNamespace(session_turns=None),
        store=task_store,
        request_store=request_store,
    )
    service._execute_agent_run = execute_agent_run
    asyncio.run(service._execute_claimed_request(request))

    assert submitted == ["primary principal message"]
    primary = sqlite_store.get_run(run_ids[0])
    secondary = sqlite_store.get_run(run_ids[1])
    assert primary is not None and primary["status"] == "succeeded"
    assert secondary is not None and secondary["status"] == "queued"
    assert secondary["metadata"]["workbench_queue_holds_run"] is False
    assert "coalesced_into_run_id" not in secondary["metadata"]


def test_revoked_coalesced_primary_releases_valid_child(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor_context = _context("editor")
    now = "2026-07-28T00:04:00+00:00"
    session_id = "revoked-coalesced-primary-session"
    test_root = harness_fixture.store.db_path.parent.parent
    request_store = TaskExecutionStore(test_root / "revoked-coalesced-requests")
    request_store._sqlite = harness_fixture.store
    task_store = ScheduledTaskStore(test_root / "revoked-coalesced-tasks.json")
    task_store._sqlite = harness_fixture.store
    task_store.load()
    monkeypatch.setattr(
        "core.scheduled_tasks.SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=project_access_service.project_scope_id(
                    harness_fixture.project_id
                ),
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor-revoked-coalesced-primary-session",
                native_session_id="",
                title="Revoked Coalesced Primary Session",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    def principal(subject: str) -> dict[str, str]:
        return {
            "principal_type": "remote",
            "instance_id": str(editor_context.instance_id),
            "subject": subject,
            "organization_member_id": str(editor_context.organization_member_id),
            "membership_version": str(editor_context.membership_version),
        }

    requests = [
        request_store.enqueue_agent_run(
            session_id=session_id,
            message=message,
            metadata={
                "harness_activation_principal": principal(subject),
                "workbench_queue_holds_run": True,
            },
        )
        for message, subject in (
            ("revoked primary message", "missing-primary"),
            ("valid child message", str(editor_context.subject)),
        )
    ]
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    run_ids = [request.id for request in requests]
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    primary_row = sqlite_store.get_run(run_ids[0])
    assert primary_row is not None

    service = ScheduledTaskService(
        controller=SimpleNamespace(session_turns=None),
        store=task_store,
        request_store=request_store,
    )
    asyncio.run(service._execute_claimed_request(TaskExecutionRequest.from_dict(primary_row)))

    primary = sqlite_store.get_run(run_ids[0])
    secondary = sqlite_store.get_run(run_ids[1])
    assert primary is not None and primary["status"] == "canceled"
    assert secondary is not None and secondary["status"] == "queued"
    assert secondary["metadata"]["workbench_queue_holds_run"] is False
    assert "coalesced_into_run_id" not in secondary["metadata"]


def test_completed_run_read_rechecks_current_definition_acl(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    harness_fixture.make_run("completed-recheck", definition_id=task_id)
    run = harness_fixture.store.get_run("completed-recheck")
    assert run is not None
    with harness_fixture.engine.connect() as connection:
        harness_auth.authorize_run(_context("viewer"), run, "detail", connection=connection)

    harness_fixture.set_policy("harness_task", task_id, "private", revision=2)
    completed = harness_fixture.store.get_run("completed-recheck")
    assert completed is not None and completed["status"] == "succeeded"
    with harness_fixture.engine.connect() as connection:
        with pytest.raises(harness_auth.HarnessAuthorizationError):
            harness_auth.authorize_run(
                _context("viewer"),
                completed,
                "detail",
                connection=connection,
            )
        harness_auth.authorize_run(
            _context("owner"),
            completed,
            "detail",
            connection=connection,
        )


def test_run_graph_rechecks_current_project_and_run_access(
    harness_fixture: HarnessFixture,
) -> None:
    now = "2026-07-28T00:01:00+00:00"
    session_id = "session-harness-graph"
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=project_access_service.project_scope_id(
                    harness_fixture.project_id
                ),
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id=session_id,
                title="Harness graph session",
                status="active",
                agent_status="idle",
                visibility="foreground",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    harness_fixture.make_run("run-harness-graph", session_id=session_id)

    visible = agent_graph.build_graph(
        engine=harness_fixture.engine,
        authorization_context=_context("viewer"),
    )
    hidden = agent_graph.build_graph(
        engine=harness_fixture.engine,
        authorization_context=_context("viewer", matching=False),
    )

    visible_node = next(
        node for node in visible["nodes"] if node["session_id"] == session_id
    )
    assert [run["id"] for run in visible_node["runs"]] == ["run-harness-graph"]
    assert all(node["session_id"] != session_id for node in hidden["nodes"])
