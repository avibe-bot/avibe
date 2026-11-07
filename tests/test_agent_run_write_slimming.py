from __future__ import annotations

from sqlalchemy import select

from core.scheduled_tasks import (
    TaskExecutionRequest,
    TaskExecutionStore,
    _agent_run_message_for_request,
)
from storage.background import SQLiteBackgroundTaskStore
from storage.models import agent_runs


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
