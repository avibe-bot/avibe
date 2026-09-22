from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, UiConfig, V2Config
from vibe import api
from vibe.ui_server import app

from tests.ui_server_test_helpers import csrf_headers


def _save_setup_host_config(host: str) -> None:
    V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="."),
        agents=AgentsConfig(),
        ui=UiConfig(setup_host=host),
    ).save()


def test_install_agent_allows_same_origin_request(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_setup_host_config("192.168.2.3")
    monkeypatch.setattr(
        api,
        "start_agent_install_job",
        lambda name: {"ok": True, "job_id": "job-1", "backend": name, "status": "running"},
    )

    client = app.test_client()
    response = client.post(
        "/api/agent/claude/install",
        headers=csrf_headers(client, "http://192.168.2.3:15131"),
        base_url="http://192.168.2.3:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["status"] == "running"


def test_install_agent_rejects_cross_origin_request(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_setup_host_config("192.168.2.3")
    monkeypatch.setattr(api, "start_agent_install_job", lambda name: {"ok": True, "name": name})

    client = app.test_client()
    headers = csrf_headers(client, "http://192.168.2.3:15131")
    headers["Origin"] = "http://evil.example"
    response = client.post(
        "/api/agent/claude/install",
        headers=headers,
        base_url="http://192.168.2.3:15131",
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid origin"


def test_install_agent_rejects_missing_csrf_token(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(api, "start_agent_install_job", lambda name: {"ok": True, "name": name})

    client = app.test_client()
    response = client.post(
        "/api/agent/codex/install",
        headers={"Origin": "http://127.0.0.1:15131"},
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: invalid csrf token"


def test_install_agent_rejects_missing_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(api, "start_agent_install_job", lambda name: {"ok": True, "name": name})

    client = app.test_client()
    response = client.post(
        "/api/agent/codex/install",
        headers={"X-Vibe-CSRF-Token": csrf_headers(client)["X-Vibe-CSRF-Token"]},
    )

    assert response.status_code == 403
    assert response.get_json()["message"] == "Forbidden: missing origin header"


def test_install_agent_status_allows_poll(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    _save_setup_host_config("192.168.2.3")
    monkeypatch.setattr(
        api,
        "get_agent_install_job",
        lambda job_id, backend=None: {
            "ok": True,
            "job_id": job_id,
            "backend": backend,
            "status": "succeeded",
            "path": "/usr/local/bin/claude",
        },
    )

    client = app.test_client()
    response = client.get(
        "/api/agent/claude/install/job-1",
        headers=csrf_headers(client, "http://192.168.2.3:15131"),
        base_url="http://192.168.2.3:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "succeeded"


def test_dependency_install_route_allows_avault(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        api,
        "start_dependency_install_job",
        lambda dep: {"ok": True, "job_id": "job-avault", "backend": dep, "status": "running"},
    )

    client = app.test_client()
    response = client.post(
        "/api/dependencies/avault/install",
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["backend"] == "avault"


def test_dependency_install_route_allows_model_hub_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        api,
        "start_dependency_install_job",
        lambda dep: {"ok": True, "job_id": "job-cpa", "backend": dep, "status": "running"},
    )

    client = app.test_client()
    response = client.post(
        "/api/dependencies/model-hub-engine/install",
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["backend"] == "model-hub-engine"


def test_install_job_fails_when_runtime_refresh_fails(monkeypatch):
    monkeypatch.setattr(api, "is_agent_backend", lambda name: name == "codex")
    monkeypatch.setattr(api, "supports_runtime_refresh", lambda name: name == "codex")
    monkeypatch.setattr(api, "_agent_runtime_fingerprint", lambda name: None)
    monkeypatch.setattr(
        api,
        "install_agent",
        lambda name: {"ok": True, "message": "Installed", "output": "done", "path": "/usr/local/bin/codex"},
    )
    monkeypatch.setattr(api, "restart_backend", lambda name, **kwargs: {"ok": False, "message": "refresh timeout"})
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()

    started = api.start_agent_install_job("codex")
    deadline = time.time() + 2.0
    result = {}
    while time.time() < deadline:
        result = api.get_agent_install_job(started["job_id"], backend="codex")
        if result.get("status") != "running":
            break
        time.sleep(0.01)

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert result["message"] == "refresh timeout"
    assert result["restart"] == {"ok": False, "message": "refresh timeout"}


def test_agent_runtime_fingerprint_fails_closed_when_config_probe_fails(monkeypatch):
    monkeypatch.setattr(api.V2Config, "load", Mock(side_effect=RuntimeError("fixture config failure")))

    assert api._agent_runtime_fingerprint("claude") is None


def _wait_for_install_job(job_id):
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        result = api.get_agent_install_job(job_id)
        if result["status"] != "running":
            return result
        time.sleep(0.001)
    pytest.fail("install worker did not finish")


@pytest.mark.parametrize("backend", ["claude", "codex", "opencode"])
@pytest.mark.parametrize(
    "change",
    [
        "unchanged",
        "version",
        "custom-cli",
        "same-target-alias",
        "symlink-target",
        "unknown-before",
        "unknown-after",
        "fresh-install",
    ],
)
def test_install_job_measures_configured_runtime(monkeypatch, tmp_path, backend, change):
    """BRR-009: actual install bookkeeping feeds the refresh decision."""
    standard = tmp_path / "标准 CLI" / backend
    custom = tmp_path / "自定义 CLI" / backend
    replacement = tmp_path / "新 CLI" / backend
    for binary in (standard, custom, replacement):
        binary.parent.mkdir()
        binary.touch()
    if change == "same-target-alias":
        custom.unlink()
        custom.symlink_to(standard)
    configured = custom if change in {"custom-cli", "same-target-alias"} else standard
    config = V2Config.default()
    getattr(config.agents, backend).cli_path = str(configured)
    config.save()
    # Load config before replacing subprocess constructors used by lazy imports.
    assert not V2Config.load().load_warnings
    installed = False
    resolved = []
    probed = []

    def resolve(binary):
        resolved.append(binary)
        if change == "fresh-install" and not installed:
            return None
        return str(standard) if binary == backend else binary

    def probe(binary):
        probed.append(binary)
        if change == ("unknown-after" if installed else "unknown-before"):
            return None
        return "1.0.1" if installed and change == "version" else "1.0.0"

    class InstallProcess:
        returncode = 0

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, **kwargs):
            nonlocal installed
            installed = True
            if change == "symlink-target":
                standard.unlink()
                standard.symlink_to(replacement)
            return "fixture installed", ""

    monkeypatch.setattr(api, "resolve_cli_path", resolve)
    monkeypatch.setattr(api, "_probe_cli_version", probe)
    monkeypatch.setattr(api, "_cached_version", Mock(side_effect=AssertionError("must measure, not use cached versions")))
    monkeypatch.setattr(api.subprocess, "Popen", InstallProcess)
    monkeypatch.setattr(
        api, "install_agent",
        lambda name: api._run_install_command(name, ["fixture-installer"], lambda value: value),
    )
    refresh = Mock(return_value={"ok": True})
    monkeypatch.setattr(api, "restart_backend", refresh)
    monkeypatch.setattr(api, "_AGENT_INSTALL_JOBS", {})
    monkeypatch.setattr(api, "_AGENT_INSTALL_LATEST_BY_BACKEND", {})

    started = api.start_agent_install_job(backend)
    result = _wait_for_install_job(started["job_id"])
    assert result["status"] == "succeeded"
    assert result["ok"] is True
    assert getattr(V2Config.load().agents, backend).cli_path == str(standard)
    assert resolved == [str(configured), backend, str(standard)]
    assert probed == ([str(standard)] if change == "fresh-install" else [str(configured), str(standard)])
    if change == "unchanged":
        refresh.assert_not_called()
        assert result["restart"] == {"ok": True, "skipped": True}
    else:
        refresh.assert_called_once_with(
            backend, metadata={"reason": "agent_install_job", "source": "ui_api"},
        )
        assert result["restart"] == {"ok": True}


def test_install_job_does_not_refresh_when_install_fails(monkeypatch):
    monkeypatch.setattr(api, "is_agent_backend", lambda name: name == "claude")
    monkeypatch.setattr(api, "supports_runtime_refresh", lambda name: name == "claude")
    monkeypatch.setattr(
        api,
        "_agent_runtime_fingerprint",
        lambda name: ("/fixture/claude", "/fixture/claude", "1.0.0"),
    )
    monkeypatch.setattr(
        api,
        "install_agent",
        lambda name: {"ok": False, "message": "Upgrade failed", "output": "error", "path": None},
    )
    monkeypatch.setattr(api, "restart_backend", lambda *args, **kwargs: pytest.fail("refresh should not run"))
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()

    started = api.start_agent_install_job("claude")
    deadline = time.time() + 2.0
    result = {}
    while time.time() < deadline:
        result = api.get_agent_install_job(started["job_id"], backend="claude")
        if result.get("status") != "running":
            break
        time.sleep(0.01)

    assert result["status"] == "failed"
    assert result["ok"] is False
    assert "restart" not in result


def test_vibe_agent_routes_return_structured_client_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / ".vibe_remote"))
    client = app.test_client()

    missing = client.get("/api/agents/missing")
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "agent_not_found"

    headers = csrf_headers(client)
    created = client.post(
        "/api/agents",
        json={"name": "worker", "backend": "codex"},
        headers=headers,
    )
    assert created.status_code == 200

    duplicate = client.post(
        "/api/agents",
        json={"name": "worker", "backend": "codex"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.get_json()["code"] == "agent_already_exists"

    immutable = client.request(
        "PATCH",
        "/api/agents/worker",
        json={"backend": "claude"},
        headers=headers,
    )
    assert immutable.status_code == 400
    assert immutable.get_json()["code"] == "invalid_agent_request"

    invalid_delete = client.delete("/api/agents/!!!", headers=headers)
    assert invalid_delete.status_code == 400
    assert invalid_delete.get_json()["code"] == "invalid_agent_request"

    client.post(
        "/api/agents",
        json={"name": "archive-fallback", "backend": "codex"},
        headers=headers,
    )
    archived = client.delete("/api/agents/worker", headers=headers).get_json()["archived_agent"]
    archived_edit = client.patch(
        f"/api/agents/{archived['name']}",
        json={"description": "changed"},
        headers=headers,
    )
    assert archived_edit.status_code == 409
    assert archived_edit.get_json()["code"] == "agent_archived_read_only"


def test_install_job_dedupes_running_backend(monkeypatch):
    calls: list[str] = []
    release = threading.Event()

    def install(name):
        calls.append(name)
        release.wait(timeout=1.0)
        return {"ok": True, "message": "Installed", "output": "done", "path": "/usr/local/bin/codex"}

    monkeypatch.setattr(api, "is_agent_backend", lambda name: name == "codex")
    monkeypatch.setattr(api, "supports_runtime_refresh", lambda name: False)
    monkeypatch.setattr(api, "install_agent", install)
    with api._AGENT_INSTALL_JOB_LOCK:
        api._AGENT_INSTALL_JOBS.clear()
        api._AGENT_INSTALL_LATEST_BY_BACKEND.clear()

    first = api.start_agent_install_job("codex")
    deadline = time.time() + 1.0
    while time.time() < deadline and not calls:
        time.sleep(0.01)

    second = api.start_agent_install_job("codex")
    release.set()
    _wait_for_install_job(first["job_id"])

    assert second["job_id"] == first["job_id"]
    assert second["status"] == "running"
    assert calls == ["codex"]
