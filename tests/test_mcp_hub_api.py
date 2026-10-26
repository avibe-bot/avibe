"""``/api/mcp*`` against the real owner runtime and stdio fixture (plan §5, §8).

Only the IPC hop is replaced: ``internal_client.mcp_request`` forwards to an
in-process ``McpRuntime`` (the CLI e2e covers the socket itself).
"""

import json
import sys
from pathlib import Path

import pytest

from core.mcp_hub import repository
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import csrf_headers
from vibe import internal_client
from vibe.authorization import http_authorization_policy
from vibe.ui_server import app

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
PASTE = json.dumps({"mcpServers": {"echo": {
    "command": sys.executable, "args": [FIXTURE], "env": {"ECHO_TOKEN": "s3cret-value"}}}})


@pytest.fixture
def owner(monkeypatch):
    runtime = McpRuntime()

    async def forward(operation, payload, **_kwargs):
        status, body = await runtime.submit(operation, payload)
        assert status == 200
        return json.loads(body)

    monkeypatch.setattr(internal_client, "mcp_request", forward)
    yield runtime
    runtime.stop()


def test_parse_save_generate_tools_and_state_through_the_api(owner):
    client = app.test_client()
    headers = csrf_headers(client)

    parsed = client.post("/api/mcp/parse", json={"text": PASTE}, headers=headers)
    assert parsed.status_code == 200
    assert parsed.get_json()["servers"][0]["transport"]["has_env"] == {"ECHO_TOKEN": True}
    assert "s3cret-value" not in parsed.text
    assert not repository.list_names()  # parse never saves

    assert client.post("/api/mcp/save", json={"text": PASTE}, headers=headers).get_json()["saved"] == ["echo"]
    assert client.post("/api/mcp/save", json={"text": PASTE}, headers=headers).status_code == 409
    generated = client.post("/api/mcp/echo/generate", headers=headers).get_json()
    assert generated["ok"] and generated["toolCount"] == 6

    owner.stop()  # tools are the published artifact; no server or owner needed
    tools = client.get("/api/mcp/echo/tools").get_json()
    assert tools["revision"] == generated["revision"]
    assert "echo" in [tool["name"] for tool in tools["tools"]]
    detail = client.get("/api/mcp/echo").get_json()
    assert detail["server"]["status"] == "generated" and detail["server"]["enabled"] is True
    assert "s3cret-value" not in json.dumps(detail)

    assert client.post("/api/mcp/echo/disable", headers=headers).get_json()["enabled"] is False
    call = {"name": "echo", "tool": "echo", "arguments": {"text": "x"}}
    import asyncio

    assert json.loads(asyncio.run(owner.submit("call", call))[1])["error"]["type"] == "disabled"
    assert client.post("/api/mcp/echo/enable", headers=headers).get_json()["enabled"] is True
    assert json.loads(asyncio.run(owner.submit("call", call))[1])["status"] == "ok"

    # The page submits the env map whole: blank keeps the stored value, omitted keys go.
    patched = client.patch("/api/mcp/echo", headers=headers, json={
        "description": "Echo", "server": {"command": sys.executable, "args": [FIXTURE], "env": {"ECHO_TOKEN": ""}}})
    assert patched.status_code == 200
    assert repository.read_definition("echo")["transport"]["env"] == {"ECHO_TOKEN": "s3cret-value"}
    assert client.get("/api/mcp/echo").get_json()["server"]["pendingChanges"] is True

    assert client.delete("/api/mcp/echo", headers=headers).get_json()["removed"] is True
    assert client.get("/api/mcp/echo").status_code == 404


def test_detail_returns_list_rows_that_404ed_before(owner, tmp_path):
    from core.mcp_hub import native

    client = app.test_client()
    headers = csrf_headers(client)
    assert client.post("/api/mcp/save", json={"text": PASTE}, headers=headers).status_code == 200
    assert client.post("/api/mcp/echo/generate", headers=headers).get_json()["ok"]
    native.remove("echo")
    listed = {item["name"]: item for item in client.get("/api/mcp").get_json()["servers"]}
    assert listed["echo"]["nativeDeleted"] is True
    detail = client.get("/api/mcp/echo")
    assert detail.status_code == 200
    body = detail.get_json()
    assert body["ok"] is True and body["server"]["nativeDeleted"] is True
    assert body.get("definition", {}).get("name") == "echo" or "definition" in body or body["server"]["name"] == "echo"


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [
        ("GET", "/api/mcp", "editor"),
        ("GET", "/api/mcp/echo", "editor"),
        ("GET", "/api/mcp/echo/tools", "editor"),
        ("POST", "/api/mcp/parse", "owner"),
        ("POST", "/api/mcp/save", "owner"),
        ("PATCH", "/api/mcp/echo", "owner"),
        ("POST", "/api/mcp/echo/generate", "owner"),
        ("POST", "/api/mcp/echo/enable", "owner"),
        ("DELETE", "/api/mcp/echo", "owner"),
    ],
)
def test_route_roles(method, path, role):
    assert http_authorization_policy(method, path).minimum_role == role


def test_replace_save_keeps_blank_secrets_and_drops_omitted_keys(owner):
    client = app.test_client()
    headers = csrf_headers(client)
    first = {"mcpServers": {"docs": {"url": "https://example.invalid/mcp",
                                     "headers": {"Authorization": "Bearer real", "X-Old": "o"}}}}
    assert client.post("/api/mcp/save", json={"text": json.dumps(first)}, headers=headers).status_code == 200
    again = {"mcpServers": {"docs": {"url": "https://example.invalid/mcp",
                                     "headers": {"Authorization": "", "X-New": "n"}}}}
    saved = client.post("/api/mcp/save", json={"text": json.dumps(again), "replace": True}, headers=headers)
    assert saved.status_code == 200
    assert repository.read_definition("docs")["transport"]["headers"] == {"Authorization": "Bearer real", "X-New": "n"}
