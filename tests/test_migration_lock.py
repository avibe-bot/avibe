"""What the migration lock promises, stated as properties.

Two of them, needed together by the same callers: at most one migrator per
database across every process on the machine, and re-entrance for the thread
that already holds it. Neither can be observed from the calling thread alone --
the lock is re-entrant, so a test that takes it and then checks the code under
test is refused has only checked that re-entrance works.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from storage.lock import MigrationFileLock, MigrationLockTimeout, migration_lock_path_for

REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_for_another_thread(lock_path: Path) -> bool:
    """Whether a thread that is not this one could take the lock right now."""

    outcome: list[bool] = []

    def attempt() -> None:
        try:
            with MigrationFileLock(lock_path, timeout_seconds=0):
                outcome.append(True)
        except MigrationLockTimeout:
            outcome.append(False)

    probe = threading.Thread(target=attempt, daemon=True)
    probe.start()
    probe.join(30)
    assert outcome, "the probe neither took the lock nor was refused"
    return outcome[0]


def test_the_holder_re_enters_while_every_other_thread_is_excluded(tmp_path: Path) -> None:
    # Nesting is ordinary, not a corner: ensure_sqlite_state holds this lock
    # across run_migrations, which takes it too. It cannot come from the OS lock
    # either -- flock attaches to an open file description, so the nested open a
    # second acquire performs is a different description and blocks against the
    # first, in the very thread already holding it.
    lock_path = tmp_path / "migration.lock"
    assert _free_for_another_thread(lock_path) is True

    with MigrationFileLock(lock_path, timeout_seconds=None):
        assert _free_for_another_thread(lock_path) is False
        with MigrationFileLock(lock_path, timeout_seconds=0):
            assert _free_for_another_thread(lock_path) is False
        # Leaving the inner acquire must not hand the lock away while the outer
        # one is still holding it.
        assert _free_for_another_thread(lock_path) is False

    assert _free_for_another_thread(lock_path) is True


def test_re_entrance_belongs_to_the_path_not_to_one_lock_object(tmp_path: Path) -> None:
    # Nested callers do not share objects. `ensure_sqlite_state` and
    # `run_migrations` each construct their own lock; all they share is the file
    # they are protecting, so that file is where the depth has to live.
    lock_path = tmp_path / "migration.lock"
    outer = MigrationFileLock(lock_path, timeout_seconds=None)
    inner = MigrationFileLock(lock_path, timeout_seconds=0)

    with outer:
        with inner:
            assert _free_for_another_thread(lock_path) is False
        assert _free_for_another_thread(lock_path) is False

    assert _free_for_another_thread(lock_path) is True


def test_the_lock_follows_the_database_not_the_route_taken_to_it(tmp_path: Path) -> None:
    # Two callers that disagree about which directory is "the" state directory
    # would otherwise take two different locks over one database and both
    # proceed, which is the whole failure this lock exists to prevent.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    db_path.touch()
    alias = tmp_path / "alias"
    alias.symlink_to(state_dir, target_is_directory=True)

    assert migration_lock_path_for(alias / "vibe.sqlite") == migration_lock_path_for(db_path)
    assert migration_lock_path_for(state_dir / "." / "vibe.sqlite") == migration_lock_path_for(db_path)
    assert migration_lock_path_for(db_path).parent == db_path.resolve().parent


def test_run_migrations_excludes_another_process_on_the_same_database(tmp_path: Path) -> None:
    # The controller and the Web UI are separate processes over one state
    # directory, so a second migrator is the ordinary pairing rather than a
    # corner. A thread lock cannot express this, which is why the entry points
    # that only had one could run `command.upgrade` against a file another
    # process was upgrading at the same moment.
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "vibe.sqlite"
    ready = tmp_path / "ready"
    done = tmp_path / "done"
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from storage.migrations import run_migrations\n"
        "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
        "run_migrations(Path(sys.argv[1]), revision='20260627_0025')\n"
        "Path(sys.argv[3]).write_text('done', encoding='utf-8')\n"
    )

    with MigrationFileLock(migration_lock_path_for(db_path), timeout_seconds=None):
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(db_path), str(ready), str(done)],
            cwd=REPO_ROOT,
            env=_child_env(tmp_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _wait_for(ready.exists, "the child never reached run_migrations")
            # It announced itself before the call, so from here on it is either
            # blocked on the lock or has walked straight past it.
            time.sleep(1.0)
            assert child.poll() is None, "the child returned while the lock was held elsewhere"
            assert not done.exists()
            assert not db_path.exists(), "the child migrated a database this process had locked"
        except BaseException:
            child.kill()
            child.communicate()
            raise

    stdout, stderr = child.communicate(timeout=120)
    assert child.returncode == 0, f"child failed: {stdout}\n{stderr}"
    assert done.exists()
    assert db_path.is_file()


def _child_env(home: Path) -> dict[str, str]:
    """A child that cannot reach the developer's real state.

    conftest isolates this process, and the child inherits none of that, so it
    is stated again here rather than assumed.
    """

    isolated = home / "home"
    isolated.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(isolated),
            "AVIBE_HOME": str(isolated / ".avibe"),
            "XDG_CONFIG_HOME": str(isolated / "config"),
            "XDG_DATA_HOME": str(isolated / "data"),
            "XDG_STATE_HOME": str(isolated / "state"),
            "XDG_CACHE_HOME": str(isolated / "cache"),
        }
    )
    return env


def _wait_for(condition, message: str, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    pytest.fail(message)
