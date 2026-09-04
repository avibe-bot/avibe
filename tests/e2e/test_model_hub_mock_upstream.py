"""Contract tests for the standalone Model Hub mock upstream."""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from tests.e2e.drivers.mock_llm_upstream import (
    MockLLMUpstream,
    OPENCODE_V4_S3_PASSTHROUGH_FIXTURES,
    OPENCODE_V4_S4_VARIANT_FIXTURES,
)


pytestmark = pytest.mark.e2e_model_hub

PROTOCOL_CASES = {
    "anthropic": {
        "path": "/v1/messages",
        "body": {
            "model": "mock-model",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}],
        },
        "response_key": "type",
        "response_value": "message",
        "terminal": "message_stop",
        "invalid_param": None,
    },
    "openai_responses": {
        "path": "/v1/responses",
        "body": {"model": "mock-model", "input": "hello"},
        "response_key": "object",
        "response_value": "response",
        "terminal": "response.completed",
        "invalid_param": "input",
    },
    "openai_chat": {
        "path": "/v1/chat/completions",
        "body": {
            "model": "mock-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
        "response_key": "object",
        "response_value": "chat.completion",
        "terminal": "[DONE]",
        "invalid_param": "messages",
    },
}

_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 2,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    request_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = Request(
        f"{base_url}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        response = _NO_PROXY_OPENER.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        return response.status, dict(response.headers), response.read()


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    status, response_headers, raw = _request(
        base_url,
        path,
        method=method,
        body=body,
        headers=headers,
    )
    return status, response_headers, json.loads(raw)


def _configure(base_url: str, **config: object) -> dict[str, Any]:
    status, _, payload = _json_request(
        base_url, "/__control/config", method="POST", body=config
    )
    assert status == 200
    assert payload["ok"] is True
    return payload["config"]


def _top_level_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        "added": {key: after[key] for key in after.keys() - before.keys()},
        "removed": sorted(before.keys() - after.keys()),
        "changed": {
            key: [before[key], after[key]]
            for key in before.keys() & after.keys()
            if before[key] != after[key]
        },
    }


@pytest.mark.parametrize(
    "fixture",
    OPENCODE_V4_S3_PASSTHROUGH_FIXTURES.values(),
    ids=OPENCODE_V4_S3_PASSTHROUGH_FIXTURES,
)
def test_opencode_v4_s3_engine_diffs_are_frozen_against_mock_capture(
    mock_llm_upstream,
    fixture: dict[str, Any],
) -> None:
    _configure(mock_llm_upstream.url, protocol=fixture["protocol"])
    path = f"/v1/{'messages' if fixture['protocol'] == 'anthropic' else 'responses'}"

    status, _, _ = _json_request(
        mock_llm_upstream.url,
        path,
        method="POST",
        body=fixture["upstream_body"],
    )

    assert status == 200
    [captured] = mock_llm_upstream.requests()
    assert captured["body"] == fixture["upstream_body"]
    assert _top_level_diff(
        fixture["frontend_body"],
        captured["body"],
    ) == fixture["expected_top_level_diff"]


@pytest.mark.parametrize(
    "fixture",
    OPENCODE_V4_S4_VARIANT_FIXTURES.values(),
    ids=OPENCODE_V4_S4_VARIANT_FIXTURES,
)
def test_opencode_v4_s4_variant_shapes_are_frozen_against_mock_capture(
    mock_llm_upstream,
    fixture: dict[str, Any],
) -> None:
    protocol = fixture["protocol"]
    _configure(mock_llm_upstream.url, protocol=protocol)
    body = {
        "model": "stored-hop-model",
        **(
            {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 128}
            if protocol == "anthropic"
            else {"input": "hello"}
        ),
        **fixture["upstream_fragment"],
    }
    path = f"/v1/{'messages' if protocol == 'anthropic' else 'responses'}"

    status, _, _ = _json_request(
        mock_llm_upstream.url,
        path,
        method="POST",
        body=body,
    )

    assert status == 200
    [captured] = mock_llm_upstream.requests()
    assert fixture["variant"] in (
        {"reasoningEffort": "high"},
        {"effort": "high"},
    )
    for key, value in fixture["upstream_fragment"].items():
        assert captured["body"][key] == value


@pytest.mark.parametrize("protocol", PROTOCOL_CASES)
def test_mock_upstream_inventory_and_buffered_protocol_shapes(
    mock_llm_upstream, protocol: str
) -> None:
    """Mock contract: all three protocol surfaces are shape-distinct."""

    case = PROTOCOL_CASES[protocol]
    config = _configure(
        mock_llm_upstream.url,
        protocol=protocol,
        models=[
            {
                "id": "relay/model",
                "display_name": "Relay Model",
                "context_length": 42_000,
                "pricing": {"input": "0.01", "output": "0.02"},
            }
        ],
    )
    assert config["models_endpoint"] == "ok"

    status, _, inventory = _json_request(
        mock_llm_upstream.url, "/v1/models"
    )
    assert status == 200
    assert inventory["object"] == "list"
    assert inventory["data"][0]["id"] == "relay/model"
    assert inventory["data"][0]["context_length"] == 42_000
    assert inventory["data"][0]["pricing"]["input"] == "0.01"
    if protocol == "anthropic":
        assert inventory["data"][0]["display_name"] == "Relay Model"
        assert inventory["data"][0]["type"] == "model"
    else:
        assert "display_name" not in inventory["data"][0]
        assert inventory["data"][0]["object"] == "model"

    status, _, payload = _json_request(
        mock_llm_upstream.url,
        case["path"],
        method="POST",
        body={**case["body"], "model": "relay/model"},
    )
    assert status == 200
    assert payload[case["response_key"]] == case["response_value"]
    assert payload["model"] == "relay/model"
    assert payload["usage"]


