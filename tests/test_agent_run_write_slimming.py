from __future__ import annotations

import json

from sqlalchemy import select

from core.scheduled_tasks import (
    ScheduledTask,
    TaskExecutionRequest,
    TaskExecutionStore,
    _agent_run_message_for_request,
)
from storage.background import SQLiteBackgroundTaskStore
from storage.models import agent_runs, run_definitions


def _run_payload(run_id: str, *, prompt: str, message: str | None = None) -> dict[str, object]:
    return {
        "id": run_id,
        "request_type": "agent_run",
        "status": "queued",
        "created_at": "2026-11-07T00:00:00+00:00",
        "updated_at": "2026-11-07T00:00:00+00:00",
        "prompt": prompt,
        "message": message if message is not None else prompt,
    }


def test_queued_run_payload_keeps_prompt_and_drops_duplicate_message() -> None:
    request = TaskExecutionRequest(
        id="run-payload",
        request_type="agent_run",
        prompt="run this instruction",
        message="run this instruction",
    )

    payload = TaskExecutionStore.queued_run_payload(request)

    assert payload["prompt"] == "run this instruction"
    assert payload["message"] is None

    distinct = TaskExecutionRequest(
        id="run-distinct",
        request_type="scheduled",
        prompt="execution prompt",
        message="display message",
    )

    assert TaskExecutionStore.queued_run_payload(distinct)["message"] == "display message"


def test_file_backend_derives_claimed_status_from_processing_path(tmp_path) -> None:
    store = TaskExecutionStore(tmp_path / "task_requests")
    request = store.enqueue_hook_send(
        session_key="slack::channel::C123",
        prompt="run this instruction",
    )

    assert store.get_run(request.id)["status"] == "queued"
    assert store.claim(request.id) is not None
    assert store.get_run(request.id)["status"] == "running"


def test_scheduled_run_drops_definition_resource_context_from_new_rows(
    tmp_path, sqlite_db_factory
) -> None:
    metadata = {
        "resource_user_context": {
            "sub": "remote-user",
            "email": "remote-user@example.com",
            "vibe_instance_id": "instance-123",
            "vibe_instance_role": "editor",
        },
        "definition_owned": "keep-me",
    }
    file_store = TaskExecutionStore(tmp_path / "file-requests")
    file_task = ScheduledTask(
        id="task-file",
        name="file task",
        session_key="slack::channel::C123",
        prompt="run task",
        schedule_type="interval",
        agent_name="default",
        session_policy="existing",
        metadata=metadata,
    )
    file_run = file_store.enqueue_task_run(
        file_task.id,
        source_kind="scheduler",
        task=file_task,
    )
    assert "resource_user_context" not in file_store.get_run(file_run.id)["metadata"]
    assert file_store.get_run(file_run.id)["metadata"]["definition_owned"] == "keep-me"

    sqlite_store = SQLiteBackgroundTaskStore(sqlite_db_factory(tmp_path / "sqlite" / "vibe.sqlite"))
    requests = TaskExecutionStore(tmp_path / "sqlite-requests")
    requests._sqlite = sqlite_store
    try:
        with sqlite_store.engine.begin() as connection:
            connection.execute(
                run_definitions.insert().values(
                    id="task-sqlite",
                    definition_type="scheduled",
                    name="scheduled task",
                    prompt="run task",
                    enabled=1,
                    created_at="2026-11-07T00:00:00+00:00",
                    updated_at="2026-11-07T00:00:00+00:00",
                    metadata_json=json.dumps(metadata),
                )
            )
            connection.execute(
                run_definitions.insert().values(
                    id="watch-sqlite",
                    definition_type="watch",
                    name="watch",
                    prompt="run watch",
                    enabled=1,
                    created_at="2026-11-07T00:00:00+00:00",
                    updated_at="2026-11-07T00:00:00+00:00",
                    metadata_json=json.dumps(metadata),
                )
            )

        sqlite_task = ScheduledTask(
            id="task-sqlite",
            name="scheduled task",
            session_key="",
            prompt="run task",
            schedule_type="interval",
            metadata=metadata,
        )
        scheduled = requests.enqueue_task_run(
            sqlite_task.id,
            source_kind="scheduler",
            task=sqlite_task,
        )
        scheduled_metadata = sqlite_store.get_run(scheduled.id)["metadata"]
        assert "resource_user_context" not in scheduled_metadata
        assert scheduled_metadata["definition_owned"] == "keep-me"

        watch = requests.enqueue_definition_run(
            definition_id="watch-sqlite",
            run_type="watch",
            source_kind="watch",
            session_key="",
            session_id=None,
            post_to=None,
            deliver_key=None,
            prompt="watch output",
            agent_name=None,
            session_policy=None,
            metadata=metadata,
        )
        assert sqlite_store.get_run(watch.id)["metadata"]["resource_user_context"] == metadata[
            "resource_user_context"
        ]
    finally:
        sqlite_store.close()


def test_new_run_persists_prompt_only_and_readers_restore_message(
    tmp_path, sqlite_db_factory
) -> None:
    store = SQLiteBackgroundTaskStore(sqlite_db_factory(tmp_path / "state" / "vibe.sqlite"))
    try:
        store.enqueue_run(_run_payload("new-run", prompt="new instruction"))

        with store.engine.connect() as connection:
            row = connection.execute(
                select(agent_runs.c.prompt, agent_runs.c.message).where(
                    agent_runs.c.id == "new-run"
                )
            ).one()

        assert row.prompt == "new instruction"
        assert row.message is None
        assert store.get_run("new-run")["message"] == "new instruction"
        restored = TaskExecutionRequest.from_dict(
            {"id": "new-run", "request_type": "agent_run", "prompt": "new instruction"}
        )
        assert _agent_run_message_for_request(restored) == "new instruction"
    finally:
        store.close()


def test_run_upsert_does_not_rewrite_legacy_message_column(
    tmp_path, sqlite_db_factory
) -> None:
    store = SQLiteBackgroundTaskStore(sqlite_db_factory(tmp_path / "state" / "vibe.sqlite"))
    try:
        store.enqueue_run(_run_payload("old-run", prompt="old instruction"))
        with store.engine.begin() as connection:
            connection.execute(
                agent_runs.update()
                .where(agent_runs.c.id == "old-run")
                .values(message="old instruction")
            )

        store.enqueue_run(
            {
                **_run_payload("old-run", prompt="old instruction"),
                "status": "running",
            }
        )

        with store.engine.connect() as connection:
            row = connection.execute(
                select(agent_runs.c.prompt, agent_runs.c.message, agent_runs.c.status).where(
                    agent_runs.c.id == "old-run"
                )
            ).one()

        assert row.prompt == "old instruction"
        assert row.message == "old instruction"
        assert row.status == "running"
    finally:
        store.close()
