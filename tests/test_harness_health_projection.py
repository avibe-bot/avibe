"""HFR-097 / HFR-098 — the CLI consumes the definition health projection.

``_enrich_definitions`` is the one place a definition's derived health is
computed, and every list, show and Workbench read already passes through it.
PR6 added a *second* read model beside it: ``vibe/cli.py`` grew its own
``_task_health_map`` / ``_task_health`` pair that queried ``agent_runs`` again
through a ``ScheduledTaskStore.definition_health_batch`` delegator that existed
for no other caller. Two read models over the same table is how the same
definition comes to have two different answers on two surfaces — and it did:
task mutations answered from the CLI-side query while watch mutations, which
never grew one, answered with no health at all.

These two scenarios pin the direction of the dependency rather than the values:
the CLI is a projection consumer (HFR-097), and every mutation answers with the
same projected row the following ``show`` prints (HFR-098).
"""

from __future__ import annotations

import inspect
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import scheduled_tasks as scheduled_tasks_module
from core.watches import ManagedWatchStore, WatchRuntimeStateStore
from storage.background import SQLiteBackgroundTaskStore
from vibe import cli

#: The fields ``_enrich_definitions`` derives that a mutation response must not
#: answer for itself. ``lifecycle_*`` rides along because health and lifecycle
#: are orthogonal axes of the same projected row: a response that carried one
#: without the other would still be reading two sources.
PROJECTED_FIELDS = (
    "health",
    "consecutive_failures",
    "recent_failures",
    "processing_health",
    "processing_consecutive_failures",
    "processing_recent_failures",
    "lifecycle_state",
    "lifecycle_detail",
)


def _configured_v2():
    return SimpleNamespace(
        slack=SimpleNamespace(bot_token="x", app_token="y"),
        discord=SimpleNamespace(bot_token=""),
        lark=SimpleNamespace(app_id="", app_secret=""),
        wechat=SimpleNamespace(enable=False),
        enabled_platforms=lambda: ["slack"],
    )


def _parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_failed_runs(definition_id: str, *, request_type: str, count: int = 2) -> None:
    """Give a definition a real failure streak in ``agent_runs``.

    Health is a fact about run history, never about the write that just
    happened, so it has to be seeded where the projection actually reads it.
    """

    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(count):
            store.enqueue_run(
                {
                    "id": f"run-{definition_id}-{index}",
                    "request_type": request_type,
                    "status": "failed",
                    "definition_id": definition_id,
                    "error": "boom",
                    "created_at": _now(),
                    "completed_at": _now(),
                }
            )
    finally:
        store.close()


def _definition(capsys) -> dict:
    return json.loads(capsys.readouterr().out)["definition"]


def _projected(payload: dict) -> dict:
    return {field: payload.get(field) for field in PROJECTED_FIELDS}


class _FailureSeedingTaskStore(cli.ScheduledTaskStore):
    """A task store whose new row already has failure history when it returns.

    Not a contrivance: an ``--at`` task whose time has already passed can be
    claimed, fail, and be recorded before the create command has printed
    anything. The response is built after that, so it must read the row rather
    than assume a write it just made is healthy.
    """

    def add_task(self, *args, **kwargs):
        task = super().add_task(*args, **kwargs)
        _seed_failed_runs(task.id, request_type="scheduled")
        return task


def test_the_cli_is_a_projection_consumer_not_a_second_health_read_model(
    monkeypatch, capsys
) -> None:
    """HFR-097 — no CLI-side health query may exist, structurally or at runtime.

    Two halves, because either alone is escapable. The source half says the
    symbols are gone; the behavioural half says nothing reintroduced the query
    under another name, by asserting that every ``definition_health_batch`` call
    made while three task mutations run is one ``_enrich_definitions`` made.
    """

    # Snapshot before any patching: ``monkeypatch.setattr(..., raising=False)``
    # CREATES a missing attribute, so a later absence assertion would pass for
    # the wrong reason.
    assert not hasattr(cli.ScheduledTaskStore, "definition_health_batch"), (
        "the ScheduledTaskStore delegator existed only to feed the CLI-side query"
    )

    cli_source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "definition_health_batch" not in cli_source
    assert "_task_health" not in cli_source
    store_source = Path(scheduled_tasks_module.__file__).read_text(encoding="utf-8")
    assert "definition_health_batch" not in store_source

    unprojected: list[list[str]] = []
    original = SQLiteBackgroundTaskStore.definition_health_batch

    def guarded(self, definition_ids, **kwargs):
        stack = [frame.function for frame in inspect.stack()]
        if "_enrich_definitions" not in stack:
            unprojected.append(stack[:6])
        return original(self, definition_ids, **kwargs)

    monkeypatch.setattr(SQLiteBackgroundTaskStore, "definition_health_batch", guarded)

    store = cli.ScheduledTaskStore()
    add_args = _parse(
        [
            "task",
            "add",
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )
    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2()),
        patch("vibe.cli._task_store", return_value=store),
    ):
        assert cli.cmd_task_add(add_args) == 0
        task_id = _definition(capsys)["id"]
        assert cli.cmd_task_set_enabled(task_id, False) == 0
        capsys.readouterr()
        assert cli.cmd_task_update(_parse(["task", "update", task_id, "--name", "renamed"])) == 0
        capsys.readouterr()

    assert unprojected == [], (
        "health was queried outside _enrich_definitions: " f"{unprojected}"
    )


