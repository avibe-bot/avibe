"""Harness Tasks/Watches lifecycle: what a row is *doing*, not whether it is on.

``enabled`` is a switch, and the harness read it as a state. That made a
one-shot watch that finished on its own indistinguishable from one the user
paused (both store ``enabled = 0``), left "still waiting" and "running right
now" unnameable, and let a waiter whose process had died keep rendering as a
healthy armed watch.

``definition_lifecycle_expression`` derives the four states from columns that
already exist — no migration — and is the single declaration both the row select
and the filter counts read, so a row cannot land in a bucket its own chip did not
count.

See ``docs/plans/harness-watch-task-readability.md`` §2 (derivation), §3 (frozen
payload contract) and §5.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import workbench_sessions_service
from storage.background import (
    DEFINITION_CYCLE_COLUMNS,
    DEFINITION_STATUS_COUNTS,
    DEFINITION_STATUS_FILTERS,
    SQLiteBackgroundTaskStore,
    _id_batches,
    compute_next_run_at,
    definition_lifecycle_detail,
    definition_resume_starts_new_cycle,
    definition_status_total,
)
from storage.db import create_sqlite_engine
from storage.models import agent_sessions
from storage.pagination import PageRequest
from storage.settings_service import upsert_scope

NOW = "2026-07-26T00:00:00+00:00"


@pytest.fixture()
def store(tmp_path: Path):
    sqlite = SQLiteBackgroundTaskStore(tmp_path / "state" / "vibe.sqlite")
    try:
        yield sqlite
    finally:
        sqlite.close()


def _task(store: SQLiteBackgroundTaskStore, task_id: str, **overrides) -> None:
    payload = {
        "id": task_id,
        "name": task_id,
        "prompt": "run it",
        "schedule_type": "cron",
        "cron": "0 * * * *",
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
        "shell_command": "tail -f deploy.log",
        "enabled": True,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    store.upsert_watch(payload)


def _run(store: SQLiteBackgroundTaskStore, run_id: str, definition_id: str, **overrides) -> None:
    payload = {
        "id": run_id,
        "request_type": "watch",
        "status": "running",
        "definition_id": definition_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    store.enqueue_run(payload)


def _state(store: SQLiteBackgroundTaskStore, watch_id: str) -> str:
    return store.get_watch(watch_id)["lifecycle_state"]


def test_watch_states_separate_the_switch_from_the_history(store) -> None:
    """The four states, one fixture per rule (plan §2)."""
    _watch(store, "armed")
    _watch(store, "executing")
    _run(store, "run-1", "executing", status="running")
    # Switched off after completing a lifetime: it retired itself.
    _watch(store, "retired", enabled=False, last_finished_at=NOW)
    # Switched off having never finished: someone paused it.
    _watch(store, "paused", enabled=False)

    assert _state(store, "armed") == "waiting"
    assert _state(store, "executing") == "running"
    assert _state(store, "retired") == "finished"
    assert _state(store, "paused") == "paused"


def test_a_switched_on_watch_that_already_finished_once_is_waiting_again(store) -> None:
    """A ``forever`` watch that finished a lifetime and was re-armed is waiting
    for its next one, not permanently retired.

    Also the guard for rows written before ``last_finished_at`` meant
    "retired": a watch the scheduler may still fire has not retired, whatever
    stamp an earlier cycle left on it.
    """
    _watch(store, "rearmed", mode="forever", last_finished_at=NOW)

    assert _state(store, "rearmed") == "waiting"


def test_a_live_waiter_heartbeat_is_not_an_execution(store) -> None:
    """The rule the whole ``waiting`` state depends on.

    A watch's supervisor heartbeat is an ``agent_runs`` row with status
    ``running`` for as long as the waiter process lives. Counting it as an
    execution would make every healthy waiter read as ``running`` and leave
    ``waiting`` unreachable — which is why waiter liveness is a separate field.
    """
    _watch(store, "healthy")
    store.write_watch_runtime(
        {"watches": {"healthy": {"running": True, "pid": 4242, "started_at": NOW}}},
        updated_at=NOW,
    )

    watch = store.get_watch("healthy")
    assert watch["lifecycle_state"] == "waiting"
    assert watch["process_alive"] is True
    assert watch["runtime"]["pid"] == 4242


def test_process_alive_tells_a_dead_waiter_from_one_never_seen(store) -> None:
    """The defect this field exists for: an armed watch whose waiter died looks
    exactly like a healthy one. ``False`` means we watched it exit; ``None``
    means we have never seen it, and the row must not claim it is dead on the
    strength of never having looked."""
    _watch(store, "died")
    _watch(store, "never-started")
    store.write_watch_runtime(
        {"watches": {"died": {"running": True, "pid": 99, "started_at": NOW}}},
        updated_at=NOW,
    )
    # The next supervisor write with no entry for it terminalizes the heartbeat.
    store.write_watch_runtime({"watches": {}}, updated_at=NOW)

    died = store.get_watch("died")
    never = store.get_watch("never-started")
    assert (died["lifecycle_state"], died["process_alive"]) == ("waiting", False)
    assert (never["lifecycle_state"], never["process_alive"]) == ("waiting", None)
    # The stale heartbeat metadata still says ``running: true``; the row's own
    # status is what decides, so the payload must not repeat the stale claim.
    assert died["runtime"]["running"] is False


def test_a_disabled_cron_task_is_paused_and_a_fired_one_shot_is_finished(store) -> None:
    """A cron task cannot retire itself, so switching one off is always a pause.
    Only a one-shot that has already fired is done."""
    _task(store, "cron-off", enabled=False)
    _task(store, "cron-on")
    _task(store, "one-shot-fired", schedule_type="at", run_at=NOW, enabled=False, last_run_at=NOW)
    _task(store, "one-shot-pending", schedule_type="at", run_at="2099-01-01T00:00:00+00:00", enabled=False)

    states = {task["id"]: task["lifecycle_state"] for task in store.list_scheduled_tasks()}
    assert states == {
        "cron-off": "paused",
        "cron-on": "waiting",
        "one-shot-fired": "finished",
        "one-shot-pending": "paused",
    }


def test_counts_and_rows_come_from_one_expression(store) -> None:
    """The invariant that makes a chip trustworthy: every filter returns exactly
    the rows its own count promised, for every filter, including the ``active``
    one that spans two states."""
    _watch(store, "w-waiting")
    _watch(store, "w-running")
    _run(store, "run-1", "w-running", status="running")
    _watch(store, "w-paused", enabled=False)
    _watch(store, "w-finished-a", enabled=False, last_finished_at=NOW)
    _watch(store, "w-finished-b", enabled=False, last_finished_at=NOW)

    counts = store.count_watches()
    assert counts == {"total": 5, "running": 1, "waiting": 1, "paused": 1, "finished": 2}
    assert set(counts) == set(DEFINITION_STATUS_COUNTS)

    for status in DEFINITION_STATUS_FILTERS:
        rows = store.list_watches_page(status=status, page_request=PageRequest(page=1, limit=50)).items
        assert len(rows) == definition_status_total(counts, status), status
        expected = DEFINITION_STATUS_FILTERS[status]
        if expected:
            assert {row["lifecycle_state"] for row in rows} <= set(expected), status


def test_an_unknown_status_is_rejected_rather_than_silently_ignored(store) -> None:
    with pytest.raises(ValueError):
        store.list_watches_page(status="enabled", page_request=PageRequest(page=1, limit=5))


@pytest.mark.parametrize(
    ("exit_code", "error", "expected"),
    [
        (0, None, "normal"),
        (None, None, "normal"),
        (124, "waiter timed out", "timeout"),
        (1, "boom", "error"),
        # Scheduled tasks never write an exit code, so the code alone would call
        # every failed one a normal ending.
        (None, "boom", "error"),
        (0, "   ", "normal"),
    ],
)
def test_lifecycle_detail_names_how_a_finished_row_ended(exit_code, error, expected) -> None:
    assert (
        definition_lifecycle_detail(lifecycle_state="finished", last_exit_code=exit_code, last_error=error)
        == expected
    )


@pytest.mark.parametrize("state", ["running", "waiting", "paused", None])
def test_only_a_finished_row_reports_an_ending(state) -> None:
    """A row still in play has no ending yet; reporting one would let the UI
    print "出错结束" beside a watch that is currently waiting."""
    assert definition_lifecycle_detail(lifecycle_state=state, last_exit_code=1, last_error="boom") is None


def test_next_run_at_and_waiting_since_are_on_the_row(store) -> None:
    """Both frozen fields (plan §3) come from the enrichment, so every list and
    get path has them without the caller computing anything."""
    _task(store, "hourly", cron="0 * * * *", timezone="UTC")
    _task(store, "switched-off", cron="0 * * * *", enabled=False)
    _watch(store, "armed", last_started_at="2026-07-26T09:00:00+00:00")
    _watch(store, "stopped", enabled=False, last_started_at="2026-07-26T09:00:00+00:00")

    tasks = {task["id"]: task for task in store.list_scheduled_tasks()}
    assert tasks["hourly"]["next_run_at"] is not None
    # A task the scheduler may not fire has no next run to promise.
    assert tasks["switched-off"]["next_run_at"] is None

    watches = {watch["id"]: watch for watch in store.list_watches()}
    assert watches["armed"]["waiting_since"] == "2026-07-26T09:00:00+00:00"
    # A paused row's last start is history, not a wait anyone is still in.
    assert watches["stopped"]["waiting_since"] is None


def _seed_session(db_path: Path, *, platform: str, scope_type: str, title: str, native_type=None) -> str:
    """A session in a scope of the caller's choosing, returning its id."""
    engine = create_sqlite_engine(db_path)
    try:
        with engine.begin() as conn:
            scope_id = upsert_scope(
                conn,
                platform=platform,
                scope_type=scope_type,
                native_id=title,
                now=NOW,
                display_name="#dev-ops",
                native_type=native_type,
            )
            session = workbench_sessions_service.create_session(
                conn, scope_id=scope_id, agent_backend="claude", agent_name="default"
            )
            conn.execute(
                update(agent_sessions).where(agent_sessions.c.id == session["id"]).values(title=title)
            )
            return session["id"]
    finally:
        engine.dispose()


