"""A write under the developer's real Avibe home fails the run, whoever makes it.

The incident this guards: a Web Push daemon thread outlived the test that
scheduled it, woke while the per-test home isolation was undone between two
tests, and migrated the developer's real ``~/.avibe/state/vibe.sqlite``. The
migration guard could not see it -- conftest sets its opt-in flag and patches
``Path.home`` for every test -- and the thread swallowed every exception, so the
run stayed green. The tripwire in ``tests/conftest.py`` refuses such a write and
fails the session even when the writer swallows the refusal.

Each inner run loads the real ``tests/conftest.py`` as a plugin. ``pytester``
points ``HOME`` at its own temporary directory, which the inner run therefore
takes for the developer's home: nothing here can reach the actual one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every write shape the tripwire refuses, each from a daemon thread whose handler
# swallows whatever the write raises -- the incident's shape exactly.
_BACKGROUND_WRITES = """
import os
import sqlite3
import threading
from pathlib import Path

HOME = Path(__HOME__)


def _in_a_swallowing_daemon_thread(write):
    def run():
        try:
            write()
        except BaseException:
            pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join()


def _migrate():
    with sqlite3.connect(HOME / ".avibe" / "state" / "vibe.sqlite") as conn:
        conn.execute("create table leaked (x)")


def test_sqlite_connect():
    _in_a_swallowing_daemon_thread(_migrate)


def test_write_mode_open():
    _in_a_swallowing_daemon_thread(lambda: (HOME / ".avibe" / "config" / "config.json").write_text("leaked"))


def test_mkdir():
    _in_a_swallowing_daemon_thread(lambda: (HOME / ".avibe" / "leaked").mkdir())


def test_rename():
    config = HOME / ".avibe" / "config"
    _in_a_swallowing_daemon_thread(lambda: os.rename(config / "config.json", config / "leaked.json"))


def test_legacy_home():
    _in_a_swallowing_daemon_thread(lambda: (HOME / ".vibe_remote" / "leaked.txt").write_text("leaked"))
"""

_READS = """
import sqlite3
from pathlib import Path

HOME = Path(__HOME__)


def test_reads_stay_allowed():
    assert (HOME / ".avibe" / "config" / "config.json").read_text() == "{}"
    database = (HOME / ".avibe" / "state" / "vibe.sqlite").as_uri()
    with sqlite3.connect(f"{database}?mode=ro", uri=True) as conn:
        assert conn.execute("select count(*) from sqlite_master").fetchone() == (0,)
"""


# A daemon thread that finishes while the session reports, after its last test.
_LATE_WRITE_CONFTEST = """
from pathlib import Path

import pytest


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary():
    try:
        (Path(__HOME__) / ".avibe" / "late.txt").write_text("leaked")
    except BaseException:
        pass
"""


@pytest.fixture
def stand_in_home(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The inner run's real home: pytester's ``HOME``, seeded like an installed Avibe."""

    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    home = pytester.path
    (home / ".avibe" / "config").mkdir(parents=True)
    (home / ".avibe" / "state").mkdir()
    (home / ".vibe_remote").mkdir()
    (home / ".avibe" / "config" / "config.json").write_text("{}")
    sqlite3.connect(home / ".avibe" / "state" / "vibe.sqlite").close()
    return home


def _run(pytester: pytest.Pytester, home: Path, source: str) -> pytest.RunResult:
    pytester.makepyfile(test_inner=source.replace("__HOME__", repr(str(home))))
    return pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider")


def test_a_swallowed_background_write_under_the_real_home_fails_the_run(
    pytester: pytest.Pytester, stand_in_home: Path
) -> None:
    result = _run(pytester, stand_in_home, _BACKGROUND_WRITES)

    # Every inner test passes: only the session-level check can fail this run.
    result.assert_outcomes(passed=5)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, result.stdout.str()
    output = result.stdout.str()
    for event, path in [
        ("sqlite3.connect", ".avibe/state/vibe.sqlite"),
        ("open", ".avibe/config/config.json"),
        ("os.mkdir", ".avibe/leaked"),
        ("os.rename", ".avibe/config/config.json"),
        ("open", ".vibe_remote/leaked.txt"),
    ]:
        assert f"{event} {stand_in_home / path}" in output, output

    # Refused, not merely reported: the home is exactly as it was seeded.
    assert (stand_in_home / ".avibe" / "config" / "config.json").read_text() == "{}"
    assert not (stand_in_home / ".avibe" / "leaked").exists()
    assert not (stand_in_home / ".avibe" / "config" / "leaked.json").exists()
    assert not (stand_in_home / ".vibe_remote" / "leaked.txt").exists()
    with sqlite3.connect(f"{(stand_in_home / '.avibe' / 'state' / 'vibe.sqlite').as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("select name from sqlite_master").fetchall() == []


def test_a_write_after_the_last_test_still_fails_the_run(pytester: pytest.Pytester, stand_in_home: Path) -> None:
    pytester.makeconftest(_LATE_WRITE_CONFTEST.replace("__HOME__", repr(str(stand_in_home))))
    result = _run(pytester, stand_in_home, "def test_nothing():\n    pass\n")

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, result.stdout.str()
    assert f"open {stand_in_home / '.avibe' / 'late.txt'}" in result.stderr.str()
    assert not (stand_in_home / ".avibe" / "late.txt").exists()


def test_reading_the_real_home_does_not_trip_it(pytester: pytest.Pytester, stand_in_home: Path) -> None:
    result = _run(pytester, stand_in_home, _READS)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK, result.stdout.str()
