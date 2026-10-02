"""Shared pytest fixtures for the Vibe Remote test suite.

Per AGENTS.md ("Tests and probes must never mutate the current local
environment or live user state"), every test runs against an isolated
data directory by default, so config writes, state files, runtime
markers, and backend credential files can never leak into the
developer's real home.

Historically a handful of install / upgrade tests mocked
``resolve_cli_path`` to return fixture paths like
``/Users/test/.nvm/.../codex`` but did not isolate the config directory.
The post-install bookkeeping in ``vibe.api._run_install_command`` then
called ``load_config()`` / ``cfg.save()`` against the real config.json and
persisted the fixture path, surfacing in the UI after the next restart.

Isolation mechanism: when pytest loads this file, before any product module is
imported, ``HOME``, the XDG homes, ``AVIBE_HOME``, ``CODEX_HOME``,
``CLAUDE_CONFIG_DIR`` and ``pathlib.Path.home`` move to a throwaway session home
for the whole run, and each test then swaps in its own tmp home on top of that.
The real home is never the ambient one, not even between tests, so work that
outlives its test lands in the session home. ``config.paths.get_vibe_remote_dir``
therefore runs as written — only its env-var-set branch is exercised under isolation, and
the function itself is never replaced, so the suite still catches regressions in
path-resolution logic while Python helpers, subprocesses, and ``expanduser("~")``
do not see the developer's real home.

The same hazard applies to the agent backends' on-disk credential files:
Codex resolves its home from ``CODEX_HOME`` (falling back to ``~/.codex``)
and Claude Code from ``CLAUDE_CONFIG_DIR`` (falling back to ``~/.claude``).
Tests that drive ``apply_codex_auth`` / ``apply_claude_auth`` — directly or
through the auth-setup scenario harness — would otherwise rewrite the
developer's real ``~/.codex/auth.json`` (dropping ``OPENAI_API_KEY``) and
``~/.claude/settings.json`` (dropping ``ANTHROPIC_*`` env). OpenCode has
no dedicated config-home env var in our helper layer and resolves
``~/.local/share/opencode/auth.json`` from ``Path.home()``, so the
patched home is its isolation boundary. We pin all three to per-test tmp
dirs for the same reason.

Path-resolution tests (e.g. ``tests/test_v2_paths.py::test_paths_are_under_home``)
intentionally cover the env-var-unset branch where ``get_vibe_remote_dir``
falls back to the default home. Those opt out with
``@pytest.mark.uses_real_paths``, run against the real environment, and
must remain read-only (they may not call ``cfg.save()`` or otherwise
write to ``~/.avibe/`` or legacy ``~/.vibe_remote/``).
"""

from __future__ import annotations

import ast
import errno
import importlib
import inspect
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import unittest
import urllib.parse
import urllib.request
import warnings
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

import psutil
import pytest
from sqlalchemy.exc import SAWarning

from tests.fake_pid_helpers import PID_LIMIT

REAL_USER_HOME = Path.home()
_SQLITE_DEFAULT_STATE_MODULES: dict[Path, bool] = {}

# The variables that name a home, and the caller identity an Agent-launched
# pytest inherits from the live conversation. Tests must opt in to that context
# explicitly, or unrelated Harness/session assertions bind themselves to the
# live Agent session.
_HOME_ENV = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "AVIBE_HOME",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
)
_CALLER_ENV = (
    "AVIBE_SESSION_ID",
    "AVIBE_CALLER_SESSION_PROOF",
    "AVIBE_RUN_ID",
    "AVIBE_NATIVE_SESSION_ID",
    "AVIBE_CALLER_SOURCE",
    "AVIBE_CALLER_BACKEND",
    "AVIBE_CALLER_PLATFORM",
    "AVIBE_CALLER_USER_ID",
    "AVIBE_CALLER_CHANNEL_ID",
    "AVIBE_CALLER_SESSION_KEY",
    "AVIBE_CALLER_MESSAGE_ID",
    "AVIBE_CALLER_WORKSPACE_ID",
    "AVIBE_CALLER_REMOTE",
    "AVIBE_CALLER_RESOURCE_CONTEXT",
    "VIBE_INTERNAL_DISPATCH_SOCKET",
    "VIBE_CURRENT_EXECUTABLE",
    "AVIBE_SKILL_WORKING_DIR",
    "AVIBE_SKILL_PROJECT_BASE",
    "AVIBE_SKILL_HOME",
    "AVIBE_SKILL_CODEX_HOME",
    "AVIBE_SKILL_CLAUDE_HOME",
    "AVIBE_SKILL_CLAUDE_CLI_PATH",
    "AVIBE_SKILL_XDG_CONFIG_HOME",
    "AVIBE_BUILTIN_SKILLS_ROOT",
    "AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID",
    # A pytest started from a desktop Runtime's terminal would otherwise act as
    # that Runtime, and every stop, restart and start refuses or claims by its id.
    "AVIBE_DESKTOP_RUNTIME_ID",
    "AVIBE_DESKTOP_RUNTIME_ROOT",
)
# What the run started with, for `uses_real_paths` tests and the tripwire.
_AMBIENT_ENV = {name: os.environ.get(name) for name in (*_HOME_ENV, *_CALLER_ENV)}
_REAL_PATH_HOME = Path.__dict__["home"]


def _home_env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "AVIBE_HOME": str(home / ".avibe"),
        # Codex and Claude Code keep credentials under these, not under HOME.
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    }


# Isolation by construction: from here on the run's own home is a throwaway one,
# before any product module is imported, through collection, session fixtures
# and the gaps between tests. Per-test isolation swaps a test's home in and back
# to this one, never to the real one, so work that outlives its test -- the
# delayed Web Push thread that once migrated a developer's real database --
# resolves this home. Only `uses_real_paths` tests see the real values, for their
# own duration. Removed at `pytest_unconfigure`.
_SESSION_HOME = Path(tempfile.mkdtemp(prefix="avibe-pytest-home-")).resolve()
# Expanded against the real HOME while it is still the ambient one.
_AMBIENT_AVIBE_HOME = os.path.expanduser(_AMBIENT_ENV["AVIBE_HOME"]) if _AMBIENT_ENV["AVIBE_HOME"] else None
for _name in _CALLER_ENV:
    os.environ.pop(_name, None)