def test_a_watch_links_every_session_chat_actually_serves(tmp_path: Path) -> None:
    """Plan §4.5, and a deliberate change in what the Watches/Tasks panel links.

    This surface used to link only *workbench* sessions, so an IM-bound watch
    printed a channel the reader could see and not open — even though
    ``/chat/<id>`` serves it, which is what the agent graph already assumed.
    Two rules for one question; the harness one was the wrong one.

    Presentation and linkability are now separate fields:
    ``session_is_workbench`` still decides whether the row reads as a title or
    as a channel, while ``session_openable`` alone decides whether it opens.
    """
    db_path = tmp_path / "state" / "vibe.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    SQLiteBackgroundTaskStore(db_path).close()

    workbench = _seed_session(db_path, platform="avibe", scope_type="project", title="巡检构建产物")
    im = _seed_session(db_path, platform="slack", scope_type="channel", title="周报推送")
    private = _seed_session(
        db_path,
        platform="avibe",
        scope_type="project",
        title="spawned",
        native_type="private_agent_run",
    )

    store = SQLiteBackgroundTaskStore(db_path)
    try:
        _watch(store, "w-workbench", session_id=workbench)
        _watch(store, "w-im", session_id=im)
        _watch(store, "w-private", session_id=private)
        rows = {watch["id"]: watch for watch in store.list_watches()}
    finally:
        store.close()

    # An IM watch reads as IM and opens: both, not one or the other.
    assert rows["w-im"]["session_is_workbench"] is False
    assert rows["w-im"]["session_label"] == "#dev-ops"
    assert rows["w-im"]["session_openable"] is True

    assert rows["w-workbench"]["session_is_workbench"] is True
    assert rows["w-workbench"]["session_openable"] is True

    # The one destination /chat/<id> genuinely refuses. The row still names it.
    assert rows["w-private"]["session_openable"] is False
    assert rows["w-private"]["session_title"] == "spawned"


