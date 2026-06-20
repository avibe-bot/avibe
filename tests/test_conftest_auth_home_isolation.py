"""Regression guard: the autouse isolation fixture must redirect the Codex
and Claude Code config homes to per-test tmp dirs.

Without this, auth-setup tests that drive ``apply_codex_auth`` /
``apply_claude_auth`` (notably the scenarios under
``tests/scenarios/auth_setup/``) rewrite the developer's real
``~/.codex/auth.json`` and ``~/.claude/settings.json``, silently dropping
``OPENAI_API_KEY`` and the ``ANTHROPIC_*`` env block. This violates the
AGENTS.md rule that tests must never mutate live user state.
"""

from __future__ import annotations

import os
from pathlib import Path

from vibe import claude_config, codex_config


def test_codex_and_claude_homes_are_isolated_from_real_home() -> None:
    real_home = Path.home()
    codex_home = codex_config.get_codex_home()
    claude_home = claude_config.get_claude_home()

    # The autouse fixture must have pinned both env vars to a tmp dir.
    assert os.environ.get("CODEX_HOME")
    assert os.environ.get("CLAUDE_CONFIG_DIR")

    # And the resolved homes must honour them rather than the real ~/.codex
    # / ~/.claude.
    assert codex_home == Path(os.environ["CODEX_HOME"]).expanduser()
    assert claude_home == Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser()
    assert codex_home != real_home / ".codex"
    assert claude_home != real_home / ".claude"


def test_apply_auth_writes_land_under_isolated_home() -> None:
    """End-to-end: the no-``home`` arg write path used by the live service
    (and exercised by the auth-setup scenarios) must land under the
    isolated home, never the real one."""
    codex_config.apply_codex_auth(
        auth_mode="api_key", api_key="sk-isolation-test", base_url=None
    )
    isolated_auth = codex_config.get_codex_home() / "auth.json"
    assert isolated_auth.exists()
    assert isolated_auth != Path.home() / ".codex" / "auth.json"

    claude_config.apply_claude_auth(auth_mode="oauth", api_key=None, base_url=None)
    isolated_settings = claude_config.get_claude_settings_path()
    assert isolated_settings.exists()
    assert isolated_settings != Path.home() / ".claude" / "settings.json"
