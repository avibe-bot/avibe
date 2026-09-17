"""Readiness observes persisted launch auth and the existing lifecycle owner."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.v2_config import V2Config
from vibe import api, internal_client, runtime, ui_server


@pytest.fixture
def connection(monkeypatch, tmp_path):
    config = V2Config.default()
    config.agents.claude.enabled = True
    config.agents.codex.enabled = True
    config.agents.opencode.enabled = True
    monkeypatch.setattr(api, "load_config", lambda: config)
    monkeypatch.setattr(api, "resolve_cli_path", lambda _: str(tmp_path / "助手 cli"))
    monkeypatch.setattr(api, "get_claude_auth", lambda: {"ok": True, "active_auth_mode": "api_key"})
    monkeypatch.setattr(api, "get_codex_auth", lambda: {"ok": True, "active_auth_mode": "oauth"})
    monkeypatch.setattr(api, "_backend_apply_receipts", {})
    probe = AsyncMock(return_value={"status_code": 200, "body": {"ok": True, "state": "applied"}})
    monkeypatch.setattr(internal_client, "backend_application", probe)
    monkeypatch.setattr(runtime, "service_process_running", lambda: True)
    return SimpleNamespace(config=config, probe=probe)


@pytest.mark.parametrize("state,ready", [("applied", True), ("draining", False), ("failed", False), ("unavailable", False)])
def test_application_is_consumed_instead_of_acceptance(connection, state, ready):
    connection.probe.return_value["body"]["state"] = state
    observed = asyncio.run(api.get_backend_connection("claude"))
    assert observed["ready"] is ready
    assert observed["entry_eligible"] is ready
    assert observed["auth"] == "api_key"


def test_stopped_entry_is_distinct_from_running_broken_ipc(connection, monkeypatch):
    connection.probe.side_effect = internal_client.InternalServerUnavailable("missing IPC")
    unknown = asyncio.run(api.get_backend_connection("claude"))
    assert unknown["application"] == "unknown"
    assert not unknown["ready"] and not unknown["entry_eligible"]
    monkeypatch.setattr(runtime, "service_process_running", lambda: False)
    stopped = asyncio.run(api.get_backend_connection("claude"))
    assert stopped["application"] == "stopped"
    assert stopped["entry_eligible"] and not stopped["ready"]


def test_marker_failure_survives_new_reads_and_only_explicit_apply_retry_clears(connection, monkeypatch):
    monkeypatch.setattr(api, "_request_controller_restart", lambda *args, **kwargs: (False, None))
    assert not api.restart_backend("claude")["ok"]
    for _ in range(3):
        failed = asyncio.run(api.get_backend_connection("claude"))
        assert failed["application"] == "failed" and not failed["ready"]
    assert asyncio.run(api.get_backend_connection("codex"))["ready"]
    monkeypatch.setattr(api, "_request_controller_restart", lambda *args, **kwargs: (True, None))
    assert api.restart_backend("claude")["ok"]
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]


def test_keychain_uncertainty_and_disabled_backend_are_not_ready(connection, monkeypatch):
    monkeypatch.setattr(api, "get_codex_auth", lambda: {"ok": True, "active_auth_mode": "oauth", "auth_mode_uncertain": True})
    assert asyncio.run(api.get_backend_connection("codex"))["auth"] == "unknown"
    connection.config.agents.claude.enabled = False
    assert not asyncio.run(api.get_backend_connection("claude"))["ready"]


def test_opencode_permission_does_not_gate_another_backend_or_launch_daemon(connection, monkeypatch):
    from vibe import opencode_config
    monkeypatch.setattr(api, "_read_opencode_config_api_key_provider_ids", AsyncMock(return_value={"provider"}))
    monkeypatch.setattr(opencode_config, "read_opencode_provider_auth_entries", lambda **_: {})
    monkeypatch.setattr(api, "opencode_permission_status", lambda: {"ok": True, "permission_allowed": False})
    daemon = AsyncMock(side_effect=AssertionError("readiness must not launch a daemon"))
    monkeypatch.setattr(api, "_opencode_get_server", daemon)
    state = asyncio.run(api.get_backend_connection("opencode"))
    assert state["auth"] == "api_key" and state["permission_required"] and not state["entry_eligible"]
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]
    daemon.assert_not_called()


def test_native_asgi_connection_route_auth_allowlist_and_body(connection):
    client = ui_server.app.test_client()
    response = client.get("/api/backend/claude/connection")
    assert response.status_code == 200
    assert response.get_json()["ready"] is True
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.get("/api/backend/other/connection").status_code == 400
    remote = client.get("/api/backend/claude/connection", environ_base={"REMOTE_ADDR": "203.0.113.44"})
    assert remote.status_code in {401, 403, 503}
    assert not remote.get_json().get("ready")


def test_only_confirmed_new_controller_clears_old_failed_delivery(connection, monkeypatch):
    monkeypatch.setattr(runtime, "resolve_service_owner_pid", lambda **_: 100)
    api.record_backend_apply_receipt("claude", {"ok": False, "message": "marker not delivered"})
    connection.probe.return_value["body"]["controller_pid"] = 100
    assert not asyncio.run(api.get_backend_connection("claude"))["ready"]
    connection.probe.return_value["body"]["controller_pid"] = 101
    assert asyncio.run(api.get_backend_connection("claude"))["ready"]