os.environ.update(_home_env(_SESSION_HOME))
Path.home = classmethod(lambda cls: _SESSION_HOME)


def pytest_configure(config):
    # A desktop caller is also whoever runs from a private tree's interpreter,
    # which no environment clearing undoes: every stop and restart under test
    # would act as that live Runtime.
    from vibe.desktop_runtime import _desktop_tree_runtime_id

    if _desktop_tree_runtime_id(sys.executable) is not None:
        raise pytest.UsageError(
            f"{sys.executable} is a desktop Runtime's private interpreter; run the tests with another Python"
        )


def _module_uses_default_sqlite_state(request: pytest.FixtureRequest) -> bool:
    """Avoid creating a migration template for tests that never use SQLite state."""

    module = request.node.getparent(pytest.Module)
    module_path = Path(str(module.path))
    cached = _SQLITE_DEFAULT_STATE_MODULES.get(module_path)
    if cached is not None:
        return cached

    source = module_path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source, filename=str(module_path))
    except SyntaxError:
        uses_sqlite = False
    else:
        uses_sqlite = any(
            _is_default_sqlite_call(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        )
    _SQLITE_DEFAULT_STATE_MODULES[module_path] = uses_sqlite
    return uses_sqlite


def _is_default_sqlite_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Name):
        function_name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        function_name = node.func.attr
    else:
        function_name = None
    if function_name in {"get_sqlite_state_path", "_vault_engine", "_open_vault_engine"}:
        return True
    if function_name not in {"ensure_sqlite_state", "create_sqlite_engine"}:
        return False
    return not node.args and not any(
        keyword.arg in {"db_path", "state_dir"} for keyword in node.keywords
    )


