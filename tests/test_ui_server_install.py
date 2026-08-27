from __future__ import annotations

import threading
import time

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


def test_dependency_install_route_allows_memory_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        api,
        "start_dependency_install_job",
        lambda dep: {"ok": True, "job_id": "job-memory", "backend": dep, "status": "running"},
    )

    client = app.test_client()
    response = client.post(
        "/api/dependencies/memory-runtime/install",
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["backend"] == "memory-runtime"


def test_dependency_install_route_allows_memory_package(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setattr(
        api,
        "start_dependency_install_job",
        lambda dep: {"ok": True, "job_id": "job-memory-package", "backend": dep, "status": "running"},
    )

    client = app.test_client()
    response = client.post(
        "/api/dependencies/memory-package/install",
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == 200
    assert response.get_json()["backend"] == "memory-package"


@pytest.mark.parametrize(
    ("dep", "result", "expected_status"),
    (
        pytest.param(
            "memory-package",
            {
                "ok": True,
                "job_id": "job-restarted",
                "backend": "memory-package",
                "status": "succeeded",
                "message": "memory_package_ready",
                "recovered": True,
            },
            200,
            id="recovered-memory-package",
        ),
        pytest.param(
            "memory-package",
            {"ok": False, "error": "job_not_found"},
            404,
            id="non-ready-memory-package",
        ),
        pytest.param(
            "askill",
            {"ok": False, "error": "job_not_found"},
            404,
            id="unrelated-dependency",
        ),
    ),
)
def test_dependency_install_status_uses_state_aware_poller(
    monkeypatch,
    tmp_path,
    dep,
    result,
    expected_status,
):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        api,
        "get_dependency_install_job",
        lambda actual_dep, job_id: calls.append((actual_dep, job_id)) or result,
    )

    client = app.test_client()
    response = client.get(
        f"/api/dependencies/{dep}/install/job-restarted",
        headers=csrf_headers(client, "http://127.0.0.1:15131"),
        base_url="http://127.0.0.1:15131",
    )

    assert response.status_code == expected_status
    assert response.get_json() == result
    assert calls == [(dep, "job-restarted")]


def test_install_job_fails_when_runtime_refresh_fails(monkeypatch):
    monkeypatch.setattr(api, "is_agent_backend", lambda name: name == "codex")
    monkeypatch.setattr(api, "supports_runtime_refresh", lambda name: name == "codex")
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

    assert second["job_id"] == first["job_id"]
    assert second["status"] == "running"
    assert calls == ["codex"]
