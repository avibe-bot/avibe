"""The controller-loop SQLite writer census (#2710).

The census replaces a hand-kept list of loop-side writers that review kept finding
incomplete, so what it must get right is which transactions it counts: every write
transaction the loop runs, under the code location that ran it, and nothing else.
"""

from __future__ import annotations

import logging
import sys
import threading

import pytest
from sqlalchemy import text

from core.session_turns import _try_reserve_write_lock
from storage import loop_writers
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
    off_loop = threading.Thread(target=_explicit_write, args=(census,))
    off_loop.start()
    off_loop.join()

    sites = _sites()
    assert set(sites) == {"_explicit_write", "_autobegun_write", "_rolled_back_write", "_never_waiting_write"}
    assert sites["_explicit_write"][0] == 2, "the off-loop write was counted, or a loop write was missed"
    assert all(totals[1] > 0 and totals[2] > 0 for totals in sites.values())
    never_waits = {name: int(totals[3]) for name, totals in sites.items()}
    assert never_waits == {
        "_explicit_write": 0,
        "_autobegun_write": 0,
        "_rolled_back_write": 0,
        "_never_waiting_write": 1,
    }


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
