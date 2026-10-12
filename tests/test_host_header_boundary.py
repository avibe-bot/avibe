import os
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from starlette.requests import Request

from vibe.ui_compat import CompatRequest
from vibe.ui_server import app


@pytest.mark.parametrize(
    "host",
    ["lan.example/assets/x?", "lan.example/assets/x#", "[::::]", "[1:2:3:4:5:6:7:8:9]"],
)
def test_invalid_host_websocket_closes_before_route(monkeypatch, tmp_path, host):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with app.test_client().websocket_connect("/api/events", headers={"Host": host}):
            pytest.fail("invalid Host was accepted")
    assert exc_info.value.code == 1008


def test_compat_request_path_uses_routed_scope_path():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/config",
        "raw_path": b"/api/config",
        "query_string": b"",
        "headers": [(b"host", b"lan.example/assets/x?")],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    assert CompatRequest(Request(scope)).path == "/api/config"
