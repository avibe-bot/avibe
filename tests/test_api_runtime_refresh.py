from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vibe import api, backend_model_catalog


def test_restart_backend_reports_claude_failure_when_controller_unacknowledged(monkeypatch) -> None:
    monkeypatch.setattr(api, "_request_controller_restart", lambda name, **kwargs: (False, None))

    result = api.restart_backend("claude")

    assert result["ok"] is False
    assert "not acknowledged" in result["message"]


def test_restart_backend_passes_metadata_to_controller_marker(monkeypatch) -> None:
    captured = {}

    def fake_request(name, **kwargs):
        captured["name"] = name
        captured["metadata"] = kwargs.get("metadata")
        return True, None

    monkeypatch.setattr(api, "_request_controller_restart", fake_request)

    result = api.restart_backend("codex", metadata={"reason": "manual_backend_restart"})

    assert result["ok"] is True
    assert captured == {
        "name": "codex",
        "metadata": {"reason": "manual_backend_restart"},
    }


def test_codex_controller_ack_budget_covers_catalog_export(monkeypatch, tmp_path) -> None:
    captured = {}
    monkeypatch.setattr(api, "_runtime_command_dir", lambda: tmp_path)

    def wait_for_ack(marker, timeout):
        captured["timeout"] = timeout
        marker.unlink()
        return True, None

    monkeypatch.setattr(api, "_wait_for_controller_ack", wait_for_ack)

    assert api._request_controller_restart("codex") == (True, None)
    assert captured["timeout"] > backend_model_catalog.CODEX_HUB_CATALOG_TIMEOUT_SECONDS