NEW_YORK = "America/New_York"


def test_next_run_at_carries_the_offset_of_the_fire_time_not_of_today() -> None:
    """A daily task fires at the same wall-clock time year round, and the row
    prints that instant on the reader's clock.

    The failure this guards is invisible for 363 days a year: computing the
    zone's offset *now* and stamping it onto a fire time on the other side of a
    DST transition, which slides the task an hour and — because the UI renders
    the instant, not the cron — silently mislabels when it will run. Asserted as
    a property rather than against a frozen clock so it also holds on the two
    days it can actually break.
    """
    tz = ZoneInfo(NEW_YORK)
    for cron, hour, minute in (("30 2 * * *", 2, 30), ("15 13 * * 3", 13, 15)):
        iso = compute_next_run_at(
            enabled=True, schedule_type="cron", cron=cron, run_at=None, timezone_name=NEW_YORK
        )
        assert iso is not None, cron
        fire = datetime.fromisoformat(iso)
        local = fire.astimezone(tz)
        assert (local.hour, local.minute) == (hour, minute), cron
        # The offset stamped on the payload must be the zone's offset at that
        # instant — not at the moment the payload was built.
        assert fire.utcoffset() == tz.utcoffset(local.replace(tzinfo=None)), cron


def test_a_naive_one_shot_is_read_in_its_own_zone_on_its_own_date() -> None:
    """``run_at`` is stored without an offset, so the zone has to supply one —
    and which one depends on the date it names, not on today's."""
    winter = f"{datetime.now().year + 1}-01-15T12:00:00"
    summer = f"{datetime.now().year + 1}-07-01T12:00:00"

    def offset(run_at: str) -> str:
        iso = compute_next_run_at(
            enabled=True, schedule_type="at", cron=None, run_at=run_at, timezone_name=NEW_YORK
        )
        assert iso is not None, run_at
        return iso[-6:]

    assert offset(winter) == "-05:00"
    assert offset(summer) == "-04:00"


