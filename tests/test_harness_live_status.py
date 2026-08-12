"""Focused contracts for persisted Run activity and the unified live view."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select, update

from core.services.harness_status import build_harness_status
from storage.background import SQLiteBackgroundTaskStore
from storage.models import agent_runs
from vibe import cli, internal_client


def _enqueue_running(store: SQLiteBackgroundTaskStore, run_id: str) -> None:
    store.enqueue_run(
        {
            "id": run_id,
            "request_type": "agent_run",
            "status": "queued",
            "agent_name": "codex",
            "agent_backend": "codex",
            "session_id": "ses-live",
            "message": "continue",
            "created_at": "2026-08-12T05:00:00+00:00",
            "updated_at": "2026-08-12T05:00:00+00:00",
        }
    )
    assert store.claim_pending_run(
        run_id,
        started_at="2026-08-12T05:01:00+00:00",
    ) is not None


def test_record_run_activity_is_monotonic_and_terminal_guarded(tmp_path) -> None:
    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        _enqueue_running(store, "run-live")
        claimed = store.get_run("run-live")
        assert claimed["last_activity_at"] is None

        assert store.record_run_activity(
            ["run-live", "run-live"],
            observed_at="2026-08-12T05:03:00+00:00",
        ) == ["run-live"]
        assert store.record_run_activity(
            ["run-live"],
            observed_at="2026-08-12T05:02:00+00:00",
        ) == []
        active = store.get_run("run-live")
        assert active["last_activity_at"] == "2026-08-12T05:03:00+00:00"
        assert active["updated_at"] == "2026-08-12T05:03:00+00:00"

        store.record_run_output(
            "run-live",
            output_id="terminal",
            text="done",
            terminal_status="succeeded",
            updated_at="2026-08-12T05:04:00+00:00",
        )
        assert store.record_run_activity(
            ["run-live"],
            observed_at="2026-08-12T05:05:00+00:00",
        ) == []
        settled = store.get_run("run-live")
        assert settled["status"] == "succeeded"
        assert settled["last_activity_at"] == "2026-08-12T05:03:00+00:00"
    finally:
        store.close()


def test_record_run_activity_preserves_unreadable_metadata(tmp_path) -> None:
    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        _enqueue_running(store, "run-malformed")
        with store.engine.begin() as connection:
            connection.execute(
                update(agent_runs)
                .where(agent_runs.c.id == "run-malformed")
                .values(metadata_json="{not json")
            )

        assert store.record_run_activity(
            ["run-malformed"],
            observed_at="2026-08-12T05:03:00+00:00",
        ) == []
        with store.engine.connect() as connection:
            stored = connection.execute(
                select(agent_runs.c.metadata_json).where(
                    agent_runs.c.id == "run-malformed"
                )
            ).scalar_one()
        assert stored == "{not json"
    finally:
        store.close()


def test_hfr_480_persisted_activity_exposes_owner_loss(tmp_path) -> None:
    """HFR-480: output activity survives while missing ownership is explicit."""

    store = SQLiteBackgroundTaskStore(tmp_path / "state.sqlite")
    try:
        _enqueue_running(store, "run-orphan")
        store.record_run_activity(
            ["run-orphan"],
            observed_at="2026-08-12T05:04:00+00:00",
        )
        runs = store.list_active_runs(limit=10)
        snapshot = build_harness_status(
            runs=runs,
            watches=[],
            tasks=[],
            runtime_snapshot={
                "available": True,
                "ownership_available": True,
                "owned_run_ids": [],
                "agents": [],
            },
            now=datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc),
        )

        assert snapshot["runs"][0]["last_activity_at"] == "2026-08-12T05:04:00+00:00"
        assert snapshot["runs"][0]["activity_age_seconds"] == 60
        assert snapshot["runs"][0]["liveness"] == "owner_missing"
        assert snapshot["runs"][0]["anomaly_codes"] == ["run_owner_missing"]
        assert snapshot["anomalies"][0]["run_id"] == "run-orphan"

        recent = store.sweep_stale_runs(
            owned_run_ids=set(),
            error_texts={"orphaned": "owner disappeared"},
            now="2026-08-12T05:05:00+00:00",
            orphan_grace_seconds=120,
        )
        assert recent == []
        expired = store.sweep_stale_runs(
            owned_run_ids=set(),
            error_texts={"orphaned": "owner disappeared"},
            now="2026-08-12T05:07:00+00:00",
            orphan_grace_seconds=120,
        )
        assert [item.run_id for item in expired] == ["run-orphan"]
        assert store.get_run("run-orphan")["status"] == "failed"
    finally:
        store.close()


def test_hfr_481_unified_view_marks_workdir_conflict_and_watch_failure() -> None:
    """HFR-481: one view combines work inventory with explicit anomalies."""

    snapshot = build_harness_status(
        runs=[],
        watches=[
            {
                "id": "watch-ci",
                "name": "CI",
                "lifecycle_state": "waiting",
                "process_alive": False,
                "health": "failing",
                "last_exit_code": 2,
                "last_error": "waiter exited",
            }
        ],
        tasks=[
            {
                "id": "task-report",
                "name": "Report",
                "lifecycle_state": "waiting",
                "next_run_at": "2026-08-13T00:00:00+00:00",
            }
        ],
        runtime_snapshot={
            "available": True,
            "ownership_available": True,
            "owned_run_ids": [],
            "agents": [
                {
                    "backend": "codex",
                    "state": "active",
                    "session_id": "ses-a",
                    "base_session_id": "base-a",
                    "workdir": "/repo/project",
                },
                {
                    "backend": "claude",
                    "state": "active",
                    "session_id": "ses-b",
                    "base_session_id": "base-b",
                    "workdir": "/repo/project/.",
                },
            ],
        },
        now=datetime(2026, 8, 12, 5, 5, tzinfo=timezone.utc),
    )

    codes = {item["code"] for item in snapshot["anomalies"]}
    assert codes == {"active_workdir_conflict", "watch_waiter_failed"}
    assert snapshot["counts"] == {
        "active_runs": 0,
        "armed_watches": 1,
        "enabled_tasks": 1,
        "runtime_agents": 2,
        "anomalies": 2,
    }
    assert all(
        row["anomaly_codes"] == ["active_workdir_conflict"]
        for row in snapshot["runtime_agents"]
    )


def test_same_backend_shared_process_is_not_a_workdir_conflict() -> None:
    snapshot = build_harness_status(
        runs=[],
        watches=[],
        tasks=[],
        runtime_snapshot={
            "available": True,
            "ownership_available": True,
            "owned_run_ids": [],
            "agents": [
                {
                    "backend": "codex",
                    "state": "active",
                    "session_id": "ses-a",
                    "base_session_id": "base-a",
                    "workdir": "/repo/project",
                    "pid": 4242,
                },
                {
                    "backend": "codex",
                    "state": "active",
                    "session_id": "ses-b",
                    "base_session_id": "base-b",
                    "workdir": "/repo/project",
                    "pid": 4242,
                },
            ],
        },
    )

    assert snapshot["anomalies"] == []


def test_harness_status_cli_returns_one_unified_payload(monkeypatch, capsys) -> None:
    sqlite_store = SimpleNamespace(
        list_active_runs=lambda *, limit: [],
        list_enabled_definitions=lambda definition_type, *, limit: [],
    )
    monkeypatch.setattr(
        cli,
        "_task_request_store",
        lambda: SimpleNamespace(sqlite_backend=sqlite_store),
    )

    async def _controller_snapshot():
        return {
            "status_code": 200,
            "body": {
                "ok": True,
                "agents": [],
                "owned_run_ids": [],
                "ownership_available": True,
                "ownership_error": None,
            },
        }

    monkeypatch.setattr(internal_client, "list_running_agents", _controller_snapshot)
    args = cli.build_parser().parse_args(["harness", "status"])

    assert cli.cmd_harness_status(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "harness_status"
    assert payload["controller"] == {
        "available": True,
        "ownership_available": True,
        "error": None,
    }
    assert payload["anomalies"] == []