@pytest.fixture(scope="session")
def _sqlite_state_template_factory(tmp_path_factory):
    """Lazily build one empty SQLite database at the current migration head."""

    template_path: Path | None = None

    def get_template() -> Path:
        nonlocal template_path
        if template_path is not None:
            return template_path

        from storage.importer import ensure_sqlite_state

        template_root = tmp_path_factory.mktemp("sqlite-state-template")
        state_dir = template_root / "state"
        template_path = state_dir / "vibe.sqlite"
        # Migration 0044 deliberately recreates ``agent_runs`` and restores
        # its expression indexes afterward; SQLAlchemy cannot reflect those
        # indexes during the rebuild, so suppress only that expected warning.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=SAWarning,
                message=r"Skipped unsupported reflection of expression-based index ix_agent_runs_.*",
            )
            ensure_sqlite_state(
                db_path=template_path,
                state_dir=state_dir,
                primary_platform="avibe",
            )
        # The template is copied as one file into each test home. Checkpoint the
        # WAL first so no untracked -wal/-shm sidecars are needed by a clone.
        with sqlite3.connect(template_path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return template_path

    return get_template


@pytest.fixture
def sqlite_db_factory(tmp_path, _sqlite_state_template_factory):
    """Give behavioral tests independent, fully initialized state databases.

    This is opt-in: migration/import tests still initialize their own empty DBs.
    An explicit template must be closed and checkpointed by its owning fixture.
    Only new paths inside this test's temporary directory may receive a copy.
    """

    def create(db_path: Path, *, template: Path | None = None) -> Path:
        db_path = db_path.resolve()
        db_path.relative_to(tmp_path.resolve())
        db_path.parent.mkdir(parents=True, exist_ok=True)
        source_path = template if template is not None else _sqlite_state_template_factory()
        with source_path.open("rb") as source, db_path.open("xb") as target:
            shutil.copyfileobj(source, target)
        return db_path

    return create


@pytest.fixture(scope="session")
def _sqlite_schema_template_factory(tmp_path_factory):
    """Lazily build raw schema only, without running the JSON/data importer."""
    template_path: Path | None = None

    def get_template() -> Path:
        nonlocal template_path
        if template_path is None:
            from storage.migrations import run_migrations

            path = tmp_path_factory.mktemp("sqlite-schema-template") / "empty.sqlite"
            run_migrations(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            template_path = path
        return template_path

    return get_template


@pytest.fixture
def sqlite_schema_db_factory(sqlite_db_factory, _sqlite_schema_template_factory):
    """Opt ordinary setup into private raw schemas; constructors/imports stay real."""
    def create(db_path: Path) -> Path:
        return sqlite_db_factory(db_path, template=_sqlite_schema_template_factory())

    return create


@pytest.fixture(autouse=True)
def _isolate_vibe_remote_home(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("uses_real_paths"):
        # The real values, for this test only; it must stay read-only.
        monkeypatch.setattr(Path, "home", _REAL_PATH_HOME)
        for name, value in _AMBIENT_ENV.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return
    isolated_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: isolated_home)
    # Tests that manage these themselves (e.g. the ``get_codex_home``
    # env-precedence tests) override them with their own monkeypatch calls,
    # which run after this fixture.
    for name, value in _home_env(isolated_home).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AVIBE_ALLOW_DEV_STATE_MIGRATION", "1")


@pytest.fixture(autouse=True)
def _reset_latest_version_cache():
    """Keep the process-lifetime version cache from crossing test boundaries.

    Its file tier already lands in each test's isolated home, but the memory
    tier is module state: one test's probe answer would otherwise satisfy the
    next test's lookup, and which test that is depends on the shuffle order.
    """

    from core import latest_version_cache

    latest_version_cache._MEMORY.clear()  # noqa: SLF001
    yield
    latest_version_cache._MEMORY.clear()  # noqa: SLF001


def _module_this_test_can_import(name: str):
    """``name``, or None where this file's own ``sys.modules`` stubs keep it from importing.

    Several test files replace packages such as ``modules.agents`` with partial
    stubs at import time; a module that cannot import under them cannot start a
    background worker in that file either, so there is nothing to stub.
    """

    try:
        return importlib.import_module(name)
    except ImportError:
        return None


@pytest.fixture(autouse=True)
def _no_background_web_push(request: pytest.FixtureRequest, monkeypatch) -> list[dict] | None:
    """Keep the delayed Web Push sender off a thread that outlives its test.

    Persisting a notifiable Workbench message schedules
    ``_send_to_enabled_subscriptions`` on a daemon thread that sleeps
    ``WEB_PUSH_NOTIFICATION_DELAY_SECONDS`` and only then resolves the state
    paths. By then the test that scheduled it has usually ended; one such thread
    woke while the next test's home isolation was half applied, resolved the
    developer's real home and migrated its database. Scheduling still runs, and
    the payloads that would have been pushed are returned, so a test that asserts
    on them requests this fixture. ``real_web_push_sender`` opts out a test that
    drives the sender itself.
    """

    if request.node.get_closest_marker("real_web_push_sender"):
        return None
    web_push_notifications = _module_this_test_can_import("core.web_push_notifications")
    if web_push_notifications is None:
        return None
    pushed: list[dict] = []
    monkeypatch.setattr(web_push_notifications, "_send_to_enabled_subscriptions", pushed.append)
    return pushed


@pytest.fixture(autouse=True)
def _no_background_catalog_refresh(request: pytest.FixtureRequest, monkeypatch) -> None:
    """Keep the remote catalog refreshes off threads that outlive their test.

    Reading the backend model catalog or the models.dev catalog from a fresh home
    finds its cache stale and starts a daemon thread that fetches over the network
    and only then resolves the state directory to write the cache, usually after
    the test has ended -- the same shape as the Web Push sender, and the real-home
    tripwire caught both writing the developer's state. Each scheduler reports
    that no refresh started. ``real_catalog_refresh`` opts out a test that drives
    the refresh itself.
    """

    if request.node.get_closest_marker("real_catalog_refresh"):
        return
    for name, scheduler in (
        ("vibe.backend_model_catalog", "schedule_remote_catalog_refresh"),
        ("vibe.models_dev_catalog", "_refresh_in_background"),
    ):
        module = _module_this_test_can_import(name)
        if module is not None:
            monkeypatch.setattr(module, scheduler, lambda: False)


@pytest.fixture(autouse=True)
def _seed_sqlite_state_template(
    request: pytest.FixtureRequest,
    _isolate_vibe_remote_home,
    _sqlite_state_template_factory,
    monkeypatch,
):
    """Clone a migrated empty DB before ordinary tests touch default state.

    Migration/bootstrap tests opt out with ``no_sqlite_template`` because their
    purpose is to exercise the real upgrade/import path from an unseeded DB.
    Real-path tests are always read-only and must never receive a template copy.
    Every other test keeps its per-test database copy, so writes and schema
    mutations remain isolated without replaying all migrations per test.
    """

    if (
        request.node.get_closest_marker("uses_real_paths")
        or request.node.get_closest_marker("no_sqlite_template")
        or not _module_uses_default_sqlite_state(request)
    ):
        yield
        return

    from config import paths

    target = paths.get_sqlite_state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    template_factory = _sqlite_state_template_factory
    shutil.copy2(template_factory(), target)

    # Some UI tests deliberately change AVIBE_HOME after fixture setup. Patch
    # already-imported references as well as the importer module so those new
    # default paths receive the same head-shaped clone on first initialization.
    from storage import importer

    original_ensure_sqlite_state = importer.ensure_sqlite_state

    @wraps(original_ensure_sqlite_state)
    def seeded_ensure_sqlite_state(*, db_path=None, state_dir=None, primary_platform=None):
        if db_path is None and state_dir is None:
            dynamic_target = paths.get_sqlite_state_path()
            if not dynamic_target.exists():
                dynamic_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(template_factory(), dynamic_target)
        return original_ensure_sqlite_state(
            db_path=db_path,
            state_dir=state_dir,
            primary_platform=primary_platform,
        )

    monkeypatch.setattr(importer, "ensure_sqlite_state", seeded_ensure_sqlite_state)
    for module in tuple(sys.modules.values()):
        try:
            if getattr(module, "ensure_sqlite_state", None) is original_ensure_sqlite_state:
                monkeypatch.setattr(module, "ensure_sqlite_state", seeded_ensure_sqlite_state)
        except Exception:
            continue
    yield


@pytest.fixture(autouse=True)
def _reset_cached_sqlite_engines():
    """Keep process-local SQLite caches scoped to each isolated test.

    Both caches key on the resolved database path, so a rebuilt home never
    inherits a previous test's engine or its "already migrated" result.
    """

    def _reset() -> None:
        # A module that has never been imported cannot own cached state. Re-read
        # at teardown to include modules first imported by the test itself.
        db = sys.modules.get("storage.db")
        if db is not None:
            db.dispose_cached_sqlite_engines()
        importer = sys.modules.get("storage.importer")
        if importer is not None:
            importer.reset_ensured_sqlite_state()

    _reset()
    yield
    _reset()


# Captured when pytest loads this file, before any test can replace them: tests
# stub `os.getpid` and `runtime.psutil.Process` -- the psutil module global that
# psutil's own `parents()` resolves through -- and the guard must not ask a fake.
_REAL_OS_KILL = getattr(os, "kill", None)
_REAL_OS_KILLPG = getattr(os, "killpg", None)
_REAL_OS_GETPID = os.getpid
_REAL_OS_GETPGID = getattr(os, "getpgid", None)
_REAL_OS_GETSID = getattr(os, "getsid", None)
_REAL_OS_GETPGRP = getattr(os, "getpgrp", None)
_REAL_PSUTIL_PROCESS = psutil.Process
_REAL_PSUTIL_PROCESS_INIT = psutil.Process.__init__
_REAL_PSUTIL_PIDS = psutil.pids
_REAL_POPEN_INIT = subprocess.Popen.__init__

# The packages the wheel ships: a process lookup made from one of them is the
# product's, and that is where a test's fake pid must not reach the real table.
_PRODUCT_PACKAGES = frozenset({"vibe", "config", "core", "modules", "storage"})


def _describe_pid(pid: int) -> str:
    try:
        return f"{pid} ({_REAL_PSUTIL_PROCESS(pid).name()})"
    except psutil.Error:
        return f"{pid} (no such process)"


class _ForeignSignalGuard:
    """One test's record of the processes it may signal.

    The contract: refuse any signal that could reach a process outside this
    pytest run. That is this pytest process, its process group, ``-1``, and any
    process that neither descends from this pytest process, nor sits in a
    process group or session founded by a process the test owns, nor names this
    test's own ``tmp_path`` in its command line -- so pytest's ancestors, the CI
    runner and a developer's live Avibe service are all refused. It does not
    isolate tests in the same run from each other: a process started by a
    module- or session-scoped fixture, or left running by an earlier test,
    descends from this pytest process and stays signalable.

    The test owns its ``subprocess.Popen`` children and any process it has
    signalled as its own; the founder rule keeps an orphan in a spawned child's
    group signalable after reparenting hides it from ancestry. The ``tmp_path``
    rule covers a detached grandchild, which keeps nothing else that ties it to
    the test and is what harnesses such as the Model Hub e2e driver clean up by.
    It is the only grant over processes outside the tree, so it names this
    test's directory, never the shared temp root. A pid that names no process
    gets the ESRCH the real call would raise, delivered to nothing, because a
    descendant the test collected may exit before it is signalled. Signal 0 is
    a liveness probe and passes.

    The same record bounds what the product may look up. A ``psutil.Process``,
    or a signal-0 probe of a pid or a group, made from the product's packages
    must name this pytest process, a process the test owns, one psutil listed
    during the test, or a pid no process can hold: for any other pid the answer depends on which
    process, if any, holds it on the machine running the test, which is how a
    fake pid such as 1234 turned into a refusal on the one CI runner where a
    real process held it. With nothing holding the pid the lookup fails at
    once, so such a test fails everywhere instead; the pids
    ``tests.fake_pid_helpers.fake_pid`` hands out are the ones that stand for
    no process. A listing makes every pid it saw fair, so a fake pid that
    collides with one the test listed earlier still slips through; that test
    has failed on every machine where the pid was free.
    """

    def __init__(self, test_temp: Path) -> None:
        self.me = _REAL_OS_GETPID()
        # The whole final component: `test_x1` must not claim `test_x10`.
        self.names_test_temp = re.compile(re.escape(str(test_temp)) + r"(?![\w.-])").search
        self.owned: set[int] = set()
        # Every pid psutil listed while this test ran: a scan finds real processes by design.
        self.listed: set[int] = set()
        self.violations: list[str] = []

    def _owns(self, pid: int) -> bool:
        if pid == self.me:
            return False
        if pid in self.owned:
            return True
        ancestor, seen = pid, set()
        while ancestor > 0 and ancestor not in seen:
            seen.add(ancestor)
            try:
                ancestor = _REAL_PSUTIL_PROCESS(ancestor).ppid()
            except psutil.Error:
                break
            if ancestor == self.me:
                self.owned.add(pid)
                return True
        for founder_of in (_REAL_OS_GETPGID, _REAL_OS_GETSID):
            try:
                founder = founder_of(pid)
            except OSError:
                continue
            if founder in self.owned:
                self.owned.add(pid)
                return True
        try:
            cmdline = _REAL_PSUTIL_PROCESS(pid).cmdline()
        except psutil.Error:
            cmdline = []
        if any(self.names_test_temp(part) for part in cmdline):
            self.owned.add(pid)
            return True
        return False

    @staticmethod
    def _in_group(pid: int, pgid: int) -> bool:
        try:
            return _REAL_OS_GETPGID(pid) == pgid
        except OSError:
            return False

    def _group_reason(self, pgid: int) -> str | None:
        if pgid == _REAL_OS_GETPGRP():
            return f"process group {pgid} is this pytest process's own"
        members = [pid for pid in _REAL_PSUTIL_PIDS() if self._in_group(pid, pgid)]
        # A member can exit between the listing and its ownership check, as the
        # short-lived commands a shell group runs do; only one still there is foreign.
        foreign = [pid for pid in members if not self._owns(pid) and self._in_group(pid, pgid)]
        if foreign:
            return f"process group {pgid} holds processes this test did not start: " + ", ".join(
                _describe_pid(pid) for pid in foreign
            )
        if not members and pgid not in self.owned:
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH))
        self.owned.add(pgid)
        return None

    def _reason(self, pid: int) -> str | None:
        """Why ``kill(pid, ...)`` could reach a process this test did not start.

        Raises the ``ProcessLookupError`` the real call would when nothing
        answers to ``pid``.
        """

        if pid == -1:
            return "pid -1 addresses every process this user may signal"
        if pid <= 0:
            return self._group_reason(-pid if pid else _REAL_OS_GETPGRP())
        if pid == self.me:
            return f"pid {pid} is this pytest process itself"
        if self._owns(pid):
            return None
        # Probed before refusing, so a vanished target is not mistaken for a
        # foreign one; raised here rather than by the real call, which could
        # reach a process that took the pid in between.
        try:
            _REAL_OS_KILL(pid, 0)
        except ProcessLookupError:
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH)) from None
        except PermissionError:
            pass
        return f"pid {_describe_pid(pid)} is not a process this test started"

    def refuse_foreign(self, primitive: str, target, sig, kill_target) -> None:
        __tracebackhide__ = True
        if sig == 0 or not isinstance(target, int):
            return
        reason = self._reason(kill_target)
        if reason is None:
            return
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = str(sig)
        message = (
            f"os.{primitive}({target}, {name}) was not delivered: {reason}. A test may "
            "signal only processes it started; stub the stop path that reached this "
            "instead of letting it act on a fake pid."
        )
        self.violations.append(message)
        # A BaseException, so a production `except Exception` around the signal
        # cannot swallow it; the teardown check covers anything broader.
        pytest.fail(message)

    def refuse_unknown_lookup(self, lookup: str, pid: int, caller) -> None:
        """Fail the test when the product's ``lookup`` of ``pid`` would read whatever the machine holds there."""

        __tracebackhide__ = True
        if pid == self.me or pid >= PID_LIMIT or pid in self.listed or self._owns(pid):
            return
        message = (
            f"{caller.f_globals.get('__name__')}.{caller.f_code.co_name} looked up pid "
            f"{_describe_pid(pid)} through {lookup}, a pid this test neither started nor found "
            "by listing processes, so what the product reads depends on which process, if any, "
            "holds that pid on the machine running the test. Use "
            "tests.fake_pid_helpers.fake_pid() for a pid that stands for no process, or start a "
            "real process when the product must read its identity."
        )
        self.violations.append(message)
        pytest.fail(message)