@pytest.mark.parametrize("protocol", PROTOCOL_CASES)
def test_mock_upstream_invalid_probe_evidence_is_family_distinctive(
    mock_llm_upstream, protocol: str
) -> None:
    """Mock contract: invalid probes expose protocol-specific evidence."""

    case = PROTOCOL_CASES[protocol]
    _configure(mock_llm_upstream.url, protocol=protocol)
    status, _, payload = _json_request(
        mock_llm_upstream.url,
        case["path"],
        method="POST",
        body={},
    )
    assert status == 400
    error = payload["error"]
    if protocol == "anthropic":
        assert payload["type"] == "error"
        assert "param" not in error
    else:
        assert error["param"] == case["invalid_param"]


@pytest.mark.parametrize("protocol", PROTOCOL_CASES)
@pytest.mark.parametrize(
    ("stream_behavior", "terminal_present"),
    [("healthy", True), ("interrupt_after_first_output", False)],
)
def test_mock_upstream_sse_healthy_and_interrupted_streams(
    mock_llm_upstream,
    protocol: str,
    stream_behavior: str,
    terminal_present: bool,
) -> None:
    """Mock contract: interruption follows the first model output."""

    case = PROTOCOL_CASES[protocol]
    _configure(
        mock_llm_upstream.url,
        protocol=protocol,
        stream=stream_behavior,
    )
    status, headers, raw = _request(
        mock_llm_upstream.url,
        case["path"],
        method="POST",
        body={**case["body"], "stream": True},
    )
    text = raw.decode("utf-8")
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert "mock response" in text
    assert (case["terminal"] in text) is terminal_present


@pytest.mark.parametrize(
    ("auth", "status"),
    [
        ("401", 401),
        ("403_banned", 403),
        ("402", 402),
        ("429", 429),
        ("quota_message", 429),
        ("5xx", 503),
    ],
)
def test_mock_upstream_auth_modes_precede_every_protocol_surface(
    mock_llm_upstream, auth: str, status: int
) -> None:
    """Mock contract: auth is evaluated before discovery or inference."""

    _configure(
        mock_llm_upstream.url,
        auth=auth,
        protocol="openai_chat",
        models_endpoint="http_500",
    )
    models_status, _, models_payload = _json_request(
        mock_llm_upstream.url, "/v1/models"
    )
    inference_status, _, inference_payload = _json_request(
        mock_llm_upstream.url,
        "/v1/responses",
        method="POST",
        body={},
    )
    assert models_status == status
    assert inference_status == status
    assert models_payload["error"]["type"] != "not_found_error"
    assert inference_payload["error"]["type"] != "not_found_error"


@pytest.mark.parametrize(
    ("behavior", "status"),
    [("http_404", 404), ("http_500", 500)],
)
def test_mock_upstream_models_endpoint_http_failures_are_isolated(
    mock_llm_upstream, behavior: str, status: int
) -> None:
    """Mock contract amendment: discovery can fail after protocol proof."""

    case = PROTOCOL_CASES["openai_chat"]
    _configure(
        mock_llm_upstream.url,
        auth="ok",
        protocol="openai_chat",
        models_endpoint=behavior,
    )
    models_status, _, _ = _json_request(
        mock_llm_upstream.url, "/v1/models"
    )
    inference_status, _, inference = _json_request(
        mock_llm_upstream.url,
        case["path"],
        method="POST",
        body=case["body"],
    )
    assert models_status == status
    assert inference_status == 200
    assert inference["object"] == "chat.completion"


def test_mock_upstream_models_endpoint_malformed_json_is_isolated(
    mock_llm_upstream,
) -> None:
    """Mock contract amendment: malformed discovery is independently set."""

    _configure(mock_llm_upstream.url, models_endpoint="malformed_json")
    status, headers, raw = _request(
        mock_llm_upstream.url, "/v1/models"
    )
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)

    status, _, payload = _json_request(
        mock_llm_upstream.url,
        "/v1/chat/completions",
        method="POST",
        body=PROTOCOL_CASES["openai_chat"]["body"],
    )
    assert status == 200
    assert payload["object"] == "chat.completion"


