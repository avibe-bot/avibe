from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select, update

from core.caller_context import (
    AVIBE_AUTHORIZATION_CAPABILITY_ENV,
    AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
    AVIBE_CALLER_BACKEND_ENV,
    AVIBE_CALLER_SOURCE_ENV,
    AVIBE_HARNESS_AUTHORIZATION_ENV,
    AVIBE_NATIVE_SESSION_ID_ENV,
    AVIBE_RUN_ID_ENV,
    AVIBE_SESSION_ID_ENV,
    issue_authorization_capability,
    resolve_authorization_capability,
)
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
    messages_service,
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
    harness_definition_dependencies,
    harness_principal_entitlements,
    harness_run_dependencies,
    run_definitions,
)
from storage.workbench_sessions_service import create_session
from vibe import cli
from vibe import internal_client
from vibe.authorization import AuthorizationContext, trusted_local_context


ORG_ID = "org-harness"
GROUP_ID = "grp_harness"
OWNER_SUBJECT = "owner-harness"
RAW_SENTINEL = "RAW-HARNESS-SENTINEL-1058"


def _issue_test_agent_capability(
    monkeypatch,
    principal: dict[str, Any],
    *,
    session_id: str,
) -> str:
    monkeypatch.setattr(
        internal_client,
        "resolve_authorization_principal_capability",
        resolve_authorization_capability,
    )
    return issue_authorization_capability(principal, session_id=session_id)


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
    monkeypatch.setattr(
        harness_auth,
        "_current_device_instance_id",
        lambda: "instance-harness",
    )
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
def test_editor_cli_lifecycle_response_redacts_owner_definition_fields(
    harness_fixture: HarnessFixture,
    monkeypatch,
    capsys,
    definition_type: str,
) -> None:
    editor = _context("editor")
    session_id = f"editor-{definition_type}-lifecycle"
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, session_id)
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        _issue_test_agent_capability(
            monkeypatch,
            {
                "principal_type": "remote",
                "instance_id": editor.instance_id,
                "subject": editor.subject,
            },
            session_id=session_id,
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.delenv(AVIBE_RUN_ID_ENV, raising=False)
    monkeypatch.delenv(AVIBE_HARNESS_AUTHORIZATION_ENV, raising=False)
    definition_id = harness_fixture.definitions[definition_type]
    if definition_type == "scheduled":
        store = ScheduledTaskStore.__new__(ScheduledTaskStore)
        store.path = harness_fixture.store.db_path
        store._sqlite = harness_fixture.store
        store._signature = None
        store._tasks = {}
        store.load()
        monkeypatch.setattr(cli, "_task_store", lambda: store)
        assert cli.cmd_task_set_enabled(definition_id, False) == 0
        resource_key = "task"
    else:
        store = ManagedWatchStore.__new__(ManagedWatchStore)
        store.path = harness_fixture.store.db_path
        store._sqlite = harness_fixture.store
        store._signature = None
        store._watches = {}
        store.load()
        monkeypatch.setattr(cli, "_watch_store", lambda: store)
        monkeypatch.setattr(
            cli,
            "_watch_runtime_store",
            lambda: SimpleNamespace(load=lambda: {"watches": {}}),
        )
        assert cli.cmd_watch_set_enabled(definition_id, False) == 0
        resource_key = "watch"

    payload = json.loads(capsys.readouterr().out)[resource_key]
    assert payload["enabled"] is False
    assert payload["redacted"] is True
    for owner_field in (
        "prompt",
        "schedule_type",
        "cron",
        "cwd",
        "agent_name",
        "session_id",
        "command",
        "message",
        "metadata",
    ):
        assert owner_field not in payload

    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(authorization_state="suspended_authorization")
        )
    store.load()
    if definition_type == "scheduled":
        assert cli.cmd_task_set_enabled(definition_id, True) == 0
    else:
        assert cli.cmd_watch_set_enabled(definition_id, True) == 0
    resumed = json.loads(capsys.readouterr().out)[resource_key]
    assert resumed["enabled"] is True
    assert resumed["authorization_state"] == "active"
    assert resumed["state"] == "enabled"


def test_resumed_sqlite_one_shot_watch_persists_reset_cycle_state(
    harness_fixture: HarnessFixture,
) -> None:
    watch_id = harness_fixture.definitions["watch"]
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == watch_id)
            .values(
                enabled=0,
                mode="once",
                last_started_at="2026-07-28T00:00:00Z",
                last_finished_at="2026-07-28T00:01:00Z",
                last_event_at="2026-07-28T00:01:00Z",
                last_exit_code=0,
                last_error=None,
            )
        )
    store = ManagedWatchStore.__new__(ManagedWatchStore)
    store.path = harness_fixture.store.db_path
    store._sqlite = harness_fixture.store
    store._signature = None
    store._watches = {}
    store.load()

    updated = store.set_enabled(watch_id, True, user_context=_context("owner"))

    assert updated.last_finished_at is None
    reloaded = harness_fixture.store.get_watch(watch_id)
    assert reloaded is not None
    for field in (
        "last_started_at",
        "last_finished_at",
        "last_event_at",
        "last_exit_code",
        "last_error",
    ):
        assert reloaded[field] is None


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


@pytest.mark.parametrize("definition_type", ["scheduled", "watch"])
def test_definition_update_reauthorizes_dependencies_and_principal_atomically(
    harness_fixture: HarnessFixture,
    definition_type: str,
) -> None:
    definition_id = harness_fixture.definitions[definition_type]
    owner = _context("owner")
    denied_skill_id = f"denied-{definition_type}-skill"
    allowed_skill_id = f"allowed-{definition_type}-skill"
    with harness_fixture.engine.begin() as connection:
        for resource_id, access_level, owner_user_id in (
            (denied_skill_id, "private", "another-owner"),
            (allowed_skill_id, "public", OWNER_SUBJECT),
        ):
            resource_access_service.ensure_resource_policy(
                connection,
                resource_kind="skill",
                resource_id=resource_id,
                organization_id=ORG_ID,
                owner_user_id=owner_user_id,
                access_level=access_level,
            )
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == definition_id)
            .values(
                metadata_json=json.dumps(
                    {"harness_execution_principal": {"principal_type": "trusted_local"}}
                )
            )
        )

    if definition_type == "scheduled":
        store = ScheduledTaskStore.__new__(ScheduledTaskStore)
        store.path = harness_fixture.store.db_path
        store._sqlite = harness_fixture.store
        store._signature = None
        store._tasks = {}
        store.load()
        definition = store.get_task(definition_id)
        assert definition is not None

        def update_definition(skill_id: str, prompt: str):
            return store.update_task(
                definition_id,
                name=definition.name,
                session_key=definition.session_key,
                session_id=definition.session_id,
                prompt=prompt,
                schedule_type=definition.schedule_type,
                agent_name=definition.agent_name,
                session_policy=definition.session_policy,
                post_to=definition.post_to,
                deliver_key=definition.deliver_key,
                cwd=definition.cwd,
                update_cwd=True,
                cron=definition.cron,
                run_at=definition.run_at,
                timezone_name=definition.timezone,
                metadata={
                    "harness_resources": [
                        {"resource_kind": "skill", "resource_id": skill_id}
                    ]
                },
                user_context=owner,
            )

        original_value = definition.prompt
    else:
        store = ManagedWatchStore.__new__(ManagedWatchStore)
        store.path = harness_fixture.store.db_path
        store._sqlite = harness_fixture.store
        store._signature = None
        store._watches = {}
        store.load()
        definition = store.get_watch(definition_id)
        assert definition is not None

        def update_definition(skill_id: str, prompt: str):
            return store.update_watch(
                definition_id,
                name=definition.name,
                session_key=definition.session_key,
                session_id=definition.session_id,
                command=definition.command,
                shell_command=definition.shell_command,
                prefix=definition.prefix,
                message=prompt,
                cwd=definition.cwd,
                mode=definition.mode,
                timeout_seconds=definition.timeout_seconds,
                lifetime_timeout_seconds=definition.lifetime_timeout_seconds,
                retry_exit_codes=definition.retry_exit_codes,
                retry_delay_seconds=definition.retry_delay_seconds,
                post_to=definition.post_to,
                deliver_key=definition.deliver_key,
                agent_name=definition.agent_name,
                session_policy=definition.session_policy,
                metadata={
                    "harness_resources": [
                        {"resource_kind": "skill", "resource_id": skill_id}
                    ]
                },
                user_context=owner,
            )

        original_value = definition.message

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_skill_access_forbidden",
    ):
        update_definition(denied_skill_id, "denied update")

    with harness_fixture.engine.connect() as connection:
        stored = connection.execute(
            select(run_definitions).where(run_definitions.c.id == definition_id)
        ).mappings().one()
        dependencies = [
            (str(row.resource_kind), str(row.resource_id))
            for row in connection.execute(
                select(
                    harness_definition_dependencies.c.resource_kind,
                    harness_definition_dependencies.c.resource_id,
                ).where(
                    harness_definition_dependencies.c.definition_id == definition_id
                )
            )
        ]
    assert (stored["prompt"] if definition_type == "scheduled" else stored["message"]) == original_value
    assert ("skill", denied_skill_id) not in dependencies

    updated = update_definition(allowed_skill_id, "authorized update")
    assert updated.authorization_state == "active"
    assert updated.metadata["harness_execution_principal"]["principal_type"] == "remote"
    assert updated.metadata["harness_execution_principal"]["subject"] == OWNER_SUBJECT
    with harness_fixture.engine.connect() as connection:
        stored = connection.execute(
            select(run_definitions).where(run_definitions.c.id == definition_id)
        ).mappings().one()
        dependencies = [
            (str(row.resource_kind), str(row.resource_id))
            for row in connection.execute(
                select(
                    harness_definition_dependencies.c.resource_kind,
                    harness_definition_dependencies.c.resource_id,
                ).where(
                    harness_definition_dependencies.c.definition_id == definition_id
                )
            )
        ]
    metadata = json.loads(stored["metadata_json"])
    assert metadata["harness_execution_principal"]["principal_type"] == "remote"
    assert ("skill", allowed_skill_id) in dependencies


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
        expected_definition_type="scheduled",
        engine=harness_fixture.engine,
    )
    assert authorized["session_key"] == "slack::channel::authorized"
    with harness_fixture.engine.connect() as connection:
        active = harness_auth.serialize_definition(
            editor,
            connection.execute(
                select(run_definitions).where(run_definitions.c.id == task_id)
            ).mappings().one(),
            connection=connection,
        )
    assert active["capabilities"]["can_run"] is True

    harness_auth.suspend_definition(task_id, engine=harness_fixture.engine)
    with harness_fixture.engine.connect() as connection:
        suspended = harness_auth.serialize_definition(
            editor,
            connection.execute(
                select(run_definitions).where(run_definitions.c.id == task_id)
            ).mappings().one(),
            connection=connection,
        )
    assert suspended["capabilities"]["can_run"] is False
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_definition_suspended",
    ):
        harness_auth.authorize_manual_run(
            editor,
            task_id,
            expected_definition_type="scheduled",
            engine=harness_fixture.engine,
        )

    with pytest.raises(harness_auth.HarnessAuthorizationError) as denied:
        harness_auth.authorize_manual_run(
            _context("editor", matching=False),
            task_id,
            expected_definition_type="scheduled",
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


@pytest.mark.parametrize(
    ("status", "can_cancel"),
    [
        ("queued", True),
        ("running", True),
        ("succeeded", False),
        ("failed", False),
        ("canceled", False),
    ],
)
def test_run_cancel_capability_requires_active_state(
    harness_fixture: HarnessFixture,
    status: str,
    can_cancel: bool,
) -> None:
    run_id = f"cancel-capability-{status}"
    run = harness_fixture.make_run(run_id, status=status)

    with harness_fixture.engine.connect() as connection:
        projected = harness_auth.serialize_run(
            _context("editor"),
            run,
            connection=connection,
            operation="list",
        )

    assert projected["capabilities"]["can_cancel"] is can_cancel


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
    harness_auth.authorize_manual_run(
        editor,
        task_id,
        expected_definition_type="scheduled",
        engine=harness_fixture.engine,
    )
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
            expected_definition_type="scheduled",
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
            expected_definition_type=(
                "scheduled" if definition_id == task_id else "watch"
            ),
            engine=harness_fixture.engine,
        )
        harness_auth.set_definition_enabled(
            editor,
            definition_id,
            True,
            expected_definition_type=(
                "scheduled" if definition_id == task_id else "watch"
            ),
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


def test_revocation_suppresses_pending_callback_without_rewriting_terminal_status(
    harness_fixture: HarnessFixture,
) -> None:
    editor = _context("editor")
    agent_id = "callback-agent"
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id=agent_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="public",
        )
    run_id = "completed-pending-callback"
    harness_fixture.make_run(
        run_id,
        dependencies=[{"resource_kind": "agent", "resource_id": agent_id}],
        activation_context=editor,
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(
                callback_session_id="callback-session",
                callback_status="pending",
            )
        )

    harness_fixture.set_policy("agent", agent_id, "private", revision=1)

    stored = harness_fixture.store.get_run(run_id)
    assert stored is not None
    assert stored["status"] == "succeeded"
    assert stored["callback_status"] == "suppressed_authorization"
    assert stored["output_quarantined"] is True
    assert stored["safe_error_code"] == "authorization_revoked"


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