# The running test's guard, or None between tests and in opted-out tests.
_active_signal_guard: _ForeignSignalGuard | None = None


def _called_from_product(caller) -> bool:
    return caller.f_globals.get("__name__", "").partition(".")[0] in _PRODUCT_PACKAGES


def _guarded_kill(pid, sig):
    __tracebackhide__ = True
    guard = _active_signal_guard
    if guard is not None:
        caller = sys._getframe(1)
        if sig == 0 and isinstance(pid, int) and pid > 0 and _called_from_product(caller):
            guard.refuse_unknown_lookup("os.kill(pid, 0)", pid, caller)
        guard.refuse_foreign("kill", pid, sig, pid)
    return _REAL_OS_KILL(pid, sig)


def _guarded_killpg(pgid, sig):
    __tracebackhide__ = True
    guard = _active_signal_guard
    if guard is not None:
        caller = sys._getframe(1)
        # A group is named by its leader's pid.
        if sig == 0 and isinstance(pgid, int) and pgid > 0 and _called_from_product(caller):
            guard.refuse_unknown_lookup("os.killpg(pgid, 0)", pgid, caller)
        # libc's killpg(pgid) is kill(-pgid): on macOS a negative pgid names one pid.
        guard.refuse_foreign("killpg", pgid, sig, -pgid if isinstance(pgid, int) else pgid)
    return _REAL_OS_KILLPG(pgid, sig)


