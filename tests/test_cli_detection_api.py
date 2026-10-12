"""Real detection evidence reaches HTTP and lifecycle consumers, without credentials."""

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from config.v2_config import V2Config
from tests.test_cli_health import make_cli, fixture_env
from vibe import api, ui_server


@pytest.fixture
def cli_api(tmp_path, monkeypatch):
    env = fixture_env(tmp_path)
    monkeypatch.setattr(api, "_command_env_for", lambda _path: dict(env))
    monkeypatch.setattr(api, "_cached_latest", lambda _name: "0.163.0")
    monkeypatch.setattr(api, "_codex_process_status", lambda _path: "stopped")
    config = V2Config.default()
    config.agents.codex.enabled = True
    config.agents.codex.cli_path = "saved-codex"
    config.save()
    return ui_server.app.test_client(), env


def test_draft_runtime_probes_the_selected_cli_and_does_not_save_it(cli_api, tmp_path):
    client, _ = cli_api
    cli = make_cli(tmp_path, 'print("codex-cli 0.162.1")\n')
    response = client.get("/api/backend/codex/runtime", params={"cli_path": str(cli)})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["status"] == "ok" and body["current_version"] == "0.162.1"
    assert body["cli_path"] == str(cli) and body["resolved_path"] == str(cli)
    assert V2Config.load().agents.codex.cli_path == "saved-codex"
    assert response.headers["Cache-Control"] == "private, no-store"


def test_crash_version_is_broken_through_both_http_consumers(cli_api, tmp_path):
    client, _ = cli_api
    cli = make_cli(
        tmp_path,
        'import sys\nprint("Error: missing platform package; PRIVATE_SENTINEL; Node.js v22.18.0",file=sys.stderr)\nsys.exit(1)\n',
    )
    detection = client.get("/api/cli/detect", params={"binary": str(cli), "backend": "codex"}).get_json()
    runtime = client.get("/api/backend/codex/runtime", params={"cli_path": str(cli)}).get_json()
    assert detection["found"] and detection["status"] == "broken" and detection["version"] is None
    assert runtime["installed"] and runtime["status"] == "broken" and runtime["current_version"] is None
    assert not runtime["has_update"]
    assert "PRIVATE_SENTINEL" not in str(detection) + str(runtime)


def test_missing_selected_path_never_substitutes_another_installation(cli_api, tmp_path, monkeypatch):
    missing = str(tmp_path / "missing/codex")
    monkeypatch.setattr(
        api, "resolve_cli_path", lambda _: pytest.fail("an explicit missing selection must not discover another CLI")
    )
    assert api.detect_cli(missing, "codex")["status"] == "missing"


@pytest.mark.parametrize(
    "route,query",
    [
        ("/api/cli/detect", {"binary": "/fixture/selected", "backend": "codex"}),
        ("/api/backend/codex/runtime", {"cli_path": "/fixture/selected"}),
    ],
)
def test_management_authority_is_checked_before_custom_path_probe(cli_api, monkeypatch, route, query):
    client, _ = cli_api
    probe = AsyncMock(side_effect=AssertionError("unauthorized request must not execute a probe"))
    monkeypatch.setattr(api, "detect_cli_async", probe)
    response = client.get(route, params=query, environ_base={"REMOTE_ADDR": "203.0.113.44"})
    assert response.status_code in {401, 403, 503}, response.get_json()
    probe.assert_not_called()


def test_invalid_backend_does_not_execute_custom_path(cli_api, monkeypatch):
    client, _ = cli_api
    probe = AsyncMock(side_effect=AssertionError("invalid backend must not probe"))
    monkeypatch.setattr(api, "probe_cli", probe)
    response = client.get("/api/cli/detect", params={"binary": "/fixture/selected", "backend": "vibey"})
    assert response.status_code == 400
    probe.assert_not_called()


def test_install_job_rejects_success_without_readback(monkeypatch):
    import time

    monkeypatch.setattr(api, "_agent_runtime_fingerprint", lambda *_args: None)
    monkeypatch.setattr(api, "install_agent", lambda _name: {"ok": True, "path": "/fixture/cli"})
    monkeypatch.setattr(
        api, "restart_backend", lambda *_args, **_kwargs: pytest.fail("unverified install must not refresh")
    )
    monkeypatch.setattr(api, "_AGENT_INSTALL_JOBS", {})
    monkeypatch.setattr(api, "_AGENT_INSTALL_LATEST_BY_BACKEND", {})
    started = api.start_agent_install_job("codex")
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = api.get_agent_install_job(started["job_id"])
        if result["status"] != "running":
            break
        time.sleep(0.01)
    assert result["status"] == "failed" and result["code"] == "cli_readback_failed"


def test_private_publication_is_not_rolled_back_when_final_probe_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace

    active = tmp_path / "active-native"
    active.write_bytes(b"published generation")
    monkeypatch.setattr(
        api,
        "install_desktop_backend",
        lambda *_args, **_kwargs: SimpleNamespace(path=str(active), version="0.162.1", output=None),
    )
    monkeypatch.setattr(
        api, "detect_cli", lambda *_args: api.CliHealth(True, str(active), "broken", reason="probe_failed").payload()
    )
    result = api._run_desktop_backend_install("codex", lambda x: x)
    assert not result["ok"] and result["code"] == "cli_readback_failed"
    assert active.read_bytes() == b"published generation"