def test_authorized_cancel_publishes_committed_run_update(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from storage import background

    run_id = "authorized-cancel-event"
    editor = _context("editor")
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=editor,
    )
    observed: list[tuple[str, str, bool]] = []

    def capture_publish(rows) -> None:
        stored = harness_fixture.store.get_run(run_id)
        assert stored is not None
        observed.extend(
            (
                str(row["id"]),
                str(stored["status"]),
                bool(stored["cancel_requested"]),
            )
            for row in rows
        )

    monkeypatch.setattr(background, "_publish_run_rows_updated", capture_publish)

    assert harness_auth.cancel_run(
        editor,
        run_id,
        engine=harness_fixture.engine,
    )
    assert observed == [(run_id, "canceled", True)]


def test_resume_keeps_incomplete_definition_suspended(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    editor = _context("editor")
    harness_auth.set_definition_enabled(
        editor,
        task_id,
        False,
        expected_definition_type="scheduled",
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
            expected_definition_type="scheduled",
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


def test_remote_owner_cannot_resume_projectless_definition(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == task_id)
            .values(
                project_id=None,
                enabled=0,
                authorization_state="suspended_authorization",
            )
        )

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_project_required",
    ):
        harness_auth.set_definition_enabled(
            _context("owner"),
            task_id,
            True,
            expected_definition_type="scheduled",
            engine=harness_fixture.engine,
        )

    stored = harness_fixture.store.get_scheduled_task(task_id)
    assert stored is not None
    assert stored["enabled"] is False
    assert stored["authorization_state"] == "suspended_authorization"


