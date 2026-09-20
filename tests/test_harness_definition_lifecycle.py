"""Harness Tasks/Watches lifecycle: what a row is *doing*, not whether it is on.

``enabled`` is a switch, and the harness read it as a state. That made a
one-shot watch that finished on its own indistinguishable from one the user
paused (both store ``enabled = 0``), left "still waiting" and "running right
now" unnameable, and let a waiter whose process had died keep rendering as a
healthy armed watch.

``definition_lifecycle_expression`` derives the four states from persisted
facts and is the single declaration both the row select and the filter counts
read, so a row cannot land in a bucket its own chip did not count.

See ``docs/plans/harness-watch-task-readability.md`` §2 (derivation), §3 (frozen
payload contract) and §5.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage import workbench_sessions_service
from storage.background import (
    COMMAND_TIMED_OUT_METADATA_KEY,
    DEFINITION_CYCLE_COLUMNS,
    DEFINITION_RETIREMENT_COLUMNS,
    DEFINITION_STATUS_COUNTS,
    DEFINITION_STATUS_FILTERS,
    NO_EVENT_EXIT_CODE,
    SQLiteBackgroundTaskStore,
    TASK_RETIREMENT_SCHEDULE_CONSUMED,
    TASK_RETIREMENT_SCHEDULE_MISSED,
    TASK_LAST_RESULT_STATUS_METADATA_KEY,
    TaskScheduleRetired,
    _id_batches,
    compute_next_run_at,
    definition_lifecycle_detail,
    definition_resume_clear_columns,
    definition_status_total,
)
from storage.db import create_sqlite_engine
from storage.models import agent_sessions, run_definitions
from storage.pagination import PageRequest
from storage.settings_service import upsert_scope

NOW = "2026-07-26T00:00:00+00:00"

# Next-fire projection still compares the schedule to the real clock. Lifecycle
# itself reads persisted retirement and never interprets these timestamps.
FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


@pytest.fixture(scope="module")
def _background_store_template(tmp_path_factory):
    path = tmp_path_factory.mktemp("background-store") / "empty.sqlite"
    sqlite = SQLiteBackgroundTaskStore(path)
    sqlite.close()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


@pytest.fixture()
def store(tmp_path: Path, sqlite_db_factory, _background_store_template):
    path = sqlite_db_factory(tmp_path / "state" / "vibe.sqlite", template=_background_store_template)
    sqlite = SQLiteBackgroundTaskStore(path)
    try:
        yield sqlite
    finally:
        sqlite.close()


def test_background_template_matches_real_store_initialization(
    tmp_path, _background_store_template, sqlite_db_factory,
):
    from tests.test_sqlite_state_migration import _schema_fingerprint

    reference = tmp_path / "reference.sqlite"
    sqlite = SQLiteBackgroundTaskStore(reference)
    sqlite.close()
    copied = sqlite_db_factory(tmp_path / "copied.sqlite", template=_background_store_template)
    with closing(sqlite3.connect(reference)) as fresh, closing(sqlite3.connect(copied)) as clone:
        assert _schema_fingerprint(fresh) == _schema_fingerprint(clone)
        assert sorted(row for row in fresh.iterdump() if row.startswith("INSERT INTO")) == sorted(
            row for row in clone.iterdump() if row.startswith("INSERT INTO")
        )
        for pragma in ("journal_mode", "user_version", "application_id"):
            assert fresh.execute(f"PRAGMA {pragma}").fetchall() == clone.execute(f"PRAGMA {pragma}").fetchall()
        assert clone.execute("PRAGMA integrity_check").fetchone() == ("ok",)


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


def test_definition_serializers_cover_every_persisted_column(store) -> None:
    """A schema addition must make both definition writers explicit."""
    columns = {column.name for column in run_definitions.columns}

    assert set(store._scheduled_task_values({"id": "scheduled"})) == columns
    assert set(store._watch_values({"id": "watch"})) == columns


def test_watch_row_serializer_covers_every_managed_watch_field(store) -> None:
    """The API row must expose every persisted field the supervisor writes."""
    from dataclasses import fields

    from core.watches import ManagedWatch

    _watch(store, "w")
    row = store.get_watch("w")

    assert {field.name for field in fields(ManagedWatch)} <= set(row)


def test_watch_numeric_defaults_preserve_zero_and_restore_nulls(store) -> None:
    """Every numeric Watch setting preserves zero and restores its declared default."""
    from dataclasses import MISSING, fields
    from numbers import Real

    from core.watches import ManagedWatch

    numeric_defaults = {
        field.name: field.default
        for field in fields(ManagedWatch)
        if field.default is not MISSING
        and isinstance(field.default, Real)
        and not isinstance(field.default, bool)
    }
    zero_values = dict.fromkeys(numeric_defaults, 0)

    _watch(store, "zero-values", **zero_values)
    zero_row = store.get_watch("zero-values")
    assert {name: zero_row[name] for name in numeric_defaults} == zero_values

    _watch(store, "absent-values")
    absent_row = store.get_watch("absent-values")
    assert {name: absent_row[name] for name in numeric_defaults} == numeric_defaults

    _watch(store, "null-values")
    with store.engine.begin() as connection:
        connection.execute(
            update(run_definitions)
            .where(run_definitions.c.id == "null-values")
            .values({name: None for name in numeric_defaults})
        )
    null_row = store.get_watch("null-values")
    assert {name: null_row[name] for name in numeric_defaults} == numeric_defaults


def test_watch_states_separate_the_switch_from_the_history(store) -> None:
    """The four states, one fixture per rule (plan §2)."""
    _watch(store, "armed")
    _watch(store, "executing")
    _run(store, "run-1", "executing", status="running")
    # Switched off after completing a lifetime: it retired itself.
    _watch(store, "retired", enabled=False, last_finished_at=NOW, retired_at=NOW)
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


@pytest.mark.parametrize("definition_type", ["scheduled", "watch"])
def test_pause_resume_and_edit_never_publish_an_inferred_pause_time(
    store,
    definition_type: str,
) -> None:
    """Pause is a state without a timestamp until the product needs that fact."""

    definition_id = f"paused-{definition_type}"
    if definition_type == "scheduled":
        _task(store, definition_id, enabled=False, updated_at="2026-07-27T00:00:00+00:00")
    else:
        _watch(store, definition_id, enabled=False, updated_at="2026-07-27T00:00:00+00:00")
    get = store.get_scheduled_task if definition_type == "scheduled" else store.get_watch
    row = get(definition_id)
    assert row["lifecycle_state"] == "paused"
    assert "paused_at" not in row

    assert store.set_definition_enabled(
        definition_id,
        True,
        definition_type=definition_type,
    )
    assert store.set_definition_enabled(
        definition_id,
        False,
        definition_type=definition_type,
    )
    if definition_type == "scheduled":
        _task(
            store,
            definition_id,
            name="renamed Friday",
            enabled=False,
            updated_at="2026-07-31T00:00:00+00:00",
        )
    else:
        _watch(
            store,
            definition_id,
            name="renamed Friday",
            enabled=False,
            updated_at="2026-07-31T00:00:00+00:00",
        )
    row = get(definition_id)
    assert row["lifecycle_state"] == "paused"
    assert "paused_at" not in row


def test_disabled_definitions_need_a_persisted_retirement_fact_to_be_finished(store) -> None:
    """A cron task cannot retire itself, so switching one off is always a pause.
    A one-shot is finished only after its scheduler owner persisted retirement;
    a legacy row with no marker remains an honest pause rather than gaining a
    fabricated outcome from its deadline."""
    _task(store, "cron-off", enabled=False)
    _task(store, "cron-on")
    _task(
        store,
        "one-shot-fired-off",
        schedule_type="at",
        run_at=PAST,
        enabled=False,
        last_run_at=NOW,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
    )
    _task(
        store,
        "one-shot-fired-on",
        schedule_type="at",
        run_at=PAST,
        enabled=True,
        last_run_at=NOW,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
    )
    _task(store, "one-shot-pending", schedule_type="at", run_at="2099-01-01T00:00:00+00:00", enabled=False)

    states = {task["id"]: task["lifecycle_state"] for task in store.list_scheduled_tasks()}
    assert states == {
        "cron-off": "paused",
        "cron-on": "waiting",
        "one-shot-fired-off": "finished",
        "one-shot-fired-on": "finished",
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
    _watch(store, "w-finished-a", enabled=False, last_finished_at=NOW, retired_at=NOW)
    _watch(store, "w-finished-b", enabled=False, last_finished_at=NOW, retired_at=NOW)

    counts = store.count_watches()
    assert counts == {"total": 5, "running": 1, "waiting": 1, "paused": 1, "finished": 2}
    assert set(counts) == set(DEFINITION_STATUS_COUNTS)

    for status in DEFINITION_STATUS_FILTERS:
        rows = store.list_watches_page(status=status, page_request=PageRequest(page=1, limit=50)).items
        assert len(rows) == definition_status_total(counts, status), status
        expected = DEFINITION_STATUS_FILTERS[status]
        if expected:
            assert {row["lifecycle_state"] for row in rows} <= set(expected), status


def test_a_one_shot_watch_that_ended_quietly_counts_as_a_success(store) -> None:
    """Exit 64 retires a ``once`` watch cleanly, so the compact list hides it.

    The two projections have to agree with the supervisor about which codes are
    clean endings. While this one recognised only ``None``/``0``, a watch that
    ended on the no-event path stayed pinned to ``vibe watch list`` as unfinished
    business and reported itself as failed — the opposite of what it did.
    """
    _watch(
        store,
        "quiet",
        mode="once",
        enabled=False,
        last_finished_at=NOW,
        retired_at=NOW,
        last_exit_code=NO_EVENT_EXIT_CODE,
    )
    _watch(
        store,
        "broken",
        mode="once",
        enabled=False,
        last_finished_at=NOW,
        retired_at=NOW,
        last_exit_code=1,
        last_error="boom",
    )

    assert _state(store, "quiet") == "finished"
    compact = store.list_watches_page(
        page_request=PageRequest(page=1, limit=10), include_successful_finished=False
    )
    full = store.list_watches_page(
        page_request=PageRequest(page=1, limit=10), include_successful_finished=True
    )

    assert {row["id"] for row in compact.items} == {"broken"}
    assert {row["id"] for row in full.items} == {"quiet", "broken"}


def test_an_unknown_status_is_rejected_rather_than_silently_ignored(store) -> None:
    with pytest.raises(ValueError):
        store.list_watches_page(status="enabled", page_request=PageRequest(page=1, limit=5))


@pytest.mark.parametrize(
    ("exit_code", "error", "timed_out", "expected"),
    [
        (0, None, None, "normal"),
        (None, None, None, "normal"),
        # A watch cycle, and every row written before the scheduler recorded the fact:
        # 124 is all there is to go on, so it still reads as a timeout.
        (124, "waiter timed out", None, "timeout"),
        (1, "boom", None, "error"),
        # Scheduled tasks never write an exit code, so the code alone would call
        # every failed one a normal ending.
        (None, "boom", None, "error"),
        (0, "   ", None, "normal"),
        # SCT-024. The scheduler is the only witness to its own timeout: a command
        # that wraps itself in ``timeout`` reports a REAL one as 124 too, and calling
        # that "stopped for running too long" tells the user to raise a limit that
        # was never reached.
        (124, "command exited with status 124", False, "error"),
        (124, "command timed out after 5 second(s)", True, "timeout"),
        # And the flag outranks the code in the other direction as well: a command
        # killed by the scheduler that still managed its own exit status.
        (137, "command timed out after 5 second(s)", True, "timeout"),
        # A waiter that finished its cycle and found nothing worth an Agent turn ended
        # cleanly. Reading 64 as a failure made the quiet path -- the reason the code
        # exists -- look broken everywhere a finished watch is rendered.
        (NO_EVENT_EXIT_CODE, None, None, "normal"),
        # The code does not excuse a real error the waiter also reported.
        (NO_EVENT_EXIT_CODE, "boom", None, "error"),
    ],
)
def test_lifecycle_detail_names_how_a_finished_row_ended(
    exit_code, error, timed_out, expected
) -> None:
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            last_exit_code=exit_code,
            last_error=error,
            timed_out=timed_out,
        )
        == expected
    )


@pytest.mark.parametrize("state", ["running", "waiting", "paused", None])
def test_only_a_finished_row_reports_an_ending(state) -> None:
    """A row still in play has no ending yet; reporting one would let the UI
    print "出错结束" beside a watch that is currently waiting."""
    assert definition_lifecycle_detail(lifecycle_state=state, last_exit_code=1, last_error="boom") is None


def test_a_legacy_one_shot_without_owner_evidence_claims_no_ending() -> None:
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            definition_type="scheduled",
            last_run_at=None,
            last_exit_code=None,
            last_error=None,
        )
        is None
    )
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            definition_type="scheduled",
            last_run_at=NOW,
            last_exit_code=None,
            last_error=None,
        )
        is None
    )


def test_legacy_consumed_one_shot_without_owner_stays_visible_in_compact(store) -> None:
    """Unknown legacy ownership must not be backfilled into a successful ending."""

    _task(
        store,
        "legacy-consumed",
        schedule_type="at",
        cron=None,
        run_at=PAST,
        enabled=False,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
        last_run_at=NOW,
        last_run_id=None,
    )

    row = store.get_scheduled_task("legacy-consumed")
    assert row["lifecycle_detail"] is None
    assert row["last_run_at"] is None
    compact = store.list_scheduled_tasks_page(
        page_request=PageRequest(limit=20),
        include_successful_finished=False,
    )
    assert {item["id"] for item in compact.items} == {"legacy-consumed"}


@pytest.mark.parametrize(
    ("status", "exit_code", "timed_out", "expected"),
    [
        ("succeeded", None, None, "normal"),
        ("canceled", None, None, "canceled"),
        ("failed", 1, False, "error"),
        ("failed", 124, False, "error"),
        ("failed", 124, True, "timeout"),
    ],
)
def test_consumed_one_shot_ending_comes_from_its_owner_run(
    status, exit_code, timed_out, expected
) -> None:
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            definition_type="scheduled",
            retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
            terminal_run_status=status,
            terminal_run_exit_code=exit_code,
            terminal_run_timed_out=timed_out,
        )
        == expected
    )


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
    _watch(store, "w", mode="once", enabled=False, last_finished_at=NOW, retired_at=NOW, last_exit_code=0)
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
    """Continuous history survives resume; a stale retirement does not."""
    _watch(
        store,
        "w",
        mode="forever",
        enabled=False,
        last_started_at=PAST,
        last_finished_at=NOW,
        retired_at=NOW,
        last_event_at=NOW,
        last_exit_code=124,
        last_error="lifetime timeout",
    )
    assert _state(store, "w") == "finished"

    store.set_definition_enabled("w", True, definition_type="watch")
    row = store.get_watch("w")
    assert row["lifecycle_state"] == "waiting"
    assert row["last_started_at"] == PAST
    assert row["last_event_at"] == NOW
    assert [row[column] for column in DEFINITION_RETIREMENT_COLUMNS] == [None] * len(
        DEFINITION_RETIREMENT_COLUMNS
    )

    store.set_definition_enabled("w", False, definition_type="watch")
    row = store.get_watch("w")
    assert row["lifecycle_state"] == "paused"
    assert row["lifecycle_detail"] is None

    assert definition_resume_clear_columns("watch", "once") == DEFINITION_CYCLE_COLUMNS
    assert definition_resume_clear_columns("watch", "forever") == DEFINITION_RETIREMENT_COLUMNS
    assert definition_resume_clear_columns("scheduled", None) == ()


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


@pytest.mark.parametrize(
    ("name", "mode", "last_finished_at", "retired_at", "last_event_at", "expected"),
    [
        ("legacy-stamped-and-paused", "forever", NOW, None, NOW, "paused"),
        ("newly-retired-forever", "forever", NOW, NOW, NOW, "finished"),
        ("newly-paused-forever", "forever", None, None, NOW, "paused"),
        ("never-run-forever", "forever", None, None, None, "paused"),
        ("newly-retired-once", "once", NOW, NOW, NOW, "finished"),
        ("newly-paused-once", "once", NOW, None, NOW, "paused"),
    ],
)
def test_watch_retirement_is_explicit_state(
    store,
    name: str,
    mode: str,
    last_finished_at: str | None,
    retired_at: str | None,
    last_event_at: str | None,
    expected: str,
) -> None:
    """History cannot prove retirement; the dedicated marker can."""
    _watch(
        store,
        name,
        mode=mode,
        enabled=False,
        last_finished_at=last_finished_at,
        retired_at=retired_at,
        last_event_at=last_event_at,
    )

    assert _state(store, name) == expected


def test_pausing_a_forever_watch_that_has_been_running_is_a_pause(store) -> None:
    """The distinction ``retired_at`` exists to make.

    A ``forever`` watch ends a cycle every time it fires or retries and keeps
    watching. While cycle history was treated as a finish, a watch the user
    paused after any successful cycle read as *finished* — it left the Paused
    filter and claimed an ending, with a "normal" or "error" verdict invented
    from whatever the last cycle happened to exit with.

    The store writes ``retired_at`` only when it also switches the watch off,
    so pause and retirement are finally two different facts.
    """
    _watch(store, "paused-after-cycles", mode="forever", enabled=False, last_exit_code=0, last_event_at=NOW)
    _watch(store, "paused-mid-retry", mode="forever", enabled=False, last_exit_code=1, last_error="boom")
    _watch(
        store,
        "retired-on-error",
        mode="forever",
        enabled=False,
        last_finished_at=NOW,
        retired_at=NOW,
        last_exit_code=1,
    )

    assert _state(store, "paused-after-cycles") == "paused"
    assert _state(store, "paused-mid-retry") == "paused"
    assert _state(store, "retired-on-error") == "finished"


def test_a_forever_watch_retired_by_its_lifetime_says_it_timed_out(store) -> None:
    """Running out of lifetime is the supervisor's deadline, not a clean stop.

    The retirement path wrote no exit code, so the ending classifier fell
    through to ``normal`` and a watch the supervisor cut off reported having
    finished normally. It carries the same 124 the per-cycle timeout uses.
    """
    _watch(
        store,
        "expired",
        mode="forever",
        enabled=False,
        last_finished_at=NOW,
        retired_at=NOW,
        last_exit_code=124,
    )

    row = store.get_watch("expired")
    assert row["lifecycle_state"] == "finished"
    assert definition_lifecycle_detail(
        lifecycle_state=row["lifecycle_state"],
        last_exit_code=row["last_exit_code"],
        last_error=row["last_error"],
    ) == "timeout"


def test_a_legacy_scheduled_task_exit_124_claims_no_terminal_outcome() -> None:
    """Neither exit 124 nor old result text proves who consumed the schedule."""
    assert (
        definition_lifecycle_detail(
            lifecycle_state="finished",
            definition_type="scheduled",
            last_run_at=NOW,
            last_exit_code=124,
            last_error="command returned status 124",
            timed_out=None,
        )
        is None
    )


def test_re_enabling_a_retired_one_shot_task_does_not_make_it_waiting_again(store) -> None:
    """``enabled`` is not a promise of a future fire.

    Switching a one-shot back on leaves its ``run_at`` in the past, so
    ``compute_next_run_at`` has nothing to offer it. Reading the switch before
    history parked such a task in the default Active view forever, inflating the
    badge with a row that will never run.
    """
    _task(
        store,
        "fired-then-re-enabled",
        schedule_type="at",
        run_at=NOW,
        enabled=True,
        last_run_at=NOW,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_CONSUMED,
    )

    assert store.get_scheduled_task("fired-then-re-enabled")["lifecycle_state"] == "finished"
    assert compute_next_run_at(
        enabled=True, schedule_type="at", cron=None, run_at=NOW, timezone_name="UTC"
    ) is None


def test_running_a_future_one_shot_by_hand_leaves_it_waiting(store) -> None:
    """``vibe task run`` does not consume the schedule.

    A manual run records ``last_run_at`` and deliberately leaves the task armed
    (``mark_task_result(disable_one_shot=False)``), so treating "has run once"
    as "is over" retired a task APScheduler was still holding a future fire for
    — and dropped it out of the default Active view on its way out.
    """
    _task(store, "run-early", schedule_type="at", run_at=FUTURE, enabled=True, last_run_at=NOW)

    assert store.get_scheduled_task("run-early")["lifecycle_state"] == "waiting"
    # The state and the time printed next to it come from one fact.
    assert compute_next_run_at(
        enabled=True, schedule_type="at", cron=None, run_at=FUTURE, timezone_name="UTC"
    ) is not None


def test_a_past_one_shot_without_a_recovery_fact_stays_nonterminal(store) -> None:
    """Wall-clock passage cannot manufacture a successful or missed outcome."""
    _task(store, "missed-it", schedule_type="at", run_at=PAST, enabled=True, last_run_at=None)

    row = store.get_scheduled_task("missed-it")
    assert row["lifecycle_state"] == "waiting"
    assert row["lifecycle_detail"] is None
    assert compute_next_run_at(
        enabled=True, schedule_type="at", cron=None, run_at=PAST, timezone_name="UTC"
    ) is None

    assert store.retire_missed_one_shot(
        "missed-it",
        expected_run_at=PAST,
        expected_timezone="UTC",
        expected_updated_at=NOW,
        retired_at=NOW,
    )
    row = store.get_scheduled_task("missed-it")
    assert row["lifecycle_state"] == "finished"
    assert row["lifecycle_detail"] == "missed"
    assert row["last_run_at"] is None


def test_schedule_missed_clears_prior_manual_result_mirrors_but_keeps_run_history(
    store,
) -> None:
    """HFR-478 -- a missed schedule cannot borrow an earlier manual failure."""

    _task(
        store,
        "missed-after-manual-failure",
        schedule_type="at",
        run_at=FUTURE,
        enabled=True,
        last_run_at=NOW,
        last_run_id="manual-failure",
        last_error="manual run failed",
        last_exit_code=7,
        metadata={
            "keep": "definition policy",
            COMMAND_TIMED_OUT_METADATA_KEY: False,
            TASK_LAST_RESULT_STATUS_METADATA_KEY: "failed",
        },
    )
    store.enqueue_run(
        {
            "id": "manual-failure",
            "definition_id": "missed-after-manual-failure",
            "request_type": "scheduled",
            "source_kind": "cli",
            "status": "failed",
            "created_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW,
            "error": "manual run failed",
            "exit_code": 7,
        }
    )

    assert store.retire_missed_one_shot(
        "missed-after-manual-failure",
        expected_run_at=FUTURE,
        expected_timezone="UTC",
        expected_updated_at=NOW,
        retired_at=NOW,
    )

    row = store.get_scheduled_task("missed-after-manual-failure")
    assert row["lifecycle_detail"] == "missed"
    assert (row["last_run_at"], row["last_run_id"]) == (None, None)
    assert (row["last_error"], row["last_exit_code"]) == (None, None)
    assert row["metadata"] == {"keep": "definition policy"}
    historical = store.get_run("manual-failure")
    assert historical is not None
    assert (historical["status"], historical["error"], historical["exit_code"]) == (
        "failed",
        "manual run failed",
        7,
    )


def test_pausing_a_one_shot_before_its_moment_is_a_pause(store) -> None:
    """Switched off with the instant still ahead: the user's doing, and undoable."""
    _task(store, "held-back", schedule_type="at", run_at=FUTURE, enabled=False)

    assert store.get_scheduled_task("held-back")["lifecycle_state"] == "paused"


