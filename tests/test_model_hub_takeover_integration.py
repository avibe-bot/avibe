"""Controller/adapter/store/HTTP boundary tests, with synthetic credentials only."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from aiohttp import web

from config.atomic_io import write_atomic
from core.handlers.model_hub.adapter import EngineEnsureResult
from core.handlers.model_hub.service import ModelHubError
from tests.scenarios.model_hub.test_model_hub_migration_scenarios import (
    _isolate_native_home,
    _service,
    _write_claude_oauth,
    _write_codex_oauth,
)
from vibe.model_hub_runtime.adapter import CLIProxyEngineAdapter
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.state import EngineStateStore


class FixtureSupervisor:
    """Replace OS process management only; keep the real engine HTTP client."""

    def __init__(self, state, origin):
        self.state_store = state
        self.connection = EngineConnection(origin, "fixture-management", "fixture-gateway")

    def client_if_running(self):
        return EngineClient(self.connection)

    def client(self):
        return self.client_if_running()

    def with_engine_excluded(self, operation):
        return operation(self.client_if_running())

    def ensure_running(self):
        return self.connection

    def restart_if_running(self):
        pass

    def reload_config_if_running(self, _previous=None):
        pass

    def status(self):
        return {"status": {
            "health": "ok", "installed_version": "fixture", "verified": True,
            "listening": {"host": "127.0.0.1", "port": int(self.connection.base_url.rsplit(":", 1)[1])},
            "last_check": None,
        }}


def real_adapter(state, origin):
    adapter = CLIProxyEngineAdapter(
        supervisor=FixtureSupervisor(state, origin), state_store=state,
    )

    async def installed(**kwargs):
        # Installation is outside this test: never download or spawn CPA.
        return EngineEnsureResult(await adapter.status(), False)

    adapter.ensure_installed = installed
    adapter.recover_installation = adapter.status
    return adapter


@asynccontextmanager
async def management_fixture(state, service, native_path, backend):
    fixture = SimpleNamespace(
        calls=[], probes=[], violations=[], fail_validation=True, rotated=False,
    )

    def inventory():
        rows = []
        for path in state.auth_dir.glob("*.json"):
            payload = json.loads(path.read_text())
            if native_path.exists():
                fixture.violations.append("native credential still exists at publication")
            if service.store.load().agents[backend].mode != "hub":
                fixture.violations.append("mode not committed before publication")
            pending = service.migration_journal.load()
            if pending is not None and pending["phase"] != "exposed":
                fixture.violations.append("ownership intent not durable before publication")
            if not payload.get("prefix"):
                fixture.violations.append("watched publication missing Source prefix")
            if not fixture.rotated:
                payload["refresh_token"] = "fixture-current-R1"
                payload["access_token"] = "fixture-current-A1"
                write_atomic(path, json.dumps(payload))
                fixture.rotated = True
            rows.append({
                "name": path.name, "id": path.name, "provider": payload["type"],
                "auth_index": path.name, "prefix": payload.get("prefix", ""),
                "status": "active",
                "id_token": {"chatgpt_account_id": payload.get("account_id")},
            })
        return rows

    async def handle(request):
        fixture.calls.append((request.method, request.path))
        if request.headers.get("X-Management-Key") != "fixture-management":
            return web.json_response({}, status=401)
        if request.method == "GET" and request.path == "/v0/management/auth-files":
            return web.json_response({"files": inventory()})
        if request.method == "POST" and request.path == "/v0/management/api-call":
            payload = await request.json()
            fixture.probes.append(payload)
            assert payload["header"]["Authorization"] == "Bearer $TOKEN$"
            assert payload["method"] == "GET"
            assert payload["url"] == (
                "https://chatgpt.com/backend-api/wham/usage"
                if backend == "codex"
                else "https://api.anthropic.com/api/oauth/profile"
            )
            assert "data" not in payload
            serialized = json.dumps(payload)
            for secret in (
                "claude-oauth-token",
                "claude-refresh-token",
                "codex-access-123456",
                "codex-refresh-123456",
            ):
                assert secret not in serialized
            status = 503 if fixture.fail_validation else 200
            body = {"error": {"type": "unavailable"}} if status == 503 else (
                {"plan_type": "future-plan", "rate_limit": {"allowed": False}}
                if backend == "codex"
                else {"account": {"uuid": "fixture-account-uuid"}}
            )
            return web.json_response({"status_code": status, "body": json.dumps(body)})
        fixture.violations.append("unexpected management mutation or model request")
        return web.json_response({}, status=500)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fixture.origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        yield fixture
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["codex", "claude"])
async def test_real_store_http_takeover_recovers_rotated_grant(monkeypatch, tmp_path, backend):
    home = tmp_path / "native"
    _isolate_native_home(monkeypatch, home)
    if backend == "codex":
        _write_codex_oauth(home)
        native_path = home / ".codex/auth.json"
    else:
        _write_claude_oauth(home)
        native_path = home / ".claude/.credentials.json"
    service, config_store, _ = _service(tmp_path, migration_home=home)
    config_store.config.agents[backend].mode = "direct"
    ids = [row["id"] for row in service.migration_scan()["items"]]
    state = EngineStateStore(tmp_path / "engine")

    async with management_fixture(state, service, native_path, backend) as fixture:
        service.adapter = real_adapter(state, fixture.origin)
        with pytest.raises(ModelHubError) as failure:
            await service.migration_apply(ids, clean_api_keys=True)
        assert failure.value.code == "migration_recovery_pending"
        assert service.migration_journal.load()["phase"] == "exposed"
        assert not native_path.exists()
        [live_path] = state.auth_dir.glob("*.json")
        assert json.loads(live_path.read_text())["refresh_token"] == "fixture-current-R1"

        # Reopen both persistence layers, discarding process-local adapter state.
        restarted, _, _ = _service(tmp_path, migration_home=home)
        restarted.store = config_store
        reopened = EngineStateStore(tmp_path / "engine")
        restarted.adapter = real_adapter(reopened, fixture.origin)
        fixture.fail_validation = False
        result = await restarted.migration_apply(ids, clean_api_keys=True)

        assert result["applied"] == 1
        assert not restarted.migration_blocked_backends
        assert restarted.migration_journal.load() is None
        assert not native_path.exists()
        current = json.loads(live_path.read_text())
        assert current["refresh_token"] == "fixture-current-R1"
        assert current["prefix"] == reopened.list_sources()[0].prefix
        assert not list(reopened.oauth_staging_dir.glob("*.json"))
        assert len(fixture.probes) == 2
        assert fixture.violations == []
        assert ("POST", "/v0/management/auth-files") not in fixture.calls