def test_definition_mutations_reject_ids_of_the_wrong_harness_kind(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    watch_id = harness_fixture.definitions["watch"]

    with pytest.raises(harness_auth.HarnessAuthorizationError) as run_error:
        harness_auth.authorize_manual_run(
            _context("editor"),
            watch_id,
            expected_definition_type="scheduled",
            engine=harness_fixture.engine,
        )
    with pytest.raises(harness_auth.HarnessAuthorizationError) as pause_error:
        harness_auth.set_definition_enabled(
            _context("editor"),
            watch_id,
            False,
            expected_definition_type="scheduled",
            engine=harness_fixture.engine,
        )
    with pytest.raises(harness_auth.HarnessAuthorizationError) as delete_error:
        harness_auth.remove_definition(
            _context("owner"),
            task_id,
            expected_definition_type="watch",
            engine=harness_fixture.engine,
        )

    assert run_error.value.hidden is True
    assert pause_error.value.hidden is True
    assert delete_error.value.hidden is True
    assert harness_fixture.store.get_watch(watch_id)["enabled"] is True
    assert harness_fixture.store.get_scheduled_task(task_id) is not None


def test_pause_results_rehydrate_task_and_watch_runtime_fields(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_store = ScheduledTaskStore()
    watch_store = ManagedWatchStore()
    task = task_store.add_task(
        name="Rehydrate task",
        session_key="slack::channel::C123",
        prompt="run",
        schedule_type="cron",
        cron="0 * * * *",
        timezone_name="UTC",
        metadata={"marker": "task-metadata"},
    )
    watch = watch_store.add_watch(
        name="Rehydrate watch",
        session_key="slack::channel::C123",
        command=["printf", "ready"],
        shell_command=None,
        prefix=None,
        message="deliver ready",
        cwd=None,
        mode="forever",
        timeout_seconds=30,
        lifetime_timeout_seconds=300,
        retry_exit_codes=[75],
        retry_delay_seconds=1,
        post_to=None,
        deliver_key=None,
        metadata={"marker": "watch-metadata"},
    )

    paused_task = task_store.set_enabled(
        task.id,
        False,
        user_context=trusted_local_context(),
    )
    paused_watch = watch_store.set_enabled(
        watch.id,
        False,
        user_context=trusted_local_context(),
    )

    assert paused_task.session_key == "slack::channel::C123"
    assert paused_task.prompt == "run"
    assert paused_task.metadata["marker"] == "task-metadata"
    assert paused_watch.session_key == "slack::channel::C123"
    assert paused_watch.command == ["printf", "ready"]
    assert paused_watch.message == "deliver ready"
    assert paused_watch.metadata["marker"] == "watch-metadata"


@pytest.mark.parametrize("definition_type", ["scheduled", "watch"])
def test_definition_registration_rolls_back_persisted_row_on_failure(
    tmp_path,
    monkeypatch,
    definition_type: str,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    def fail_registration(*_args, **_kwargs):
        raise harness_auth.HarnessAuthorizationError("registration_failed")

    monkeypatch.setattr(harness_auth, "register_definition", fail_registration)
    if definition_type == "scheduled":
        store = ScheduledTaskStore()
        with pytest.raises(harness_auth.HarnessAuthorizationError):
            store.add_task(
                name="Atomic task",
                session_key="slack::channel::C123",
                prompt="run",
                schedule_type="cron",
                cron="0 * * * *",
                timezone_name="UTC",
            )
        assert store.list_tasks() == []
        assert store._sqlite.list_scheduled_tasks() == []
    else:
        store = ManagedWatchStore()
        with pytest.raises(harness_auth.HarnessAuthorizationError):
            store.add_watch(
                name="Atomic watch",
                session_key="slack::channel::C123",
                command=["true"],
                shell_command=None,
                prefix=None,
                message="done",
                cwd=None,
                mode="forever",
                timeout_seconds=30,
                lifetime_timeout_seconds=300,
                retry_exit_codes=[],
                retry_delay_seconds=1,
                post_to=None,
                deliver_key=None,
            )
        assert store.list_watches() == []
        assert store._sqlite.list_watches() == []


def test_terminal_transition_does_not_overwrite_concurrent_quarantine(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    run_id = "atomic-terminal-revocation"
    harness_fixture.make_run(
        run_id,
        definition_id=harness_fixture.definitions["scheduled"],
        status="running",
    )
    request_store = TaskExecutionStore(
        harness_fixture.store.db_path.parent / "atomic-terminal-request-store"
    )
    request_store._sqlite = harness_fixture.store
    assert request_store.get_run(run_id) is not None
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    original_update = sqlite_store.update_run_status

    def revoke_before_update(*args, connection=None, **kwargs):
        assert connection is not None
        harness_auth.quarantine_runs_in_connection(connection, [run_id])
        return original_update(*args, connection=connection, **kwargs)

    monkeypatch.setattr(sqlite_store, "update_run_status", revoke_before_update)
    completed = request_store.complete(
        TaskExecutionRequest(
            id=run_id,
            request_type="scheduled",
            task_id=harness_fixture.definitions["scheduled"],
        ),
        ok=True,
    )

    stored = request_store.get_run(run_id)
    assert completed is False
    assert stored is not None
    assert stored["status"] == "canceled"
    assert stored["output_quarantined"] is True


def test_sync_agent_run_wait_projects_vault_output(
    harness_fixture: HarnessFixture,
) -> None:
    editor = _context("editor")
    vault_id = "sync-wait-vault"
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id=vault_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="public",
        )
    run_id = "sync-vault-run"
    harness_fixture.make_run(
        run_id,
        dependencies=[
            {"resource_kind": "vault_secret", "resource_id": vault_id}
        ],
        raw_sentinel=True,
        activation_context=editor,
    )

    request_store = TaskExecutionStore(
        harness_fixture.store.db_path.parent / "sync-wait-request-store"
    )
    request_store._sqlite = harness_fixture.store
    assert request_store.get_run(run_id) is not None
    projected = cli._wait_for_run_result(
        request_store,
        run_id,
        wait_timeout=0,
        user_context=editor,
    )

    serialized = json.dumps(projected)
    assert RAW_SENTINEL not in serialized
    assert projected["redaction"]["reason"] == "vault_resource_used"


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

    def revalidate(_execution_id, *, engine):
        nonlocal rechecks
        rechecks += 1
        if rechecks > 1:
            harness_auth.suspend_definition(watch.id, engine=engine)
            raise harness_auth.HarnessAuthorizationError("authorization_revoked")
        return trusted_local_context()

    monkeypatch.setattr(harness_auth, "revalidate_definition_for_execution", revalidate)
    monkeypatch.setattr(harness_auth, "revalidate_run_for_execution", revalidate)

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


@pytest.mark.parametrize(
    ("resource_kind", "resource_id", "queued", "terminal_code"),
    [
        ("skill", "watch-dynamic-skill", True, None),
        (
            "vault_secret",
            "watch-dynamic-vault",
            False,
            "vault_output_nonserializable",
        ),
    ],
)
def test_watch_waiter_run_records_dynamic_use_before_followup(
    harness_fixture: HarnessFixture,
    monkeypatch,
    resource_kind: str,
    resource_id: str,
    queued: bool,
    terminal_code: str | None,
) -> None:
    watch_id = harness_fixture.definitions["watch"]
    owner = _context("owner")
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind=resource_kind,
            resource_id=resource_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="public",
        )

    request_store = TaskExecutionStore(
        harness_fixture.store.db_path.parent / "watch-task-requests"
    )
    request_store._sqlite = harness_fixture.store
    request = request_store.start_watch_waiter_run(
        definition_id=watch_id,
        session_key="",
        session_id=None,
        post_to=None,
        deliver_key=None,
        agent_name=None,
        session_policy=None,
        metadata={},
        activation_context=owner,
    )
    assert request is not None
    monkeypatch.setenv("AVIBE_RUN_ID", request.id)
    monkeypatch.setenv("AVIBE_HARNESS_AUTHORIZATION", "1")
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.setattr(
        harness_auth,
        "get_cached_sqlite_engine",
        lambda: harness_fixture.engine,
    )

    execution_context = resource_access_service.resolve_resource_access_context()
    assert execution_context.subject == owner.subject
    with harness_fixture.engine.begin() as connection:
        assert resource_access_service.can_use_resource(
            execution_context,
            resource_kind,
            resource_id,
            connection=connection,
        )

    followup = "DYNAMIC-WATCH-OUTPUT-1058"
    assert request_store.queue_watch_followup(request, prompt=followup) is queued
    stored = request_store.get_run(request.id)
    assert stored is not None
    with harness_fixture.engine.connect() as connection:
        dependency = connection.execute(
            select(harness_run_dependencies).where(
                harness_run_dependencies.c.run_id == request.id,
                harness_run_dependencies.c.resource_kind == resource_kind,
                harness_run_dependencies.c.resource_id == resource_id,
            )
        ).mappings().one()
    assert dependency["access_mode"] == "use"
    provenance = stored["authorization_provenance"]
    assert followup in provenance["forbidden_content"]
    if queued:
        assert stored["status"] == "queued"
        assert stored["prompt"] == followup
    else:
        assert stored["status"] == "succeeded"
        assert stored["prompt"] is None
        assert stored["safe_error_code"] == terminal_code
        assert stored["output_classification"] == "vault_tainted"
        assert stored["callback_status"] == "suppressed_authorization"


def test_hidden_harness_ids_match_absent_not_found_payloads(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from storage import background
    from vibe import ui_server
    from vibe.ui_compat import g

    task_id = harness_fixture.definitions["scheduled"]
    watch_id = harness_fixture.definitions["watch"]
    run_id = "hidden-definition-run"
    harness_fixture.make_run(
        run_id,
        definition_id=task_id,
        activation_context=_context("editor"),
    )
    harness_fixture.set_policy("harness_task", task_id, "private", revision=2)
    harness_fixture.set_policy("harness_watch", watch_id, "private", revision=2)
    monkeypatch.setattr(
        background,
        "SQLiteBackgroundTaskStore",
        lambda: SQLiteBackgroundTaskStore(harness_fixture.store.db_path),
    )

    def call(path: str, route, resource_id: str):
        with ui_server.app.test_request_context(path):
            g.authorization_context = _context("editor")
            g.remote_session_payload = {
                "vibe_instance_authorization_revision": 1,
                "claims_issued_at": int(time.time()),
            }
            result = route(resource_id)
        if isinstance(result, tuple):
            response, status = result
            response.status_code = status
            return response
        return result

    for path_prefix, route, hidden_id, missing_id in (
        ("/api/harness/tasks", ui_server.harness_task_detail, task_id, "missing-task"),
        (
            "/api/harness/watches",
            ui_server.harness_watch_detail,
            watch_id,
            "missing-watch",
        ),
        ("/api/harness/runs", ui_server.harness_run_detail, run_id, "missing-run"),
    ):
        hidden = call(f"{path_prefix}/{hidden_id}", route, hidden_id)
        missing = call(f"{path_prefix}/{missing_id}", route, missing_id)
        assert hidden.status_code == missing.status_code == 404
        assert json.loads(hidden.body) == json.loads(missing.body)


@pytest.mark.parametrize(
    ("path", "route", "resource_id"),
    [
        ("/api/harness/tasks/task-authz", "harness_task_patch", "task-authz"),
        ("/api/harness/watches/watch-authz", "harness_watch_patch", "watch-authz"),
    ],
)
@pytest.mark.parametrize("enabled", ["false", 0, None, {}, []])
def test_harness_patch_rejects_non_boolean_enabled_values(
    path: str,
    route: str,
    resource_id: str,
    enabled: Any,
) -> None:
    from vibe import ui_server

    with ui_server.app.test_request_context(
        path,
        method="PATCH",
        json={"enabled": enabled},
    ):
        response, status = getattr(ui_server, route)(resource_id)

    assert status == 400
    assert json.loads(response.body) == {"ok": False, "code": "invalid_payload"}


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


def test_revisionless_remote_agent_principal_never_falls_back_to_trusted_local(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    principal = harness_auth.fail_closed_remote_principal(editor)
    session_id = "revisionless-remote-agent"
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, session_id)
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        _issue_test_agent_capability(
            monkeypatch,
            principal,
            session_id=session_id,
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.delenv(AVIBE_RUN_ID_ENV, raising=False)
    monkeypatch.delenv(AVIBE_HARNESS_AUTHORIZATION_ENV, raising=False)

    resolved = resource_access_service.resolve_resource_access_context()

    assert resolved.is_remote is True
    assert resolved.is_trusted_local is False
    assert resolved.has_role("viewer") is False
    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_owner_required",
    ):
        ScheduledTaskStore().add_task(
            name="Revisionless remote task",
            session_key="slack::channel::C123",
            prompt="must fail closed",
            schedule_type="cron",
            cron="0 * * * *",
            timezone_name="UTC",
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
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        _issue_test_agent_capability(
            monkeypatch,
            principal,
            session_id="remote-agent-session",
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
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


@pytest.mark.parametrize(
    ("command", "definition_id"),
    [
        ("task_add", None),
        ("task_update", "scheduled"),
        ("watch_add", None),
        ("watch_update", "watch"),
    ],
)
def test_denied_definition_write_never_reserves_session(
    harness_fixture: HarnessFixture,
    monkeypatch,
    capsys,
    command: str,
    definition_id: str | None,
) -> None:
    editor = _context("editor")
    principal = {
        "principal_type": "remote",
        "instance_id": editor.instance_id,
        "subject": editor.subject,
        "organization_member_id": editor.organization_member_id,
        "membership_version": editor.membership_version,
    }
    monkeypatch.setenv("AVIBE_SESSION_ID", "denied-definition-write-session")
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        _issue_test_agent_capability(
            monkeypatch,
            principal,
            session_id="denied-definition-write-session",
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)
    monkeypatch.setattr(
        cli,
        "_resolve_agent_for_target",
        lambda **_kwargs: SimpleNamespace(name=None),
    )
    task_store = ScheduledTaskStore.__new__(ScheduledTaskStore)
    task_store.path = harness_fixture.store.db_path
    task_store._sqlite = harness_fixture.store
    task_store._signature = None
    task_store._tasks = {}
    task_store.load()
    watch_store = ManagedWatchStore.__new__(ManagedWatchStore)
    watch_store.path = harness_fixture.store.db_path
    watch_store._sqlite = harness_fixture.store
    watch_store._signature = None
    watch_store._watches = {}
    watch_store.load()
    monkeypatch.setattr(cli, "_task_store", lambda: task_store)
    monkeypatch.setattr(cli, "_watch_store", lambda: watch_store)
    monkeypatch.setattr(
        cli,
        "_validate_existing_scope_id",
        lambda scope_id, *, help_command: cli._parse_validated_scope_id(
            scope_id,
            help_command=help_command,
        ),
    )
    reservations: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "_reserve_definition_session",
        lambda **kwargs: reservations.append(kwargs) or "unexpected-session",
    )
    scope_id = project_access_service.project_scope_id(harness_fixture.project_id)
    if command == "task_add":
        args = cli.build_parser().parse_args(
            [
                "task",
                "add",
                "--create-session",
                "--scope-id",
                scope_id,
                "--cron",
                "0 * * * *",
                "--message",
                "owner-only task",
            ]
        )
        result = cli.cmd_task_add(args)
    elif command == "task_update":
        args = cli.build_parser().parse_args(
            [
                "task",
                "update",
                harness_fixture.definitions[str(definition_id)],
                "--create-session",
                "--scope-id",
                scope_id,
                "--message",
                "owner-only task update",
            ]
        )
        result = cli.cmd_task_update(args)
    elif command == "watch_add":
        args = cli.build_parser().parse_args(
            [
                "watch",
                "add",
                "--create-session",
                "--scope-id",
                scope_id,
                "--shell",
                "true",
            ]
        )
        result = cli.cmd_watch_add(args)
    else:
        args = cli.build_parser().parse_args(
            [
                "watch",
                "update",
                harness_fixture.definitions[str(definition_id)],
                "--create-session",
                "--scope-id",
                scope_id,
                "--message",
                "owner-only watch update",
            ]
        )
        result = cli.cmd_watch_update(args)

    assert result == 1
    assert reservations == []
    expected_error = (
        "harness_owner_required"
        if command.endswith("add")
        else "harness_operation_forbidden"
    )
    assert expected_error in capsys.readouterr().err


def test_malformed_agent_principal_env_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)
    monkeypatch.delenv(AVIBE_AUTHORIZATION_CAPABILITY_ENV, raising=False)
    monkeypatch.setenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, "not-json")

    context = resource_access_service.resolve_resource_access_context()

    assert context.is_remote is True
    assert context.is_trusted_local is False


def test_stale_agent_principal_env_fails_closed(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    principal = {
        "principal_type": "remote",
        "instance_id": editor.instance_id,
        "subject": editor.subject,
    }
    capability = _issue_test_agent_capability(
        monkeypatch,
        principal,
        session_id="stale-agent-session",
    )
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
    monkeypatch.setenv("AVIBE_SESSION_ID", "stale-agent-session")
    monkeypatch.setenv(AVIBE_AUTHORIZATION_CAPABILITY_ENV, capability)
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)

    context = resource_access_service.resolve_resource_access_context()

    assert context.is_remote is True
    assert context.has_role("viewer") is False
    assert context.is_trusted_local is False


def test_agent_principal_capability_ignores_forged_raw_owner_principal(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    editor_principal = {
        "principal_type": "remote",
        "instance_id": editor.instance_id,
        "subject": editor.subject,
    }
    capability = _issue_test_agent_capability(
        monkeypatch,
        editor_principal,
        session_id="bound-agent-session",
    )
    monkeypatch.setenv("AVIBE_SESSION_ID", "bound-agent-session")
    monkeypatch.setenv(AVIBE_AUTHORIZATION_CAPABILITY_ENV, capability)
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        json.dumps(
            {
                "principal_type": "remote",
                "instance_id": editor.instance_id,
                "subject": OWNER_SUBJECT,
            }
        ),
    )
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)

    resolved = resource_access_service.resolve_resource_access_context()

    assert resolved.subject == editor.subject
    assert resolved.instance_role == "editor"
    assert resolved.has_role("owner") is False


def test_agent_principal_capability_is_bound_to_originating_session(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    capability = _issue_test_agent_capability(
        monkeypatch,
        {
            "principal_type": "remote",
            "instance_id": editor.instance_id,
            "subject": editor.subject,
        },
        session_id="originating-session",
    )
    monkeypatch.setenv("AVIBE_SESSION_ID", "forged-session")
    monkeypatch.setenv(AVIBE_AUTHORIZATION_CAPABILITY_ENV, capability)
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.delenv("AVIBE_RUN_ID", raising=False)
    monkeypatch.delenv("AVIBE_HARNESS_AUTHORIZATION", raising=False)

    resolved = resource_access_service.resolve_resource_access_context()

    assert resolved.is_remote is True
    assert resolved.has_role("viewer") is False
    assert resolved.is_trusted_local is False


def test_remote_agent_cannot_become_local_by_stripping_authorization_env(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from core import caller_context as caller_context_module

    editor = _context("editor")
    capability = _issue_test_agent_capability(
        monkeypatch,
        {
            "principal_type": "remote",
            "instance_id": editor.instance_id,
            "subject": editor.subject,
        },
        session_id="stripped-agent-session",
    )
    for key in (
        AVIBE_SESSION_ID_ENV,
        AVIBE_RUN_ID_ENV,
        AVIBE_HARNESS_AUTHORIZATION_ENV,
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        AVIBE_CALLER_SOURCE_ENV,
        AVIBE_CALLER_BACKEND_ENV,
        AVIBE_NATIVE_SESSION_ID_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        caller_context_module,
        "_ancestor_caller_environments",
        lambda: [
            {
                AVIBE_SESSION_ID_ENV: "stripped-agent-session",
                AVIBE_AUTHORIZATION_CAPABILITY_ENV: capability,
                AVIBE_CALLER_SOURCE_ENV: "agent_turn",
                AVIBE_CALLER_BACKEND_ENV: "codex",
            }
        ],
    )

    resolved = resource_access_service.resolve_resource_access_context()

    assert resolved.is_remote is True
    assert resolved.is_trusted_local is False
    assert resolved.subject == editor.subject
    assert resolved.instance_role == "editor"
    assert resolved.has_role("owner") is False


def test_remote_agent_run_capability_rejects_forged_run_id(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    editor = _context("editor")
    harness_fixture.make_run(
        "other-owner-run",
        status="queued",
        activation_context=_context("owner"),
    )
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, "bound-run-session")
    monkeypatch.setenv(AVIBE_RUN_ID_ENV, "other-owner-run")
    monkeypatch.setenv(AVIBE_HARNESS_AUTHORIZATION_ENV, "1")
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        issue_authorization_capability(
            {
                "principal_type": "remote",
                "instance_id": editor.instance_id,
                "subject": editor.subject,
            },
            session_id="bound-run-session",
            run_id="originating-editor-run",
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.setattr(
        internal_client,
        "resolve_authorization_principal_capability",
        resolve_authorization_capability,
    )

    resolved = resource_access_service.resolve_resource_access_context()

    assert resolved.is_remote is True
    assert resolved.is_trusted_local is False
    assert resolved.has_role("viewer") is False


def test_stripped_agent_env_records_and_commits_vault_dependency(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from core import caller_context as caller_context_module

    run_id = "stripped-vault-dependency-run"
    vault_id = "vault-stripped-dependency"
    harness_fixture.make_run(
        run_id,
        status="running",
        activation_context=_context("owner"),
    )
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id=vault_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
    for key in (
        AVIBE_SESSION_ID_ENV,
        AVIBE_RUN_ID_ENV,
        AVIBE_HARNESS_AUTHORIZATION_ENV,
        AVIBE_AUTHORIZATION_PRINCIPAL_ENV,
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        caller_context_module,
        "_ancestor_caller_environments",
        lambda: [
            {
                AVIBE_SESSION_ID_ENV: "stripped-vault-session",
                AVIBE_RUN_ID_ENV: run_id,
                AVIBE_HARNESS_AUTHORIZATION_ENV: "1",
            }
        ],
    )

    with harness_fixture.engine.connect() as connection:
        connection.execute(select(agent_runs.c.id).where(agent_runs.c.id == run_id)).one()
        assert resource_access_service.can_use_resource(
            _context("owner"),
            "vault_secret",
            vault_id,
            connection=connection,
        )

    with harness_fixture.engine.connect() as connection:
        dependency = connection.execute(
            select(harness_run_dependencies).where(
                harness_run_dependencies.c.run_id == run_id,
                harness_run_dependencies.c.resource_kind == "vault_secret",
                harness_run_dependencies.c.resource_id == vault_id,
            )
        ).mappings().one()
        run = connection.execute(
            select(agent_runs).where(agent_runs.c.id == run_id)
        ).mappings().one()
    assert dependency["access_mode"] == "use"
    assert run["output_classification"] == "vault_tainted"
    assert run["member_safe_json"] is None
    assert run["callback_status"] == "suppressed_authorization"


def test_denied_resource_probe_does_not_record_run_dependency(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    run_id = "denied-resource-probe"
    vault_id = "private-denied-vault"
    harness_fixture.make_run(
        run_id,
        status="running",
        activation_context=_context("editor"),
    )
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id=vault_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
    monkeypatch.setenv(AVIBE_RUN_ID_ENV, run_id)
    monkeypatch.setenv(AVIBE_HARNESS_AUTHORIZATION_ENV, "1")

    with harness_fixture.engine.begin() as connection:
        assert not resource_access_service.can_use_resource(
            _context("editor"),
            "vault_secret",
            vault_id,
            connection=connection,
        )
        dependencies = connection.execute(
            select(harness_run_dependencies).where(
                harness_run_dependencies.c.run_id == run_id,
                harness_run_dependencies.c.resource_kind == "vault_secret",
                harness_run_dependencies.c.resource_id == vault_id,
            )
        ).all()
        run = connection.execute(
            select(agent_runs).where(agent_runs.c.id == run_id)
        ).mappings().one()

    assert dependencies == []
    assert run["output_classification"] != "vault_tainted"


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


def test_remote_entitlement_from_previous_instance_fails_closed(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    run_id = "previous-instance-entitlement"
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=_context("editor"),
    )
    monkeypatch.setattr(
        harness_auth,
        "_current_device_instance_id",
        lambda: "replacement-instance",
    )

    with pytest.raises(
        harness_auth.HarnessAuthorizationError,
        match="harness_entitlement_instance_changed",
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


def test_entitlement_change_immediately_suspends_principal_work(
    harness_fixture: HarnessFixture,
) -> None:
    editor = _context("editor")
    task_id = harness_fixture.definitions["scheduled"]
    harness_auth.set_definition_enabled(
        editor,
        task_id,
        False,
        expected_definition_type="scheduled",
        engine=harness_fixture.engine,
    )
    harness_auth.set_definition_enabled(
        editor,
        task_id,
        True,
        expected_definition_type="scheduled",
        engine=harness_fixture.engine,
    )
    run_id = "entitlement-change-queued-run"
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=editor,
    )

    downgraded = replace(
        editor,
        instance_role="viewer",
        membership_version="membership-v2",
        claims_issued_at=editor.claims_issued_at + 1,
    )
    harness_auth.mirror_remote_principal(
        downgraded,
        {
            "vibe_instance_authorization_revision": 2,
            "claims_issued_at": downgraded.claims_issued_at,
        },
        engine=harness_fixture.engine,
    )

    definition = harness_fixture.store.get_scheduled_task(task_id)
    run = harness_fixture.store.get_run(run_id)
    assert definition is not None
    assert definition["enabled"] is False
    assert definition["authorization_state"] == "suspended_authorization"
    assert run is not None
    assert run["status"] == "canceled"
    assert run["output_quarantined"] is True
    assert run["safe_error_code"] == "authorization_revoked"


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


def test_archiving_project_suspends_definitions_and_quarantines_active_runs(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    run_id = "archived-project-run"
    harness_fixture.make_run(
        run_id,
        definition_id=task_id,
        status="running",
        activation_context=_context("editor"),
    )

    with harness_fixture.engine.begin() as connection:
        projects_service.archive_project(
            connection,
            harness_fixture.project_id,
            authorization_context=trusted_local_context(),
        )

    definition = harness_fixture.store.get_scheduled_task(task_id)
    run = harness_fixture.store.get_run(run_id)
    assert definition is not None
    assert definition["enabled"] is False
    assert definition["authorization_state"] == "suspended_authorization"
    assert run is not None
    assert run["status"] == "canceled"
    assert run["cancel_requested"] is True
    assert run["output_quarantined"] is True
    assert run["safe_error_code"] == "authorization_revoked"


def test_authorized_id_queries_exclude_archived_projects_for_owners(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    run_id = "archived-project-query-run"
    harness_fixture.make_run(
        run_id,
        definition_id=task_id,
        status="running",
        activation_context=_context("editor"),
    )

    with harness_fixture.engine.begin() as connection:
        projects_service.archive_project(
            connection,
            harness_fixture.project_id,
            authorization_context=trusted_local_context(),
        )

    with harness_fixture.engine.connect() as connection:
        for context in (trusted_local_context(), _context("owner")):
            definition_ids = set(
                connection.execute(
                    harness_auth.authorized_definition_ids_query(
                        context,
                        connection=connection,
                    )
                ).scalars()
            )
            run_ids = set(
                connection.execute(
                    harness_auth.authorized_run_ids_query(
                        context,
                        connection=connection,
                    )
                ).scalars()
            )
            assert task_id not in definition_ids
            assert run_id not in run_ids


def test_deleted_resource_policy_unconditionally_revokes_dependent_work(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    run_id = "deleted-skill-run"
    completed_run_id = "deleted-skill-completed-run"
    skill_id = "deleted-skill"
    now = "2026-07-28T00:02:00+00:00"
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="skill",
            resource_id=skill_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            owner_email="owner@example.com",
            access_level="private",
        )
        connection.execute(
            harness_definition_dependencies.insert().values(
                definition_id=task_id,
                resource_kind="skill",
                resource_id=skill_id,
                access_mode="use",
                created_at=now,
            )
        )
    harness_fixture.make_run(
        run_id,
        definition_id=task_id,
        status="running",
        dependencies=[{"resource_kind": "skill", "resource_id": skill_id}],
        activation_context=_context("owner"),
    )
    harness_fixture.make_run(
        completed_run_id,
        definition_id=task_id,
        status="succeeded",
        dependencies=[{"resource_kind": "skill", "resource_id": skill_id}],
        activation_context=_context("owner"),
    )
    harness_fixture.make_safe(completed_run_id)

    with harness_fixture.engine.begin() as connection:
        assert resource_access_service.delete_resource_policy(
            connection,
            "skill",
            skill_id,
        )

    definition = harness_fixture.store.get_scheduled_task(task_id)
    run = harness_fixture.store.get_run(run_id)
    completed_run = harness_fixture.store.get_run(completed_run_id)
    assert definition is not None
    assert definition["enabled"] is False
    assert definition["authorization_state"] == "suspended_authorization"
    assert run is not None
    assert run["status"] == "canceled"
    assert run["cancel_requested"] is True
    assert run["output_quarantined"] is True
    assert run["safe_error_code"] == "authorization_revoked"
    assert completed_run is not None
    assert completed_run["status"] == "succeeded"
    assert completed_run["output_quarantined"] is True
    assert completed_run["member_safe"] is None
    assert completed_run["safe_error_code"] == "authorization_revoked"


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
    assert sse_payload is None

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


def test_sse_projection_drops_events_when_run_access_is_revoked(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from vibe import ui_server

    run_id = "sse-projection-revoked"
    harness_fixture.make_run(run_id)
    harness_fixture.make_safe(run_id, "safe before revocation")

    def revoked(*_args, **_kwargs):
        raise harness_auth.HarnessAuthorizationError(
            "harness_definition_access_forbidden",
            hidden=True,
        )

    monkeypatch.setattr(harness_auth, "serialize_run", revoked)
    context = _context("viewer")
    assert (
        ui_server._workbench_event_payload_for_context(
            context,
            "runs.updated",
            json.dumps({"data": {"run_id": run_id, "status": "succeeded"}}),
        )
        is None
    )
    assert (
        ui_server._workbench_event_payload_for_context(
            context,
            "message.new",
            json.dumps(
                {
                    "data": {
                        "id": "revoked-message",
                        "type": "result",
                        "text": "safe before revocation",
                        "metadata": {"harness_run_id": run_id},
                    }
                }
            ),
        )
        is None
    )


def test_private_run_is_owner_searchable_without_member_unread_leaks(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from vibe import ui_server
    from vibe.ui_compat import g

    run_id = "private-search-unread-run"
    vault_run_id = "vault-search-run"
    vault_search_sentinel = "VAULT-SEARCH-SENTINEL-1058"
    scope_id = project_access_service.project_scope_id(harness_fixture.project_id)
    with harness_fixture.engine.begin() as connection:
        session = create_session(
            connection,
            scope_id=scope_id,
            agent_backend="codex",
        )
    harness_fixture.make_run(
        run_id,
        definition_id=harness_fixture.definitions["scheduled"],
        session_id=session["id"],
    )
    harness_fixture.set_policy(
        "harness_task",
        harness_fixture.definitions["scheduled"],
        "private",
        revision=2,
    )
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id="vault-search-secret",
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
        messages_service.append(
            connection,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text=RAW_SENTINEL,
            metadata={"harness_run_id": run_id},
            native_message_id=f"agent_run:{run_id}",
        )
    harness_fixture.make_run(
        vault_run_id,
        session_id=session["id"],
        dependencies=[
            {
                "resource_kind": "vault_secret",
                "resource_id": "vault-search-secret",
            }
        ],
    )
    with harness_fixture.engine.begin() as connection:
        messages_service.append(
            connection,
            scope_id=scope_id,
            session_id=session["id"],
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text=vault_search_sentinel,
            metadata={"harness_run_id": vault_run_id},
            native_message_id=f"agent_run:{vault_run_id}",
            read_at="2026-07-28T00:00:00Z",
        )

    monkeypatch.setattr(ui_server, "_projects_engine", lambda: harness_fixture.engine)
    with ui_server.app.test_request_context(f"/api/search/messages?q={RAW_SENTINEL}"):
        g.authorization_context = trusted_local_context()
        owner_search = json.loads(ui_server.search_messages_list().body)
    assert owner_search["total"] == 1
    assert owner_search["sessions"][0]["session_id"] == session["id"]
    with ui_server.app.test_request_context(f"/api/search/messages?q={RAW_SENTINEL}"):
        g.authorization_context = _context("owner")
        remote_owner_search = json.loads(ui_server.search_messages_list().body)
    assert remote_owner_search["total"] == 1
    with ui_server.app.test_request_context(
        f"/api/search/messages?q={vault_search_sentinel}"
    ):
        g.authorization_context = trusted_local_context()
        vault_search = json.loads(ui_server.search_messages_list().body)
    assert vault_search == {"session_count": 0, "sessions": [], "total": 0}

    editor = _context("editor")
    with ui_server.app.test_request_context(f"/api/search/messages?q={RAW_SENTINEL}"):
        g.authorization_context = editor
        editor_search = json.loads(ui_server.search_messages_list().body)
    assert editor_search == {"session_count": 0, "sessions": [], "total": 0}

    with ui_server.app.test_request_context("/api/inbox?unread_only=1"):
        g.authorization_context = editor
        inbox = json.loads(ui_server.inbox_list().body)
    assert inbox["sessions"] == []
    assert inbox["unread_by_session"] == {}
    assert inbox["unread_total"] == 0

    unread_event = ui_server._workbench_event_payload_for_context(
        editor,
        "inbox.unread.changed",
        json.dumps(
            {
                "type": "inbox.unread.changed",
                "data": {
                    "unread_counts": {scope_id: 1},
                    "unread_by_session": {session["id"]: 1},
                },
            }
        ),
    )
    unread_data = json.loads(unread_event)["data"]
    assert unread_data["unread_counts"] == {}
    assert unread_data["unread_by_session"] == {}

    session_event = ui_server._workbench_event_payload_for_context(
        editor,
        "inbox.session.updated",
        json.dumps(
            {
                "type": "inbox.session.updated",
                "data": {
                    "session_id": session["id"],
                    "scope_id": scope_id,
                    "preview_text": RAW_SENTINEL,
                    "unread_count": 1,
                    "unread": True,
                    "_harness_originated": True,
                    "_harness_run_id": run_id,
                },
            }
        ),
    )
    session_data = json.loads(session_event)["data"]
    assert RAW_SENTINEL not in session_event
    assert session_data["unread_count"] == 0
    assert session_data["unread"] is False


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
    hidden_project_path = harness_fixture.store.db_path.parent.parent / "hidden-project"
    hidden_project_path.mkdir()
    with harness_fixture.engine.begin() as connection:
        hidden_project = projects_service.create_project(
            connection,
            str(hidden_project_path),
            display_name="Hidden Project",
        )
        project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": hidden_project["id"],
                "organization_id": ORG_ID,
                "revision": 1,
                "mode": "restricted",
                "bindings": [],
            },
        )
    harness_fixture.store.enqueue_run(
        {
            "id": "hidden-other-project-run",
            "request_type": "agent_run",
            "project_id": hidden_project["id"],
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
                },
            },
        },
        activation_context=trusted_local_context(),
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


def test_member_safe_classifier_rejects_embedded_single_word_prompt(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "single-word-member-output"
    harness_fixture.make_run(run_id)
    _replace_run_prompt_provenance(harness_fixture, run_id, "status")

    assert not harness_auth.record_member_safe_output(
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
            "scope_id": "avibe::project::private",
            "session_id": "private-session",
            "platform": "avibe",
            "author": "agent",
            "author_id": "private-agent-id",
            "author_name": "Authorized Agent",
            "source": "agent",
            "type": "result",
            "text": "raw final",
            "content": {"text": "raw final"},
            "metadata": {
                "harness_run_id": run_id,
                "backend": "private-backend",
                "causation_id": "private-causation",
            },
            "native_message_id": f"agent_run:{run_id}",
            "parent_native_message_id": "private-parent",
            "source_session_id": "private-source-session",
            "created_at": "2026-07-28T00:00:00Z",
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
    member_result = member_rows[-1]
    assert member_result["native_message_id"] is None
    assert member_result["parent_native_message_id"] is None
    assert member_result["metadata"] == {}
    assert "author_id" not in member_result
    assert "source_session_id" not in member_result
    assert member_result["content"]["run_id"] == run_id
    assert [row["text"] for row in owner_rows] == [
        "private trigger",
        "raw progress",
        "raw final",
    ]


def test_transcript_requires_content_access_to_every_coalesced_run(
    harness_fixture: HarnessFixture,
) -> None:
    primary_id = "coalesced-transcript-primary"
    child_id = "coalesced-transcript-child"
    agent_id = "coalesced-private-agent"
    harness_fixture.make_run(primary_id)
    harness_fixture.make_run(
        child_id,
        dependencies=[{"resource_kind": "agent", "resource_id": agent_id}],
    )
    harness_fixture.make_safe(primary_id, "shared member-safe output")
    harness_fixture.make_safe(child_id, "shared member-safe output")
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="agent",
            resource_id=agent_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
    messages = [
        {
            "id": "coalesced-result",
            "author": "agent",
            "source": "agent",
            "type": "result",
            "text": "raw shared output",
            "content": {"text": "raw shared output"},
            "metadata": {
                "harness_run_id": primary_id,
                "_web_push_harness_run_ids": [primary_id, child_id],
            },
        }
    ]

    with harness_fixture.engine.connect() as connection:
        viewer_rows = harness_auth.project_transcript_messages(
            _context("viewer"),
            messages,
            connection=connection,
        )
        owner_rows = harness_auth.project_transcript_messages(
            trusted_local_context(),
            messages,
            connection=connection,
        )

    assert viewer_rows == []
    assert owner_rows[0]["text"] == "raw shared output"


def test_worker_failure_suppresses_callback_when_error_repeats_owner_input(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "unsafe-worker-failure"
    editor = _context("editor")
    harness_fixture.make_run(
        run_id,
        status="running",
        raw_sentinel=True,
        activation_context=editor,
    )
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(
                callback_session_id="private-callback-session",
                callback_status="pending",
            )
        )
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    request_store = TaskExecutionStore(
        harness_fixture.store.db_path.parent / "failure-callback-requests"
    )
    request_store._sqlite = harness_fixture.store

    assert request_store.complete(
        TaskExecutionRequest.from_dict(run),
        ok=False,
        error=f"backend failed while handling {RAW_SENTINEL}",
    )

    failed = harness_fixture.store.get_run(run_id)
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["output_classification"] == "unsafe"
    assert failed["member_safe"] is None
    assert failed["callback_status"] == "suppressed_authorization"


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


def test_member_safe_classifier_rejects_multiline_command_output(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "multiline-command-output"
    command = "printf private\necho owner-only"
    harness_fixture.make_run(run_id)
    with harness_fixture.engine.begin() as connection:
        run = connection.execute(
            select(agent_runs).where(agent_runs.c.id == run_id)
        ).mappings().one()
        provenance = json.loads(run["authorization_provenance_json"])
        provenance["forbidden_content"] = harness_auth._forbidden_manifest(
            {"shell_command": command, "command": ["sh", "line one\nline two"]}
        )
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(authorization_provenance_json=json.dumps(provenance))
        )

    assert command in provenance["forbidden_content"]
    assert not harness_auth.record_member_safe_output(
        run_id,
        {"text": f"Shell trace:\n{command}", "status": "complete"},
        engine=harness_fixture.engine,
    )


def test_member_safe_classifier_allows_default_utc_timezone_mentions(
    harness_fixture: HarnessFixture,
) -> None:
    run_id = "default-utc-output"
    harness_fixture.make_run(
        run_id,
        definition_id=harness_fixture.definitions["scheduled"],
    )

    assert harness_auth.record_member_safe_output(
        run_id,
        {
            "text": "Completed at 2026-07-28T19:00:00Z (UTC).",
            "status": "complete",
        },
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


def test_coalesced_member_safe_output_is_all_or_nothing(
    harness_fixture: HarnessFixture,
) -> None:
    safe_run_id = "coalesced-safe-run"
    vault_run_id = "coalesced-vault-run"
    vault_id = "coalesced-output-vault"
    with harness_fixture.engine.begin() as connection:
        resource_access_service.ensure_resource_policy(
            connection,
            resource_kind="vault_secret",
            resource_id=vault_id,
            organization_id=ORG_ID,
            owner_user_id=OWNER_SUBJECT,
            access_level="private",
        )
    harness_fixture.make_run(safe_run_id)
    harness_fixture.make_safe(safe_run_id, "previous safe output")
    harness_fixture.make_run(
        vault_run_id,
        dependencies=[
            {"resource_kind": "vault_secret", "resource_id": vault_id}
        ],
    )

    assert not harness_auth.record_coalesced_member_safe_output(
        [safe_run_id, vault_run_id],
        {"text": "shared output", "status": "complete"},
        engine=harness_fixture.engine,
    )

    for run_id in (safe_run_id, vault_run_id):
        stored = harness_fixture.store.get_run(run_id)
        assert stored is not None
        assert stored["member_safe"] is None
        assert stored["output_quarantined"] is True
        assert stored["callback_status"] == "suppressed_authorization"
    safe = harness_fixture.store.get_run(safe_run_id)
    assert safe is not None
    assert safe["safe_error_code"] == "vault_resource_used"


def test_coalesced_member_safe_output_persists_only_after_full_preflight(
    harness_fixture: HarnessFixture,
) -> None:
    run_ids = ["coalesced-safe-one", "coalesced-safe-two"]
    for run_id in run_ids:
        harness_fixture.make_run(run_id)

    assert harness_auth.record_coalesced_member_safe_output(
        run_ids,
        {"text": "shared safe output", "status": "complete"},
        engine=harness_fixture.engine,
    )

    for run_id in run_ids:
        stored = harness_fixture.store.get_run(run_id)
        assert stored is not None
        assert stored["member_safe"]["text"] == "shared safe output"
        assert stored["output_classification"] == "member_safe"
        assert stored["output_quarantined"] is False


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

    requests = [
        request_store.enqueue_agent_run(
            session_id=session_id,
            message=message,
            metadata={"workbench_queue_holds_run": True},
            activation_context=editor_context,
        )
        for message in (
            "revoked primary message",
            "valid child message",
        )
    ]
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    run_ids = [request.id for request in requests]
    primary = sqlite_store.get_run(run_ids[0])
    assert primary is not None
    revoked_provenance = dict(primary["authorization_provenance"])
    revoked_principal = dict(revoked_provenance["execution_principal"])
    revoked_principal["subject"] = "missing-primary"
    revoked_provenance["execution_principal"] = revoked_principal
    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_ids[0])
            .values(authorization_provenance_json=json.dumps(revoked_provenance))
        )
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


def test_deleted_definition_retains_acl_for_historical_run_reads(
    harness_fixture: HarnessFixture,
) -> None:
    task_id = harness_fixture.definitions["scheduled"]
    run_id = "completed-deleted-private-definition"
    harness_fixture.make_run(run_id, definition_id=task_id)
    harness_fixture.set_policy("harness_task", task_id, "private", revision=2)
    with harness_fixture.engine.begin() as connection:
        project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": harness_fixture.project_id,
                "organization_id": ORG_ID,
                "revision": 2,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": GROUP_ID,
                        "access_role": "owner",
                    }
                ],
            },
        )
    harness_auth.remove_definition(
        _context("owner"),
        task_id,
        expected_definition_type="scheduled",
        engine=harness_fixture.engine,
    )

    unrelated_owner = AuthorizationContext(
        instance_role="owner",
        subject="unrelated-instance-owner",
        email="unrelated-owner@example.com",
        instance_id="instance-harness",
        instance_access_source="owner",
        organization_id=ORG_ID,
        organization_member_id="member-unrelated-owner",
        organization_role="member",
        group_ids=frozenset({GROUP_ID}),
        membership_version="membership-v1",
        claims_issued_at=int(time.time()),
        is_remote=True,
    )
    run = harness_fixture.store.get_run(run_id)
    assert run is not None
    with harness_fixture.engine.connect() as connection:
        assert resource_access_service.get_resource_policy(
            "harness_task",
            task_id,
            connection=connection,
        ) is not None
        for operation in ("detail", "raw"):
            with pytest.raises(harness_auth.HarnessAuthorizationError):
                harness_auth.authorize_run(
                    unrelated_owner,
                    run,
                    operation,
                    connection=connection,
                )
        harness_auth.authorize_run(
            _context("owner"),
            run,
            "raw",
            connection=connection,
        )


def test_denied_project_and_session_probes_are_not_recorded_as_run_dependencies(
    harness_fixture: HarnessFixture,
    monkeypatch,
    tmp_path,
) -> None:
    editor = _context("editor")
    run_id = "project-enumeration-dependency"
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=editor,
    )
    denied_project_dir = tmp_path / "denied-project"
    denied_project_dir.mkdir()
    with harness_fixture.engine.begin() as connection:
        denied_project = projects_service.create_project(
            connection,
            str(denied_project_dir),
            display_name="Denied Harness Project",
        )
        project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": denied_project["id"],
                "organization_id": ORG_ID,
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": "grp-denied",
                        "access_role": "editor",
                    }
                ],
            },
        )
        denied_session_id = "denied-project-session"
        now = "2026-07-28T00:00:00+00:00"
        connection.execute(
            agent_sessions.insert().values(
                id=denied_session_id,
                scope_id=project_access_service.project_scope_id(denied_project["id"]),
                agent_backend="codex",
                agent_variant="default",
                session_anchor="anchor-denied-project-session",
                native_session_id="",
                title="Denied Project Session",
                status="active",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )

    monkeypatch.setenv(AVIBE_RUN_ID_ENV, run_id)
    monkeypatch.setenv(AVIBE_HARNESS_AUTHORIZATION_ENV, "1")
    with harness_fixture.engine.begin() as connection:
        assert (
            project_access_service.get_effective_project_role(
                connection,
                editor,
                denied_project["id"],
            )
            is None
        )
        assert (
            project_access_service.get_effective_session_role(
                connection,
                editor,
                denied_session_id,
            )
            is None
        )
    with harness_fixture.engine.connect() as connection:
        assert (
            connection.execute(
                select(harness_run_dependencies).where(
                    harness_run_dependencies.c.run_id == run_id,
                    harness_run_dependencies.c.resource_kind == "project",
                    harness_run_dependencies.c.resource_id == denied_project["id"],
                )
            ).first()
            is None
        )
        assert (
            connection.execute(
                select(harness_run_dependencies).where(
                    harness_run_dependencies.c.run_id == run_id,
                    harness_run_dependencies.c.resource_kind == "session",
                    harness_run_dependencies.c.resource_id == denied_session_id,
                )
            ).first()
            is None
        )

    harness_auth.revalidate_run_for_execution(
        run_id,
        engine=harness_fixture.engine,
    )


def test_read_only_project_and_session_dependencies_remain_executable(
    harness_fixture: HarnessFixture,
    monkeypatch,
    tmp_path,
) -> None:
    editor = _context("editor")
    run_id = "read-only-project-dependency"
    harness_fixture.make_run(
        run_id,
        status="queued",
        activation_context=editor,
    )
    read_project_dir = tmp_path / "read-project"
    read_project_dir.mkdir()
    with harness_fixture.engine.begin() as connection:
        read_project = projects_service.create_project(
            connection,
            str(read_project_dir),
            display_name="Read-only Harness Project",
        )
        project_access_service.apply_project_access_intent(
            connection,
            {
                "project_id": read_project["id"],
                "organization_id": ORG_ID,
                "revision": 1,
                "mode": "restricted",
                "bindings": [
                    {
                        "principal_kind": "organization_group",
                        "principal_value": GROUP_ID,
                        "access_role": "viewer",
                    }
                ],
            },
        )
        session = create_session(
            connection,
            scope_id=project_access_service.project_scope_id(read_project["id"]),
            agent_backend="codex",
        )

    monkeypatch.setenv(AVIBE_RUN_ID_ENV, run_id)
    monkeypatch.setenv(AVIBE_HARNESS_AUTHORIZATION_ENV, "1")
    with harness_fixture.engine.begin() as connection:
        assert project_access_service.can_read_project(
            connection,
            editor,
            read_project["id"],
        )
        assert not project_access_service.can_chat_project(
            connection,
            editor,
            read_project["id"],
        )
        assert (
            project_access_service.get_effective_session_role(
                connection,
                editor,
                session["id"],
            )
            == "viewer"
        )

    with harness_fixture.engine.connect() as connection:
        dependencies = {
            (str(row.resource_kind), str(row.resource_id)): str(row.access_mode)
            for row in connection.execute(
                select(harness_run_dependencies).where(
                    harness_run_dependencies.c.run_id == run_id,
                    harness_run_dependencies.c.resource_id.in_(
                        [read_project["id"], session["id"]]
                    ),
                )
            )
        }
    assert dependencies == {
        ("project", read_project["id"]): "read",
        ("session", session["id"]): "read",
    }

    context = harness_auth.revalidate_run_for_execution(
        run_id,
        engine=harness_fixture.engine,
    )
    assert context.subject == editor.subject


def test_async_agent_monitor_cancels_live_turn_and_every_coalesced_run(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    task_store = ScheduledTaskStore()
    request_store = TaskExecutionStore()
    session_id = "session-async-authorization-monitor"
    requests = [
        request_store.enqueue_agent_run(
            session_id=session_id,
            message=f"coalesced message {index}",
            metadata={"workbench_queue_holds_run": True},
        )
        for index in range(2)
    ]
    sqlite_store = request_store._sqlite
    assert sqlite_store is not None
    run_ids = [request.id for request in requests]
    assert sqlite_store.claim_queued_runs_for_workbench(run_ids) == run_ids
    primary = sqlite_store.get_run(run_ids[0])
    assert primary is not None

    canceled = asyncio.Event()

    async def cancel_turn(active_session_id: str) -> dict[str, Any]:
        assert active_session_id == session_id
        canceled.set()
        return {"ok": True, "status": "cancel_requested"}

    context = SimpleNamespace(
        platform_specific={
            "task_execution_id": run_ids[0],
            "coalesced_queue": {"execution_ids": run_ids},
        }
    )
    turn_manager = SimpleNamespace(
        in_flight={session_id: SimpleNamespace(context=context)},
        cancel=cancel_turn,
    )
    service = ScheduledTaskService(
        controller=SimpleNamespace(session_turns=turn_manager),
        store=task_store,
        request_store=request_store,
    )

    async def accepted_agent_run(**_kwargs):
        return AgentRunExecutionResult(error=None, complete_on_return=False)

    service._execute_agent_run = accepted_agent_run
    rechecks: dict[str, int] = {}

    def revalidate(run_id: str, *, engine):
        del engine
        rechecks[run_id] = rechecks.get(run_id, 0) + 1
        if run_id == run_ids[1] and rechecks[run_id] > 1:
            raise harness_auth.HarnessAuthorizationError("authorization_revoked")
        return trusted_local_context()

    monkeypatch.setattr(harness_auth, "revalidate_run_for_execution", revalidate)

    async def exercise() -> None:
        execution = asyncio.create_task(
            service._execute_claimed_request(TaskExecutionRequest.from_dict(primary))
        )
        await execution
        assert run_ids[0] in service._authorization_monitors
        await asyncio.wait_for(canceled.wait(), timeout=3)
        for _ in range(20):
            if run_ids[0] not in service._authorization_monitors:
                break
            await asyncio.sleep(0.01)
        assert run_ids[0] not in service._authorization_monitors

    asyncio.run(exercise())
    for run_id in run_ids:
        stored = sqlite_store.get_run(run_id)
        assert stored is not None
        assert stored["status"] == "canceled"
        assert stored["output_quarantined"] is True
        assert stored["metadata"]["workbench_queue_holds_run"] is False
        assert "coalesced_into_run_id" not in stored["metadata"]


def test_remote_harness_cli_reads_and_cancel_use_current_authorization(
    harness_fixture: HarnessFixture,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    task_store = ScheduledTaskStore(tmp_path / "cli-tasks.json")
    task_store._sqlite = harness_fixture.store
    task_store.load()
    watch_store = ManagedWatchStore(tmp_path / "cli-watches.json")
    watch_store._sqlite = harness_fixture.store
    watch_store.load()
    request_store = TaskExecutionStore(tmp_path / "cli-runs")
    request_store._sqlite = harness_fixture.store
    request_store.recover_processing()
    runtime_store = WatchRuntimeStateStore(tmp_path / "cli-watch-runtime.json")
    monkeypatch.setattr(cli, "_task_store", lambda: task_store)
    monkeypatch.setattr(cli, "_watch_store", lambda: watch_store)
    monkeypatch.setattr(cli, "_task_request_store", lambda: request_store)
    monkeypatch.setattr(cli, "_watch_runtime_store", lambda: runtime_store)

    harness_fixture.make_run("cli-sensitive-run", raw_sentinel=True)
    harness_fixture.make_safe("cli-sensitive-run", "sanitized CLI result")
    context = _context("viewer")
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda *_args, **_kwargs: context,
    )

    assert cli.cmd_task_list() == 0
    task_payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in task_payload["tasks"]] == [
        harness_fixture.definitions["scheduled"]
    ]
    assert "prompt" not in task_payload["tasks"][0]

    assert cli.cmd_watch_show(harness_fixture.definitions["watch"]) == 0
    watch_payload = json.loads(capsys.readouterr().out)["watch"]
    assert watch_payload["redacted"] is True
    assert "command" not in watch_payload
    assert "cwd" not in watch_payload

    list_args = cli.build_parser().parse_args(["runs", "list"])
    assert cli.cmd_runs_list(list_args) == 0
    runs_payload = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in runs_payload["runs"]] == [
        "cli-sensitive-run"
    ]
    assert RAW_SENTINEL not in json.dumps(runs_payload)

    show_args = cli.build_parser().parse_args(
        ["runs", "show", "cli-sensitive-run"]
    )
    assert cli.cmd_runs_show(show_args) == 0
    shown = json.loads(capsys.readouterr().out)["run"]
    assert shown["result_text"] == "sanitized CLI result"
    assert RAW_SENTINEL not in json.dumps(shown)

    context = _context("viewer", matching=False)
    assert cli.cmd_task_list() == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == []
    assert cli.cmd_runs_show(show_args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "run_not_found"

    harness_fixture.make_run(
        "cli-cancel-run",
        status="queued",
        activation_context=_context("editor"),
    )
    cancel_args = cli.build_parser().parse_args(
        ["runs", "cancel", "cli-cancel-run"]
    )
    context = _context("viewer")
    assert cli.cmd_runs_cancel(cancel_args) == 1
    assert json.loads(capsys.readouterr().err)["code"] == "harness_operation_forbidden"
    context = _context("editor")
    assert cli.cmd_runs_cancel(cancel_args) == 0
    capsys.readouterr()
    canceled_run = harness_fixture.store.get_run("cli-cancel-run")
    assert canceled_run is not None
    assert canceled_run["status"] == "canceled"
    assert canceled_run["output_quarantined"] is True


def test_remote_harness_cli_denials_are_structured(
    harness_fixture: HarnessFixture,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    task_store = ScheduledTaskStore(tmp_path / "denied-cli-tasks.json")
    task_store._sqlite = harness_fixture.store
    task_store.load()
    watch_store = ManagedWatchStore(tmp_path / "denied-cli-watches.json")
    watch_store._sqlite = harness_fixture.store
    watch_store.load()
    request_store = TaskExecutionStore(tmp_path / "denied-cli-runs")
    request_store._sqlite = harness_fixture.store
    request_store.recover_processing()
    monkeypatch.setattr(cli, "_task_store", lambda: task_store)
    monkeypatch.setattr(cli, "_watch_store", lambda: watch_store)
    monkeypatch.setattr(cli, "_task_request_store", lambda: request_store)
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda *_args, **_kwargs: _context("viewer"),
    )

    task_id = harness_fixture.definitions["scheduled"]
    watch_id = harness_fixture.definitions["watch"]
    commands = (
        lambda: cli.cmd_task_run(task_id),
        lambda: cli.cmd_task_set_enabled(task_id, False),
        lambda: cli.cmd_task_remove(task_id),
        lambda: cli.cmd_watch_set_enabled(watch_id, False),
        lambda: cli.cmd_watch_remove(watch_id),
    )
    for command in commands:
        assert command() == 1
        error = capsys.readouterr().err
        assert "Traceback" not in error
        assert json.loads(error)["code"] == "harness_operation_forbidden"


def test_remote_harness_cli_hidden_definitions_match_not_found(
    harness_fixture: HarnessFixture,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    task_store = ScheduledTaskStore(tmp_path / "hidden-cli-tasks.json")
    task_store._sqlite = harness_fixture.store
    task_store.load()
    watch_store = ManagedWatchStore(tmp_path / "hidden-cli-watches.json")
    watch_store._sqlite = harness_fixture.store
    watch_store.load()
    request_store = TaskExecutionStore(tmp_path / "hidden-cli-runs")
    request_store._sqlite = harness_fixture.store
    request_store.recover_processing()
    monkeypatch.setattr(cli, "_task_store", lambda: task_store)
    monkeypatch.setattr(cli, "_watch_store", lambda: watch_store)
    monkeypatch.setattr(cli, "_task_request_store", lambda: request_store)
    monkeypatch.setattr(
        resource_access_service,
        "resolve_resource_access_context",
        lambda *_args, **_kwargs: replace(
            _context("owner"),
            subject="unrelated-owner",
            email="unrelated-owner@example.com",
        ),
    )

    task_id = harness_fixture.definitions["scheduled"]
    watch_id = harness_fixture.definitions["watch"]
    harness_fixture.set_policy("harness_task", task_id, "private", revision=2)
    harness_fixture.set_policy("harness_watch", watch_id, "private", revision=2)
    commands = (
        (lambda: cli.cmd_task_run(task_id), "task_not_found"),
        (lambda: cli.cmd_task_set_enabled(task_id, False), "task_not_found"),
        (lambda: cli.cmd_watch_set_enabled(watch_id, False), "watch_not_found"),
    )
    for command, expected_code in commands:
        assert command() == 1
        error = json.loads(capsys.readouterr().err)
        assert error["code"] == expected_code
        assert "harness_definition_access_forbidden" not in error["error"]


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


def test_harness_web_push_reauthorizes_each_run_recipient(
    harness_fixture: HarnessFixture,
    monkeypatch,
) -> None:
    from core import web_push_notifications
    from storage import web_push_service

    session_id = "session-harness-private-push"
    run_id = "run-harness-private-push"
    scope_id = project_access_service.project_scope_id(harness_fixture.project_id)
    now = "2026-07-28T00:01:00+00:00"
    owner = _context("owner")
    editor = _context("editor")
    owner_key = f"remote:{owner.subject}"
    editor_key = f"remote:{editor.subject}"
    authorization_records = [
        web_push_notifications.web_push_authorization_context_record(
            user_key,
            context,
        )
        for user_key, context in ((owner_key, owner), (editor_key, editor))
    ]
    assert all(record is not None for record in authorization_records)

    with harness_fixture.engine.begin() as connection:
        connection.execute(
            agent_sessions.insert().values(
                id=session_id,
                scope_id=scope_id,
                agent_name="codex",
                agent_backend="codex",
                agent_variant="default",
                session_anchor=session_id,
                native_session_id=session_id,
                title="Private Harness push",
                status="active",
                agent_status="idle",
                visibility="foreground",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    harness_fixture.make_run(
        run_id,
        definition_id=harness_fixture.definitions["scheduled"],
        session_id=session_id,
        activation_context=owner,
    )
    harness_fixture.make_safe(run_id, "member-safe push")
    harness_fixture.set_policy(
        "harness_task",
        harness_fixture.definitions["scheduled"],
        "private",
        revision=2,
    )
    with harness_fixture.engine.begin() as connection:
        messages_service.append(
            connection,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="user",
            source="user",
            message_type="user",
            text="Run the private task",
            metadata={
                web_push_notifications.WEB_PUSH_USER_KEYS_METADATA: [
                    owner_key,
                    editor_key,
                ],
                web_push_notifications.WEB_PUSH_AUTHORIZATION_CONTEXTS_METADATA: [
                    record for record in authorization_records if record is not None
                ],
            },
        )
        message = messages_service.append(
            connection,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="member-safe push",
            metadata={
                "harness_run_id": run_id,
                web_push_notifications.WEB_PUSH_HARNESS_RUN_IDS_METADATA: [run_id],
            },
        )
        for user_key in (owner_key, editor_key):
            web_push_service.upsert_subscription(
                connection,
                user_key=user_key,
                payload={
                    "endpoint": f"https://push.example.test/{user_key}",
                    "keys": {"p256dh": f"{user_key}-key", "auth": "auth"},
                },
            )

        contexts = {owner_key: owner, editor_key: editor}
        assert web_push_notifications._badge_count_for_user_key(
            connection,
            user_key=owner_key,
            contexts=contexts,
        ) == 1
        assert web_push_notifications._badge_count_for_user_key(
            connection,
            user_key=editor_key,
            contexts=contexts,
        ) == 0

    sends: list[tuple[dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(web_push_notifications.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Harness result",
            "body": "member-safe push",
            "session_id": session_id,
            "message_id": message["id"],
        }
    )

    assert [delivery[0]["user_key"] for delivery in sends] == [owner_key]
    assert sends[0][1]["body"] == "member-safe push"

    with harness_fixture.engine.begin() as connection:
        connection.execute(
            update(agent_runs)
            .where(agent_runs.c.id == run_id)
            .values(output_quarantined=1, member_safe_json=None)
        )
        revoked_message = messages_service.append(
            connection,
            scope_id=scope_id,
            session_id=session_id,
            platform="avibe",
            author="agent",
            source="agent",
            message_type="result",
            text="member-safe push",
            metadata={
                "harness_run_id": run_id,
                web_push_notifications.WEB_PUSH_HARNESS_RUN_IDS_METADATA: [run_id],
            },
        )

    web_push_notifications._send_to_enabled_subscriptions(
        {
            "title": "Harness result after revocation",
            "body": "member-safe push",
            "session_id": session_id,
            "message_id": revoked_message["id"],
        }
    )

    assert [delivery[0]["user_key"] for delivery in sends] == [owner_key]


@pytest.mark.parametrize("target_flag", ["--session-id", "--fork-session"])
def test_remote_agent_run_denies_target_before_resolution_or_reservation(
    harness_fixture: HarnessFixture,
    monkeypatch,
    capsys,
    target_flag: str,
) -> None:
    editor = _context("editor")
    caller_session_id = "session-direct-run-caller"
    denied_session_id = "session-direct-run-denied"
    now = "2026-07-28T00:01:00+00:00"
    with harness_fixture.engine.begin() as connection:
        for session_id, scope_id in (
            (
                caller_session_id,
                project_access_service.project_scope_id(harness_fixture.project_id),
            ),
            (denied_session_id, None),
        ):
            connection.execute(
                agent_sessions.insert().values(
                    id=session_id,
                    scope_id=scope_id,
                    agent_name="codex",
                    agent_backend="codex",
                    agent_variant="default",
                    session_anchor=session_id,
                    native_session_id=session_id,
                    title=session_id,
                    status="active",
                    agent_status="idle",
                    visibility="foreground",
                    metadata_json="{}",
                    created_at=now,
                    updated_at=now,
                    last_active_at=now,
                )
            )
    principal = {
        "principal_type": "remote",
        "instance_id": str(editor.instance_id),
        "subject": str(editor.subject),
        "organization_member_id": str(editor.organization_member_id),
        "membership_version": str(editor.membership_version),
    }
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, caller_session_id)
    monkeypatch.setenv(
        AVIBE_AUTHORIZATION_CAPABILITY_ENV,
        _issue_test_agent_capability(
            monkeypatch,
            principal,
            session_id=caller_session_id,
        ),
    )
    monkeypatch.delenv(AVIBE_AUTHORIZATION_PRINCIPAL_ENV, raising=False)
    monkeypatch.delenv(AVIBE_RUN_ID_ENV, raising=False)
    monkeypatch.delenv(AVIBE_HARNESS_AUTHORIZATION_ENV, raising=False)
    test_root = harness_fixture.store.db_path.parent.parent
    request_store = TaskExecutionStore(test_root / "denied-direct-run-requests")
    request_store._sqlite = harness_fixture.store
    monkeypatch.setattr(cli, "_task_request_store", lambda: request_store)
    monkeypatch.setattr(
        cli,
        "resolve_session_id_target",
        lambda *_args, **_kwargs: pytest.fail(
            "denied target must not be resolved before authorization"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_reserve_forked_cli_session",
        lambda **_kwargs: pytest.fail(
            "denied fork must not reserve a Session"
        ),
    )
    before_runs = harness_fixture.store.count_runs()
    args = cli.build_parser().parse_args(
        [
            "agent",
            "run",
            target_flag,
            denied_session_id,
            "--no-callback",
            "--message",
            "must fail before side effects",
        ]
    )

    assert cli.cmd_agent_run(args) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "harness_session_access_forbidden"
    assert harness_fixture.store.count_runs() == before_runs


def test_remote_owner_cannot_preflight_projectless_direct_run(
    harness_fixture: HarnessFixture,
) -> None:
    payload = {
        "request_type": "agent_run",
        "metadata": {"harness_resources": []},
    }
    with harness_fixture.engine.begin() as connection:
        with pytest.raises(
            harness_auth.HarnessAuthorizationError,
            match="harness_project_required",
        ):
            harness_auth.preflight_direct_run(
                connection,
                payload,
                activation_context=_context("owner"),
            )

        prepared = harness_auth.preflight_direct_run(
            connection,
            payload,
            activation_context=trusted_local_context(),
        )

    assert prepared["launch_project_id"] is None
    assert prepared["execution_principal"] == {"principal_type": "trusted_local"}


def test_run_graph_redacts_trigger_schedule_without_definition_management(
    harness_fixture: HarnessFixture,
) -> None:
    now = "2026-07-28T00:01:00+00:00"
    session_id = "session-harness-trigger-redaction"
    definition_id = harness_fixture.definitions["scheduled"]
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
                title="Harness trigger redaction session",
                status="active",
                agent_status="idle",
                visibility="foreground",
                metadata_json="{}",
                created_at=now,
                updated_at=now,
                last_active_at=now,
            )
        )
    harness_fixture.make_run(
        "run-harness-trigger-redaction",
        definition_id=definition_id,
        session_id=session_id,
    )
    non_managing_owner = AuthorizationContext(
        instance_role="owner",
        subject="instance-owner-nonmanager",
        email="instance-owner-nonmanager@example.com",
        instance_id="instance-harness",
        instance_access_source="owner",
        organization_id=ORG_ID,
        organization_member_id="member-instance-owner-nonmanager",
        organization_role="member",
        group_ids=frozenset({GROUP_ID}),
        membership_version="membership-v1",
        claims_issued_at=int(time.time()),
        is_remote=True,
    )
    harness_auth.mirror_remote_principal(
        non_managing_owner,
        {
            "vibe_instance_authorization_revision": 1,
            "claims_issued_at": int(time.time()),
        },
        engine=harness_fixture.engine,
    )

    projected = agent_graph.build_graph(
        engine=harness_fixture.engine,
        authorization_context=non_managing_owner,
    )
    managing = agent_graph.build_graph(
        engine=harness_fixture.engine,
        authorization_context=_context("owner"),
    )

    projected_trigger = next(
        trigger
        for trigger in projected["trigger_nodes"]
        if trigger["definition_id"] == definition_id
    )
    managing_trigger = next(
        trigger
        for trigger in managing["trigger_nodes"]
        if trigger["definition_id"] == definition_id
    )
    assert projected_trigger["name"] == "Authorized scheduled"
    assert projected_trigger["schedule_label"] is None
    assert "0 * * * *" not in json.dumps(projected)
    assert managing_trigger["schedule_label"] == "cron 0 * * * *"
