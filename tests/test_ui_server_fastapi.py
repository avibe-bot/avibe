import gzip

import pytest

from storage.importer import ensure_sqlite_state
from vibe.ui_compat import (
    TEST_REMOTE_ADDR_HEADER,
    CompatApp,
    normalize_response,
    route_path_to_fastapi,
    run_maybe_async,
    request,
)
from starlette.websockets import WebSocketDisconnect

from vibe import ui_server
from vibe.ui_server import app
from tests.test_api_save_config_merge import _full_config_payload
from tests.ui_server_test_helpers import csrf_headers


def _raw_client_get(client, path: str, *, headers: dict[str, str] | None = None):
    request_headers = {TEST_REMOTE_ADDR_HEADER: "127.0.0.1"}
    request_headers.update(headers or {})
    with client._client.stream(
        "GET",
        f"http://127.0.0.1{path}",
        headers=request_headers,
    ) as response:
        body = b"".join(response.iter_raw())
    return response, body


def test_websocket_echo_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VIBE_UI_ENABLE_WS_ECHO", raising=False)

    with pytest.raises(WebSocketDisconnect) as exc:
        with app.test_client().websocket_connect("/ws/echo"):
            pass

    assert exc.value.code == 1008


def test_websocket_echo_smoke_when_enabled(monkeypatch):
    monkeypatch.setenv("VIBE_UI_ENABLE_WS_ECHO", "1")

    with app.test_client().websocket_connect("/ws/echo") as websocket:
        websocket.send_text("hello")

        assert websocket.receive_text() == "echo: hello"


def test_fastapi_schema_routes_are_not_exposed():
    client = app.test_client()

    docs_response = client.get("/docs")
    assert b"swagger-ui" not in docs_response.content.lower()
    assert client.get("/openapi.json").status_code != 200


def test_route_path_to_fastapi_converts_named_path_converter():
    assert route_path_to_fastapi("/files/<path:file_path>") == "/files/{file_path:path}"


def test_opencode_model_delete_route_captures_slashes():
    routes = [getattr(route, "path", "") for route in app.routes]

    assert (
        "/api/backend/opencode/provider/{provider_id}/models/{model_id:path}"
        in routes
    )


def test_compat_app_matches_named_path_converter():
    compat_app = CompatApp()

    @compat_app.route("/files/<path:file_path>")
    def get_file(file_path):
        return {"file_path": file_path}

    response = compat_app.test_client().get("/files/nested/example.txt")

    assert response.status_code == 200
    assert response.get_json() == {"file_path": "nested/example.txt"}


def test_normalize_response_supports_body_headers_tuple():
    response = normalize_response(("ok", {"X-Test": "yes"}))

    assert response.status_code == 200
    assert response.headers["X-Test"] == "yes"
    assert response.body == b"ok"


def test_harness_routes_page_filter_and_return_counts(monkeypatch, tmp_path):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(5):
            store.upsert_scheduled_task(
                {
                    "id": f"task-{index}",
                    "name": f"Task {index}",
                    "prompt": "run it",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": index < 3,
                    "created_at": f"2026-06-04T00:0{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:0{index}:00+00:00",
                }
            )
        for index in range(6):
            store.upsert_watch(
                {
                    "id": f"watch-{index}",
                    "name": f"Deploy watch {index}",
                    "shell_command": f"tail deploy-{index}.log",
                    "enabled": index == 0,
                    "created_at": f"2026-06-04T00:1{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:1{index}:00+00:00",
                }
            )
        for index, status in enumerate(["pending", "processing", "completed", "failed"]):
            store.enqueue_run(
                {
                    "id": f"run-{index}",
                    "request_type": "watch",
                    "status": status,
                    "message": "deploy status",
                    "created_at": f"2026-06-04T00:2{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:2{index}:00+00:00",
                }
            )
    finally:
        store.close()

    client = app.test_client()
    legacy_tasks = client.get("/api/harness/tasks").get_json()
    legacy_watches = client.get("/api/harness/watches").get_json()
    tasks = client.get("/api/harness/tasks?status=enabled&page=1&limit=2").get_json()
    watches = client.get("/api/harness/watches?status=disabled&query=deploy&page=1&limit=2").get_json()
    runs = client.get("/api/harness/runs?page=1&limit=2").get_json()
    counts = client.get("/api/harness/counts").get_json()

    assert len(legacy_tasks["tasks"]) == 5
    assert legacy_tasks["has_more"] is False
    assert len(legacy_watches["watches"]) == 6
    assert legacy_watches["has_more"] is False
    assert [item["id"] for item in tasks["tasks"]] == ["task-2", "task-1"]
    assert tasks["counts"] == {"all": 5, "enabled": 3, "disabled": 2}
    assert tasks["total"] == 3
    assert tasks["has_more"] is True
    assert [item["id"] for item in watches["watches"]] == ["watch-5", "watch-4"]
    assert watches["counts"] == {"all": 6, "enabled": 1, "disabled": 5}
    assert watches["total"] == 5
    assert watches["has_more"] is True
    assert [item["id"] for item in runs["runs"]] == ["run-3", "run-2"]
    assert runs["total"] == 4
    assert runs["counts"]["queued"] == 1
    assert runs["counts"]["running"] == 1
    assert runs["counts"]["succeeded"] == 1
    assert runs["counts"]["failed"] == 1
    assert counts["tasks"]["all"] == 5
    assert counts["watches"]["disabled"] == 5
    assert counts["runs"]["all"] == 4


