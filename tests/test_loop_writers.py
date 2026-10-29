"""The controller-loop SQLite writer census (#2710).

The census replaces a hand-kept list of loop-side writers that review kept finding
incomplete, so what it must get right is which transactions it counts: every write
transaction the loop runs, under the code location that ran it, and nothing else.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading

import pytest
from sqlalchemy import text

from sqlalchemy.exc import IntegrityError, OperationalError

from core.session_turns import _try_reserve_write_lock
from storage import loop_writers
from storage.agent_session_rows import reserve_write_lock
from storage.db import create_sqlite_engine


@pytest.fixture
def census(monkeypatch, tmp_path):
    monkeypatch.setattr(loop_writers, "_totals", {})
    monkeypatch.setattr(loop_writers, "_changed_since_summary", False)
    monkeypatch.setattr(loop_writers, "_last_summary_at", float("inf"))
    monkeypatch.setattr(loop_writers, "_loop_thread", None)
    loop_writers.bind_loop_thread()
    engine = create_sqlite_engine(tmp_path / "state.sqlite")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"))
    loop_writers._totals.clear()
    yield engine
    engine.dispose()


def _sites() -> dict[str, list[float]]:
    return {
        frames[0][2]: totals
        for frames, totals in loop_writers._totals.items()
    }


def _matching_sites(*, filename: str, name: str) -> list[tuple[tuple, list[float]]]:
    return [
        (site, totals)
        for site, totals in loop_writers._totals.items()
        if site and site[0][0].endswith(filename) and site[0][2] == name
    ]


def _explicit_write(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO notes (body) VALUES ('a')"))


def _autobegun_write(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("UPDATE notes SET body = 'b'"))
        conn.commit()


def _read(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT count(*) FROM notes")).scalar()


def _rolled_back_write(engine) -> None:
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM notes"))
        conn.rollback()


def _never_waiting_write(engine) -> None:
    with engine.begin() as conn:
        assert _try_reserve_write_lock(conn)
        conn.execute(text("INSERT INTO notes (body) VALUES ('c')"))


def _timed_out_writer_wait(engine, db_path) -> None:
    """``reserve_write_lock`` while another connection holds the writer: the wait
    times out and leaves no transaction, but it held the loop."""

    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA busy_timeout = 50")
            try:
                with pytest.raises(OperationalError, match="locked"):
                    reserve_write_lock(conn)
            finally:
                conn.exec_driver_sql("PRAGMA busy_timeout = 5000")
    finally:
        holder.rollback()
        holder.close()


def test_loop_side_metadata_ddl_is_counted(census, tmp_path) -> None:
    """The known ``metadata.create_all`` path reports its autocommit DDL once."""

    from storage.sessions_service import SQLiteSessionsService

    loop_writers._totals.clear()
    service = SQLiteSessionsService(tmp_path / "metadata.sqlite")
    service.close()

    matches = _matching_sites(filename="storage/sessions_service.py", name="__init__")
    assert len(matches) == 1
    _site, totals = matches[0]
    assert totals[0] == 1
    assert totals[1] > 0
    assert totals[2] > 0
    assert totals[3] == 0


def test_loop_side_journal_mode_setup_is_counted(census, tmp_path) -> None:
    """The connection-initialization WAL change is visible when setup runs on-loop."""

    loop_writers._totals.clear()
    engine = create_sqlite_engine(tmp_path / "journal.sqlite")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
    finally:
        engine.dispose()

    matches = _matching_sites(filename="storage/db.py", name="_set_sqlite_pragmas")
    assert len(matches) == 1
    _site, totals = matches[0]
    assert totals[0] == 1
    assert totals[1] > 0
    assert totals[2] > 0


def test_off_loop_connection_setup_is_not_counted(census, tmp_path) -> None:
    """A worker opening a normal engine does not enter the controller census."""

    loop_writers._totals.clear()
    errors: list[BaseException] = []

    def setup() -> None:
        try:
            engine = create_sqlite_engine(tmp_path / "off-loop.sqlite")
            with engine.connect():
                pass
            engine.dispose()
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    worker = threading.Thread(target=setup)
    worker.start()
    worker.join()

    assert errors == []
    assert loop_writers._totals == {}


def test_read_only_pragmas_are_not_counted(census) -> None:
    """Connection-local pragma settings and data-version reads are not writers."""

    loop_writers._totals.clear()
    with census.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        conn.exec_driver_sql("PRAGMA busy_timeout = 5000")
        conn.exec_driver_sql("PRAGMA data_version").scalar_one()

    assert loop_writers._totals == {}


def test_a_failed_marked_autocommit_write_is_closed_once(census) -> None:
    """A failed marked operation is counted, and its checkout record stays clean."""

    def failed_ddl() -> None:
        with census.connect() as conn:
            with loop_writers.track_autocommit_write(conn):
                conn.exec_driver_sql("CREATE TABLE broken (")

    with pytest.raises(OperationalError, match="incomplete input"):
        failed_ddl()

    matches = _matching_sites(filename="tests/test_loop_writers.py", name="failed_ddl")
    assert len(matches) == 1
    _site, totals = matches[0]
    assert totals[0] == 1
    assert totals[1] > 0

    failed_counts = [
        totals[0]
        for site, totals in loop_writers._totals.items()
        if site[0][2] == "failed_ddl"
    ]
    assert failed_counts == [1]


def test_the_census_counts_exactly_the_loops_write_transactions(census) -> None:
    """Every write the loop thread runs is counted under the function that ran it,
    whether it began explicitly or on its first statement, committed or rolled back.
    Reads and writes on other threads are not counted: they do not hold the loop."""

    for _ in range(2):
        _explicit_write(census)
    _autobegun_write(census)
    _rolled_back_write(census)
    _read(census)
    _never_waiting_write(census)
    _timed_out_writer_wait(census, census.url.database)
    off_loop = threading.Thread(target=_explicit_write, args=(census,))
    off_loop.start()
    off_loop.join()
    loop_writers.unbind_loop_thread()
    _autobegun_write(census)

    sites = _sites()
    assert set(sites) == {
        "_explicit_write",
        "_autobegun_write",
        "_rolled_back_write",
        "_never_waiting_write",
        "_timed_out_writer_wait",
    }
    assert sites["_autobegun_write"][0] == 1, "a write after the loop stopped serving was counted"
    assert sites["_explicit_write"][0] == 2, "the off-loop write was counted, or a loop write was missed"
    assert all(totals[1] > 0 and totals[2] > 0 for totals in sites.values())
    never_waits = {name for name, totals in sites.items() if totals[3]}
    assert never_waits == {"_never_waiting_write"}
    assert sites["_timed_out_writer_wait"][1] >= 0.05, "the timed-out wait was not timed"


def test_a_write_is_timed_to_the_end_of_sqlites_commit(census, monkeypatch) -> None:
    """The loop time of a write includes SQLite's own commit, ``fsync`` and all: the
    census records it only once the commit has finished, not when SQLAlchemy is
    about to issue it."""

    seen: list[bool] = []
    original = loop_writers._count

    def count(site, elapsed, *, never_waits):
        seen.append(bool(holder["conn"].in_transaction))
        original(site, elapsed, never_waits=never_waits)

    holder: dict = {}
    monkeypatch.setattr(loop_writers, "_count", count)
    with census.begin() as conn:
        holder["conn"] = conn.connection.dbapi_connection
        conn.execute(text("INSERT INTO notes (body) VALUES ('d')"))

    assert seen == [False], "the write was counted before its commit finished"


def test_a_site_names_the_caller_of_a_shared_transaction_helper(census) -> None:
    """A write behind a helper is reported with the helper's caller too, so a helper
    shared by many writers does not hide which of them ran on the loop."""

    def caller_one():
        _explicit_write(census)

    def caller_two():
        _explicit_write(census)

    caller_one()
    caller_two()

    callers = sorted(frames[1][2] for frames in loop_writers._totals)
    assert callers == ["caller_one", "caller_two"]


def test_the_census_reports_new_sites_once_and_ranks_the_summary(census, caplog) -> None:
    caplog.set_level(logging.INFO, logger=loop_writers.__name__)

    for _ in range(2):
        _explicit_write(census)
    _autobegun_write(census)
    loop_writers.log_summary()
    loop_writers.log_summary()
    loop_writers.log_summary(final=True)

    messages = [record.getMessage() for record in caplog.records]
    first_seen = [message for message in messages if message.startswith("Controller-loop SQLite write site:")]
    assert len(first_seen) == 2, "a site was reported more than once, or not at all"
    summaries = [message for message in messages if message.startswith("Controller-loop SQLite writers")]
    assert len(summaries) == 2, "an unchanged periodic summary was logged, or the final one was skipped"
    assert "3 writes at 2 sites" in summaries[0]
    assert summaries[1].startswith("Controller-loop SQLite writers at shutdown")
    assert "tests/test_loop_writers.py:" in summaries[0]


def test_a_commit_that_fails_counts_its_transaction_once(census) -> None:
    """A commit can fail with the transaction still open, as a deferred constraint
    does. The rollback that follows ends that transaction, and it is counted once."""

    with census.begin() as conn:
        conn.execute(text("CREATE TABLE parents (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER "
                "REFERENCES parents (id) DEFERRABLE INITIALLY DEFERRED)"
            )
        )
    loop_writers._totals.clear()

    def _deferred_violation():
        with pytest.raises(IntegrityError):
            with census.begin() as conn:
                conn.execute(text("INSERT INTO children (parent_id) VALUES (42)"))

    _deferred_violation()

    assert {name: int(totals[0]) for name, totals in _sites().items()} == {"_deferred_violation": 1}


def test_the_shutdown_summary_lists_every_site(census, caplog, monkeypatch) -> None:
    """A periodic summary is capped; the final one keeps every site, so a rotated log
    cannot lose a rare writer from the inventory."""

    caplog.set_level(logging.INFO, logger=loop_writers.__name__)
    monkeypatch.setattr(loop_writers, "SUMMARY_SITES", 2)
    for name in ("writer_a", "writer_b", "writer_c"):
        namespace: dict = {"_explicit_write": _explicit_write}
        exec(f"def {name}(engine):\n    _explicit_write(engine)\n", namespace)
        namespace[name](census)

    loop_writers.log_summary()
    loop_writers.log_summary(final=True)

    periodic, final = [record.getMessage() for record in caplog.records if "SQLite writers" in record.getMessage()]
    assert "(top 2)" in periodic and len(periodic.splitlines()) == 3
    assert "(top" not in final and len(final.splitlines()) == 4


def test_the_final_summary_reports_a_run_without_writes(census, caplog) -> None:
    """A run that wrote nothing on the loop still ends with a census, so it cannot be
    mistaken for a census that never ran."""

    caplog.set_level(logging.INFO, logger=loop_writers.__name__)

    loop_writers.log_summary(final=True)

    assert [record.getMessage() for record in caplog.records] == [
        "Controller-loop SQLite writers at shutdown: 0 writes at 0 sites, by total loop time"
    ]


def test_the_site_walk_is_bounded(census) -> None:
    """The walk inspects at most ``_MAX_FRAMES`` frames per write, so a deep stack
    inside the internal modules costs the same and reports an unknown site."""

    def internal_depth(depth: int):
        if depth:
            return internal_depth(depth - 1)
        return loop_writers._site(sys._getframe())

    original = loop_writers._INTERNAL_PREFIXES
    loop_writers._INTERNAL_PREFIXES = original + (__file__,)
    try:
        assert internal_depth(3 * loop_writers._MAX_FRAMES) == ()
    finally:
        loop_writers._INTERNAL_PREFIXES = original
    assert loop_writers._format_site(()) == "unknown"