def test_one_shot_states_and_counts_agree_on_persisted_retirement(store) -> None:
    """Rows and counts share the same terminal fact, never the wall clock."""
    _task(store, "ahead", schedule_type="at", run_at=FUTURE, enabled=True, last_run_at=NOW)
    _task(
        store,
        "behind",
        schedule_type="at",
        run_at=PAST,
        enabled=False,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_MISSED,
    )
    _task(store, "paused-ahead", schedule_type="at", run_at=FUTURE, enabled=False)

    page = store.list_scheduled_tasks_page(page_request=PageRequest(limit=50))
    states = {row["id"]: row["lifecycle_state"] for row in page.items}
    assert states == {"ahead": "waiting", "behind": "finished", "paused-ahead": "paused"}

    counts = store.count_scheduled_tasks()
    assert (counts["waiting"], counts["finished"], counts["paused"]) == (1, 1, 1)


def test_naive_one_shot_uses_its_task_timezone_for_the_next_fire_only(store) -> None:
    """Offset-free ``run_at`` has one owner; lifecycle never reinterprets it."""

    now_utc = datetime.now(timezone.utc)
    los_angeles = ZoneInfo("America/Los_Angeles")
    shanghai = ZoneInfo("Asia/Shanghai")
    ahead = (now_utc.astimezone(los_angeles) + timedelta(hours=1)).replace(tzinfo=None)
    behind = (now_utc.astimezone(shanghai) - timedelta(hours=1)).replace(tzinfo=None)

    _task(
        store,
        "naive-ahead",
        schedule_type="at",
        run_at=ahead.isoformat(),
        timezone="America/Los_Angeles",
    )
    _task(
        store,
        "naive-behind",
        schedule_type="at",
        run_at=behind.isoformat(),
        timezone="Asia/Shanghai",
    )

    rows = {row["id"]: row for row in store.list_scheduled_tasks()}
    assert rows["naive-ahead"]["lifecycle_state"] == "waiting"
    assert rows["naive-behind"]["lifecycle_state"] == "waiting"
    assert rows["naive-ahead"]["next_run_at"] is not None
    assert rows["naive-behind"]["next_run_at"] is None

    counts = store.count_scheduled_tasks()
    assert (counts["waiting"], counts["finished"]) == (2, 0)