def test_task_and_watch_mutations_return_the_enriched_projection(capsys) -> None:
    """HFR-098 — every mutation answers with the row the next ``show`` prints.

    A create/pause/resume/update response is the only thing an agent sees before
    it reports back, so a definition that is already failing has to say so
    there. Watch mutations carried no health at all; task mutations carried a
    separately-queried copy of it. Both are asserted against ``show``, which
    reads the canonical projection, so the two can no longer disagree.
    """

    def _assert_matches_show(label: str, payload: dict, show, definition_id: str) -> None:
        """A mutation response, checked against the very next ``show``.

        Immediately after, not once at the end: ``lifecycle_state`` legitimately
        changes with each pause and resume, and comparing every response to one
        final snapshot would test the wrong thing.
        """

        assert show(definition_id) == 0
        shown = _definition(capsys)
        if label.startswith("watch"):
            assert shown["health"] == "unknown", f"{label}: an unobserved waiter was guessed healthy"
            assert shown["processing_health"] == "failing", (
                f"{label}: the seeded downstream streak is not visible at all"
            )
            assert shown["processing_consecutive_failures"] == 2, (
                f"{label}: downstream processing lost the streak"
            )
        else:
            assert shown["health"] == "failing", f"{label}: the seeded streak is not visible at all"
            assert shown["consecutive_failures"] == 2, f"{label} lost the streak"
        assert _projected(payload) == _projected(shown), f"{label} disagrees with show"

    task_store = _FailureSeedingTaskStore()
    add_args = _parse(
        [
            "task",
            "add",
            "--session-key",
            "slack::channel::C123",
            "--cron",
            "0 * * * *",
            "--message",
            "hello",
        ]
    )
    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2()),
        patch("vibe.cli._task_store", return_value=task_store),
    ):
        assert cli.cmd_task_add(add_args) == 0
        created = _definition(capsys)
        task_id = created["id"]
        _assert_matches_show("task add", created, cli.cmd_task_show, task_id)

        assert cli.cmd_task_set_enabled(task_id, False) == 0
        _assert_matches_show("task pause", _definition(capsys), cli.cmd_task_show, task_id)

        assert cli.cmd_task_set_enabled(task_id, True) == 0
        _assert_matches_show("task resume", _definition(capsys), cli.cmd_task_show, task_id)

        assert cli.cmd_task_update(_parse(["task", "update", task_id, "--name", "renamed"])) == 0
        _assert_matches_show("task update", _definition(capsys), cli.cmd_task_show, task_id)

    watch_store = ManagedWatchStore()
    watch_runtime = WatchRuntimeStateStore()

    def _startup(store, runtime_store, watch_id, **kwargs):
        # A ``once`` watch can complete — and fail — while the create command is
        # still confirming startup, which is exactly when this row's health
        # stops being the default.
        _seed_failed_runs(watch_id, request_type="watch")
        return store.get_watch(watch_id), runtime_store.load().get("watches", {}).get(watch_id)

    watch_add_args = _parse(
        [
            "watch",
            "add",
            "--session-key",
            "slack::channel::C123",
            "--name",
            "Wait for export",
            "--shell",
            "true",
        ]
    )
    with (
        patch("vibe.cli._ensure_config", return_value=_configured_v2()),
        patch("vibe.cli._watch_store", return_value=watch_store),
        patch("vibe.cli._watch_runtime_store", return_value=watch_runtime),
        patch("vibe.cli._wait_for_watch_startup", side_effect=_startup),
    ):
        assert cli.cmd_watch_add(watch_add_args) == 0
        watch_created = _definition(capsys)
        watch_id = watch_created["id"]
        _assert_matches_show("watch add", watch_created, cli.cmd_watch_show, watch_id)

        assert cli.cmd_watch_set_enabled(watch_id, False) == 0
        _assert_matches_show("watch pause", _definition(capsys), cli.cmd_watch_show, watch_id)

        assert cli.cmd_watch_set_enabled(watch_id, True) == 0
        _assert_matches_show("watch resume", _definition(capsys), cli.cmd_watch_show, watch_id)

        assert cli.cmd_watch_update(_parse(["watch", "update", watch_id, "--name", "renamed"])) == 0
        _assert_matches_show("watch update", _definition(capsys), cli.cmd_watch_show, watch_id)


def test_watch_health_separates_waiter_from_event_processing(tmp_path, monkeypatch) -> None:
    """HFR-436 — provenance does not create one shared health lifecycle."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    watch_store = ManagedWatchStore()
    watch = watch_store.add_watch(
        name="successful waiter",
        session_key="slack::channel::C123",
        command=[],
        shell_command="true",
        prefix=None,
        cwd=None,
        mode="once",
        timeout_seconds=30,
        lifetime_timeout_seconds=0,
        retry_exit_codes=[75],
        retry_delay_seconds=0,
        post_to=None,
        deliver_key=None,
    )
    sqlite = SQLiteBackgroundTaskStore()
    try:
        unobserved = sqlite.get_watch(watch.id)
    finally:
        sqlite.close()
    assert unobserved is not None and unobserved["health"] == "unknown"
    assert watch_store.mark_cycle_start(watch.id)
    assert watch_store.mark_cycle_result(
        watch.id,
        exit_code=0,
        error=None,
        event_detected=True,
        disable=True,
    )
    _seed_failed_runs(watch.id, request_type="watch")

    sqlite = SQLiteBackgroundTaskStore()
    try:
        projected = sqlite.get_watch(watch.id)
    finally:
        sqlite.close()

    assert projected is not None
    assert projected["last_error"] is None
    assert projected["health"] == "healthy"
    assert projected["consecutive_failures"] == 0
    assert projected["processing_health"] == "failing"
    assert projected["processing_consecutive_failures"] == 2