@wraps(_REAL_POPEN_INIT)
def _recording_popen_init(popen, *args, **kwargs):
    _REAL_POPEN_INIT(popen, *args, **kwargs)
    guard = _active_signal_guard
    if guard is not None:
        guard.owned.add(popen.pid)


@wraps(_REAL_PSUTIL_PROCESS_INIT)
def _guarded_process_init(process, pid=None):
    __tracebackhide__ = True
    guard = _active_signal_guard
    caller = sys._getframe(1)
    # The guard reads the real table through this same constructor.
    if guard is not None and caller.f_globals is not globals() and isinstance(pid, int) and pid > 0:
        if caller.f_globals.get("__name__", "").partition(".")[0] == "psutil":
            # What psutil constructs itself it has listed: process_iter, children(), parents().
            guard.listed.add(pid)
        elif _called_from_product(caller):
            guard.refuse_unknown_lookup("psutil.Process", pid, caller)
    _REAL_PSUTIL_PROCESS_INIT(process, pid)


@wraps(_REAL_PSUTIL_PIDS)
def _listing_pids():
    pids = _REAL_PSUTIL_PIDS()
    guard = _active_signal_guard
    if guard is not None:
        guard.listed.update(pids)
    return pids


# Installed once for the whole run rather than per test through `monkeypatch`:
# a test's own `monkeypatch.undo()` would otherwise remove the guard mid-test.
# psutil's `send_signal`/`terminate`/`kill` and `subprocess.Popen`'s all end in
# `os.kill` on POSIX, so replacing the `os` attributes covers them. POSIX only:
# on Windows `os.kill` is TerminateProcess (and signal 0 is CTRL_C_EVENT, not a
# probe), `os.killpg` does not exist, and the product's Windows stop path calls
# TerminateProcess through ctypes, so wrapping `os.kill` there would guard
# nothing the product reaches. Every psutil lookup constructs a `Process`, and
# `process_iter` lists through the module's `pids`; a test that replaces
# `psutil.Process` with its own fake bypasses both, as it means to.
_SIGNAL_GUARD_SUPPORTED = os.name != "nt" and _REAL_OS_KILLPG is not None
if _SIGNAL_GUARD_SUPPORTED:
    os.kill = _guarded_kill
    os.killpg = _guarded_killpg
    subprocess.Popen.__init__ = _recording_popen_init
    psutil.Process.__init__ = _guarded_process_init
    psutil.pids = _listing_pids


@pytest.fixture(autouse=True)
def _foreign_signal_guard(request, tmp_path):
    """Fail any test that signals a process it did not start, and deliver nothing.

    Tests routinely make ``pid_alive`` true for fake pids such as 1234 or 5678.
    One stop path left unstubbed then signals whatever real process holds that
    pid -- on a CI runner that was the pytest process itself, on a developer
    machine it can be the live Avibe service. Processes a test starts stay
    signalable; ``allow_foreign_signals(reason=...)`` opts a test out.

    A fake pid the product only reads is as machine-dependent: a desktop start
    that asked psutil for pid 1234's environment was refused on the one runner
    where a root process held 1234. So the product's lookups are bounded the
    same way.
    """

    global _active_signal_guard
    marker = request.node.get_closest_marker("allow_foreign_signals")
    if marker is not None:
        if not (marker.kwargs.get("reason") or marker.args):
            pytest.fail("@pytest.mark.allow_foreign_signals needs a reason")
        yield None
        return
    if not _SIGNAL_GUARD_SUPPORTED:
        yield None
        return
    guard = _ForeignSignalGuard(tmp_path)
    _active_signal_guard = guard
    try:
        yield guard
    finally:
        _active_signal_guard = None
    __tracebackhide__ = True
    if guard.violations:
        pytest.fail("signals and lookups this test may not make were blocked:\n" + "\n".join(guard.violations))