def test_the_ui_chips_ask_for_filters_this_store_actually_serves() -> None:
    """The chips are declared twice — once here, once in the client that draws
    them — and a chip whose states the store buckets differently is a count that
    does not match its own list.

    MIRROR of ``DEFINITION_STATUS_FILTER_STATES`` in
    ``ui/src/components/workbench/harnessLifecycle.ts``. The two tables must be
    EQUAL. A chip this store rejects is a 400 the user cannot avoid; a filter
    this store serves with no chip is a view the UI cannot reach, which is what
    ``running`` and ``waiting`` were — buckets the store counted, the API
    accepted, and nothing on screen could ask for.
    """
    source = Path("ui/src/components/workbench/harnessLifecycle.ts").read_text(encoding="utf-8")

    block = re.search(
        r"DEFINITION_STATUS_FILTER_STATES[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL
    )
    assert block, "could not find DEFINITION_STATUS_FILTER_STATES"
    client_states = {
        key: tuple(re.findall(r"'([a-z_]+)'", values))
        for key, values in re.findall(r"(\w+):\s*\[([^\]]*)\]", block.group(1))
    }
    assert client_states, "parsed no chips out of the client"

    assert set(client_states) == set(DEFINITION_STATUS_FILTERS)
    for chip, states in client_states.items():
        assert set(states) == set(DEFINITION_STATUS_FILTERS[chip]), chip

    # The chip list and the default landing view, same file, same names.
    chips = re.search(r"DEFINITION_STATUS_FILTERS\s*=\s*\[([^\]]*)\]", source)
    assert chips and set(re.findall(r"'([a-z_]+)'", chips.group(1))) == set(client_states)
    default = re.search(r"DEFAULT_DEFINITION_STATUS\s*=\s*'([a-z_]+)'", source)
    assert default and default.group(1) in client_states


