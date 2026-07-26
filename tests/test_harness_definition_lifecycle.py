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
    DEFINITION_STATUS_COUNTS,
    DEFINITION_STATUS_FILTERS,
    SQLiteBackgroundTaskStore,
    compute_next_run_at,
    definition_lifecycle_detail,
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
    """The switch outranks history: a ``forever`` watch that finished a lifetime
    and was re-armed is waiting for its next one, not permanently retired."""
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
    ``ui/src/components/workbench/harnessLifecycle.ts``. The client offers four
    of the six filters this store accepts (``running`` and ``waiting`` are
    reachable through the API and the CLI but merged into one chip); what it
    must never do is name a filter this store rejects, or select a different set
    of states under the same name.
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

    for chip, states in client_states.items():
        assert chip in DEFINITION_STATUS_FILTERS, chip
        assert set(states) == set(DEFINITION_STATUS_FILTERS[chip]), chip

    # The chip list and the default landing view, same file, same names.
    chips = re.search(r"DEFINITION_STATUS_FILTERS\s*=\s*\[([^\]]*)\]", source)
    assert chips and set(re.findall(r"'([a-z_]+)'", chips.group(1))) == set(client_states)
    default = re.search(r"DEFAULT_DEFINITION_STATUS\s*=\s*'([a-z_]+)'", source)
    assert default and default.group(1) in client_states


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
