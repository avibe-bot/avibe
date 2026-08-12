from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from storage.background import (
    SQLiteBackgroundTaskStore,
    TASK_RETIREMENT_SCHEDULE_CONSUMED,
)
from storage.models import agent_runs, run_definitions
from storage.pagination import PageRequest
from vibe import cli
from vibe.ui_server import app

NOW = "2026-07-27T12:00:00+00:00"
FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
CANONICAL_FIELDS = (
    "lifecycle_state",
    "lifecycle_detail",
    "lifecycle_finished_at",
    "next_run_at",
    "waiting_since",
    "running_since",
)


def _task(store: SQLiteBackgroundTaskStore, task_id: str, **overrides) -> None:
    payload = {
        "id": task_id,
        "name": task_id,
        "prompt": "run it",
        "schedule_type": "cron",
        "cron": "0 * * * *",
        "timezone": "UTC",
        "enabled": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    store.upsert_scheduled_task(payload)


def _watch(store: SQLiteBackgroundTaskStore, watch_id: str, **overrides) -> None:
    payload = {
        "id": watch_id,
        "name": watch_id,
        "shell_command": "true",
        "mode": "once",
        "enabled": True,
        "created_at": NOW,
        "updated_at": NOW,
        "last_started_at": NOW,
    }
    payload.update(overrides)
    store.upsert_watch(payload)


def _rows(store: SQLiteBackgroundTaskStore, table) -> list[dict]:
    with store.engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(select(table).order_by(table.c.id)).mappings()
        ]


def _canonical(row: dict, *, watch: bool = False) -> dict:
    fields = CANONICAL_FIELDS + (("process_alive",) if watch else ())
    return {field: row.get(field) for field in fields}


