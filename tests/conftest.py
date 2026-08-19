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
import shutil
import sqlite3
import sys
import warnings
from functools import wraps
from pathlib import Path

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


@pytest.fixture(autouse=True)
def _isolate_vibe_remote_home(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("uses_real_paths"):
        return
    monkeypatch.delenv("AVIBE_HOME", raising=False)
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
        try:
            from storage.db import dispose_cached_sqlite_engines
            from storage.importer import reset_ensured_sqlite_state
        except Exception:
            return
        dispose_cached_sqlite_engines()
        reset_ensured_sqlite_state()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_memory_artifact_manager():
    """Keep the managed Memory runtime bound to the current test home."""
    try:
        from core.memory.artifact import set_memory_artifact_manager_for_tests
    except Exception:
        yield
        return
    set_memory_artifact_manager_for_tests(None)
    yield
    set_memory_artifact_manager_for_tests(None)


@pytest.fixture
async def memory_runtime_factory():
    """Own active Memory runtimes until their test has fully torn down."""

    from tests.memory_runtime_factory import finalizing_memory_runtimes

    async with finalizing_memory_runtimes() as factory:
        yield factory


@pytest.fixture(autouse=True)
def _reset_oauth_runtime_state():
    """Reset module-level in-memory OAuth caches between tests.

    The handshake store, diagnostic-log throttles, and the unauthenticated /auth
    rate limiter live in process memory (not under the isolated Avibe home),
    so without this they would leak across tests sharing a pytest process — e.g. the
    rate limiter accumulating across files and spuriously 429-ing an unrelated test.
    """
    try:
        from vibe import remote_access, ui_server
    except Exception:
        yield
        return
    caches = (remote_access._oauth_handshakes, ui_server._oauth_diag_log_state, ui_server._auth_ratelimit)
    remote_access._clear_active_hostnames_cache()
    for cache in caches:
        cache.clear()
    yield
    remote_access._clear_active_hostnames_cache()
    for cache in caches:
        cache.clear()


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