def test_the_ui_numbers_weekdays_the_way_the_scheduler_does() -> None:
    """The client turns a cron day-of-week field into words, and it is the only
    place in the product that does. If it uses crontab(5)'s numbering — Sunday
    is 0 — while ``CronTrigger.from_crontab`` uses APScheduler's — Monday is 0 —
    every weekly row is off by a day, and it reads as a correct schedule.

    MIRROR of ``WEEKDAY_NAMES`` in
    ``ui/src/components/workbench/harnessLifecycle.ts``, pinned to the library's
    own table rather than to a copy of it, so an APScheduler upgrade that
    renumbered the week would fail here instead of on screen.
    """
    from apscheduler.triggers.cron.expressions import WEEKDAYS

    source = Path("ui/src/components/workbench/harnessLifecycle.ts").read_text(encoding="utf-8")
    block = re.search(r"const WEEKDAY_NAMES = \[([^\]]*)\]", source)
    assert block, "could not find WEEKDAY_NAMES"
    assert re.findall(r"'([a-z]+)'", block.group(1)) == list(WEEKDAYS)


@pytest.mark.parametrize("field", ["7", "5-1", "0-7"])
def test_the_day_of_week_forms_the_ui_refuses_are_the_ones_that_never_fire(field: str) -> None:
    """The client hands these back as raw expressions instead of describing
    them. This is why: the scheduler will not build a trigger for them at all,
    so a task carrying one never fires, and a plain-English weekly phrase would
    be a promise nothing can keep."""
    from apscheduler.triggers.cron import CronTrigger

    with pytest.raises(ValueError):
        CronTrigger.from_crontab(f"0 8 * * {field}")


def test_resuming_a_one_shot_watch_clears_the_cycle_that_ended_it(store) -> None:
    """Both doorways write ``enabled``; only one used to know that resuming
    starts a new lifecycle. Leaving the old cycle behind makes the *next* pause
    render as "finished" — the row drops out of the default list and out of the
    paused chip, so it is nowhere the user would look for it."""
    _watch(store, "w", mode="once", enabled=False, last_finished_at=NOW, last_exit_code=0)
    assert _state(store, "w") == "finished"

    # The Harness UI's path, which bypassed ``core/watches.py`` entirely.
    store.set_definition_enabled("w", True, definition_type="watch")
    row = store.get_watch("w")
    assert row["lifecycle_state"] == "waiting"
    assert [row[column] for column in DEFINITION_CYCLE_COLUMNS] == [None] * len(
        DEFINITION_CYCLE_COLUMNS
    )

    store.set_definition_enabled("w", False, definition_type="watch")
    assert _state(store, "w") == "paused"


def test_resuming_keeps_the_history_a_forever_watch_exists_to_show(store) -> None:
    """The other half of the same rule: "last fired 2h ago" is what a continuous
    watch's row is for, and pausing it does not make that untrue."""
    _watch(store, "w", mode="forever", enabled=False, last_event_at=NOW)
    store.set_definition_enabled("w", True, definition_type="watch")
    assert store.get_watch("w")["last_event_at"] == NOW

    assert definition_resume_starts_new_cycle("watch", "once") is True
    assert definition_resume_starts_new_cycle("watch", "forever") is False
    # A task's history is never a lifecycle marker to clear: a fired one-shot has
    # no next fire to protect, and a cron task keeps reporting its last run.
    assert definition_resume_starts_new_cycle("scheduled", None) is False


def test_a_running_row_is_timed_from_the_run_that_is_running(store) -> None:
    """``last_started_at`` is the definition's previous cycle. A watch that fired
    yesterday and started a fresh run a minute ago would report a day of running
    if the row read that column — a duration assembled from two cycles, which
    nothing is actually spending."""
    yesterday = "2026-07-25T00:00:00+00:00"
    started = "2026-07-26T09:00:00+00:00"
    _watch(store, "w", mode="forever", last_started_at=yesterday, last_event_at=yesterday)
    _run(store, "r", "w", status="running", started_at=started)

    row = store.get_watch("w")
    assert row["lifecycle_state"] == "running"
    assert row["running_since"] == started