def test_harness_bootstrap_returns_counts_and_selected_page(monkeypatch, tmp_path):
    from storage.background import SQLiteBackgroundTaskStore

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    store = SQLiteBackgroundTaskStore()
    try:
        for index in range(4):
            store.upsert_scheduled_task(
                {
                    "id": f"task-{index}",
                    "name": f"Task {index}",
                    "prompt": "run it",
                    "schedule_type": "cron",
                    "cron": "0 * * * *",
                    "enabled": index < 2,
                    "created_at": f"2026-06-04T00:0{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:0{index}:00+00:00",
                }
            )
        for index, status in enumerate(["pending", "completed"]):
            store.enqueue_run(
                {
                    "id": f"run-{index}",
                    "request_type": "task",
                    "status": status,
                    "message": "run status",
                    "created_at": f"2026-06-04T00:2{index}:00+00:00",
                    "updated_at": f"2026-06-04T00:2{index}:00+00:00",
                }
            )
    finally:
        store.close()

    client = app.test_client()
    response = client.get("/api/harness/bootstrap?tab=tasks&status=enabled&page=1&limit=1")

    assert response.status_code == 200
    assert response.headers["X-Vibe-Request-Ms"]
    payload = response.get_json()
    assert payload["counts"]["tasks"] == {"all": 4, "enabled": 2, "disabled": 2}
    assert payload["counts"]["runs"]["all"] == 2
    assert payload["page"]["tasks"][0]["id"] == "task-1"
    assert payload["page"]["total"] == 2
    assert payload["page"]["has_more"] is True


