"""Hermetic MCP Hub OAuth contracts owned by the runtime/storage boundary."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

from core.mcp_hub import oauth, repository
from core.mcp_hub.definition import definition_from_provider
from core.mcp_hub.prompt import build_mcp_prompt_blocks
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import save_config


class _LoopbackOauth:
    """One process serving AS metadata, token, and a streamable-http MCP resource."""

    def __init__(self) -> None:
        self.access = "access-1"
        self.refresh = "refresh-1"
        self.token_posts = 0
        self.mcp_posts = 0
        self.resource_status = 200
        self.refresh_status = 200
        self.refresh_error = "invalid_grant"
        self.tool_error: str | None = None
        self.codes: dict[str, str] = {}
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.origin = ""

    def start(self) -> str:
        app = Starlette(routes=[
            Route("/.well-known/oauth-protected-resource", self._prm),
            Route("/.well-known/oauth-authorization-server", self._as_meta),
            Route("/register", self._register, methods=["POST"]),
            Route("/authorize", self._authorize),
            Route("/token", self._token, methods=["POST"]),
            Route("/mcp", self._mcp, methods=["POST", "GET", "DELETE"]),
        ])
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._server.started:
                break
            time.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        return self.origin

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _prm(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "resource": f"{self.origin}/mcp",
            "authorization_servers": [self.origin],
        })

    async def _as_meta(self, request: Request) -> JSONResponse:
        return JSONResponse({
            "issuer": self.origin,
            "authorization_endpoint": f"{self.origin}/authorize",
            "token_endpoint": f"{self.origin}/token",
            "registration_endpoint": f"{self.origin}/register",
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
        })

    async def _register(self, request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({
            "client_id": "loop-client",
            "redirect_uris": body.get("redirect_uris") or [],
            "token_endpoint_auth_method": "none",
        }, status_code=201)

    async def _authorize(self, request: Request) -> Response:
        query = dict(request.query_params)
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = query.get("state", "")
        redirect = f"{query['redirect_uri']}?code={code}&state={query['state']}&iss={self.origin}"
        return Response(status_code=302, headers={"location": redirect})

    async def _token(self, request: Request) -> JSONResponse:
        self.token_posts += 1
        form = await request.form()
        grant = form.get("grant_type")
        if grant == "refresh_token":
            if self.refresh_status != 200:
                return JSONResponse({"error": self.refresh_error}, status_code=self.refresh_status)
            self.access = f"access-{self.token_posts}"
            self.refresh = f"refresh-{self.token_posts}"
        elif grant == "authorization_code":
            self.access = f"access-{self.token_posts}"
            self.refresh = f"refresh-{self.token_posts}"
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        return JSONResponse({
            "access_token": self.access,
            "refresh_token": self.refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
        })

    async def _mcp(self, request: Request) -> Response:
        self.mcp_posts += 1
        if self.resource_status == 401:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{self.origin}/.well-known/oauth-protected-resource"'},
            )
        auth = request.headers.get("authorization") or ""
        if auth != f"Bearer {self.access}":
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{self.origin}/.well-known/oauth-protected-resource"'},
            )
        payload = await request.json()
        method = payload.get("method")
        rpc_id = payload.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": payload.get("params", {}).get("protocolVersion"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "loop", "version": "0.1.0"},
            }
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return Response(status_code=202)
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
        elif method == "tools/call":
            if self.tool_error:
                return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32002, "message": self.tool_error}})
            result = {"content": [{"type": "text", "text": payload.get("params", {}).get("arguments", {}).get("text", "")}]}
        else:
            result = {}
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _run(runtime: McpRuntime, operation: str, payload: dict) -> dict:
    status, body = asyncio.run(runtime.submit(operation, payload))
    assert status == 200
    return json.loads(body)


def _seed_tokens(name: str, url: str, *, expires_at: float, access: str = "access-1", refresh: str = "refresh-1") -> None:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken, ProtectedResourceMetadata

    storage = oauth.AvibeTokenStorage(name, url)
    origin = url.rsplit("/", 1)[0]
    asyncio.run(storage.set_tokens(OAuthToken(access_token=access, refresh_token=refresh, expires_in=1, token_type="Bearer")))
    asyncio.run(storage.set_client_info(OAuthClientInformationFull(client_id="loop-client", redirect_uris=["http://127.0.0.1:5123/api/mcp/oauth/callback"])))
    raw = storage.raw()
    raw["expiresAt"] = expires_at
    raw["authServerMetadata"] = OAuthMetadata(
        issuer=origin,
        authorization_endpoint=f"{origin}/authorize",
        token_endpoint=f"{origin}/token",
        registration_endpoint=f"{origin}/register",
    ).model_dump(mode="json")
    raw["resourceMetadata"] = ProtectedResourceMetadata(
        resource=url,
        authorization_servers=[origin],
    ).model_dump(mode="json")
    oauth._write(name, raw)


@pytest.fixture
def runtime(tmp_path):
    config = save_config(tmp_path)
    config.save()
    owner = McpRuntime()
    yield owner
    owner.stop()


@pytest.fixture
def loopback():
    server = _LoopbackOauth()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_redirect_mode_obeys_oauth_loopback_rule():
    cases = [
        ("http://127.0.0.1:5123", "direct", "http://127.0.0.1:5123/api/mcp/oauth/callback"),
        ("http://localhost:5123", "direct", "http://localhost:5123/api/mcp/oauth/callback"),
        ("https://cloud.example", "direct", "https://cloud.example/api/mcp/oauth/callback"),
        ("http://192.168.1.20:5123", "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback", 5123),
        ("http://host.lan:5123", "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback", 5123),
        (None, "paste", "http://127.0.0.1:5123/api/mcp/oauth/callback", 5123),
    ]
    for origin, mode, redirect, *port in cases:
        kwargs = {"port": port[0]} if port else {}
        assert oauth.redirect_mode(origin, **kwargs) == (mode, redirect)


def test_redirect_mode_uses_configured_loopback_port():
    assert oauth.redirect_mode("http://192.168.1.20:9000", port=7777)[1] == "http://127.0.0.1:7777/api/mcp/oauth/callback"


def test_redirect_mode_requires_port_for_paste():
    with pytest.raises(Exception, match="loopback setup port"):
        oauth.redirect_mode("http://192.168.1.20:9000")


def test_definition_accepts_only_phase_one_oauth_options():
    definition = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"clientId": "ignored", "scopes": "offline_access", "secret": "ignored"}})
    assert definition["oauth"] == {"clientId": "ignored", "scopes": "offline_access"}


def test_storage_filters_client_by_provider_redirect_and_client_id_is_used(tmp_path):
    save_config(tmp_path).save()

    async def exercise():
        storage = oauth.AvibeTokenStorage("suno", "https://api.example/mcp", "https://cloud.example/api/mcp/oauth/callback")
        from mcp.shared.auth import OAuthClientInformationFull
        await storage.set_client_info(OAuthClientInformationFull(client_id="registered", redirect_uris=["http://127.0.0.1/api/mcp/oauth/callback"]))
        assert await storage.get_client_info() is None
        call_time = oauth.AvibeTokenStorage("suno", "https://api.example/mcp")
        stored = await call_time.get_client_info()
        assert stored is not None and stored.client_id == "registered"
        definition = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"clientId": "fixed"}})
        provider = oauth.make_provider("suno", definition, "https://cloud.example/api/mcp/oauth/callback", redirect_handler=lambda _: None, callback_handler=lambda: None)
        assert provider.context.client_info.client_id == "fixed"
        await provider._initialize()
        assert provider.context.client_info.client_id == "fixed"

    asyncio.run(exercise())


def test_runtime_caches_one_provider_per_server(runtime):
    definition = definition_from_provider("suno", {"url": "https://api.example/mcp"})

    async def exercise():
        first, second = await asyncio.gather(runtime._oauth_provider(definition), runtime._oauth_provider(definition))
        third = await runtime._oauth_provider(definition)
        assert first is second is third

    asyncio.run(exercise())


def test_readd_with_new_oauth_client_id_is_exists_not_unchanged(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    first = _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": url, "oauth": {"clientId": "old"}}}]})
    assert first["ok"] is True
    blocked = _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": url, "oauth": {"clientId": "new"}}}]})
    assert blocked["ok"] is False
    assert blocked["failed"][0]["type"] == "exists"
    replaced = _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": url, "oauth": {"clientId": "new"}}}], "replace": True})
    assert replaced["ok"] is True
    saved = repository.read_avibe_definition("suno")
    assert saved["oauth"]["clientId"] == "new"

    async def cached_client():
        provider = await runtime._oauth_provider(saved)
        return provider.context.client_info.client_id

    assert asyncio.run(cached_client()) == "new"


def test_oauth_provider_cache_invalidates_on_client_id_change(runtime):
    first_def = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"clientId": "old"}})
    second_def = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"clientId": "new"}})

    async def exercise():
        first = await runtime._oauth_provider(first_def)
        same = await runtime._oauth_provider(first_def)
        second = await runtime._oauth_provider(second_def)
        assert first is same
        assert second is not first
        assert second.context.client_info.client_id == "new"

    asyncio.run(exercise())


def test_oauth_storage_is_url_bound_and_private(tmp_path):
    save_config(tmp_path).save()
    storage = oauth.AvibeTokenStorage("suno", "https://api.example/mcp")
    from mcp.shared.auth import OAuthToken

    asyncio.run(storage.set_tokens(OAuthToken(access_token="secret", refresh_token="refresh", expires_in=60)))
    path = oauth.oauth_path("suno")
    assert path.stat().st_mode & 0o777 == 0o600
    assert storage.raw()["serverUrl"] == "https://api.example/mcp"
    assert oauth.AvibeTokenStorage("suno", "https://changed.example/mcp").raw() == {}


def test_needs_login_is_listed_but_omitted_from_prompt(tmp_path):
    save_config(tmp_path).save()
    definition = definition_from_provider("suno", {"url": "https://api.example/mcp", "oauth": {"scopes": "offline_access"}})
    repository.write_definition(definition)
    repository.publish("suno", definition=definition, manifest={"description": "", "auth": "needs_login"}, tools=[], commands=[])
    assert repository.list_packages()[0]["auth"] == "needs_login"
    assert "suno" not in "".join(block.text for block in build_mcp_prompt_blocks())


def test_callback_state_is_single_use_on_runtime_loop(runtime):
    async def exercise():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        runtime._pending_logins["state"] = {"callback": future}
        first = json.loads(await runtime._oauth_callback({"state": "state", "code": "code"}))
        assert first["ok"]
        with pytest.raises(Exception, match="unknown or already used"):
            await runtime._oauth_callback({"state": "state", "code": "again"})

    asyncio.run(exercise())


def test_callback_url_validation_rejects_wrong_path_or_missing_code():
    with pytest.raises(Exception, match="not an Avibe"):
        oauth.parse_callback_url("https://example.test/wrong?code=x&state=y")
    with pytest.raises(Exception, match="missing OAuth"):
        oauth.parse_callback_url("https://example.test/api/mcp/oauth/callback?state=y")


def test_first_contact_oauth_401_publishes_needs_login(runtime, loopback):
    added = _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    assert added["ok"]
    assert added["added"][0]["toolCount"] == 0
    assert added["added"][0]["auth"] == "needs_login"
    listed = repository.list_packages()[0]
    assert listed["auth"] == "needs_login"
    assert "suno" not in "".join(block.text for block in build_mcp_prompt_blocks())


def test_authorization_header_skips_oauth(runtime, loopback):
    added = _run(runtime, "add", {"servers": [{"name": "suno", "server": {
        "url": f"{loopback.origin}/mcp",
        "headers": {"Authorization": f"Bearer {loopback.access}"},
    }}]})
    assert added["ok"] and added["added"][0]["toolCount"] == 1
    assert loopback.token_posts == 0
    listed = json.dumps(repository.list_packages())
    assert "access-1" not in listed and "refresh-1" not in listed


def test_expired_access_token_refreshes_once_on_real_token_endpoint(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    definition = definition_from_provider("suno", {"url": url})
    repository.write_definition(definition)
    _seed_tokens("suno", url, expires_at=time.time() - 10)
    refreshed = _run(runtime, "refresh", {"name": "suno"})
    assert refreshed["ok"] and refreshed["toolCount"] == 1
    assert loopback.token_posts == 1
    called = _run(runtime, "call", {"name": "suno", "tool": "echo", "arguments": {"text": "hi"}})
    assert called["status"] == "ok"
    assert loopback.token_posts == 1


def test_restart_then_refresh_uses_stored_token_endpoint(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() - 10)
    runtime.stop()
    again = McpRuntime()
    try:
        refreshed = _run(again, "refresh", {"name": "suno"})
        assert refreshed["ok"]
        assert loopback.token_posts == 1
    finally:
        again.stop()


def test_rejected_refresh_deletes_tokens_and_call_is_auth_required(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() - 10)
    loopback.refresh_status = 400
    refreshed = _run(runtime, "refresh", {"name": "suno"})
    assert refreshed.get("auth") == "needs_login"
    assert "tokens" not in oauth.read_state("suno", url)
    called = _run(runtime, "call", {"name": "suno", "tool": "echo", "arguments": {"text": "hi"}})
    assert called["error"]["type"] == "auth_required"
    assert "vibe mcp suno --login" in called["error"]["hint"]


def test_call_time_resource_401_is_auth_required_without_replay(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() + 3600)
    assert _run(runtime, "refresh", {"name": "suno"})["ok"]
    before = loopback.mcp_posts
    loopback.resource_status = 401
    called = _run(runtime, "call", {"name": "suno", "tool": "echo", "arguments": {"text": "hi"}})
    assert called["error"]["type"] == "auth_required"
    assert loopback.mcp_posts == before + 1
    assert repository.list_packages()[0]["auth"] == "needs_login"


def test_concurrent_call_time_auth_required_keeps_valid_package(runtime, loopback, monkeypatch):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() + 3600)
    assert _run(runtime, "refresh", {"name": "suno"})["ok"]
    loopback.resource_status = 401
    original = repository.publish
    needs_login = []

    def counted(*args, **kwargs):
        revision = original(*args, **kwargs)
        if (kwargs.get("manifest") or {}).get("auth") == "needs_login":
            needs_login.append(revision)
        return revision

    monkeypatch.setattr(repository, "publish", counted)

    async def many():
        results = await asyncio.gather(*[
            runtime.submit("call", {"name": "suno", "tool": "echo", "arguments": {"text": "hi"}, "sessionId": f"s{i}"})
            for i in range(8)
        ])
        return [json.loads(body) for _, body in results]

    bodies = asyncio.run(many())
    assert all(body["error"]["type"] == "auth_required" for body in bodies)
    assert len(needs_login) == 1
    published = repository.read_published("suno")
    assert published is not None
    assert published["manifest"]["auth"] == "needs_login"
    assert repository.list_packages()[0]["auth"] == "needs_login"


def test_concurrent_calls_share_one_token_refresh(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() + 3600)
    assert _run(runtime, "refresh", {"name": "suno"})["ok"]
    _seed_tokens("suno", url, expires_at=time.time() - 10, access=loopback.access, refresh=loopback.refresh)
    provider = asyncio.run(runtime._oauth_provider(definition_from_provider("suno", {"url": url})))
    provider._initialized = False
    loopback.token_posts = 0

    async def many():
        await asyncio.gather(*(
            runtime.submit("call", {"name": "suno", "tool": "echo", "arguments": {"text": str(i)}})
            for i in range(4)
        ))

    asyncio.run(many())
    assert loopback.token_posts == 1


def test_login_drives_grant_and_callback_redirects_to_skills(runtime, loopback):
    _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    started = _run(runtime, "oauth_start", {"name": "suno", "origin": "http://127.0.0.1:5123", "port": 5123})
    assert started["ok"] and started["mode"] == "direct"
    parsed = urlparse(started["authorizeUrl"])
    query = parse_qs(parsed.query)
    state = query["state"][0]
    done = _run(runtime, "oauth_callback", {"state": state, "code": "code-1", "iss": loopback.origin})
    assert done["ok"]
    assert done["redirect"] == "http://127.0.0.1:5123/skills?mcp=suno&oauth=success"
    listed = repository.list_packages()[0]
    assert listed["auth"] == "ok" and listed["toolCount"] == 1


def test_new_login_rejects_old_callback_state(runtime, loopback):
    _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    first = _run(runtime, "oauth_start", {"name": "suno", "origin": "http://127.0.0.1:5123"})
    old_state = parse_qs(urlparse(first["authorizeUrl"]).query)["state"][0]
    second = _run(runtime, "oauth_start", {"name": "suno", "origin": "http://127.0.0.1:5123"})
    new_state = parse_qs(urlparse(second["authorizeUrl"]).query)["state"][0]
    rejected = _run(runtime, "oauth_callback", {"state": old_state, "code": "late"})
    assert rejected["ok"] is False
    assert rejected["error"]["type"] == "invalid_callback"
    accepted = _run(runtime, "oauth_callback", {"state": new_state, "code": "code-2", "iss": loopback.origin})
    assert accepted["ok"]


def test_overlapping_oauth_start_keeps_only_latest_grant(runtime, loopback):
    _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    payload = {"name": "suno", "origin": "http://127.0.0.1:5123", "port": 5123}

    async def both():
        first, second = await asyncio.wait_for(
            asyncio.gather(
                runtime.submit("oauth_start", payload),
                runtime.submit("oauth_start", payload),
            ),
            5,
        )
        return json.loads(first[1]), json.loads(second[1])

    first, second = asyncio.run(both())
    assert first["ok"] and second["ok"]
    first_state = parse_qs(urlparse(first["authorizeUrl"]).query)["state"][0]
    second_state = parse_qs(urlparse(second["authorizeUrl"]).query)["state"][0]
    pending = list(runtime._pending_logins)
    assert len(pending) == 1
    winner = pending[0]
    loser = first_state if winner == second_state else second_state
    rejected = _run(runtime, "oauth_callback", {"state": loser, "code": "late"})
    assert rejected["ok"] is False
    assert rejected["error"]["type"] == "invalid_callback"
    accepted = _run(runtime, "oauth_callback", {"state": winner, "code": "code-2", "iss": loopback.origin})
    assert accepted["ok"]


def test_login_completion_serializes_publish_with_refresh(runtime, loopback, monkeypatch):
    _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    started = _run(runtime, "oauth_start", {"name": "suno", "origin": "http://127.0.0.1:5123", "port": 5123})
    state = parse_qs(urlparse(started["authorizeUrl"]).query)["state"][0]
    original = repository.publish
    concurrent = 0
    peak = 0

    def counted(*args, **kwargs):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        try:
            return original(*args, **kwargs)
        finally:
            concurrent -= 1

    monkeypatch.setattr(repository, "publish", counted)

    async def both():
        return await asyncio.gather(
            runtime.submit("oauth_callback", {"state": state, "code": "code-1", "iss": loopback.origin}),
            runtime.submit("refresh", {"name": "suno"}),
        )

    asyncio.run(both())
    assert peak == 1
    published = repository.read_published("suno")
    assert published is not None
    current = repository.package_dir("suno") / "revisions"
    survivors = [path for path in current.iterdir() if path.is_dir()]
    assert len(survivors) == 1
    assert survivors[0].name == published["revision"]


def test_logout_republishes_needs_login_without_tokens(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() + 3600)
    assert _run(runtime, "refresh", {"name": "suno"})["ok"]
    logged_out = _run(runtime, "oauth_logout", {"name": "suno"})
    assert logged_out["ok"]
    listed = repository.list_packages()[0]
    assert listed["auth"] == "needs_login"
    assert listed["toolCount"] == 0
    assert not oauth.oauth_path("suno").exists()
    dump = json.dumps(listed)
    assert "access-" not in dump and "refresh-" not in dump


def test_stdio_logout_is_rejected(runtime, tmp_path):
    definition = definition_from_provider("echo", {"command": sys.executable, "args": ["-c", "pass"]})
    repository.write_definition(definition)
    logged_out = _run(runtime, "oauth_logout", {"name": "echo"})
    assert logged_out["ok"] is False
    assert logged_out["error"]["type"] == "usage"


def test_authorization_header_logout_is_rejected(runtime, loopback):
    added = _run(runtime, "add", {"servers": [{"name": "suno", "server": {
        "url": f"{loopback.origin}/mcp",
        "headers": {"Authorization": f"Bearer {loopback.access}"},
    }}]})
    assert added["ok"] and added["added"][0]["toolCount"] == 1
    before = repository.list_packages()[0]
    assert before.get("auth") != "needs_login"
    logged_out = _run(runtime, "oauth_logout", {"name": "suno"})
    assert logged_out["ok"] is False
    assert logged_out["error"]["type"] == "usage"
    after = repository.list_packages()[0]
    assert after.get("auth") != "needs_login"
    assert after["toolCount"] == 1


def test_remove_cancels_pending_login(runtime, loopback):
    _run(runtime, "add", {"servers": [{"name": "suno", "server": {"url": f"{loopback.origin}/mcp"}}]})
    started = _run(runtime, "oauth_start", {"name": "suno", "origin": "http://127.0.0.1:5123", "port": 5123})
    state = parse_qs(urlparse(started["authorizeUrl"]).query)["state"][0]
    removed = _run(runtime, "remove", {"name": "suno"})
    assert removed["ok"]
    rejected = _run(runtime, "oauth_callback", {"state": state, "code": "late"})
    assert rejected["ok"] is False
    assert rejected["error"]["type"] == "invalid_callback"


def test_tool_error_containing_auth_stays_tool_error(runtime, loopback):
    url = f"{loopback.origin}/mcp"
    repository.write_definition(definition_from_provider("suno", {"url": url}))
    _seed_tokens("suno", url, expires_at=time.time() + 3600)
    assert _run(runtime, "refresh", {"name": "suno"})["ok"]
    loopback.tool_error = "auth failed for user"
    called = _run(runtime, "call", {"name": "suno", "tool": "echo", "arguments": {"text": "hi"}})
    assert called["error"]["type"] == "tool_error"
