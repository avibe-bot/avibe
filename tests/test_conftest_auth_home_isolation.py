"""Regression guard: the autouse isolation fixture must redirect backend
credential homes to per-test tmp dirs.

Without this, auth-setup tests that drive ``apply_codex_auth`` /
``apply_claude_auth`` (notably the scenarios under
``tests/scenarios/auth_setup/``) rewrite the developer's real
``~/.codex/auth.json`` and ``~/.claude/settings.json``, silently dropping
``OPENAI_API_KEY`` and the ``ANTHROPIC_*`` env block. OpenCode resolves
``~/.local/share/opencode/auth.json`` from ``Path.home()`` in our helper
layer, so it needs the same default isolation even though the current
auth-setup scenarios mock its installer. This violates the AGENTS.md rule
that tests must never mutate live user state.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.conftest import REAL_USER_HOME
from vibe import claude_config, codex_config, opencode_config


def test_backend_credential_homes_are_isolated_from_real_home() -> None:
    codex_home = codex_config.get_codex_home()
    claude_home = claude_config.get_claude_home()
    opencode_auth = opencode_config.get_opencode_auth_path()

    # The autouse fixture must have pinned explicit backend homes where the
    # underlying tool supports them.
    assert os.environ.get("CODEX_HOME")
    assert os.environ.get("CLAUDE_CONFIG_DIR")

    # And the resolved homes must honour them rather than the real ~/.codex
    # / ~/.claude.
    assert codex_home == Path(os.environ["CODEX_HOME"]).expanduser()
    assert claude_home == Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
    assert codex_home != REAL_USER_HOME / ".codex"
    assert claude_home != REAL_USER_HOME / ".claude"

    # OpenCode has no env-var home override in our helpers, so the autouse
    # Path.home patch is its default isolation boundary.
    assert Path.home() != REAL_USER_HOME
    assert opencode_auth == (
        Path.home() / ".local" / "share" / "opencode" / "auth.json"
    )
    assert opencode_auth != (
        REAL_USER_HOME / ".local" / "share" / "opencode" / "auth.json"
    )


def test_apply_auth_writes_land_under_isolated_home() -> None:
    """End-to-end: the no-``home`` arg write path used by the live service
    (and exercised by the auth-setup scenarios) must land under the
    isolated home, never the real one."""
    codex_config.apply_codex_auth(
        auth_mode="api_key", api_key="sk-isolation-test", base_url=None
    )
    isolated_auth = codex_config.get_codex_home() / "auth.json"
    assert isolated_auth.exists()
    assert isolated_auth != REAL_USER_HOME / ".codex" / "auth.json"

    claude_config.apply_claude_auth(auth_mode="oauth", api_key=None, base_url=None)
    isolated_settings = claude_config.get_claude_settings_path()
    assert isolated_settings.exists()
    assert isolated_settings != REAL_USER_HOME / ".claude" / "settings.json"

    opencode_auth = opencode_config.get_opencode_auth_path()
    opencode_auth.parent.mkdir(parents=True, exist_ok=True)
    opencode_auth.write_text(
        '{"opencode":{"type":"api","key":"sk-isolation-test"}}\n',
        encoding="utf-8",
    )
    assert opencode_auth.exists()
    assert opencode_auth != (
        REAL_USER_HOME / ".local" / "share" / "opencode" / "auth.json"
    )
