import asyncio

import pytest
from fastapi import WebSocketDisconnect
from starlette.requests import Request

from vibe.ui_compat import CompatRequest
from vibe.ui_server import _RejectInvalidHostMiddleware, _is_valid_host_header, app


@pytest.mark.parametrize(
    "host",
    [
        "lan.example/assets/x?",
        "lan.example/assets/x#",
        "[::::]",
        "[1:2:3:4:5:6:7:8:9]",
        "[fe80::1%x?y]",
        "[fe80::1%x#y]",
    ],
)
def test_invalid_host_websocket_closes_before_route(monkeypatch, tmp_path, host):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with app.test_client().websocket_connect("/api/events", headers={"Host": host}):
            pytest.fail("invalid Host was accepted")
    assert exc_info.value.code == 1008


@pytest.mark.parametrize(
    "host",
    ["lan.example:0", "[fe80::1%en0]", "[fe80::1%25en0]", "[v1.fe80::1]", "lan.example%20:443"],
)
def test_valid_host_authorities_remain_accepted(host):
    assert _is_valid_host_header(host)


@pytest.mark.parametrize(
    ("scope_type", "headers"),
    [
        ("http", [(b"host", b"lan.example:\xb2")]),
        ("websocket", [(b"host", b"[fe80::1%x?y]")]),
        ("websocket", [(b"host", b"[fe80::1%x#y]")]),
    ],
)
def test_raw_invalid_host_bytes_are_rejected_before_inner_app(scope_type, headers):
    sent = []
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def receive():
        raise AssertionError("invalid Host must not read the request")

    async def send(message):
        sent.append(message)

    asyncio.run(
        _RejectInvalidHostMiddleware(inner)(
            {"type": scope_type, "headers": headers}, receive, send
        )
    )
    assert not reached
    if scope_type == "http":
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 400
    else:
        assert sent == [{"type": "websocket.close", "code": 1008}]


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
