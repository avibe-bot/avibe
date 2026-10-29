"""``/api/mcp*`` against the real owner runtime and stdio fixture."""

import json
import sys
from pathlib import Path

import pytest

from core.mcp_hub import repository
from core.mcp_hub.runtime import McpRuntime
from tests.ui_server_test_helpers import csrf_headers, save_config
from vibe import internal_client
from vibe.authorization import http_authorization_policy
from vibe.ui_server import app

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
PASTE = json.dumps({"mcpServers": {"echo": {
    "command": sys.executable, "args": [FIXTURE], "env": {"ECHO_TOKEN": "s3cret-value"}}}})


@pytest.fixture
def owner(monkeypatch, tmp_path):
    config = save_config(tmp_path)
    config.runtime.default_cwd = str(tmp_path)
    config.save()
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


def test_all_failed_batch_returns_per_server_reasons_and_identical_readd_is_unchanged(owner):
    client = app.test_client()
    headers = csrf_headers(client)
    first = client.post("/api/mcp", json={"text": PASTE}, headers=headers)
    assert first.status_code == 200 and first.get_json()["ok"]

    again = client.post("/api/mcp", json={"text": PASTE}, headers=headers)
    body = again.get_json()
    assert again.status_code == 200
    assert body["ok"] is True
    assert body["unchanged"][0]["name"] == "echo"
    assert body["failed"] == []
    assert body["added"] == []

    batch = {
        "mcpServers": {
            "echo": {"command": sys.executable, "args": [FIXTURE], "env": {"ECHO_TOKEN": "s3cret-value"}},
            "github": {"command": "/nonexistent/mcp-github"},
            "other": {"command": "/nonexistent/mcp-other"},
        }
    }
    failed = client.post("/api/mcp", json={"text": json.dumps(batch)}, headers=headers)
    payload = failed.get_json()
    assert failed.status_code == 200
    assert payload["ok"] is True  # echo is unchanged
    names = {item["name"]: item for item in payload["failed"]}
    assert "github" in names and names["github"]["error"]
    assert "other" in names
    assert payload["unchanged"][0]["name"] == "echo"
    assert payload.get("error", {}).get("message") or names["github"]["error"]

    empty_fail = {
        "mcpServers": {
            "github": {"command": "/nonexistent/mcp-github"},
            "other": {"command": "/nonexistent/mcp-other"},
        }
    }
    none = client.post("/api/mcp", json={"text": json.dumps(empty_fail)}, headers=headers)
    none_body = none.get_json()
    assert none.status_code == 200
    assert none_body["ok"] is False
    assert {item["name"] for item in none_body["failed"]} == {"github", "other"}
    assert none_body["error"]["message"]
    assert "Unexpected MCP Hub response" not in json.dumps(none_body)


def test_malformed_json_add_returns_top_level_error_without_batch_arrays(owner):
    client = app.test_client()
    headers = csrf_headers(client)
    response = client.post("/api/mcp", json={"text": "{not json"}, headers=headers)
    body = response.get_json()
    assert response.status_code == 400
    assert body["ok"] is False
    assert body.get("error", {}).get("message")
    assert "added" not in body
    assert "failed" not in body


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
