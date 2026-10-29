"""``/api/mcp*`` against the real owner runtime and stdio fixture."""

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


def test_add_registers_and_delete_removes_through_the_api(owner):
    client = app.test_client()
    headers = csrf_headers(client)

    added = client.post("/api/mcp", json={"text": PASTE}, headers=headers)
    body = added.get_json()
    assert added.status_code == 200 and body["ok"] and body["toolCount"] == 7
    assert "s3cret-value" not in added.text
    assert repository.read_avibe_definition("echo") is not None

    listed = client.get("/api/mcp").get_json()["servers"]
    assert listed[0]["name"] == "echo" and listed[0]["source"] == "avibe"

    owner.stop()
    detail = client.get("/api/mcp/echo").get_json()
    assert detail["server"]["name"] == "echo"
    assert "s3cret-value" not in json.dumps(detail)
    assert any(tool["name"] == "echo" for tool in detail.get("tools") or detail["server"].get("tools") or [])

    assert client.delete("/api/mcp/echo", headers=headers).get_json()["removed"] is True
    assert client.get("/api/mcp/echo").status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [
        ("GET", "/api/mcp", "editor"),
        ("GET", "/api/mcp/echo", "editor"),
        ("POST", "/api/mcp", "owner"),
        ("POST", "/api/mcp/discover", "owner"),
        ("POST", "/api/mcp/echo/refresh", "owner"),
        ("DELETE", "/api/mcp/echo", "owner"),
    ],
)
def test_route_roles(method, path, role):
    assert http_authorization_policy(method, path).minimum_role == role