def test_retired_one_shot_requires_a_new_schedule_before_resume(store) -> None:
    """Every toggle doorway preserves the terminal marker and disabled switch."""

    _task(
        store,
        "retired",
        schedule_type="at",
        run_at=PAST,
        enabled=False,
        retired_at=NOW,
        retirement_reason=TASK_RETIREMENT_SCHEDULE_MISSED,
    )

    with pytest.raises(TaskScheduleRetired):
        store.set_definition_enabled(
            "retired",
            True,
            definition_type="scheduled",
        )

    row = store.get_scheduled_task("retired")
    assert row["enabled"] is False
    assert (row["retired_at"], row["retirement_reason"]) == (
        NOW,
        TASK_RETIREMENT_SCHEDULE_MISSED,
    )


def test_searching_tasks_looks_at_what_a_command_task_actually_runs(store) -> None:
    """SCT-011 -- a command task is findable by its command, like a watch already is.

    Search offers a different field list per definition type, and the ``scheduled``
    branch was written when every task was a prompt: it reads ``message``/``prompt``
    but not ``shell_command``/``command_json``. A command task stores its instruction
    in the latter and can leave ``message`` empty (nothing is being said to an Agent),
    so the only text distinguishing it was the one text search would not read. The
    ``watch`` branch has always read both columns; tasks now share them.

    ``cwd`` is deliberately still excluded here: task rows overwhelmingly share one
    working directory, so matching on it would return almost everything.
    """

    _task(store, "deploy-task", name="nightly", shell_command="./scripts/sync.sh --dry-run")
    _task(store, "argv-task", name="probe", command=["curl", "-sf", "https://health.local"])
    _task(store, "prompt-task", name="digest", prompt="summarise the day")

    def _found(query: str) -> set[str]:
        page = store.list_scheduled_tasks_page(page_request=PageRequest(limit=50), query=query)
        return {row["id"] for row in page.items}

    assert _found("sync.sh") == {"deploy-task"}, "a shell command task is unfindable"
    assert _found("health.local") == {"argv-task"}, "an argv command task is unfindable"
    assert _found("summarise") == {"prompt-task"}, "and message search still works"
    # The chips count the same rows the list returns, because both go through
    # ``_definitions_query``; a field added to one but not the other splits them.
    assert store.count_scheduled_tasks(query="sync.sh")["total"] == 1


