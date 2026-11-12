"""S2: inspect emitted logs, not just helper return values.

Existing logging tests cover lifecycle/rotation, not credential-bearing URLs or
exception rendering. Synthetic payloads also catch body/preview leaks that no
credential-pattern redactor can recognize. All HTTP clients are in-memory fakes.
"""
from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
from unittest.mock import AsyncMock

import httpx
import httpx2
import pytest

import main
from modules.im import wechat_api
from vibe import service_logging, ui_server


@pytest.fixture(params=["service", "ui"])
def log_output(request, monkeypatch, tmp_path):
    service_logging.shutdown_service_logging()
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    library_names = ("httpx", "httpx2", "httpcore", "httpcore2")
    old_levels = {name: logging.getLogger(name).level for name in library_names}
    for name in library_names:
        logging.getLogger(name).setLevel(logging.NOTSET)
    stream = io.StringIO()
    if request.param == "service":
        monkeypatch.setattr(main, "get_logs_dir", lambda: tmp_path)
        monkeypatch.setattr(main.sys, "stdout", stream)
        monkeypatch.delenv("VIBE_DISABLE_STDOUT_LOGGING", raising=False)
        raw_setup = main.setup_logging
        filename = "vibe_remote.log"
    else:
        monkeypatch.setattr(ui_server.paths, "get_logs_dir", lambda: tmp_path)
        monkeypatch.setattr(ui_server.sys, "stderr", stream)
        raw_setup = ui_server._configure_ui_logging
        filename = "vibe_ui.log"

    def setup(level="INFO"):
        # pytest replaces capture streams between fixture setup and test calls.
        monkeypatch.setattr(main.sys, "stdout" if request.param == "service" else "stderr", stream)
        raw_setup(level)

    def output():
        service_logging._runtime.handler.queue.join()
        return (tmp_path / filename).read_text(), stream.getvalue()

    try:
        yield setup, output
    finally:
        service_logging.shutdown_service_logging()
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)
        assert root.handlers == old_handlers and root.level == old_level


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_http_clients_default_to_warning_and_keep_safe_failures(log_output, level):
    setup, output = log_output
    setup(level)
    url = "https://mcp.example/mcp/?tavilyApiKey=synthetic-query-credential"
    for client_module in (httpx, httpx2):
        with client_module.Client(transport=client_module.MockTransport(lambda _: client_module.Response(200))) as client:
            assert client.get(url).status_code == 200
        logger = logging.getLogger(client_module.__name__)
        assert logger.getEffectiveLevel() == logging.WARNING
        logger.warning("upstream failed status=503 method=GET url=%s", url)
    for name in ("httpcore", "httpcore2"):
        logging.getLogger(name).debug("headers=[(b'opaque', b'synthetic-header-payload')]")
    file_text, stream_text = output()
    for text in (file_text, stream_text):
        assert "HTTP Request:" not in text
        assert "synthetic-query-credential" not in text
        assert "synthetic-header-payload" not in text
        assert "status=503 method=GET url=https://mcp.example/mcp/" in text


@pytest.mark.parametrize("url, safe_path, forbidden", [
    ("https://user:synthetic-password@example.test/mcp?key=synthetic-query#synthetic-fragment", "/mcp", ("user:", "synthetic-password", "synthetic-query", "synthetic-fragment")),
    ("https://api.telegram.org/file/bot123:synthetic-bot-token/photos/file.jpg", "/photos/file.jpg", ("123:synthetic-bot-token",)),
    ("https://api.telegram.org/bot123%3Asynthetic-bot-token/sendMessage", "/sendMessage", ("synthetic-bot-token",)),
    ("https://hooks.slack.com/services/T123/B456/synthetic-webhook-token", "/services/", ("T123", "B456", "synthetic-webhook-token")),
    ("https://discord.com/api/v10/webhooks/123/synthetic-webhook-token/messages/@original", "/messages/@original", ("synthetic-webhook-token",)),
    ("https://open.feishu.cn/open-apis/bot/v2/hook/synthetic-hook-token", "/open-apis/bot/v2/hook/", ("synthetic-hook-token",)),
    ("wss://user:synthetic-password@[::1]:5123/show/session/__vite_hmr?token=synthetic-query", "/show/session/__vite_hmr", ("user:", "synthetic-password", "synthetic-query")),
    ("//user:synthetic-password@example.test/mcp?key=synthetic-query", "/mcp", ("user:", "synthetic-password", "synthetic-query")),
    ("socks5h://user:synthetic-password@example.test:1080/?key=synthetic-query", "example.test:1080/", ("user:", "synthetic-password", "synthetic-query")),
    ("/file/bot123:synthetic-bot-token/photos/file.jpg?key=synthetic-query", "/photos/file.jpg", ("synthetic-bot-token", "synthetic-query")),
    ("/show/session/__vite_hmr?token=synthetic-relative-query", "/show/session/__vite_hmr", ("synthetic-relative-query",)),
    ("https://user:synthetic-password@[invalid/mcp?key=synthetic-query", "[redacted-url]", ("synthetic-password", "synthetic-query")),
])
def test_urls_are_safe_in_arguments_and_exception_chains(log_output, url, safe_path, forbidden):
    setup, output = log_output
    setup("DEBUG")
    logger = logging.getLogger("s2-network")
    # Exercise %-formatting, structured args, cached exception text and chaining.
    logger.warning("request method=POST url=%s status=401", url)
    logging.getLogger("uvicorn.error").info("WebSocket %s [accepted]", url)
    try:
        try:
            raise ValueError(url)
        except ValueError as exc:
            raise RuntimeError("upstream unavailable") from exc
    except RuntimeError:
        logger.exception("request failed method=POST")
    for text in output():
        for secret in forbidden:
            assert secret not in text
        assert safe_path in text
        assert "status=401" in text
        assert "Traceback (most recent call last)" in text
        assert "RuntimeError: upstream unavailable" in text


