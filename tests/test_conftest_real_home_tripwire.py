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

import os
import sqlite3
import stat
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


def test_symlink():
    _in_a_swallowing_daemon_thread(lambda: os.symlink(HOME / ".avibe", HOME / ".avibe" / "leaked-link"))


def test_hard_link():
    config = HOME / ".avibe" / "config"
    _in_a_swallowing_daemon_thread(lambda: os.link(config / "config.json", config / "leaked-link.json"))


def test_chmod():
    _in_a_swallowing_daemon_thread(lambda: os.chmod(HOME / ".avibe" / "config" / "config.json", 0o777))


def test_cwd_relative_path():
    previous = os.getcwd()
    os.chdir(HOME / ".avibe")
    try:
        _in_a_swallowing_daemon_thread(lambda: os.mkdir("leaked-relative"))
    finally:
        os.chdir(previous)


def test_dir_fd_relative_path():
    state = os.open(HOME / ".avibe" / "state", os.O_RDONLY)
    try:
        _in_a_swallowing_daemon_thread(lambda: os.mkdir("leaked-at", dir_fd=state))
    finally:
        os.close(state)


def test_symlinked_alias():
    # The alias lives outside the protected homes and names one of them.
    (HOME / "alias").symlink_to(HOME / ".avibe")
    _in_a_swallowing_daemon_thread(lambda: (HOME / "alias" / "leaked.txt").write_text("leaked"))


def test_custom_home():
    _in_a_swallowing_daemon_thread(lambda: (HOME / "custom-home" / "leaked.txt").write_text("leaked"))


def test_sqlite_read_only_mode():
    # `mode=ro` on a WAL database can still create its `-shm` sidecar.
    database = (HOME / ".avibe" / "state" / "other.sqlite").as_uri()
    _in_a_swallowing_daemon_thread(lambda: sqlite3.connect(f"{database}?mode=ro", uri=True))
"""

_READS = """
import sqlite3
from pathlib import Path

HOME = Path(__HOME__)


def test_reads_stay_allowed():
    assert (HOME / ".avibe" / "config" / "config.json").read_text() == "{}"
    database = (HOME / ".avibe" / "state" / "vibe.sqlite").as_uri()
    with sqlite3.connect(f"{database}?immutable=1", uri=True) as conn:
        assert conn.execute("select count(*) from sqlite_master").fetchone() == (0,)
"""


# A daemon thread that finishes after the last test: while the session reports,
# or while pytest unconfigures -- after every other unconfigure hook has run.
_LATE_WRITE_CONFTEST = """
from pathlib import Path

import pytest


def _write():
    try:
        (Path(__HOME__) / ".avibe" / "late.txt").write_text("leaked")
    except BaseException:
        pass


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary():
    if __WHEN__ == "reporting":
        _write()


@pytest.hookimpl(wrapper=True)
def pytest_unconfigure():
    result = yield
    if __WHEN__ == "unconfiguring":
        _write()
    return result
"""


# A worker that outlives its test and resolves the home only once that test has
# torn down -- the incident's Web Push thread, made deterministic: the hook below
# runs after every fixture of the test is finalized, then lets the worker go.
_OUTLIVING_WORKER_CONFTEST = """
import threading
from pathlib import Path

import pytest

released = threading.Event()


def resolve_the_home_after_teardown():
    released.wait(10)
    from config import paths

    state = paths.get_state_dir()
    Path(__OUT__).write_text(f"{Path.home()}\\n{state}\\n")
    state.mkdir(parents=True, exist_ok=True)
    (state / "written-after-teardown.txt").write_text("leaked")


worker = threading.Thread(target=resolve_the_home_after_teardown, daemon=True)


@pytest.fixture
def outliving_worker():
    worker.start()


def pytest_runtest_logfinish(nodeid):
    released.set()
    worker.join(10)