def test_an_escalation_turn_is_not_a_verdict_on_the_command_that_queued_it(store) -> None:
    """SCT-014 -- the Agent turn REPORTING a failure must not report it as a success.

    A ``--on-failure agent`` escalation is an ``agent_runs`` row carrying the failing
    definition's own ``definition_id`` -- which is what links the turn to the task -- and
    it settles ``succeeded`` whenever the Agent answers, because answering is all it was
    asked to do. Read as one of the definition's verdicts it is the newest row in the
    window, so the health badge went green on the strength of the very turn that exists
    to say the command broke, "last succeeded" pointed at that turn, and a success
    between two failures closed the failure streak.

    Exactly the ``watch_runtime`` shape (a row sharing a definition's id that is not
    that definition's outcome), so it takes the same exclusion.
    """

    settled = datetime.now(timezone.utc)
    fresh = (settled - timedelta(minutes=5)).isoformat()
    later = settled.isoformat()

    _task(store, "backup", name="nightly backup", shell_command="./scripts/backup.sh")
    for index in range(2):
        _run(
            store,
            f"fire-{index}",
            "backup",
            request_type="scheduled",
            run_type="scheduled",
            status="failed",
            error="command exited with status 2",
            created_at=fresh,
            completed_at=fresh,
        )
    # The turn the second failure queued, and it answered.
    _run(
        store,
        "escalation-1",
        "backup",
        request_type="task_escalation",
        run_type="task_escalation",
        status="succeeded",
        parent_run_id="fire-1",
        created_at=later,
        completed_at=later,
    )

    health = store.definition_health("backup")
    assert health["health"] == "failing", (
        "the escalation turn reporting the failure was counted as the task succeeding: "
        f"{health}"
    )
    assert health["consecutive_failures"] == 2
    assert store.last_success_settled_at("backup") is None, (
        "the definition has never succeeded, but the escalation turn claimed it did"
    )