# The developer's real Avibe state: both homes under REAL_USER_HOME, plus the
# custom home AVIBE_HOME names when pytest starts with one, each spelled as
# written and as resolved, because the legacy name is usually a symlink to the
# current one. Fixed when pytest loads this file, before any test replaces HOME,
# AVIBE_HOME or `Path.home`; teardown restores them, so work that outlives a test
# resolves exactly these. The SQLite migration guard cannot stand in for this:
# every test runs with its opt-in flag set and with `Path.home` naming the test's
# own home, so under pytest neither of its checks can recognise the real database.
_REAL_STATE_HOMES = [REAL_USER_HOME / ".avibe", REAL_USER_HOME / ".vibe_remote"]
if _AMBIENT_AVIBE_HOME:
    _REAL_STATE_HOMES.append(Path(_AMBIENT_AVIBE_HOME))
_REAL_STATE_ROOTS = tuple(
    sorted(
        {
            spelling
            for home in _REAL_STATE_HOMES
            for spelling in (os.path.abspath(home), os.path.realpath(home))
        }
    )
)
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
# Every path-mutating audit event CPython raises, mapped to the arguments that
# name a path it writes, each with the index of its dir_fd (None when the event
# carries none). shutil's copy, move and chown helpers end in these events or in
# `open`; only rmtree is listed itself, because its fd-based walk names entries
# relative to descriptors. `open` and `sqlite3.connect` depend on their mode.
_WRITE_EVENTS = {
    "os.mkdir": ((0, 2),),
    "os.rmdir": ((0, 1),),
    "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "os.symlink": ((1, 2),),
    "os.link": ((1, 3),),
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.utime": ((0, 3),),
    "os.truncate": ((0, None),),
    "os.chflags": ((0, None),),
    "os.setxattr": ((0, None),),
    "os.removexattr": ((0, None),),
    "shutil.rmtree": ((0, 1),),
}
_TRIPWIRE_EVENTS = frozenset({"open", "sqlite3.connect", *_WRITE_EVENTS})
# Events that act on a final symlink itself rather than on what it names.
_LINK_LEVEL_EVENTS = frozenset(
    {"os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.symlink", "os.link", "shutil.rmtree"}
)
# What the audit arguments carry for a dir_fd the caller did not pass.
_NO_DIR_FD = (None, -1)
# Every refused write in this run, in order. A daemon thread or a broad `except`
# can swallow the refusal itself; this record is what fails the session.
_real_home_writes: list[str] = []
_tripwire_busy = threading.local()


def _descriptor_path(fd: int) -> str | None:
    """The directory an open descriptor names, where the platform can say."""

    try:
        if sys.platform == "darwin":
            import fcntl

            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0])
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, AttributeError):
        return None


def _under_real_state(path, dir_fd=None, *, follows: bool = True) -> str | None:
    if isinstance(path, int):
        return None  # a descriptor: its own open was checked by name
    text = os.fsdecode(path)
    if not os.path.isabs(text) and dir_fd not in _NO_DIR_FD:
        directory = _descriptor_path(dir_fd)
        if directory is None:
            return None
        text = os.path.join(directory, text)
    absolute = os.path.abspath(text)
    # Resolved as well, so an alias -- a symlink elsewhere into a protected home --
    # is recognised; an operation on a link itself resolves only its directory.
    head, tail = os.path.split(absolute)
    resolved = os.path.realpath(absolute) if follows else os.path.join(os.path.realpath(head), tail)
    for spelling in (absolute, resolved):
        if any(spelling == root or spelling.startswith(root + os.sep) for root in _REAL_STATE_ROOTS):
            return absolute
    return None


def _sqlite_write_target(database) -> str | None:
    text = os.fsdecode(database)
    if not text.startswith("file:"):
        return _under_real_state(text)
    uri = urllib.parse.urlsplit(text)
    query = urllib.parse.parse_qs(uri.query)
    # Only an immutable open writes nothing: `mode=ro` on a WAL database can still
    # create its `-shm` sidecar.
    if query.get("immutable") == ["1"]:
        return None
    return _under_real_state(urllib.request.url2pathname(uri.path))


def _real_state_writes(event: str, args: tuple) -> list[str]:
    if event == "open":
        path, mode, flags = args
        if isinstance(flags, int):
            writes = bool(flags & _WRITE_OPEN_FLAGS)
        else:
            writes = any(letter in (mode or "") for letter in "wax+")
        targets = [_under_real_state(path)] if writes else []
    elif event == "sqlite3.connect":
        targets = [_sqlite_write_target(args[0])]
    else:
        follows = event not in _LINK_LEVEL_EVENTS
        targets = [
            # shutil.rmtree carries its dir_fd only from Python 3.12.
            _under_real_state(
                args[at], args[fd_at] if fd_at is not None and fd_at < len(args) else None, follows=follows
            )
            for at, fd_at in _WRITE_EVENTS[event]
        ]
    return [target for target in targets if target is not None]


def _describe_real_home_write(event: str, written: list[str]) -> str:
    frames = [
        f"{frame.filename}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_stack()
        if frame.filename.startswith(_REPO_ROOT) and frame.filename != __file__
    ]
    return (
        f"{event} {', '.join(written)} from thread {threading.current_thread().name} during "
        f"{os.environ.get('PYTEST_CURRENT_TEST', 'no test')}: {'; '.join(frames[-6:]) or 'no repository frame'}"
    )