def test_cli_and_workbench_share_the_definition_read_projection(capsys) -> None:
    store = SQLiteBackgroundTaskStore()
    try:
        _task(
            store,
            "queued-disabled",
            schedule_type="at",
            cron=None,
            run_at=FUTURE,
            enabled=False,
            last_run_at=NOW,
        )
        store.enqueue_run(
            {
                "id": "queued-run",
                "request_type": "scheduled",
                "status": "queued",
                "definition_id": "queued-disabled",
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

        _watch(store, "waiting-live")
        _watch(store, "waiting-dead")
        _watch(store, "waiting-never")
        _watch(store, "paused", enabled=False, last_started_at=None)
        _watch(
            store,
            "finished-normal",
            enabled=False,
            retired_at=NOW,
            last_finished_at=NOW,
            last_exit_code=0,
        )
        _watch(
            store,
            "finished-timeout",
            enabled=False,
            retired_at=NOW,
            last_finished_at=NOW,
            last_exit_code=124,
        )
        _watch(
            store,
            "finished-error",
            enabled=False,
            retired_at=NOW,
            last_finished_at=NOW,
            last_exit_code=1,
            last_error="waiter failed",
        )
        store.write_watch_runtime(
            {
                "watches": {
                    "waiting-live": {
                        "running": True,
                        "pid": 1234,
                        "started_at": NOW,
                        "updated_at": NOW,
                    },
                    "waiting-dead": {
                        "running": False,
                        "pid": 2345,
                        "started_at": NOW,
                        "updated_at": NOW,
                    },
                }
            },
            updated_at=NOW,
        )

        before_definitions = _rows(store, run_definitions)
        before_runs = _rows(store, agent_runs)

        assert cli.cmd_task_list(page_request=PageRequest(limit=20)) == 0
        cli_tasks = {
            row["id"]: row
            for row in json.loads(capsys.readouterr().out)["definitions"]
        }
        assert cli.cmd_watch_list(include_finished=True, page_request=PageRequest(limit=20)) == 0
        cli_watches = {
            row["id"]: row
            for row in json.loads(capsys.readouterr().out)["definitions"]
        }
        assert cli.cmd_task_show("queued-disabled") == 0
        cli_task_detail = json.loads(capsys.readouterr().out)["definition"]
        assert cli.cmd_watch_show("waiting-live") == 0
        cli_watch_detail = json.loads(capsys.readouterr().out)["definition"]
        assert cli.cmd_watch_show("waiting-never") == 0
        cli_unknown_watch_detail = json.loads(capsys.readouterr().out)["definition"]

        client = app.test_client()
        api_tasks = {
            row["id"]: row
            for row in client.get("/api/harness/tasks?page=1&limit=20").get_json()["tasks"]
        }
        api_watches = {
            row["id"]: row
            for row in client.get("/api/harness/watches?page=1&limit=20").get_json()["watches"]
        }

        assert _canonical(cli_tasks["queued-disabled"]) == _canonical(
            api_tasks["queued-disabled"]
        )
        assert _canonical(cli_task_detail) == _canonical(api_tasks["queued-disabled"])
        assert cli_tasks["queued-disabled"]["lifecycle_state"] == "running"
        assert cli_tasks["queued-disabled"]["running_since"] is None
        assert cli_tasks["queued-disabled"]["state"] == "active"

        for watch_id in api_watches:
            assert _canonical(cli_watches[watch_id], watch=True) == _canonical(
                api_watches[watch_id], watch=True
            )
        assert _canonical(cli_watch_detail, watch=True) == _canonical(
            api_watches["waiting-live"], watch=True
        )
        assert cli_watch_detail["runtime"]["pid"] == 1234
        assert cli_unknown_watch_detail["process_alive"] is None
        assert cli_unknown_watch_detail["runtime"] == {}
        assert api_watches["waiting-never"]["runtime"] == {}

        expected = {
            "waiting-live": ("waiting", None, True),
            "waiting-dead": ("waiting", None, False),
            "waiting-never": ("waiting", None, None),
            "paused": ("paused", None, None),
            "finished-normal": ("finished", "normal", None),
            "finished-timeout": ("finished", "timeout", None),
            "finished-error": ("finished", "error", None),
        }
        actual = {
            watch_id: (
                row["lifecycle_state"],
                row["lifecycle_detail"],
                row["process_alive"],
            )
            for watch_id, row in cli_watches.items()
        }
        assert actual == expected
        assert cli_watches["waiting-live"]["state"] == "running"
        assert cli_watches["waiting-live"]["waiting_since"] == NOW
        assert cli_watches["waiting-dead"]["state"] == "pending"
        assert cli_watches["waiting-dead"]["waiting_since"] == NOW
        assert cli_watches["waiting-never"]["state"] == "pending"
        assert cli_watches["waiting-never"]["waiting_since"] == NOW
        assert cli_watches["finished-normal"]["state"] == "completed"
        assert cli_watches["finished-timeout"]["state"] == "failed"
        assert cli_watches["finished-error"]["state"] == "failed"

        assert _rows(store, run_definitions) == before_definitions
        assert _rows(store, agent_runs) == before_runs
    finally:
        store.close()


def test_cli_definition_lists_page_before_enrichment(monkeypatch, capsys) -> None:
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(25):
            _task(store, f"task-{index:02d}")
            _watch(store, f"watch-{index:02d}")
    finally:
        store.close()

    enriched_sizes: list[tuple[str, int]] = []
    original = SQLiteBackgroundTaskStore._enrich_definitions

    def record_page(self, rows, conn, *, definition_type):
        enriched_sizes.append((definition_type, len(rows)))
        return original(self, rows, conn, definition_type=definition_type)

    monkeypatch.setattr(SQLiteBackgroundTaskStore, "_enrich_definitions", record_page)

    page_request = PageRequest(page=1, limit=2)
    assert cli.cmd_task_list(page_request=page_request) == 0
    task_payload = json.loads(capsys.readouterr().out)
    assert cli.cmd_watch_list(page_request=page_request) == 0
    watch_payload = json.loads(capsys.readouterr().out)

    assert task_payload["pagination"]["next_command"] == "vibe task list --page 2 --limit 2"
    assert watch_payload["pagination"]["next_command"] == "vibe watch list --page 2 --limit 2"
    assert enriched_sizes == [("scheduled", 3), ("watch", 3)]


def test_task_list_pagination_is_stable_across_run_at_boundary(capsys) -> None:
    store = SQLiteBackgroundTaskStore()
    try:
        _task(
            store,
            "boundary",
            schedule_type="at",
            cron=None,
            run_at=FUTURE,
            created_at="2026-07-27T12:00:00+00:00",
        )
        _task(store, "still-waiting", created_at="2026-07-27T12:01:00+00:00")

        assert cli.cmd_task_list(page_request=PageRequest(page=1, limit=1)) == 0
        first_page = json.loads(capsys.readouterr().out)

        # Simulate the wall clock crossing run_at between offset pages.
        with store.engine.begin() as conn:
            conn.execute(
                update(run_definitions)
                .where(run_definitions.c.id == "boundary")
                .values(run_at=PAST)
            )

        assert cli.cmd_task_list(page_request=PageRequest(page=2, limit=1)) == 0
        second_page = json.loads(capsys.readouterr().out)

        assert [first_page["definitions"][0]["id"], second_page["definitions"][0]["id"]] == [
            "boundary",
            "still-waiting",
        ]
    finally:
        store.close()


def test_task_list_keeps_manual_and_ownerless_one_shots_visible(capsys) -> None:
    store = SQLiteBackgroundTaskStore()
    try:
        _task(
            store,
            "manual-run",
            schedule_type="at",
            cron=None,
            run_at=PAST,
            enabled=True,
            last_run_at=NOW,
        )
        _task(
            store,
            "scheduler-completed",
            schedule_type="at",
            cron=None,
            run_at=PAST,
            enabled=False,
            last_run_at=NOW,
            retired_at=NOW,
            retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
        )

        assert cli.cmd_task_list(page_request=PageRequest(limit=20)) == 0
        rows = json.loads(capsys.readouterr().out)["definitions"]

        by_id = {row["id"]: row for row in rows}
        assert set(by_id) == {"manual-run", "scheduler-completed"}
        assert by_id["manual-run"]["lifecycle_state"] == "waiting"
        legacy = by_id["scheduler-completed"]
        assert legacy["lifecycle_state"] == "finished"
        assert legacy["lifecycle_detail"] is None
        assert legacy["lifecycle_finished_at"] is None
        assert store.get_scheduled_task("scheduler-completed")["last_run_at"] is None
    finally:
        store.close()