def test_a_queued_run_has_no_start_to_report(store) -> None:
    """``running`` covers queued-or-running. A queued run has not started, so
    there is no duration — and the row must say nothing rather than reach back to
    a column that would answer with the last cycle."""
    _watch(store, "w", last_started_at="2026-07-25T00:00:00+00:00")
    _run(store, "r", "w", status="queued", started_at=None)

    row = store.get_watch("w")
    assert row["lifecycle_state"] == "running"
    assert row["running_since"] is None


def test_running_since_is_only_set_while_running(store) -> None:
    _watch(store, "w", enabled=True, last_started_at=NOW)
    assert store.get_watch("w")["lifecycle_state"] == "waiting"
    assert store.get_watch("w")["running_since"] is None


def test_batches_are_sized_by_bound_parameters_not_by_values() -> None:
    """SQLite's cap counts parameters. A resolver matching a three-column tuple
    binds three per value, so a batch sized in values overflows at a third of the
    count it was checked at — the failure only shows up on a store big enough to
    reach it, which is the one place nobody tests."""
    values = [f"id-{index}" for index in range(1000)]

    assert all(len(batch) <= 400 for batch in _id_batches(values))
    assert all(len(batch) * 3 <= 400 for batch in _id_batches(values, params_per_value=3))
    # Every value still lands in exactly one batch, whatever the chunking.
    assert [item for batch in _id_batches(values, params_per_value=3) for item in batch] == values


def test_one_failing_lookup_does_not_blank_the_others(store, monkeypatch) -> None:
    """These are independent questions, and they used to share one ``except``.
    A failure resolving session keys took ``process_alive`` down with it, so
    every watch row said "liveness unknown" for a reason nothing named."""
    _watch(store, "w", legacy_session_key="slack::channel::C1")
    store.write_watch_runtime({"watches": {"w": {"running": True, "pid": 42}}}, updated_at=NOW)

    def _boom(*args, **kwargs):
        raise RuntimeError("session key lookup failed")

    monkeypatch.setattr(SQLiteBackgroundTaskStore, "_key_summaries", staticmethod(_boom))

    row = store.get_watch("w")
    assert row["process_alive"] is True


