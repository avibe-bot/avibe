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

from core.services import agent_graph
from core.scheduled_tasks import (
    ScheduledTaskService,
    ScheduledTaskStore,
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

    for run_id in ("running-owner-task", "running-owner-agent"):
        run = harness_fixture.store.get_run(run_id)
        assert run is not None
        assert run["status"] == "running"
        assert run["output_quarantined"] is False

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
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(prompt="summarize the quarterly sales report")
        )
    # Re-enqueue through the authorization preparation path so the manifest is
    # derived from the representative prompt.
    with harness_fixture.engine.begin() as connection:
        provenance = harness_auth.prepare_run_authorization(
            connection,
            {
                "id": run_id,
                "request_type": "agent_run",
                "project_id": harness_fixture.project_id,
                "prompt": "summarize the quarterly sales report",
                "metadata": {},
            },
            activation_context=trusted_local_context(),
        )
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(
                authorization_provenance_json=json.dumps(provenance),
            )
        )

    assert harness_auth.record_member_safe_output(
        run_id,
        {"text": "The sales report shows quarterly growth.", "status": "complete"},
        engine=harness_fixture.engine,
    )


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