"""


@pytest.fixture
def stand_in_home(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The inner run's real home: pytester's ``HOME``, seeded like an installed Avibe.

    The inner run also starts with ``AVIBE_HOME`` naming a custom home beside it.
    """

    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    home = pytester.path
    (home / "custom-home").mkdir()
    # In tilde form, which only the real HOME expands to the custom home.
    monkeypatch.setenv("AVIBE_HOME", "~/custom-home")
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
    seeded_mode = stat.S_IMODE((stand_in_home / ".avibe" / "config" / "config.json").stat().st_mode)
    result = _run(pytester, stand_in_home, _BACKGROUND_WRITES)

    # Every inner test passes: only the session-level check can fail this run.
    result.assert_outcomes(passed=13)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, result.stdout.str()
    output = result.stdout.str()
    for event, path in [
        ("sqlite3.connect", ".avibe/state/vibe.sqlite"),
        ("open", ".avibe/config/config.json"),
        ("os.mkdir", ".avibe/leaked"),
        ("os.rename", ".avibe/config/config.json"),
        ("open", ".vibe_remote/leaked.txt"),
        ("os.symlink", ".avibe/leaked-link"),
        ("os.link", ".avibe/config/leaked-link.json"),
        ("os.chmod", ".avibe/config/config.json"),
        ("os.mkdir", ".avibe/leaked-relative"),
        ("open", "alias/leaked.txt"),
        ("open", "custom-home/leaked.txt"),
        ("sqlite3.connect", ".avibe/state/other.sqlite"),
    ]:
        assert f"{event} {stand_in_home / path}" in output, output
    # A descriptor names its directory as resolved.
    assert f"os.mkdir {os.path.realpath(stand_in_home / '.avibe' / 'state')}/leaked-at" in output, output

    # Refused, not merely reported: the home is exactly as it was seeded.
    config = stand_in_home / ".avibe" / "config" / "config.json"
    assert config.read_text() == "{}"
    assert stat.S_IMODE(config.stat().st_mode) == seeded_mode
    for leaked in (
        ".avibe/leaked",
        ".avibe/config/leaked.json",
        ".vibe_remote/leaked.txt",
        ".avibe/leaked-link",
        ".avibe/config/leaked-link.json",
        ".avibe/leaked-relative",
        ".avibe/state/leaked-at",
        ".avibe/leaked.txt",
        "custom-home/leaked.txt",
        ".avibe/state/other.sqlite",
    ):
        assert not os.path.lexists(stand_in_home / leaked), leaked
    with sqlite3.connect(f"{(stand_in_home / '.avibe' / 'state' / 'vibe.sqlite').as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("select name from sqlite_master").fetchall() == []


@pytest.mark.parametrize("when", ["reporting", "unconfiguring"])
def test_a_write_after_the_last_test_still_fails_the_run(
    pytester: pytest.Pytester, stand_in_home: Path, when: str
) -> None:
    conftest = _LATE_WRITE_CONFTEST.replace("__HOME__", repr(str(stand_in_home)))
    pytester.makeconftest(conftest.replace("__WHEN__", repr(when)))
    result = _run(pytester, stand_in_home, "def test_nothing():\n    pass\n")

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED, result.stdout.str() + result.stderr.str()
    if when == "reporting":
        assert f"open {stand_in_home / '.avibe' / 'late.txt'}" in result.stderr.str()
    assert not (stand_in_home / ".avibe" / "late.txt").exists()


def test_work_outliving_its_test_lands_in_the_session_home(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between isolations the run's home is a throwaway one, never the real one."""

    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.delenv("AVIBE_HOME")  # the stand-in real home is the default one
    real_home = pytester.path
    out = pytester.path / "resolved.txt"
    pytester.makeconftest(_OUTLIVING_WORKER_CONFTEST.replace("__OUT__", repr(str(out))))
    pytester.makepyfile(test_inner="def test_starts_a_worker(outliving_worker):\n    pass\n")

    result = pytester.runpytest_subprocess("-p", "tests.conftest", "-p", "no:cacheprovider")

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK, result.stdout.str() + result.stderr.str()
    resolved_home, resolved_state = out.read_text().splitlines()
    assert not Path(resolved_home).is_relative_to(real_home), resolved_home
    assert not Path(resolved_state).is_relative_to(real_home), resolved_state
    assert not (real_home / ".avibe").exists()


def test_reading_the_real_home_does_not_trip_it(pytester: pytest.Pytester, stand_in_home: Path) -> None:
    result = _run(pytester, stand_in_home, _READS)

    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.OK, result.stdout.str()