def test_mock_upstream_models_endpoint_timeout_is_isolated(
    mock_llm_upstream,
) -> None:
    """Mock contract amendment: discovery can time out independently."""

    _configure(mock_llm_upstream.url, models_endpoint="timeout")
    with pytest.raises((TimeoutError, URLError)):
        _request(
            mock_llm_upstream.url,
            "/v1/models",
            timeout=0.05,
        )
    status, _, payload = _json_request(
        mock_llm_upstream.url,
        "/v1/chat/completions",
        method="POST",
        body=PROTOCOL_CASES["openai_chat"]["body"],
    )
    assert status == 200
    assert payload["object"] == "chat.completion"


def test_mock_upstream_timeout_teardown_joins_handlers_and_closes_socket(
) -> None:
    """Mock lifecycle: timeout handlers and accepted sockets end at stop."""

    upstream = MockLLMUpstream().start()
    parsed = urlsplit(upstream.url)
    assert parsed.hostname is not None and parsed.port is not None
    address = (parsed.hostname, parsed.port)
    handler_threads = ()
    try:
        _configure(upstream.url, models_endpoint="timeout")
        with pytest.raises((TimeoutError, URLError)):
            _request(upstream.url, "/v1/models", timeout=0.05)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            handler_threads = upstream.active_handler_threads()
            if handler_threads:
                break
            time.sleep(0.01)
        assert handler_threads
    finally:
        upstream.stop()

    assert all(not thread.is_alive() for thread in handler_threads)
    assert upstream.active_handler_threads() == ()
    with pytest.raises(OSError):
        socket.create_connection(address, timeout=0.2)


def test_mock_upstream_empty_inventory_is_success_not_discovery_failure(
    mock_llm_upstream,
) -> None:
    """Mock contract amendment: an empty successful inventory stays 200."""

    _configure(
        mock_llm_upstream.url,
        auth="ok",
        models_endpoint="ok",
        models=[],
    )
    status, _, payload = _json_request(
        mock_llm_upstream.url, "/v1/models"
    )
    assert status == 200
    assert payload["data"] == []


def test_mock_upstream_control_validation_capture_envelope_and_reset(
    mock_llm_upstream,
) -> None:
    """Mock contract: control config is strict and capture is enveloped."""

    for invalid in (
        {"unknown": True},
        {"auth": "invalid"},
        {"stream": "invalid"},
        {"protocol": "invalid"},
        {"models_endpoint": "invalid"},
        {"models": [""]},
    ):
        status, _, payload = _json_request(
            mock_llm_upstream.url,
            "/__control/config",
            method="POST",
            body=invalid,
        )
        assert status == 400
        assert payload["ok"] is False

    headers = {
        "Authorization": "Bearer test-secret",
        "User-Agent": "model-hub-e2e",
        "X-Uncaptured": "ignored",
    }
    status, _, _ = _json_request(
        mock_llm_upstream.url,
        "/v1/chat/completions",
        method="POST",
        body=PROTOCOL_CASES["openai_chat"]["body"],
        headers=headers,
    )
    assert status == 200
    status, _, captured = _json_request(
        mock_llm_upstream.url, "/__control/requests"
    )
    assert status == 200
    assert set(captured) == {"requests"}
    assert len(captured["requests"]) == 1
    record = captured["requests"][0]
    assert record == {
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": {
            "authorization": "Bearer test-secret",
            "content-type": "application/json",
            "user-agent": "model-hub-e2e",
        },
        "body": PROTOCOL_CASES["openai_chat"]["body"],
    }

    status, _, payload = _json_request(
        mock_llm_upstream.url,
        "/__control/requests",
        method="DELETE",
    )
    assert status == 200
    assert payload == {"ok": True}
    _, _, captured = _json_request(
        mock_llm_upstream.url, "/__control/requests"
    )
    assert captured == {"requests": []}


def test_mock_upstream_contract_client_ignores_inherited_proxy(
    monkeypatch,
    mock_llm_upstream,
) -> None:
    """Mock contract: loopback control and data calls never use host proxies."""

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)

    _configure(mock_llm_upstream.url, protocol="openai_chat")
    status, _, payload = _json_request(
        mock_llm_upstream.url,
        "/v1/chat/completions",
        method="POST",
        body=PROTOCOL_CASES["openai_chat"]["body"],
    )
    assert status == 200
    assert payload["object"] == "chat.completion"

    status, _, captured = _json_request(
        mock_llm_upstream.url, "/__control/requests"
    )
    assert status == 200
    assert [request["path"] for request in captured["requests"]] == [
        "/v1/chat/completions"
    ]


def test_mock_upstream_starts_standalone_with_bare_python() -> None:
    """Mock contract: the CLI imports no Avibe or pytest dependency."""

    driver = Path(__file__).parent / "drivers" / "mock_llm_upstream.py"
    process = subprocess.Popen(
        [sys.executable, str(driver), "--host", "127.0.0.1", "--port", "0"],
        cwd=driver.parents[3],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        first_line = process.stdout.readline().strip()
        match = re.search(r"(http://127\.0\.0\.1:\d+)$", first_line)
        assert match, first_line
        status, _, payload = _json_request(match.group(1), "/v1/models")
        assert status == 200
        assert payload["data"][0]["id"] == "mock-model"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