def test_workbench_projects_bootstrap_returns_requested_session_pages(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project
    from storage.workbench_sessions_service import create_session

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_a_dir = tmp_path / "project-a"
    project_b_dir = tmp_path / "project-b"
    project_a_dir.mkdir()
    project_b_dir.mkdir()
    with engine.begin() as conn:
        project_a = create_project(conn, str(project_a_dir), display_name="Project A")
        project_b = create_project(conn, str(project_b_dir), display_name="Project B")
        create_session(conn, scope_id=project_a["scope_id"], agent_backend="", title="First")
        create_session(conn, scope_id=project_a["scope_id"], agent_backend="", title="Second")
        create_session(conn, scope_id=project_b["scope_id"], agent_backend="", title="Other")

    client = app.test_client()
    response = client.get(f"/api/workbench/projects-bootstrap?project_id={project_a['id']}&limit=1")

    assert response.status_code == 200
    assert response.headers["Server-Timing"].startswith("app;dur=")
    payload = response.get_json()
    assert {project["id"] for project in payload["projects"]} == {project_a["id"], project_b["id"]}
    assert set(payload["sessions"]) == {project_a["id"]}
    page = payload["sessions"][project_a["id"]]
    assert len(page["sessions"]) == 1
    assert page["next_before_id"] == page["sessions"][0]["id"]


def test_config_get_on_fresh_install_returns_default_needing_setup(monkeypatch, tmp_path):
    # Fresh install edge: no config file exists yet, but the setup wizard
    # (and the reused provider-config modal that calls getConfig()) must be
    # able to load. GET /api/config must serve an in-memory default with
    # needs_setup=True instead of propagating FileNotFoundError as a 500 —
    # and must not create the file (the read stays a read; save_config owns
    # the first write).
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths

    assert not paths.get_config_path().exists()

    client = app.test_client()
    response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    assert data["mode"] == "self_host"
    assert data["setup_completed"] is False
    assert data["setup_state"]["needs_setup"] is True
    assert not paths.get_config_path().exists(), "GET must not persist a config file"


def test_config_routes_redact_platform_and_gateway_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    payload = _full_config_payload()
    payload["slack"] = {
        **payload["slack"],
        "bot_token": "xoxb-route-secret",
        "app_token": "xapp-route-secret",
        "signing_secret": "slack-route-secret",
    }
    payload["telegram"] = {
        "bot_token": "123456:telegram-route-secret",
        "webhook_secret_token": "telegram-webhook-route-secret",
        "require_mention": True,
        "forum_auto_topic": True,
        "use_webhook": True,
    }
    payload["lark"] = {
        "app_id": "cli_route_lark_id",
        "app_secret": "lark-route-secret",
        "require_mention": False,
        "domain": "feishu",
    }
    payload["wechat"] = {
        "bot_token": "wechat-route-secret",
        "base_url": "https://ilinkai.weixin.qq.com",
        "cdn_base_url": "https://novac2c.cdn.weixin.qq.com/c2c",
        "require_mention": False,
    }
    payload["gateway"] = {
        "relay_url": "https://relay.example",
        "workspace_token": "workspace-route-secret",
        "client_id": "client-id",
        "client_secret": "client-route-secret",
    }

    client = app.test_client()
    response = client.post("/api/config", json=payload, headers=csrf_headers(client))

    assert response.status_code == 200
    saved = response.get_json()
    fetched = client.get("/api/config").get_json()
    for data in (saved, fetched):
        assert data["slack"]["has_bot_token"] is True
        assert data["slack"]["has_app_token"] is True
        assert data["slack"]["has_signing_secret"] is True
        assert "bot_token" not in data["slack"]
        assert "app_token" not in data["slack"]
        assert "signing_secret" not in data["slack"]
        assert data["discord"]["has_bot_token"] is True
        assert "bot_token" not in data["discord"]
        assert data["telegram"]["has_bot_token"] is True
        assert data["telegram"]["has_webhook_secret_token"] is True
        assert "bot_token" not in data["telegram"]
        assert "webhook_secret_token" not in data["telegram"]
        assert data["lark"]["has_app_secret"] is True
        assert "app_secret" not in data["lark"]
        assert data["wechat"]["has_bot_token"] is True
        assert "bot_token" not in data["wechat"]
        assert data["gateway"]["has_workspace_token"] is True
        assert data["gateway"]["has_client_secret"] is True
        assert "workspace_token" not in data["gateway"]
        assert "client_secret" not in data["gateway"]


def test_config_post_hot_reconciles_platform_enablement(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    reconcile_calls = []
    restart_calls = []

    async def _reconcile_platforms():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "added": ["slack"]}}

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(ui_server, "_schedule_service_restart_for_config_fallback", lambda: restart_calls.append(True) or {"ok": True})

    next_payload = {
        **payload,
        "platforms": {"enabled": ["discord", "slack"], "primary": "discord"},
        "slack": {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"},
    }
    client = app.test_client()
    response = client.post("/api/config", json=next_payload, headers=csrf_headers(client))

    assert response.status_code == 200
    data = response.get_json()
    assert data["platform_runtime"]["hot_reconciled"] is True
    assert reconcile_calls == [True]
    assert restart_calls == []


def test_config_post_hot_reconciles_platform_runtime_credential_change(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    reconcile_calls = []

    async def _reconcile_platforms():
        reconcile_calls.append(True)
        return {"status_code": 200, "body": {"ok": True, "rebuilt": ["discord"]}}

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"discord": {"bot_token": "discord-new-token-12345"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["platform_runtime"]["body"]["rebuilt"] == ["discord"]
    assert reconcile_calls == [True]


def test_platform_runtime_fields_changed_detects_primary_only_change():
    from config.v2_config import V2Config

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord", "slack"], "primary": "discord"}
    payload["slack"] = {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"}
    previous = V2Config.from_payload(payload)
    current = V2Config.from_payload(
        {
            **payload,
            "platform": "slack",
            "platforms": {"enabled": ["discord", "slack"], "primary": "slack"},
        }
    )

    assert (
        ui_server._platform_runtime_fields_changed(
            previous,
            current,
            {"platforms": {"enabled": ["discord", "slack"], "primary": "slack"}},
        )
        is True
    )


def test_config_post_non_platform_change_does_not_reconcile_platforms(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        raise AssertionError("platform reconcile should not run")

    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)

    client = app.test_client()
    response = client.post("/api/config", json={"show_duration": False}, headers=csrf_headers(client))

    assert response.status_code == 200
    assert "platform_runtime" not in response.get_json()


def test_config_post_schedules_service_restart_when_hot_reconcile_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        raise internal_client.InternalServerUnavailable("missing socket")

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True) or {"ok": True, "restart": {"job_id": "job-hot-fallback"}},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"platforms": {"enabled": ["discord", "slack"], "primary": "discord"}, "slack": {"bot_token": "xoxb-hot-token", "app_token": "xapp-hot-token"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime = response.get_json()["platform_runtime"]
    assert runtime["hot_reconciled"] is False
    assert runtime["restart_scheduled"] is True
    assert restart_calls == [True]


def test_config_post_schedules_service_restart_when_hot_reconcile_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api
    from vibe import internal_client

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": ["discord"], "primary": "discord"}
    api.save_config(payload)

    async def _reconcile_platforms():
        return {
            "status_code": 500,
            "body": {"ok": False, "error": "IM thread for discord did not stop within timeout"},
        }

    restart_calls = []
    monkeypatch.setattr(internal_client, "reconcile_platforms", _reconcile_platforms)
    monkeypatch.setattr(
        ui_server,
        "_schedule_service_restart_for_config_fallback",
        lambda: restart_calls.append(True) or {"ok": True, "restart": {"job_id": "job-hot-failure"}},
    )

    client = app.test_client()
    response = client.post(
        "/api/config",
        json={"discord": {"bot_token": "discord-new-token-12345"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    runtime = response.get_json()["platform_runtime"]
    assert runtime["hot_reconciled"] is False
    assert runtime["restart_scheduled"] is True
    assert runtime["body"]["error"] == "IM thread for discord did not stop within timeout"
    assert restart_calls == [True]


def test_config_restart_fallback_marks_pending_restart_when_restart_in_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import restart_supervisor
    from vibe import runtime

    runtime.get_restart_status_path().parent.mkdir(parents=True, exist_ok=True)
    restart_status = {
        "ok": None,
        "state": "running",
        "job_id": "job-in-flight",
        "supervisor_pid": 4242,
    }
    runtime.write_json(runtime.get_restart_status_path(), restart_status)
    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: True)
    monkeypatch.setattr(
        restart_supervisor,
        "schedule_restart",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not overlap restart jobs")),
    )

    result = ui_server._schedule_service_restart_for_config_fallback()

    assert result["ok"] is True
    assert result["code"] == "restart_pending_after_in_progress"
    assert result["restart"] == restart_status
    pending = runtime.read_json(restart_supervisor._pending_restart_path())
    assert pending["restart_job_id"] == "job-in-flight"
    assert pending["trigger"] == "web-ui-config-pending"
    assert pending["scope"] == "service"


def test_config_restart_fallback_schedules_when_in_flight_finishes_after_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import restart_supervisor
    from vibe import runtime

    runtime.get_restart_status_path().parent.mkdir(parents=True, exist_ok=True)
    restart_status = {
        "ok": None,
        "state": "running",
        "job_id": "job-in-flight",
        "supervisor_pid": 4242,
    }
    runtime.write_json(runtime.get_restart_status_path(), restart_status)
    in_flight_results = iter([True, False])
    scheduled: list[dict] = []

    monkeypatch.setattr(ui_server, "_restart_in_flight", lambda: next(in_flight_results))
    monkeypatch.setattr(restart_supervisor, "schedule_restart", lambda **kwargs: scheduled.append(kwargs) or {"job_id": "followup"})
    monkeypatch.setattr(runtime, "read_status", lambda: {"service_pid": 11, "ui_pid": 22})

    result = ui_server._schedule_service_restart_for_config_fallback()

    assert result["ok"] is True
    assert result["code"] == "restart_scheduled_after_in_flight_finished"
    assert result["restart"] == {"job_id": "followup"}
    assert scheduled == [{"delay_seconds": 0.0, "trigger": "web-ui-config", "scope": "service"}]
    assert runtime.read_json(restart_supervisor._pending_restart_path()) is None


def test_static_ui_assets_use_cache_headers(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (ui_dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (ui_dist / "manifest.webmanifest").write_text("{}", encoding="utf-8")
    (assets_dir / "app-abc123.js").write_text("console.log('ok')", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    asset_response = client.get("/assets/app-abc123.js")
    manifest_response = client.get("/manifest.webmanifest")
    index_response = client.get("/")
    spa_response = client.get("/workbench/session-1")

    assert asset_response.status_code == 200
    assert asset_response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert manifest_response.status_code == 200
    assert manifest_response.headers["Cache-Control"] == "public, max-age=3600"
    assert index_response.status_code == 200
    assert index_response.headers["Cache-Control"] == "no-store, private"
    assert spa_response.status_code == 200
    assert spa_response.headers["Cache-Control"] == "no-store, private"


def test_static_ui_asset_omits_csrf_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "index-abc123.js").write_text("console.log('ok')", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    response = app.test_client().get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert not any(header.startswith("vibe_csrf_token=") for header in response.headers.getlist("Set-Cookie"))


def test_static_ui_documents_keep_csrf_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    ui_dist.mkdir(parents=True)
    (ui_dist / "index.html").write_text("<html>app</html>", encoding="utf-8")

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    index_response = app.test_client().get("/")
    spa_response = app.test_client().get("/workbench/session-1")

    assert index_response.status_code == 200
    assert any(header.startswith("vibe_csrf_token=") for header in index_response.headers.getlist("Set-Cookie"))
    assert spa_response.status_code == 200
    assert any(header.startswith("vibe_csrf_token=") for header in spa_response.headers.getlist("Set-Cookie"))


def test_static_ui_asset_gzip_uses_shared_response_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    original = b"console.log('edge cache');\n" * 200
    (assets_dir / "index-abc123.js").write_bytes(original)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    identity_response = client.get("/assets/index-abc123.js", headers={"Accept-Encoding": ""})
    assert identity_response.status_code == 200
    assert identity_response.content == original
    assert "Content-Encoding" not in identity_response.headers
    assert "Accept-Encoding" in identity_response.headers["Vary"]
    assert identity_response.headers["Accept-Ranges"] == "bytes"
    assert identity_response.headers["ETag"]
    assert identity_response.headers["Last-Modified"]

    gzip_disabled_response = client.get("/assets/index-abc123.js", headers={"Accept-Encoding": "br, gzip;q=0"})
    assert gzip_disabled_response.status_code == 200
    assert gzip_disabled_response.content == original
    assert "Content-Encoding" not in gzip_disabled_response.headers
    assert "Accept-Encoding" in gzip_disabled_response.headers["Vary"]

    with client._client.stream(
        "GET",
        "http://127.0.0.1/assets/index-abc123.js",
        headers={
            "Accept-Encoding": "gzip",
            TEST_REMOTE_ADDR_HEADER: "127.0.0.1",
        },
    ) as gzip_response:
        compressed = b"".join(gzip_response.iter_raw())

    assert gzip_response.status_code == 200
    assert gzip_response.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in gzip_response.headers["Vary"]
    assert gzip.decompress(compressed) == original


def test_static_ui_asset_range_request_keeps_file_response_semantics(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    original = b"0123456789abcdef"
    (assets_dir / "index-abc123.js").write_bytes(original)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    response = app.test_client().get(
        "/assets/index-abc123.js",
        headers={
            "Accept-Encoding": "gzip",
            "Range": "bytes=0-9",
        },
    )

    assert response.status_code == 206
    assert response.content == b"0123456789"
    assert response.headers["Content-Range"] == f"bytes 0-9/{len(original)}"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "Content-Encoding" not in response.headers


def test_static_ui_asset_gzip_skips_small_and_binary_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))
    ui_dist = tmp_path / "dist"
    assets_dir = ui_dist / "assets"
    assets_dir.mkdir(parents=True)
    small_js = b"console.log('small')"
    png = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 2048)
    (assets_dir / "small-abc123.js").write_bytes(small_js)
    (assets_dir / "logo-abc123.png").write_bytes(png)

    monkeypatch.setattr(ui_server, "get_ui_dist_path", lambda: ui_dist)

    client = app.test_client()
    small_response = client.get("/assets/small-abc123.js", headers={"Accept-Encoding": "gzip"})
    binary_response = client.get("/assets/logo-abc123.png", headers={"Accept-Encoding": "gzip"})

    assert small_response.status_code == 200
    assert small_response.content == small_js
    assert "Content-Encoding" not in small_response.headers
    assert binary_response.status_code == 200
    assert binary_response.content == png
    assert "Content-Encoding" not in binary_response.headers
    assert "Vary" not in binary_response.headers


def test_json_api_gzip_uses_shared_response_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))

    client = app.test_client()
    identity_response = client.get("/api/config", headers={"Accept-Encoding": ""})
    original = identity_response.content

    assert identity_response.status_code == 200
    assert identity_response.is_json
    assert len(original) >= ui_server._SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES
    assert "Content-Encoding" not in identity_response.headers
    assert "Accept-Encoding" in identity_response.headers["Vary"]

    gzip_disabled_response = client.get("/api/config", headers={"Accept-Encoding": "br, gzip;q=0"})
    assert gzip_disabled_response.status_code == 200
    assert gzip_disabled_response.content == original
    assert "Content-Encoding" not in gzip_disabled_response.headers
    assert "Accept-Encoding" in gzip_disabled_response.headers["Vary"]

    gzip_client = app.test_client()
    gzip_response, compressed = _raw_client_get(
        gzip_client,
        "/api/config",
        headers={"Accept-Encoding": "gzip"},
    )

    assert gzip_response.status_code == 200
    assert gzip_response.headers["Content-Encoding"] == "gzip"
    assert gzip_response.headers["Content-Length"] == str(len(compressed))
    assert "Accept-Encoding" in gzip_response.headers["Vary"]
    assert gzip.decompress(compressed) == original
    assert any(header.startswith("vibe_csrf_token=") for header in gzip_response.headers.get_list("Set-Cookie"))


def test_json_api_gzip_skips_small_responses(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "home"))

    response = app.test_client().get("/api/csrf-token", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert response.is_json
    assert len(response.content) < ui_server._SHOW_RUNTIME_COMPRESSIBLE_MIN_BYTES
    assert "Content-Encoding" not in response.headers


def test_json_api_gzip_skips_sse_streaming_response():
    from fastapi.responses import StreamingResponse

    async def generate():
        yield b": stream connected\n\n"

    response = StreamingResponse(generate(), media_type="text/event-stream")

    with app.test_request_context("/api/events", headers={"Accept-Encoding": "gzip"}):
        compressed_response = ui_server._compress_materialized_api_response(response)

    assert compressed_response is response
    assert "Content-Encoding" not in response.headers
    assert response.media_type == "text/event-stream"

    body = b"event: message\ndata: {}\n\n" * 100
    materialized = ui_server.Response(content=body, mimetype="text/event-stream")
    with app.test_request_context("/api/events", headers={"Accept-Encoding": "gzip"}):
        materialized_response = ui_server._compress_materialized_api_response(materialized)

    assert materialized_response is materialized
    assert materialized_response.body == body
    assert "Content-Encoding" not in materialized_response.headers


def test_json_api_gzip_skips_attachments_and_existing_encoding():
    body = b'{"items":[' + (b'"payload",' * 300) + b'"end"]}'
    attachment = ui_server.Response(
        content=body,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=data.json"},
    )
    with app.test_request_context("/api/export", headers={"Accept-Encoding": "gzip"}):
        attachment_response = ui_server._compress_materialized_api_response(attachment)

    assert attachment_response is attachment
    assert attachment_response.body == body
    assert "Content-Encoding" not in attachment_response.headers
    assert "Vary" not in attachment_response.headers

    encoded = ui_server.Response(
        content=body,
        mimetype="application/json",
        headers={"Content-Encoding": "br"},
    )
    with app.test_request_context("/api/precompressed", headers={"Accept-Encoding": "gzip"}):
        encoded_response = ui_server._compress_materialized_api_response(encoded)

    assert encoded_response is encoded
    assert encoded_response.body == body
    assert encoded_response.headers["Content-Encoding"] == "br"


def test_run_maybe_async_offloads_sync_handlers_without_losing_context():
    import asyncio
    import threading
    import time

    loop_thread_id = threading.get_ident()

    def blocking_handler():
        assert threading.get_ident() != loop_thread_id
        time.sleep(0.05)
        return request.path

    async def ticker():
        await asyncio.sleep(0.01)
        return "tick"

    async def exercise():
        return await asyncio.gather(
            run_maybe_async(blocking_handler),
            ticker(),
        )

    compat_app = CompatApp()
    with compat_app.test_request_context("/threadpool-check"):
        result, tick = asyncio.run(exercise())

    assert result == "/threadpool-check"
    assert tick == "tick"


def test_wechat_qr_poll_marks_bind_hint_and_schedules_managed_restart(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
                "user_id": "wx-user",
            }

    bound_users = []
    restart_calls = []
    persisted = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_persist_wechat_qr_credentials", lambda result: persisted.append(result.copy()))
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr(
        "vibe.api.auto_bind_wechat_user",
        lambda user_id: bound_users.append(user_id)
        or {"ok": True, "already_bound": False, "is_admin": True, "pending_bind_menu_hint": True},
    )

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "confirmed"
    assert persisted == [
        {
            "status": "confirmed",
            "bot_token": "wechat-token",
            "base_url": "https://wechat.example.com",
            "user_id": "wx-user",
        }
    ]
    assert bound_users == ["wx-user"]
    assert restart_calls == [True]


def test_wechat_qr_poll_passes_verify_code(monkeypatch):
    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {"status": "need_verifycode", "verify_code": verify_code}

    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session", "verify_code": "1234"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "need_verifycode", "verify_code": "1234"}


def test_persist_wechat_qr_credentials_saves_before_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from vibe import api

    payload = _full_config_payload()
    payload["platforms"] = {"enabled": [], "primary": "avibe"}
    payload["wechat"] = {
        "bot_token": "",
        "base_url": "https://old-wechat.example.com",
        "cdn_base_url": "https://cdn.example.com/c2c",
        "proxy_url": "socks5://127.0.0.1:1080",
    }
    api.save_config(payload)

    ui_server._persist_wechat_qr_credentials(
        {
            "status": "confirmed",
            "bot_token": "new-token",
            "base_url": "https://new-wechat.example.com",
            "user_id": "wx-user",
        }
    )

    updated = api.load_config()
    assert updated.wechat is not None
    assert updated.wechat.bot_token == "new-token"
    assert updated.wechat.base_url == "https://new-wechat.example.com"
    assert updated.wechat.cdn_base_url == "https://cdn.example.com/c2c"
    assert updated.wechat.proxy_url == "socks5://127.0.0.1:1080"
    assert updated.platforms.enabled == ["wechat"]
    assert updated.platforms.primary == "wechat"


def test_persist_wechat_qr_credentials_seeds_fresh_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    from config import paths
    from vibe import api

    assert not paths.get_config_path().exists()

    ui_server._persist_wechat_qr_credentials(
        {
            "status": "confirmed",
            "bot_token": "new-token",
            "base_url": "https://new-wechat.example.com",
            "user_id": "wx-user",
        }
    )

    updated = api.load_config()
    assert updated.wechat is not None
    assert updated.wechat.bot_token == "new-token"
    assert updated.wechat.base_url == "https://new-wechat.example.com"
    assert updated.platforms.enabled == ["wechat"]
    assert updated.platforms.primary == "wechat"


def test_wechat_qr_start_sends_saved_token_list_to_fixed_qr_host(monkeypatch):
    class _Auth:
        async def start_login(self, base_url=None, local_token_list=None):
            return {
                "session_key": "qr-session",
                "qrcode_url": "https://wechat.example.com/qr",
                "base_url": base_url,
                "local_token_list": local_token_list,
            }

    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_load_wechat_local_tokens", lambda: ["saved-token"])

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/start",
        json={"base_url": "https://wechat.example.com"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["base_url"] == "https://ilinkai.weixin.qq.com"
    assert payload["local_token_list"] == ["saved-token"]


def test_wechat_qr_poll_does_not_autobind_without_user_id(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
            }

    bound_users = []
    restart_calls = []
    persisted = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(ui_server, "_persist_wechat_qr_credentials", lambda result: persisted.append(result.copy()))
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr(
        "vibe.api.auto_bind_wechat_user",
        lambda user_id: bound_users.append(user_id)
        or {"ok": True, "already_bound": False, "is_admin": True, "pending_bind_menu_hint": True},
    )

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "confirmed"
    assert persisted == []
    assert bound_users == []
    assert restart_calls == []


def test_wechat_qr_poll_blocks_restart_when_credential_persist_fails(monkeypatch):
    from vibe import runtime

    class _Auth:
        async def poll_status(self, session_key, verify_code=None):
            assert session_key == "qr-session"
            return {
                "status": "confirmed",
                "bot_token": "wechat-token",
                "base_url": "https://wechat.example.com",
                "user_id": "wx-user",
            }

    restart_calls = []
    bound_users = []

    runtime.ensure_config()
    monkeypatch.setattr(ui_server, "_get_wechat_auth", lambda: _Auth())
    monkeypatch.setattr(
        ui_server,
        "_persist_wechat_qr_credentials",
        lambda result: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    monkeypatch.setattr(
        ui_server,
        "_schedule_wechat_qr_login_restart",
        lambda: restart_calls.append(True) or {"job_id": "restart-1"},
    )
    monkeypatch.setattr("vibe.api.auto_bind_wechat_user", lambda user_id: bound_users.append(user_id))

    client = app.test_client()
    response = client.post(
        "/api/wechat/qr_login/poll",
        json={"session_key": "qr-session"},
        headers=csrf_headers(client),
    )

    assert response.status_code == 500
    assert response.get_json()["error"] == "failed_to_persist_wechat_credentials"
    assert restart_calls == []
    assert bound_users == []


def test_web_push_subscription_routes_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/1",
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
    }

    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": subscription, "device_label": "iPhone", "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200
    created_body = created.get_json()
    assert created_body["ok"] is True
    assert created_body["subscription"]["endpoint"] == subscription["endpoint"]
    assert created_body["subscription"]["enabled"] is True
    assert created_body["subscription"]["device_label"] == "iPhone"
    assert created_body["subscription"]["device_id"] == "device-1"

    status = client.post("/api/web-push/status", json={"endpoint": subscription["endpoint"]}, headers=headers)
    assert status.status_code == 200
    status_body = status.get_json()
    assert status_body["ok"] is True
    assert status_body["configured"] is True
    assert status_body["public_key"]
    assert status_body["subscription_count"] == 1
    assert status_body["current_subscription_enabled"] is True

    removed = client.delete(
        "/api/web-push/subscriptions",
        json={"endpoint": subscription["endpoint"]},
        headers=headers,
    )
    assert removed.status_code == 200
    assert removed.get_json() == {"ok": True, "disabled": True}

    status_after = client.post("/api/web-push/status", json={"endpoint": subscription["endpoint"]}, headers=headers)
    assert status_after.get_json()["subscription_count"] == 0
    assert status_after.get_json()["current_subscription_enabled"] is False


def test_web_push_status_sync_disables_previous_endpoint_for_same_device(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    old_subscription = {
        "endpoint": "https://push.example.test/sub/old",
        "keys": {"p256dh": "old-key", "auth": "old-auth"},
    }
    new_subscription = {
        "endpoint": "https://push.example.test/sub/new",
        "keys": {"p256dh": "new-key", "auth": "new-auth"},
    }

    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": old_subscription, "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200
    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": new_subscription},
        headers=headers,
    )
    assert created.status_code == 200

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": new_subscription["endpoint"],
            "subscription": new_subscription,
            "device_id": "device-1",
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["current_subscription_enabled"] is True
    assert status.get_json()["subscription_count"] == 1
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=old_subscription["endpoint"],
            user_key="local",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=new_subscription["endpoint"],
            user_key="local",
        ) is not None


def test_web_push_status_sync_disables_client_known_previous_endpoint(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    previous_subscription = {
        "endpoint": "https://push.example.test/sub/previous",
        "keys": {"p256dh": "previous-key", "auth": "previous-auth"},
    }
    current_subscription = {
        "endpoint": "https://push.example.test/sub/current",
        "keys": {"p256dh": "current-key", "auth": "current-auth"},
    }
    other_subscription = {
        "endpoint": "https://push.example.test/sub/other",
        "keys": {"p256dh": "other-key", "auth": "other-auth"},
    }

    for subscription in [previous_subscription, current_subscription, other_subscription]:
        created = client.post(
            "/api/web-push/subscriptions",
            json={"subscription": subscription},
            headers=headers,
        )
        assert created.status_code == 200

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": current_subscription["endpoint"],
            "subscription": current_subscription,
            "device_id": "device-1",
            "previous_endpoints": [previous_subscription["endpoint"]],
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["current_subscription_enabled"] is True
    assert status.get_json()["subscription_count"] == 2
    engine = create_sqlite_engine()
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=previous_subscription["endpoint"],
            user_key="local",
        ) is None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=current_subscription["endpoint"],
            user_key="local",
        ) is not None
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=other_subscription["endpoint"],
            user_key="local",
        ) is not None


def test_web_push_status_sync_does_not_reenable_disabled_endpoint(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/dead",
        "keys": {"p256dh": "dead-key", "auth": "dead-auth"},
    }
    created = client.post(
        "/api/web-push/subscriptions",
        json={"subscription": subscription, "device_id": "device-1"},
        headers=headers,
    )
    assert created.status_code == 200

    engine = create_sqlite_engine()
    with engine.begin() as conn:
        web_push_service.mark_send_failure(conn, endpoint=subscription["endpoint"], disable=True)

    status = client.post(
        "/api/web-push/status",
        json={
            "endpoint": subscription["endpoint"],
            "subscription": subscription,
            "device_id": "device-1",
        },
        headers=headers,
    )

    assert status.status_code == 200
    assert status.get_json()["subscription_count"] == 0
    assert status.get_json()["current_subscription_enabled"] is False
    with engine.connect() as conn:
        assert web_push_service.get_enabled_by_endpoint(
            conn,
            endpoint=subscription["endpoint"],
            user_key="local",
        ) is None


def test_web_push_unsubscribe_is_scoped_to_current_user(monkeypatch, tmp_path):
    from storage import web_push_service
    from storage.db import create_sqlite_engine

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    monkeypatch.setattr(ui_server, "_web_push_user_key", lambda: "remote:user-a")

    endpoint = "https://push.example.test/sub/other"
    engine = create_sqlite_engine()
    with engine.begin() as conn:
        web_push_service.upsert_subscription(
            conn,
            user_key="remote:user-b",
            payload={
                "endpoint": endpoint,
                "keys": {
                    "p256dh": "p256dh-key",
                    "auth": "auth-secret",
                },
            },
        )

    client = app.test_client()
    removed = client.delete(
        "/api/web-push/subscriptions",
        json={"endpoint": endpoint},
        headers=csrf_headers(client),
    )

    assert removed.status_code == 200
    assert removed.get_json() == {"ok": True, "disabled": False}
    with engine.connect() as conn:
        assert web_push_service.count_enabled(conn, user_key="remote:user-b") == 1


def test_web_push_test_route_sends_to_enabled_subscriptions(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    sends = []
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    client = app.test_client()
    headers = csrf_headers(client)
    subscription = {
        "endpoint": "https://push.example.test/sub/1",
        "keys": {
            "p256dh": "p256dh-key",
            "auth": "auth-secret",
        },
    }

    missing_endpoint = client.post("/api/web-push/test", json={}, headers=headers)
    assert missing_endpoint.status_code == 400
    assert missing_endpoint.get_json()["error"] == "endpoint_required"

    empty = client.post(
        "/api/web-push/test",
        json={"endpoint": subscription["endpoint"]},
        headers=headers,
    )
    assert empty.status_code == 404
    assert empty.get_json()["error"] == "no_subscription"

    client.post("/api/web-push/subscriptions", json={"subscription": subscription}, headers=headers)
    sent = client.post(
        "/api/web-push/test",
        json={"title": "Hello", "body": "World", "url": "/inbox", "endpoint": subscription["endpoint"]},
        headers=headers,
    )

    assert sent.status_code == 200
    assert sent.get_json() == {"ok": True, "sent": 1, "failed": 0}
    assert sends[0][0]["endpoint"] == subscription["endpoint"]
    assert sends[0][1]["title"] == "Hello"


def test_web_push_test_route_targets_current_endpoint_only(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()

    sends = []
    monkeypatch.setattr(
        "core.web_push.send_web_push",
        lambda *, subscription, payload: sends.append((subscription, payload)),
    )

    client = app.test_client()
    headers = csrf_headers(client)
    subscriptions = [
        {
            "endpoint": "https://push.example.test/sub/desktop",
            "keys": {"p256dh": "desktop-key", "auth": "desktop-auth"},
        },
        {
            "endpoint": "https://push.example.test/sub/mobile",
            "keys": {"p256dh": "mobile-key", "auth": "mobile-auth"},
        },
    ]
    for subscription in subscriptions:
        client.post("/api/web-push/subscriptions", json={"subscription": subscription}, headers=headers)

    sent = client.post(
        "/api/web-push/test",
        json={
            "title": "Hello",
            "body": "World",
            "url": "/inbox",
            "endpoint": subscriptions[0]["endpoint"],
        },
        headers=headers,
    )

    assert sent.status_code == 200
    assert sent.get_json() == {"ok": True, "sent": 1, "failed": 0}
    assert [send[0]["endpoint"] for send in sends] == [subscriptions[0]["endpoint"]]


def test_sessions_create_preserves_metadata_without_web_push_owner(monkeypatch, tmp_path):
    from storage.db import create_sqlite_engine
    from storage.projects_service import create_project

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    ensure_sqlite_state()
    engine = create_sqlite_engine()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    with engine.begin() as conn:
        project = create_project(conn, str(project_dir), display_name="Project")

    client = app.test_client()
    response = client.post(
        "/api/sessions",
        json={"project_id": project["id"], "metadata": {"client": "test"}},
        headers=csrf_headers(client),
    )

    assert response.status_code == 201
    metadata = response.get_json()["metadata"]
    assert metadata["client"] == "test"
    assert "_web_push_user_key" not in metadata
