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

Isolation mechanism: we set ``HOME``, XDG config/data/cache/state homes, and
``AVIBE_HOME`` to a per-test tmp directory, and patch
``pathlib.Path.home`` to match. This means ``config.paths.get_vibe_remote_dir``
runs as written — only its env-var-set branch is exercised under isolation, and
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
import inspect
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import unittest
import warnings
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

import psutil
import pytest
from sqlalchemy.exc import SAWarning

REAL_USER_HOME = Path.home()
_SQLITE_DEFAULT_STATE_MODULES: dict[Path, bool] = {}


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
        return
    monkeypatch.delenv("AVIBE_HOME", raising=False)
    # Agent-launched pytest processes inherit the active conversation's caller
    # identity. Tests must opt in to that context explicitly or unrelated
    # Harness/session assertions can bind themselves to the live Agent session.
    for name in (
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
    ):
        monkeypatch.delenv(name, raising=False)
    isolated_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: isolated_home)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(isolated_home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(isolated_home / ".local" / "share"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(isolated_home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_home / ".local" / "state"))
    monkeypatch.setenv("AVIBE_HOME", str(isolated_home / ".avibe"))
    monkeypatch.setenv("AVIBE_ALLOW_DEV_STATE_MIGRATION", "1")
    # Keep Codex / Claude Code credential writes off the developer's real
    # home. Tests that manage these env vars themselves (e.g. the
    # ``get_codex_home`` env-precedence tests) override these via their own
    # monkeypatch calls, which run after this fixture.
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))


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
_REAL_PSUTIL_PIDS = psutil.pids
_REAL_POPEN_INIT = subprocess.Popen.__init__


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
    """

    def __init__(self, test_temp: Path) -> None:
        self.me = _REAL_OS_GETPID()
        # The whole final component: `test_x1` must not claim `test_x10`.
        self.names_test_temp = re.compile(re.escape(str(test_temp)) + r"(?![\w.-])").search
        self.owned: set[int] = set()
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


# The running test's guard, or None between tests and in opted-out tests.
_active_signal_guard: _ForeignSignalGuard | None = None


def _guarded_kill(pid, sig):
    __tracebackhide__ = True
    guard = _active_signal_guard
    if guard is not None:
        guard.refuse_foreign("kill", pid, sig, pid)
    return _REAL_OS_KILL(pid, sig)


def _guarded_killpg(pgid, sig):
    __tracebackhide__ = True
    guard = _active_signal_guard
    if guard is not None:
        # libc's killpg(pgid) is kill(-pgid): on macOS a negative pgid names one pid.
        guard.refuse_foreign("killpg", pgid, sig, -pgid if isinstance(pgid, int) else pgid)
    return _REAL_OS_KILLPG(pgid, sig)


@wraps(_REAL_POPEN_INIT)
def _recording_popen_init(popen, *args, **kwargs):
    _REAL_POPEN_INIT(popen, *args, **kwargs)
    guard = _active_signal_guard
    if guard is not None:
        guard.owned.add(popen.pid)


# Installed once for the whole run rather than per test through `monkeypatch`:
# a test's own `monkeypatch.undo()` would otherwise remove the guard mid-test.
# psutil's `send_signal`/`terminate`/`kill` and `subprocess.Popen`'s all end in
# `os.kill` on POSIX, so replacing the `os` attributes covers them. POSIX only:
# on Windows `os.kill` is TerminateProcess (and signal 0 is CTRL_C_EVENT, not a
# probe), `os.killpg` does not exist, and the product's Windows stop path calls
# TerminateProcess through ctypes, so wrapping `os.kill` there would guard
# nothing the product reaches.
_SIGNAL_GUARD_SUPPORTED = os.name != "nt" and _REAL_OS_KILLPG is not None
if _SIGNAL_GUARD_SUPPORTED:
    os.kill = _guarded_kill
    os.killpg = _guarded_killpg
    subprocess.Popen.__init__ = _recording_popen_init


@pytest.fixture(autouse=True)
def _foreign_signal_guard(request, tmp_path):
    """Fail any test that signals a process it did not start, and deliver nothing.

    Tests routinely make ``pid_alive`` true for fake pids such as 1234 or 5678.
    One stop path left unstubbed then signals whatever real process holds that
    pid -- on a CI runner that was the pytest process itself, on a developer
    machine it can be the live Avibe service. Processes a test starts stay
    signalable; ``allow_foreign_signals(reason=...)`` opts a test out.
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
        pytest.fail("signals to processes this test did not start were blocked:\n" + "\n".join(guard.violations))


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