def test_a_page_costs_a_fixed_number_of_queries(store) -> None:
    """The reason enrichment is batched: the per-row version this replaced
    issued a query per row, which a 30-row page paid thirty times over."""
    for index in range(30):
        _watch(store, f"watch-{index}", session_id=f"session-{index}")
    store.write_watch_runtime(
        {"watches": {f"watch-{index}": {"running": True, "pid": index} for index in range(30)}},
        updated_at=NOW,
    )

    statements: list[str] = []

    @event.listens_for(store.engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    try:
        page = store.list_watches_page(status="all", page_request=PageRequest(page=1, limit=30))
    finally:
        event.remove(store.engine, "before_cursor_execute", _record)

    assert len(page.items) == 30
    assert len(statements) <= 4, statements


def test_pausing_a_forever_watch_that_has_been_running_is_a_pause(store) -> None:
    """The distinction ``last_finished_at`` exists to make.

    A ``forever`` watch ends a cycle every time it fires or retries and keeps
    watching. While a cycle ending was stamped as a finish, a watch the user
    paused after any successful cycle read as *finished* — it left the Paused
    filter and claimed an ending, with a "normal" or "error" verdict invented
    from whatever the last cycle happened to exit with.

    The store writes that column only when it also switches the watch off
    (``core/watches.py::mark_cycle_result``), so it means "retired", and pause
    and retirement are finally two different things.
    """
    _watch(store, "paused-after-cycles", mode="forever", enabled=False, last_exit_code=0, last_event_at=NOW)
    _watch(store, "paused-mid-retry", mode="forever", enabled=False, last_exit_code=1, last_error="boom")
    _watch(store, "retired-on-error", mode="forever", enabled=False, last_finished_at=NOW, last_exit_code=1)

    assert _state(store, "paused-after-cycles") == "paused"
    assert _state(store, "paused-mid-retry") == "paused"
    assert _state(store, "retired-on-error") == "finished"


def test_a_forever_watch_retired_by_its_lifetime_says_it_timed_out(store) -> None:
    """Running out of lifetime is the supervisor's deadline, not a clean stop.

    The retirement path wrote no exit code, so the ending classifier fell
    through to ``normal`` and a watch the supervisor cut off reported having
    finished normally. It carries the same 124 the per-cycle timeout uses.
    """
    _watch(store, "expired", mode="forever", enabled=False, last_finished_at=NOW, last_exit_code=124)

    row = store.get_watch("expired")
    assert row["lifecycle_state"] == "finished"
    assert definition_lifecycle_detail(
        lifecycle_state=row["lifecycle_state"],
        last_exit_code=row["last_exit_code"],
        last_error=row["last_error"],
    ) == "timeout"


def test_re_enabling_a_fired_one_shot_task_does_not_make_it_waiting_again(store) -> None:
    """``enabled`` is not a promise of a future fire.

    Switching a one-shot back on leaves its ``run_at`` in the past, so
    ``compute_next_run_at`` has nothing to offer it. Reading the switch before
    history parked such a task in the default Active view forever, inflating the
    badge with a row that will never run.
    """
    _task(store, "fired-then-re-enabled", schedule_type="at", run_at=NOW, enabled=True, last_run_at=NOW)

    assert store.get_scheduled_task("fired-then-re-enabled")["lifecycle_state"] == "finished"
    assert compute_next_run_at(
        enabled=True, schedule_type="at", cron=None, run_at=NOW, timezone_name="UTC"
    ) is None


def test_mark_cycle_result_stamps_a_finish_only_when_it_retires_the_watch(tmp_path: Path) -> None:
    """The writer half of the rule above, stated where it is written.

    A continuing cycle also clears the column, so a row stamped under the older
    rule heals itself the first time it runs instead of staying wrong forever.
    """
    from core.watches import ManagedWatchStore

    store = ManagedWatchStore(tmp_path / "watches.json")
    watch = store.add_watch(
        name="w",
        session_key="",
        command=[],
        shell_command="true",
        prefix=None,
        cwd=None,
        mode="forever",
        timeout_seconds=0.0,
        lifetime_timeout_seconds=0.0,
        retry_exit_codes=[1],
        retry_delay_seconds=0.0,
        post_to=None,
        deliver_key=None,
    )

    store.mark_cycle_result(watch.id, exit_code=1, error="retrying", disable=False)
    assert store.get_watch(watch.id).last_finished_at is None

    store.mark_cycle_result(watch.id, exit_code=124, error="lifetime timeout", disable=True)
    retired = store.get_watch(watch.id)
    assert retired.last_finished_at is not None
    assert retired.enabled is False

    # A stale stamp does not survive the next cycle.
    store.mark_cycle_result(watch.id, exit_code=0, error=None, event_detected=True, disable=False)
    assert store.get_watch(watch.id).last_finished_at is None


def test_the_scheduler_and_the_row_read_a_naive_run_at_the_same_way() -> None:
    """One resolver, two callers — the defect this closes was two of them.

    ``compute_next_run_at`` attached the task's own zone to a naive ``run_at``
    while ``_build_trigger`` called ``.astimezone()``, which reads a naive value
    in the *host* zone first. The row then promised a fire time the scheduler
    would not honour, off by the offset between the two zones — a gap that only
    opens on rows whose timezone is not the machine's, so it never showed up on
    the developer's own tasks.
    """
    from core.scheduled_tasks import ScheduledTask, ScheduledTaskService

    run_at = f"{datetime.now().year + 1}-01-15T12:00:00"
    task = ScheduledTask(
        id="t",
        name="t",
        session_key="",
        prompt="go",
        schedule_type="at",
        run_at=run_at,
        timezone=NEW_YORK,
    )

    trigger = ScheduledTaskService._build_trigger(object(), task)
    shown = compute_next_run_at(
        enabled=True, schedule_type="at", cron=None, run_at=run_at, timezone_name=NEW_YORK
    )

    assert shown is not None
    assert trigger.run_date == datetime.fromisoformat(shown)
    # And that instant is noon in New York, whatever zone the host is in.
    assert trigger.run_date.astimezone(ZoneInfo(NEW_YORK)).hour == 12
