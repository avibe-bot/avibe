from __future__ import annotations

from config import v2_settings
from config.v2_settings import SettingsStore
from vibe import api


def test_create_bind_code_api_returns_machine_readable_limit_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(v2_settings, "_MAX_ACTIVE_BIND_CODES", 1)
    SettingsStore.reset_instance()
    try:
        assert api.create_bind_code()["ok"] is True

        result = api.create_bind_code()

        assert result == {
            "ok": False,
            "error": {
                "code": "bind_code_limit_reached",
                "message": "active bind code limit reached (1)",
            },
        }
    finally:
        SettingsStore.reset_instance()