def test_header_values_are_redacted_in_exception_output(log_output):
    setup, output = log_output
    setup()
    diagnostic = {"Authorization": "Bearer synthetic-bearer", "Cookie": "sid=synthetic-cookie", "X-Api-Key": "synthetic-api-key"}
    try:
        raise RuntimeError(json.dumps(diagnostic))
    except RuntimeError:
        logging.getLogger("s2-network").exception("HTTP failure status=403")
    for text in output():
        assert "status=403" in text
        assert "RuntimeError" in text
        for secret in ("synthetic-bearer", "synthetic-cookie", "synthetic-api-key"):
            assert secret not in text


@pytest.mark.parametrize("operation", ["post", "updates", "qrcode", "qr_status"])
@pytest.mark.parametrize("status", [200, 500])
async def test_wechat_diagnostics_omit_request_and_response_payloads(log_output, monkeypatch, operation, status):
    setup, output = log_output
    setup("DEBUG")
    request_secret = "opaque-request-content"
    response_secret = "opaque-response-content"
    response_body = json.dumps({"text": response_secret})

    class Response:
        ok = status == 200
        reason = "test status"
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def text(self):
            return response_body

        async def json(self, **kwargs):
            return json.loads(response_body)

    Response.status = status

    class Session(Response):
        def post(self, *args, **kwargs):
            return Response()

        get = post

    monkeypatch.setattr(wechat_api.aiohttp, "ClientSession", lambda **kwargs: Session())
    if operation == "post":
        call = wechat_api._api_fetch("https://example.test", "sendmessage", {"text": request_secret}, token="synthetic-auth-value")
    elif operation == "updates":
        call = wechat_api.get_updates("https://example.test", "synthetic-auth-value", request_secret)
    elif operation == "qrcode":
        call = wechat_api.get_bot_qrcode("https://example.test", local_token_list=[request_secret])
    else:
        call = wechat_api.get_qrcode_status("https://example.test", request_secret)
    try:
        result = await call
        assert result == {"text": response_secret}
        assert status == 200
    except RuntimeError:
        assert status == 500
        logging.getLogger("s2-wechat-caller").exception("WeChat request failed")
    file_text, stream_text = output()
    if operation != "qrcode" or status != 200:
        assert str(status) in file_text
    else:
        assert "Fetching QR code" in file_text
    for text in (file_text, stream_text):
        assert request_secret not in text
        assert response_secret not in text
        assert "synthetic-auth-value" not in text
        assert "?bot_type=" not in text
        assert "synthet..." not in text


@pytest.mark.parametrize("message_type", ["toolcall", "assistant"])
async def test_hidden_messages_log_metadata_not_preview(log_output, monkeypatch, message_type):
    from tests.test_message_dispatcher_status_bubble import _StubController, _ctx, _dispatcher

    setup, output = log_output
    setup()
    controller = _StubController(progress_style="verbose", hidden_types={message_type})
    dispatcher = _dispatcher(controller)
    persist = AsyncMock()
    monkeypatch.setattr("core.message_dispatcher.persist_agent_message", persist)
    secret = 'opaque-hidden-command-value'
    payload = f'🔧 bash {{"command":"echo {secret}"}}'
    assert await dispatcher.emit_agent_message(_ctx(), message_type, payload) is None
    assert persist.await_args.args[2] == payload
    assert not controller.im_client.sent
    text, _ = output()
    assert f"Skipping {message_type} message" in text
    assert "C1" in text
    assert secret not in text
    assert "Preview:" not in text


def test_invalid_cdn_key_is_not_echoed_in_tracebacks(log_output):
    import base64
    from modules.im.wechat_cdn import parse_aes_key

    setup, output = log_output
    setup()
    encoded = base64.b64encode(b"invalid-key-material").decode()
    try:
        parse_aes_key(encoded)
    except ValueError:
        logging.getLogger("s2-cdn-caller").exception("CDN key rejected")
    for text in output():
        assert "CDN key rejected" in text
        assert encoded not in text


def test_long_non_url_diagnostic_finishes_without_blocking_formatter():
    # A scheme scan that retries at every character is quadratic. A separate,
    # bounded process makes that regression fail without hanging the test run.
    script = """
import logging
from vibe.logging_config import RedactingFormatter
payload = 'a' * 40_000
url = 'https://user:private-password@example.test/path?key=private-query'
record = logging.makeLogRecord({'msg': 'status=503 %s %s', 'args': (payload, url)})
text = RedactingFormatter('%(message)s').format(record)
assert text == 'status=503 ' + payload + ' https://example.test/path'
print('ok')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5, check=True)
    assert result.stdout == "ok\n"


def test_installer_retention_formatter_redacts_without_mutating_exception():
    from vibe.cli import _InstallerRetentionFormatter

    url = "https://synthetic-user:synthetic-password@example.test/install?key=synthetic-query"
    error = RuntimeError(url)
    record = logging.LogRecord("retention", logging.WARNING, __file__, 1, "collection failed", (), (RuntimeError, error, None))
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(_InstallerRetentionFormatter())
    handler.handle(record)
    assert "collection failed: https://example.test/install" in output.getvalue()
    assert "synthetic-" not in output.getvalue()
    assert "Traceback" not in output.getvalue()
    assert record.exc_info[1] is error and str(error) == url