def test_mark_cycle_result_stamps_a_finish_only_when_it_retires_the_watch(tmp_path: Path) -> None:
    """Only the cycle that changes enabled -> disabled owns retirement state."""
    from core.watches import ManagedWatchStore

    store = ManagedWatchStore(tmp_path / "watches.json")
    paused = store.add_watch(
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

    # A result landing after a manual pause cannot turn it into retirement.
    store.set_enabled(paused.id, False)
    before = (paused.last_finished_at, paused.retired_at)
    store.mark_cycle_result(paused.id, exit_code=0, error=None, disable=True)
    assert (paused.last_finished_at, paused.retired_at) == before == (None, None)

    retiring = store.add_watch(
        name="retiring",
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
    store.mark_cycle_result(retiring.id, exit_code=124, error=None, disable=True)
    retired = store.get_watch(retiring.id)
    assert retired.last_finished_at is not None
    assert retired.retired_at is not None
    assert retired.last_error is None
    assert retired.last_exit_code == 124
    assert retired.enabled is False

    # A later non-disabling result cannot erase a genuine retirement.
    before = (
        retired.last_finished_at,
        retired.retired_at,
        retired.last_exit_code,
        retired.last_error,
    )
    store.mark_cycle_result(
        retiring.id,
        exit_code=7,
        error="late cycle failed",
        event_detected=True,
        disable=False,
    )
    assert (
        retired.last_finished_at,
        retired.retired_at,
        retired.last_exit_code,
        retired.last_error,
    ) == before

    continuing = store.add_watch(
        name="continuing",
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
    continuing.last_finished_at = NOW
    continuing.retired_at = NOW
    store.upsert_watch(continuing)
    store.mark_cycle_result(continuing.id, exit_code=1, error="retrying", disable=False)
    assert continuing.last_finished_at is None
    assert continuing.retired_at is None


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
