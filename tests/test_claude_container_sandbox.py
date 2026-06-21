from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.handlers.session_handler import SessionHandler


@dataclass
class _ClaudeConfig:
    permission_mode: str = "bypassPermissions"


@dataclass
class _Config:
    platform: str = "lark"
    claude: _ClaudeConfig = field(default_factory=_ClaudeConfig)


class _Controller:
    def __init__(self) -> None:
        self.config = _Config()
        self.im_client = type("IM", (), {"formatter": None})()
        self.settings_manager = type("Settings", (), {"sessions": None})()
        self.session_manager = object()
        self.claude_sessions = {}
        self.receiver_tasks = {}
        self.stored_session_mappings = {}

    def get_cwd(self, context):  # pragma: no cover - unused in these tests
        return "/tmp/workdir"

    def _get_settings_key(self, context):  # pragma: no cover - unused in these tests
        return "dummy"

    def _get_session_key(self, context):  # pragma: no cover - unused in these tests
        return f"{getattr(context, 'platform', None) or 'test'}::{self._get_settings_key(context)}"


def test_mark_claude_isolated_env_in_root_container(monkeypatch) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    handler = SessionHandler(_Controller())

    assert handler._should_mark_claude_isolated_env() is True


def test_skip_isolated_env_marker_when_env_already_set(monkeypatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    handler = SessionHandler(_Controller())

    assert handler._should_mark_claude_isolated_env() is False


def test_skip_isolated_env_marker_when_not_root(monkeypatch) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)

    handler = SessionHandler(_Controller())

    assert handler._should_mark_claude_isolated_env() is False
