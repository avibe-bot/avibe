"""Deterministic, stdlib-only LLM upstream for Model Hub E2E tests.

The server has no Avibe or pytest imports, so another test lane can start it
with a bare Python 3.11+ interpreter. Pytest owns only the lifecycle fixture in
tests/e2e/conftest.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


AUTH_BEHAVIORS = frozenset(
    {"ok", "401", "403_banned", "402", "429", "quota_message", "5xx"}
)
STREAM_BEHAVIORS = frozenset({"healthy", "interrupt_after_first_output"})
PROTOCOLS = frozenset({"anthropic", "openai_responses", "openai_chat"})
MODELS_ENDPOINT_BEHAVIORS = frozenset(
    {"ok", "http_404", "http_500", "timeout", "malformed_json"}
)
PROTOCOL_PATHS = {
    "anthropic": "/v1/messages",
    "openai_responses": "/v1/responses",
    "openai_chat": "/v1/chat/completions",
}
CAPTURED_HEADER_NAMES = (
    "accept",
    "anthropic-beta",
    "anthropic-version",
    "authorization",
    "content-type",
    "openai-beta",
    "user-agent",
    "x-api-key",
)
DEFAULT_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "mock-model",
        "display_name": "Mock Model",
        "context_length": 128_000,
        "pricing": {"input": "0.001", "output": "0.002"},
    },
)
MAX_CAPTURED_REQUESTS = 200
MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
FIXED_CREATED_AT = 1_700_000_000
MODELS_ENDPOINT_TIMEOUT_SECONDS = 30


class ConfigurationError(ValueError):
    """The control plane received an invalid behavior config."""


class MockUpstreamState:
    """Thread-safe behavior and request-capture owner."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {
            "auth": "ok",
            "stream": "healthy",
            "models": [copy.deepcopy(model) for model in DEFAULT_MODELS],
            "protocol": "openai_chat",
            "models_endpoint": "ok",
        }
        self._requests: deque[dict[str, Any]] = deque(
            maxlen=MAX_CAPTURED_REQUESTS
        )

    def config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._config)

    def configure(self, update: Mapping[str, Any]) -> dict[str, Any]:
        unknown = set(update) - {
            "auth",
            "stream",
            "models",
            "protocol",
            "models_endpoint",
        }
        if unknown:
            raise ConfigurationError(
                f"unknown behavior fields: {', '.join(sorted(unknown))}"
            )
        candidate = self.config()
        candidate.update(copy.deepcopy(dict(update)))
        if candidate["auth"] not in AUTH_BEHAVIORS:
            raise ConfigurationError("unsupported auth behavior")
        if candidate["stream"] not in STREAM_BEHAVIORS:
            raise ConfigurationError("unsupported stream behavior")
        if candidate["protocol"] not in PROTOCOLS:
            raise ConfigurationError("unsupported protocol")
        if candidate["models_endpoint"] not in MODELS_ENDPOINT_BEHAVIORS:
            raise ConfigurationError("unsupported models endpoint behavior")
        candidate["models"] = _validated_models(candidate["models"])
        with self._lock:
            self._config = candidate
            return copy.deepcopy(self._config)

    def capture(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._requests.append(copy.deepcopy(dict(record)))

    def requests(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(list(self._requests))

    def reset_requests(self) -> None:
        with self._lock:
            self._requests.clear()


def _validated_models(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigurationError("models must be an array")
    models: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            model = {"id": item}
        elif isinstance(item, dict):
            model = copy.deepcopy(item)
        else:
            raise ConfigurationError(
                "models entries must be strings or objects"
            )
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            raise ConfigurationError("every model must have a non-empty id")
        model["id"] = model_id.strip()
        models.append(model)
    return models


class MockLLMUpstreamHandler(BaseHTTPRequestHandler):
    """Serve the frozen Model Hub mock-upstream contract."""

    protocol_version = "HTTP/1.1"
    server_version = "AvibeMockLLM/1"

    @property
    def state(self) -> MockUpstreamState:
        return self.server.mock_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/__control/requests":
            self._write_json(
                HTTPStatus.OK, {"requests": self.state.requests()}
            )
            return
        if path == "/v1/models":
            self._capture(path, None)
            config = self.state.config()
            if config["auth"] != "ok":
                self._write_behavior_error(config)
                return
            if config["models_endpoint"] != "ok":
                self._write_models_endpoint_failure(
                    config["models_endpoint"]
                )
                return
            self._write_json(
                HTTPStatus.OK,
                _models_payload(config["protocol"], config["models"]),
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, _not_found_payload())

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if path == "/__control/requests":
            self.state.reset_requests()
            self._write_json(HTTPStatus.OK, {"ok": True})
            return
        self._write_json(HTTPStatus.NOT_FOUND, _not_found_payload())

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "message": str(exc),
                    }
                },
            )
            return

        if path == "/__control/config":
            self._configure(body)
            return
        if path not in PROTOCOL_PATHS.values():
            self._write_json(HTTPStatus.NOT_FOUND, _not_found_payload())
            return

        self._capture(path, body)
        config = self.state.config()
        protocol = config["protocol"]
        if config["auth"] != "ok":
            self._write_behavior_error(config)
            return
        if path != PROTOCOL_PATHS[protocol]:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                _invalid_probe_payload(protocol),
            )
            return
        if _is_observation_probe(protocol, body):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                _invalid_probe_payload(protocol),
            )
            return
        if isinstance(body, dict) and body.get("stream") is True:
            self._write_stream(protocol, body, config["stream"])
            return
        self._write_json(
            HTTPStatus.OK,
            _buffered_response(protocol, body),
        )

    def _configure(self, body: object) -> None:
        if not isinstance(body, dict):
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "config_must_be_an_object"},
            )
            return
        try:
            config = self.state.configure(body)
        except ConfigurationError as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": str(exc)},
            )
            return
        self._write_json(
            HTTPStatus.OK, {"ok": True, "config": config}
        )

    def _read_json_body(self) -> object:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise ValueError("invalid Content-Length") from None
        if length < 0 or length > MAX_REQUEST_BODY_BYTES:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("request body must be valid JSON") from None

    def _capture(self, path: str, body: object) -> None:
        headers = {
            name: self.headers[name]
            for name in CAPTURED_HEADER_NAMES
            if self.headers.get(name) is not None
        }
        self.state.capture(
            {
                "method": self.command,
                "path": path,
                "headers": headers,
                "body": body,
            }
        )

    def _write_behavior_error(
        self, config: Mapping[str, Any]
    ) -> None:
        status, error_type, message, retry_after = _behavior_error(
            config["auth"]
        )
        if config["protocol"] == "anthropic":
            payload = {
                "type": "error",
                "error": {"type": error_type, "message": message},
            }
        else:
            payload = {
                "error": {
                    "type": error_type,
                    "code": error_type,
                    "param": None,
                    "message": message,
                }
            }
        headers = (
            {"Retry-After": retry_after}
            if retry_after is not None
            else None
        )
        self._write_json(status, payload, extra_headers=headers)

    def _write_models_endpoint_failure(self, behavior: str) -> None:
        if behavior == "timeout":
            time.sleep(MODELS_ENDPOINT_TIMEOUT_SECONDS)
            return
        if behavior == "malformed_json":
            self._write_bytes(
                HTTPStatus.OK,
                b'{"object":"list","data":',
                content_type="application/json; charset=utf-8",
            )
            return
        status = (
            HTTPStatus.NOT_FOUND
            if behavior == "http_404"
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        self._write_json(status, _not_found_payload())

    def _write_stream(
        self,
        protocol: str,
        body: Mapping[str, Any],
        stream_behavior: str,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type", "text/event-stream; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        frames = _stream_frames(protocol, body)
        limit = len(frames)
        if stream_behavior == "interrupt_after_first_output":
            limit = 3 if protocol == "anthropic" else 2
        try:
            for frame in frames[:limit]:
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_json(
        self,
        status: int | HTTPStatus,
        payload: object,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self._write_bytes(
            status,
            encoded,
            content_type="application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    def _write_bytes(
        self,
        status: int | HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(
        self, _format: str, *_args: object
    ) -> None:
        return


class MockLLMUpstream:
    """Background-thread lifecycle wrapper used by pytest and local probes."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        self.host = host
        self.port = port
        self.state = MockUpstreamState()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("mock upstream is not running")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "MockLLMUpstream":
        if self._server is not None:
            return self
        server = _create_server(self.host, self.port, self.state)
        thread = threading.Thread(
            target=server.serve_forever,
            name="model-hub-mock-upstream",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def configure(self, **update: Any) -> dict[str, Any]:
        return self.state.configure(update)

    def requests(self) -> list[dict[str, Any]]:
        return self.state.requests()

    def reset_requests(self) -> None:
        self.state.reset_requests()

    def __enter__(self) -> "MockLLMUpstream":
        return self.start()

    def __exit__(self, *_exc_info: object) -> None:
        self.stop()


class _MockThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _create_server(
    host: str,
    port: int,
    state: MockUpstreamState | None = None,
) -> ThreadingHTTPServer:
    server = _MockThreadingHTTPServer(
        (host, port), MockLLMUpstreamHandler
    )
    server.mock_state = state or MockUpstreamState()  # type: ignore[attr-defined]
    return server


def _models_payload(
    protocol: str, models: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for raw_model in models:
        model = copy.deepcopy(dict(raw_model))
        if protocol == "anthropic":
            model.setdefault("display_name", model["id"])
            model.setdefault("type", "model")
        else:
            model.pop("display_name", None)
            model.setdefault("object", "model")
            model.setdefault("created", 0)
            model.setdefault("owned_by", "avibe-mock")
        data.append(model)
    return {"object": "list", "data": data, "has_more": False}


def _is_observation_probe(protocol: str, body: object) -> bool:
    if not isinstance(body, dict):
        return True
    if protocol == "anthropic":
        return (
            not isinstance(body.get("model"), str)
            or body.get("max_tokens") == 0
        )
    if protocol == "openai_responses":
        return "input" not in body
    return "messages" not in body


def _invalid_probe_payload(protocol: str) -> dict[str, Any]:
    if protocol == "anthropic":
        return {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "model is required",
            },
        }
    parameter = (
        "input" if protocol == "openai_responses" else "messages"
    )
    return {
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_parameter",
            "param": parameter,
            "message": f"{parameter} is required",
        }
    }


def _behavior_error(
    auth: str,
) -> tuple[int, str, str, str | None]:
    rows = {
        "401": (401, "authentication_error", "invalid API key", None),
        "403_banned": (403, "account_banned", "account banned", None),
        "402": (402, "billing_error", "payment required", None),
        "429": (429, "rate_limit_error", "rate limit exceeded", "120"),
        "quota_message": (
            429,
            "insufficient_quota",
            "insufficient quota",
            "300",
        ),
        "5xx": (503, "server_error", "upstream unavailable", "600"),
    }
    return rows[auth]


def _requested_model(body: object) -> str:
    if (
        isinstance(body, dict)
        and isinstance(body.get("model"), str)
    ):
        return body["model"]
    return "mock-model"


def _buffered_response(
    protocol: str, body: object
) -> dict[str, Any]:
    model = _requested_model(body)
    if protocol == "anthropic":
        return {
            "id": "msg_mock_001",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "mock response"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": 12,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 0,
                "output_tokens": 5,
            },
        }
    if protocol == "openai_responses":
        return {
            "id": "resp_mock_001",
            "object": "response",
            "created_at": FIXED_CREATED_AT,
            "status": "completed",
            "model": model,
            "output": [
                {
                    "id": "msg_mock_001",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "mock response",
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": {
                "input_tokens": 12,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens": 5,
                "total_tokens": 17,
            },
        }
    return {
        "id": "chatcmpl_mock_001",
        "object": "chat.completion",
        "created": FIXED_CREATED_AT,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "mock response",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens": 5,
            "total_tokens": 17,
        },
    }


def _sse(
    event_name: str | None, payload: object
) -> bytes:
    lines: list[str] = []
    if event_name is not None:
        lines.append(f"event: {event_name}")
    data = (
        payload
        if isinstance(payload, str)
        else json.dumps(payload, separators=(",", ":"))
    )
    lines.append(f"data: {data}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _stream_frames(
    protocol: str, body: Mapping[str, Any]
) -> list[bytes]:
    model = _requested_model(body)
    if protocol == "anthropic":
        return _anthropic_stream_frames(model)
    if protocol == "openai_responses":
        return _responses_stream_frames(model)
    return _chat_stream_frames(model)


def _anthropic_stream_frames(model: str) -> list[bytes]:
    return [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_mock_001",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "usage": {
                        "input_tokens": 12,
                        "cache_read_input_tokens": 3,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 0,
                    },
                },
            },
        ),
        _sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "text_delta",
                    "text": "mock response",
                },
            },
        ),
        _sse(
            "content_block_stop",
            {"type": "content_block_stop", "index": 0},
        ),
        _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 5},
            },
        ),
        _sse("message_stop", {"type": "message_stop"}),
    ]


def _responses_stream_frames(model: str) -> list[bytes]:
    return [
        _sse(
            "response.created",
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": {
                    "id": "resp_mock_001",
                    "object": "response",
                    "status": "in_progress",
                    "model": model,
                },
            },
        ),
        _sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "item_id": "msg_mock_001",
                "output_index": 0,
                "content_index": 0,
                "delta": "mock response",
            },
        ),
        _sse(
            "response.completed",
            {
                "type": "response.completed",
                "sequence_number": 2,
                "response": {
                    "id": "resp_mock_001",
                    "object": "response",
                    "status": "completed",
                    "model": model,
                    "usage": {
                        "input_tokens": 12,
                        "input_tokens_details": {
                            "cached_tokens": 3
                        },
                        "output_tokens": 5,
                        "total_tokens": 17,
                    },
                },
            },
        ),
    ]


def _chat_stream_frames(model: str) -> list[bytes]:
    common = {
        "id": "chatcmpl_mock_001",
        "object": "chat.completion.chunk",
        "created": FIXED_CREATED_AT,
        "model": model,
    }
    return [
        _sse(
            None,
            {
                **common,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            },
        ),
        _sse(
            None,
            {
                **common,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "mock response"},
                        "finish_reason": None,
                    }
                ],
            },
        ),
        _sse(
            None,
            {
                **common,
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "prompt_tokens_details": {
                        "cached_tokens": 3
                    },
                    "completion_tokens": 5,
                    "total_tokens": 17,
                },
            },
        ),
        _sse(None, "[DONE]"),
    ]


def _not_found_payload() -> dict[str, Any]:
    return {
        "error": {
            "type": "not_found_error",
            "code": "not_found_error",
            "message": "not found",
        }
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    server = _create_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(
        f"Mock LLM upstream listening at http://{host}:{port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