def _real_home_tripwire(event: str, args: tuple) -> None:
    """Refuse any write under the developer's real Avibe home, from any thread.

    An audit hook rather than a fixture, so it holds between tests, during
    collection and in threads that outlive the test that started them -- the
    window per-test isolation cannot reach. Reads stay allowed, including
    ``uses_real_paths`` tests and ``immutable=1`` SQLite URIs. Child processes
    are outside it: each pytest child loads
    its own copy of this file, and other children inherit the test's home. The
    one write it cannot place is ``os.open`` relative to a ``dir_fd``, whose
    audit event omits the descriptor; creating that directory chain still trips.
    """

    if event not in _TRIPWIRE_EVENTS or getattr(_tripwire_busy, "active", False):
        return
    # Reading source for the description opens files, which re-enters this hook.
    _tripwire_busy.active = True
    try:
        try:
            written = _real_state_writes(event, args)
        except (TypeError, ValueError, OSError):
            return  # not a path this hook can name, such as a cwd that no longer exists
        if not written:
            return
        message = _describe_real_home_write(event, written)
        _real_home_writes.append(message)
        _fail_running_session()
    finally:
        _tripwire_busy.active = False
    # A BaseException, so a product `except Exception` around the write cannot
    # swallow it; the session-end check covers anything broader.
    pytest.fail(f"refused a write under the real Avibe home: {message}")


sys.addaudithook(_real_home_tripwire)


_REPORTED_WRITES = pytest.StashKey[int]()
_running_session: pytest.Session | None = None


def _fail_running_session() -> None:
    # pytest overwrites the status once the tests have run, so
    # `pytest_unconfigure` marks it again; a refusal after that point is
    # marked here, until pytest returns.
    session = _running_session
    if session is not None and session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_sessionstart(session: pytest.Session) -> None:
    global _running_session
    _running_session = session


def pytest_terminal_summary(terminalreporter) -> None:
    written = list(_real_home_writes)
    terminalreporter.config.stash[_REPORTED_WRITES] = len(written)
    if written:
        terminalreporter.section("writes under the real Avibe home were refused", red=True)
        for message in written:
            terminalreporter.line(message, red=True)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    # The session home and the tripwire stay in place until the process exits:
    # daemon threads can outlive this hook, and restoring the real values here
    # would hand them the real home again. Only a write during interpreter
    # shutdown, after pytest has returned its status, is refused without failing.
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)
    for message in _real_home_writes[config.stash.get(_REPORTED_WRITES, 0) :]:
        sys.stderr.write(f"refused a write under the real Avibe home: {message}\n")
    if _real_home_writes:
        _fail_running_session()


@pytest.fixture
def hold_migration_lock_elsewhere():
    """Hold a migration lock path from a thread that is genuinely not the caller.

    Taking it in the calling thread is not a stand-in for a competing holder and
    never was, it only used to look like one: `MigrationFileLock` is re-entrant
    per path and thread, so code under test running in that same thread takes the
    lock again and proceeds. A test written that way asserts nothing about
    exclusion and keeps passing after the exclusion is gone.
    """

    import threading

    from storage.lock import MigrationFileLock

    @contextmanager
    def _holder(lock_path: Path):
        acquired = threading.Event()
        release = threading.Event()
        failures: list[BaseException] = []

        def hold() -> None:
            try:
                with MigrationFileLock(lock_path, timeout_seconds=None):
                    acquired.set()
                    release.wait(30)
            except BaseException as exc:  # surfaced to the test, never swallowed
                failures.append(exc)
                acquired.set()

        holder = threading.Thread(target=hold, name="migration-lock-holder", daemon=True)
        holder.start()
        assert acquired.wait(30), f"lock holder never started for {lock_path}"
        assert not failures, failures[0]
        try:
            yield
        finally:
            release.set()
            holder.join(30)
        assert not failures, failures[0]

    return _holder


@pytest.fixture(autouse=True)
def _reset_oauth_runtime_state():
    """Reset module-level in-memory OAuth caches between tests.

    The handshake store, diagnostic-log throttles, and the unauthenticated /auth
    rate limiter live in process memory (not under the isolated Avibe home),
    so without this they would leak across tests sharing a pytest process — e.g. the
    rate limiter accumulating across files and spuriously 429-ing an unrelated test.
    """
    def _reset() -> None:
        remote_access = sys.modules.get("vibe.remote_access")
        if remote_access is not None:
            remote_access._clear_active_hostnames_cache()
            remote_access._oauth_handshakes.clear()
        ui_server = sys.modules.get("vibe.ui_server")
        if ui_server is not None:
            ui_server._oauth_diag_log_state.clear()
            ui_server._auth_ratelimit.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_show_runtime_manager():
    """Stop and clear any global Show Runtime manager spawned during a test.

    The Show Runtime manager is a process-global singleton. Serving-path tests
    that do not install a fake manager cause ``get_show_runtime_manager()`` to
    lazily create the real manager, which spawns a Node ``cli.js`` + ``esbuild``
    subprocess tree whenever a runtime is installed on the machine. Without an
    explicit teardown the reference can be overwritten by a later test's
    ``set_show_runtime_manager_for_tests`` swap; the ``atexit`` cleanup at pytest
    exit then no longer sees it, and the Node/esbuild tree leaks for the lifetime
    of the machine. Reset after every test so no real subprocess can outlive it.
    """
    yield
    try:
        from core import show_runtime
    except Exception:
        return
    try:
        show_runtime.set_show_runtime_manager_for_tests(None)
    except Exception:
        pass


