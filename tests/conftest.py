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
``VIBE_REMOTE_HOME`` to a per-test tmp directory, and patch
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
write to ``~/.vibe_remote/``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REAL_USER_HOME = Path.home()


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
    monkeypatch.setenv("VIBE_REMOTE_HOME", str(isolated_home / ".vibe_remote"))
    # Keep Codex / Claude Code credential writes off the developer's real
    # home. Tests that manage these env vars themselves (e.g. the
    # ``get_codex_home`` env-precedence tests) override these via their own
    # monkeypatch calls, which run after this fixture.
    monkeypatch.setenv("CODEX_HOME", str(isolated_home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(isolated_home / ".claude"))


@pytest.fixture(autouse=True)
def _reset_oauth_runtime_state():
    """Reset module-level in-memory OAuth caches between tests.

    The handshake store, diagnostic-log throttles, and the unauthenticated /auth
    rate limiter live in process memory (not under the isolated VIBE_REMOTE_HOME),
    so without this they would leak across tests sharing a pytest process — e.g. the
    rate limiter accumulating across files and spuriously 429-ing an unrelated test.
    """
    try:
        from vibe import remote_access, ui_server
    except Exception:
        yield
        return
    caches = (remote_access._oauth_handshakes, ui_server._oauth_diag_log_state, ui_server._auth_ratelimit)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()
