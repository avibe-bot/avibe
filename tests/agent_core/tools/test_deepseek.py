"""DeepSeek's standalone Search/Fetch tools: validation, shaping, and credential boundaries."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading

import pytest

import core.agent_core.tools.deepseek as deepseek
from core.agent_core.cancel import CancelToken
from core.agent_core.tools.base import ToolContext
from core.agent_core.tools.deepseek import DeepSeekFetchTool, DeepSeekSearchTool
from tests.agent_core.tools.conftest import result_text


def ctx(*, env=None) -> ToolContext:
    return ToolContext(
        session_id="ses_test",
        tool_call_id="toolu_1",
        call=None,
        cwd="/tmp",
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cancel=CancelToken(),
    )


def test_tools_are_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(deepseek.DEEPSEEK_TOOLS_ENV, raising=False)
    assert not deepseek.deepseek_tools_enabled()
    monkeypatch.setenv(deepseek.DEEPSEEK_TOOLS_ENV, "1")
    assert deepseek.deepseek_tools_enabled()


def test_specs_name_the_two_tools() -> None:
    assert DeepSeekSearchTool().spec.name == "deepseek_web_search"
    assert DeepSeekFetchTool().spec.name == "deepseek_fetch"
    assert DeepSeekSearchTool().spec.input_schema["required"] == ["query"]
    assert DeepSeekFetchTool().spec.input_schema["required"] == ["url"]


async def test_search_returns_numbered_results_with_urls(monkeypatch) -> None:
    async def post(path, payload, env):
        assert path == "/v1/tools/search"
        assert payload == {"query": "q", "count": 2, "need_content": False}
        return {"results": [{"title": "T", "url": "https://e", "summary": "M", "authority": 3}]}

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekSearchTool().execute({"query": "q", "count": 2}, ctx())

    assert not result.is_error
    assert "https://e" in result_text(result)
    assert "Summary: M" in result_text(result)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "query must be a string"),
        ({"query": "   "}, "query must not be empty"),
        ({"query": "x" * 129}, "query must be at most 128 characters"),
        ({"query": "x", "count": 0}, "count must be an integer between 1 and 50"),
        ({"query": "x", "count": 51}, "count must be an integer between 1 and 50"),
        ({"query": "x", "need_content": "yes"}, "need_content must be a boolean"),
    ],
)
async def test_search_argument_errors_are_results(arguments, message) -> None:
    result = await DeepSeekSearchTool().execute(arguments, ctx())
    assert result.is_error
    assert result_text(result) == message


async def test_fetch_returns_markdown(monkeypatch) -> None:
    async def post(path, payload, env):
        assert path == "/v1/tools/fetch"
        assert payload == {"url": "https://example.com/a"}
        return {"title": "T", "content": "body"}

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekFetchTool().execute({"url": "https://example.com/a"}, ctx())

    assert not result.is_error
    assert result_text(result) == "# T\n\nbody"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("", "url must not be empty"),
        ("ftp://example.com", "url must be an absolute HTTP or HTTPS URL with a host"),
        ("example.com", "url must be an absolute HTTP or HTTPS URL with a host"),
        ("https://user:pass@example.com", "url must not contain a username or password"),
    ],
)
async def test_fetch_argument_errors_are_results(url, message) -> None:
    result = await DeepSeekFetchTool().execute({"url": url}, ctx())
    assert result.is_error
    assert result_text(result) == message


async def test_search_uses_vault_when_no_key_is_in_the_turn(monkeypatch) -> None:
    calls = []

    async def vault(url, payload, env):
        calls.append((url, payload, env))
        return {"results": []}

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(deepseek, "_post_via_vault", vault)
    result = await DeepSeekSearchTool().execute({"query": "q"}, ctx())

    assert not result.is_error
    assert calls and calls[0][0] == "https://api.deepseek.com/v1/tools/search"


async def test_direct_mode_refuses_redirects_instead_of_forwarding_the_key() -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", "https://example.org/steal")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(deepseek._ToolError) as caught:
            await deepseek._post_direct(
                f"http://127.0.0.1:{server.server_port}/v1/tools/search",
                {"query": "q"},
                "test-key",
            )
        assert "Redirects are refused" in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_direct_response_is_read_with_a_bound(monkeypatch) -> None:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = b"x" * (100 * 1024)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(deepseek, "MAX_RESPONSE_BYTES", 1024)
    try:
        with pytest.raises(deepseek._ToolError) as caught:
            await deepseek._post_direct(
                f"http://127.0.0.1:{server.server_port}/v1/tools/search", {"query": "q"}, "test-key"
            )
        assert "size limit" in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_content_caps_are_applied(monkeypatch) -> None:
    async def post(path, payload, env):
        return {"results": [{"title": "T", "url": "https://e", "content": "c" * (deepseek.MAX_CONTENT_CHARS + 1)}]}

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekSearchTool().execute({"query": "q", "need_content": True}, ctx())

    assert "(truncated)" in result_text(result)


async def test_a_provider_error_that_echoes_the_key_is_redacted() -> None:
    body = b'{"error":{"message":"bad key sk-1234567890abcdef","code":"invalid_api_key"}}'
    message = deepseek._http_error_message(401, body, "sk-1234567890abcdef")
    assert "sk-1234567890abcdef" not in message
    assert "[redacted]" in message


async def test_search_output_has_one_total_budget(monkeypatch) -> None:
    monkeypatch.setattr(deepseek, "MAX_OUTPUT_CHARS", 5_000)

    async def post(path, payload, env):
        return {
            "results": [
                {"title": f"T{i}", "url": f"https://e/{i}", "summary": "s" * 2000}
                for i in range(50)
            ]
        }

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekSearchTool().execute({"query": "q"}, ctx())

    text = result_text(result)
    assert len(text) <= deepseek.MAX_OUTPUT_CHARS
    assert "more result(s) omitted" in text


async def test_fetch_rejects_an_empty_page(monkeypatch) -> None:
    async def post(path, payload, env):
        return {"title": "T", "content": "   "}

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekFetchTool().execute({"url": "https://example.com"}, ctx())

    assert result.is_error
    assert "no page content" in result_text(result)


async def test_a_lone_surrogate_does_not_crash(monkeypatch) -> None:
    async def post(path, payload, env):
        return {"results": [{"title": "bad \ud800 title", "url": "https://e"}]}

    monkeypatch.setattr(deepseek, "_post", post)
    result = await DeepSeekSearchTool().execute({"query": "q"}, ctx())

    assert not result.is_error
    assert "bad" in result_text(result) and "title" in result_text(result)