def _async_items_missing_plugin(items) -> list:
    """Return selected native coroutine items that need pytest-asyncio."""
    offenders = []
    for item in items:
        if item.get_closest_marker("anyio") is not None:
            continue
        if item.get_closest_marker("skip") is not None:
            continue
        cls = getattr(item, "cls", None)
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
            continue
        try:
            func = item.obj
        except Exception:
            continue
        if inspect.iscoroutinefunction(func):
            offenders.append(item)
    return offenders


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Report missing pytest-asyncio without aborting legitimate selections."""
    if config.pluginmanager.hasplugin("asyncio"):
        return
    offenders = _async_items_missing_plugin(items)
    if not offenders:
        return
    message = (
        'pytest-asyncio is not installed, so asyncio_mode="auto" is inactive. '
        f"{len(offenders)} selected tests are native 'async def' tests and will "
        'fail as "async def functions are not natively supported" -- that is the '
        "missing dev dependency, not a product regression. Install it with "
        "`uv sync --group dev`."
    )
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{message}", yellow=True, bold=True)
    else:
        print(f"\n{message}", file=sys.stderr)

@pytest.fixture(autouse=True)
def _reset_oauth_runtime_state():
    """Reset module-level in-memory OAuth caches between tests.

    The handshake store, diagnostic-log throttles, and the unauthenticated /auth
    rate limiter live in process memory (not under the isolated Avibe home),
    so without this they would leak across tests sharing a pytest process — e.g. the
    rate limiter accumulating across files and spuriously 429-ing an unrelated test.
    """
    def _reset() -> None:
        remote_access = sys.modules.get("vibe.remote_access")
        if remote_access is not None:
            remote_access._clear_active_hostnames_cache()
            remote_access._oauth_handshakes.clear()
        ui_server = sys.modules.get("vibe.ui_server")
        if ui_server is not None:
            ui_server._oauth_diag_log_state.clear()
            ui_server._auth_ratelimit.clear()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_show_runtime_manager():
    """Stop and clear any global Show Runtime manager spawned during a test.

    The Show Runtime manager is a process-global singleton. Serving-path tests
    that do not install a fake manager cause ``get_show_runtime_manager()`` to
    lazily create the real manager, which spawns a Node ``cli.js`` + ``esbuild``
    subprocess tree whenever a runtime is installed on the machine. Without an
    explicit teardown the reference can be overwritten by a later test's
    ``set_show_runtime_manager_for_tests`` swap; the ``atexit`` cleanup at pytest
    exit then no longer sees it, and the Node/esbuild tree leaks for the lifetime
    of the machine. Reset after every test so no real subprocess can outlive it.
    """
    yield
    try:
        from core import show_runtime
    except Exception:
        return
    try:
        show_runtime.set_show_runtime_manager_for_tests(None)
    except Exception:
        pass


def _async_items_missing_plugin(items) -> list:
    """Return the selected items pytest will try, and fail, to run as coroutines.

    Three kinds of coroutine item are deliberately excluded because pytest
    handles them without the plugin:

    * ``unittest.TestCase`` methods -- ``IsolatedAsyncioTestCase`` brings its own
      event loop, and 1087 of this suite's async items are of this kind;
    * ``anyio``-marked coroutines -- anyio is a runtime dependency, so its plugin
      is present wherever the package installs;
    * items already marked ``skip`` -- pytest reports the skip without ever
      awaiting them (``tests/e2e/conftest.py`` skips ``e2e_model_hub`` items
      unless they are explicitly selected).

    ``skipif`` is not evaluated here: deciding it needs pytest's private
    evaluation helpers, and over-counting 15 items costs one inaccurate number
    in an advisory message rather than a broken run.
    """
    offenders = []
    for item in items:
        if item.get_closest_marker("anyio") is not None:
            continue
        if item.get_closest_marker("skip") is not None:
            continue
        cls = getattr(item, "cls", None)
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
            continue
        try:
            func = item.obj
        except Exception:
            # Exotic collectors (doctests, custom items) have no test function.
            continue
        if inspect.iscoroutinefunction(func):
            offenders.append(item)
    return offenders


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Say once, up front, that a missing pytest-asyncio explains the failures.

    ``asyncio_mode = "auto"`` only takes effect when the plugin is installed.
    Without it each native ``async def`` test fails on its own with "async def
    functions are not natively supported", while the mode setting is reported
    merely as an unknown-config warning. That names the tests instead of the one
    missing dev dependency, so a suite run in an environment that never
    installed the dev group reads as a broad product regression -- measured as
    10 failures across 2 files in a container whose runtime venv installs only
    the wheel.

    This reports and lets the run proceed; it deliberately does not abort.
    Aborting requires being right about every item, and the cost of being wrong
    is asymmetric: a wrong abort breaks a legitimate run, while a wrong count
    costs one inaccurate line. An earlier revision of this guard aborted, and
    broke the packaged-test and publish jobs before running a single test. The
    value here was always diagnosis, never saving time, and a message buys that
    without being able to break anything.

    ``trylast`` matters: pytest's own ``-k`` / ``-m`` deselection runs in this
    same hook, so an earlier-running implementation would count items the user
    already filtered out.

    The notice goes to the terminal reporter rather than ``warnings.warn`` for
    the same reason. A warning is not inert: under ``-W error`` (or
    ``filterwarnings = error``) it is promoted to an exception, and an exception
    raised from a collection hook ends the run as an INTERNALERROR before any
    test executes. Advisory has to mean advisory in every environment, not just
    the ones this repository's CI happens to configure.
    """
    if config.pluginmanager.hasplugin("asyncio"):
        return
    offenders = _async_items_missing_plugin(items)
    if not offenders:
        return
    message = (
        f"pytest-asyncio is not installed, so asyncio_mode=\"auto\" is inactive. "
        f"{len(offenders)} selected tests are native 'async def' tests and will "
        'fail as "async def functions are not natively supported" -- that is the '
        "missing dev dependency, not a product regression. Install it with "
        "`uv sync --group dev`."
    )
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\n{message}", yellow=True, bold=True)
    else:
        # No terminal reporter -- ``-p no:terminal``, or pytest driven as a
        # library. stderr still reaches the operator and, unlike
        # ``warnings.warn``, cannot be promoted to an exception.
        print(f"\n{message}", file=sys.stderr)
