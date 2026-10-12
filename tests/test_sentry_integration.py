from __future__ import annotations

import sys
import types
import importlib
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.v2_config import AgentsConfig, RuntimeConfig, SlackConfig, V2Config
from vibe import sentry_integration


def _config() -> V2Config:
    return V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token=""),
        runtime=RuntimeConfig(default_cwd="/tmp/workdir", log_level="INFO"),
        agents=AgentsConfig(),
    )


def test_resolve_sentry_options_prefers_environment(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_DSN", "https://env@example.ingest.sentry.io/2")
    monkeypatch.setenv("VIBE_DEPLOYMENT_ENV", "regression")
    monkeypatch.setenv("VIBE_SENTRY_TRACES_SAMPLE_RATE", "0.25")

    options = sentry_integration.resolve_sentry_options()

    assert options is not None
    assert options["dsn"] == "https://env@example.ingest.sentry.io/2"
    assert options["environment"] == "regression"
    assert options["traces_sample_rate"] == 0.25


def test_resolve_sentry_options_returns_none_without_dsn(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_DSN", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(sentry_integration, "DEFAULT_SENTRY_DSN", "")

    assert sentry_integration.resolve_sentry_options() is None


def test_resolve_sentry_options_honors_empty_env_dsn_as_opt_out(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_DSN", "")
    monkeypatch.setattr(sentry_integration, "DEFAULT_SENTRY_DSN", "https://default@example.ingest.sentry.io/1")

    assert sentry_integration.resolve_sentry_options() is None


def test_detect_sentry_environment_defaults_to_local(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIBE_DEPLOYMENT_ENV", raising=False)
    monkeypatch.delenv("E2E_TEST_MODE", raising=False)
    monkeypatch.setenv("AVIBE_HOME", "/tmp/vibe-remote-home")
    monkeypatch.setattr(sentry_integration, "Path", lambda _: type("P", (), {"exists": staticmethod(lambda: False)})())

    assert sentry_integration.detect_sentry_environment() == "local"


def test_detect_sentry_environment_uses_explicit_deployment_env(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("VIBE_DEPLOYMENT_ENV", "production")

    assert sentry_integration.detect_sentry_environment() == "production"


def test_detect_sentry_environment_marks_integration_mode(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SENTRY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("VIBE_DEPLOYMENT_ENV", raising=False)
    monkeypatch.setenv("E2E_TEST_MODE", "true")
    monkeypatch.setenv("AVIBE_HOME", "/tmp/vibe-remote-home")

    assert sentry_integration.detect_sentry_environment() == "integration"


def test_build_sentry_contexts_contains_debug_metadata(monkeypatch):
    monkeypatch.setenv("AVIBE_HOME", "/tmp/vibe-remote-home")

    contexts = sentry_integration.build_sentry_contexts(_config(), component="service", environment="regression")

    assert contexts["deployment"]["environment"] == "regression"
    assert contexts["deployment"]["component"] == "service"
    assert contexts["deployment"]["default_agent_name"] is None
    assert contexts["runtime"]["python_version"]
    assert "hostname" in contexts["host"]


def test_init_sentry_returns_false_when_sdk_init_raises(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_DSN", "https://env@example.ingest.sentry.io/2")

    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.init = lambda **kwargs: (_ for _ in ()).throw(ValueError("bad dsn"))
    sentry_sdk.set_tag = lambda *args, **kwargs: None
    sentry_sdk.set_context = lambda *args, **kwargs: None

    logging_integration_module = types.ModuleType("sentry_sdk.integrations.logging")

    class FakeLoggingIntegration:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    logging_integration_module.LoggingIntegration = FakeLoggingIntegration

    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.logging", logging_integration_module)

    assert sentry_integration.init_sentry(_config(), component="service") is False


def test_init_sentry_disables_client_discard_reports(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_DSN", "https://env@example.ingest.sentry.io/2")
    init_kwargs = {}

    sentry_sdk = types.ModuleType("sentry_sdk")
    sentry_sdk.init = lambda **kwargs: init_kwargs.update(kwargs)
    sentry_sdk.set_tag = lambda *args, **kwargs: None
    sentry_sdk.set_context = lambda *args, **kwargs: None

    logging_integration_module = types.ModuleType("sentry_sdk.integrations.logging")

    class FakeLoggingIntegration:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    logging_integration_module.LoggingIntegration = FakeLoggingIntegration

    monkeypatch.setitem(sys.modules, "sentry_sdk", sentry_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations.logging", logging_integration_module)

    assert sentry_integration.init_sentry(_config(), component="service") is True
    assert init_kwargs["send_client_reports"] is False


def test_run_ui_server_skips_sentry_when_config_load_fails(monkeypatch):
    ui_server = importlib.import_module("vibe.ui_server")

    class DummyServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, sockets=None):
            return None

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = lambda *args, **kwargs: (args, kwargs)
    fake_uvicorn.Server = DummyServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    monkeypatch.setattr(ui_server.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(
        ui_server.V2Config,
        "load",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad config")),
    )
    sentry_calls = []
    monkeypatch.setattr(ui_server, "init_sentry", lambda *args, **kwargs: sentry_calls.append((args, kwargs)))

    ui_server.run_ui_server("127.0.0.1", 0)

    assert sentry_calls == []


def test_run_ui_server_reconciles_remote_access_after_binding(monkeypatch):
    from vibe import ui_server
    from vibe import remote_access

    class DummyServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, sockets=None):
            return None

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = lambda *args, **kwargs: (args, kwargs)
    fake_uvicorn.Server = DummyServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    import time

    from core.services import settings as settings_service

    reconcile_calls = []
    config = _config()

    monkeypatch.setattr(ui_server.paths, "ensure_data_dirs", lambda: None)
    # run_ui_server loads config via settings_service.load_config (PR #340), not
    # V2Config.load — patch the real seam, otherwise config is None and the
    # reconcile thread early-returns.
    monkeypatch.setattr(settings_service, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(ui_server, "init_sentry", lambda *args, **kwargs: None)
    # With a non-None config the start path also kicks the status heartbeat; keep it inert.
    monkeypatch.setattr(remote_access, "start_status_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_access, "reconcile", lambda next_config=None: reconcile_calls.append(next_config) or {"ok": True})

    ui_server.run_ui_server("127.0.0.1", 0)

    # Reconcile runs on a daemon thread spawned after binding; poll briefly.
    for _ in range(200):
        if reconcile_calls:
            break
        time.sleep(0.01)

    assert reconcile_calls == [config]


def test_run_ui_server_retries_when_prebind_reports_port_in_use(monkeypatch):
    from vibe import ui_server

    class DummySocket:
        def close(self):
            return None

    class DummyServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, sockets=None):
            run_calls.append(sockets)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = lambda *args, **kwargs: (args, kwargs)
    fake_uvicorn.Server = DummyServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    attempts = {"count": 0}
    run_calls = []

    def bind_socket(_host, _port):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError(48, "Address already in use")
        return DummySocket()

    monkeypatch.setattr(ui_server.paths, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(ui_server.V2Config, "load", lambda *args, **kwargs: _config())
    monkeypatch.setattr(ui_server, "init_sentry", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_server, "_bind_ui_socket", bind_socket)
    monkeypatch.setattr(ui_server.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ui_server, "_reconcile_remote_access_for_ui_start", lambda _config: None)

    ui_server.run_ui_server("127.0.0.1", 5123)

    assert attempts["count"] == 2
    assert len(run_calls) == 1


def test_run_ui_server_keepalive_outlives_upstream_proxy_pool(monkeypatch):
    """The UI's idle keep-alive must outlive any proxy pooling its connections.

    cloudflared reuses idle origin connections for up to its own keep-alive
    timeout. If the server expires them first, a reused connection races the
    close and the browser gets a 502 from a perfectly healthy service. Assert
    the ordering property rather than a specific number, so retuning either
    constant cannot silently reintroduce the race.
    """

    from vibe import ui_server
    from core.services import settings as settings_service

    captured: dict = {}

    class DummySocket:
        def close(self):
            return None

    class DummyServer:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, sockets=None):
            return None

    def capture_config(*args, **kwargs):
        captured.update(kwargs)
        return (args, kwargs)

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = capture_config
    fake_uvicorn.Server = DummyServer
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    monkeypatch.setattr(ui_server.paths, "ensure_data_dirs", lambda: None)
    # Keep the probe hermetic: no real config load, no remote-access reconcile.
    monkeypatch.setattr(settings_service, "load_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_server, "_bind_ui_socket", lambda _host, _port: DummySocket())
    monkeypatch.setattr(ui_server, "_reconcile_remote_access_for_ui_start", lambda _config: None)

    ui_server.run_ui_server("127.0.0.1", 5123)

    assert captured["timeout_keep_alive"] > ui_server._CLOUDFLARED_PROXY_KEEPALIVE_TIMEOUT_SECONDS


def test_ui_error_handler_does_not_explicitly_capture_exceptions():
    source = Path("vibe/ui_server.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    handle_exception = next(
        node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "handle_exception"
    )

    calls_capture_exception = False
    for node in ast.walk(handle_exception):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "capture_exception":
            calls_capture_exception = True
            break

    assert calls_capture_exception is False


def test_scrub_data_redacts_sensitive_values():
    event = {
        "request": {
            "headers": {"Authorization": "Bearer abc", "Cookie": "session=secret"},
            "data": {"bot_token": "xoxb-secret", "nested": [{"client_secret": "shh"}]},
        },
        "message": "token=abc123 and xapp-123456",
    }

    scrubbed = sentry_integration.scrub_data(event)

    assert scrubbed["request"]["headers"]["Authorization"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["Cookie"] == "[Filtered]"
    assert scrubbed["request"]["data"]["bot_token"] == "[Filtered]"
    assert scrubbed["request"]["data"]["nested"][0]["client_secret"] == "[Filtered]"
    assert "[Filtered]" in scrubbed["message"]


@pytest.mark.parametrize(
    ("text", "secret", "kept"),
    [
        (
            "lark config rejected: LarkConfig(app_id='cli_x', app_secret='lark-s3cret', domain='feishu')",
            "lark-s3cret",
            "app_id='cli_x'",
        ),
        (
            'save failed: {"api_key": "sk-s3cret", "base_url": "https://api.example"}',
            "sk-s3cret",
            '"base_url": "https://api.example"',
        ),
        ("refresh failed client_secret=gw-s3cret status=401", "gw-s3cret", "status=401"),
        # Credential names the code uses, in the spellings it uses them.
        ({"provider": {"apiKey": "oc-s3cret"}, "model": "m1"}, "oc-s3cret", '"model": "m1"'),
        ("request headers x-api-key: an-s3cret accept: json", "an-s3cret", "accept: json"),
        ("engine health X-Management-Key: mk-s3cret status=503", "mk-s3cret", "status=503"),
        ("media decrypt failed aes_key=aes-s3cret file=a.jpg", "aes-s3cret", "file=a.jpg"),
        ('{"passphrase": "pp-s3cret", "vault": "v1"}', "pp-s3cret", '"vault": "v1"'),
        ("Binding(private_key='pk-s3cret', agent='a1')", "pk-s3cret", "agent='a1'"),
        # Generic names, as whole keys only: Model Hub validates ``{"key": ...}``.
        ("probe failed {'vendor': 'openai', 'key': 'mh-s3cret'} cache_key=c1", "mh-s3cret", "cache_key=c1"),
        ("upstream auth: Bearer au-s3cret author=me", "au-s3cret", "author=me"),
        # Quoting: an escaped quote inside, a trailing backslash, JSON in a JSON string.
        ('api_key="ab\\"esc-s3cret" retry=2', "esc-s3cret", "retry=2"),
        ("cfg {'api_key': 'bs-s3cret\\\\'}", "bs-s3cret", "cfg {'api_key': "),
        ('{"body": "{\\"api_key\\": \\"nest-s3cret\\", \\"n\\": 1}"}', "nest-s3cret", '\\"n\\": 1'),
        # Any URL keeps only its origin.
        (
            "poll error url=https://api.telegram.org/bot123456:AAHs3cretS3cretS3cretS3cretS3cret/getUpdates",
            "AAHs3cretS3cretS3cretS3cretS3cret",
            "url=https://api.telegram.org/",
        ),
        ("409 url=https://hooks.slack.com/actions/T01/123/sl-s3cret", "sl-s3cret", "https://hooks.slack.com/"),
        ("POST https://discord.com/api/v10/interactions/1/dc-s3cret/callback", "dc-s3cret", "https://discord.com/"),
        ("GET https://cdn.weixin.qq.com/download?encrypted_query_param=wx-s3cret", "wx-s3cret", "https://cdn.weixin.qq.com/"),
        ("proxy error http://alice:px-s3cret@proxy.local:8080 timed out", "px-s3cret", "@proxy.local:8080 timed out"),
        ("bad proxy http://bob:p/w-s3cret@proxy.local/x", "w-s3cret", "bad proxy http://"),
        ("POST hooks.slack.com/actions/T01/123/ns-s3cret failed", "ns-s3cret", "POST hooks.slack.com/"),
    ],
)
def test_scrub_data_redacts_secrets_inside_free_text(text, secret, kept):
    """Log and exception messages carry secrets as text, not under a dict key."""
    import json

    scrubbed = sentry_integration.scrub_data({"logentry": {"params": [text]}})

    (value,) = scrubbed["logentry"]["params"]
    rendered = json.dumps(value) if isinstance(value, dict) else value
    assert secret not in rendered
    assert "[Filtered]" in rendered
    assert kept in rendered
    assert sentry_integration.scrub_data(value) == value


def test_before_send_redacts_stored_credentials_in_any_text(monkeypatch, tmp_path):
    """A credential Avibe stores leaves no trace however the text around it is shaped.

    Prose such as ``auth failed for <secret>`` has no key for a pattern to find, a
    secret can be escaped by JSON or repr or name a dict entry, and an unreadable
    store must not keep a rotated credential from being covered.
    """
    import json

    from config import paths
    from config.v2_config import LarkConfig, TelegramConfig

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    config = _config()
    config.lark = LarkConfig(app_id="cli_x", app_secret='lark"first\\s3cret')
    config.telegram = TelegramConfig(bot_token="s3cr7")
    config.save()
    state = paths.get_runtime_dir() / "model-hub" / "state"
    (state / "credentials").mkdir(parents=True)
    (state / "instances" / "i1").mkdir(parents=True)
    (state / "credentials" / "cred_a.json").write_text(
        json.dumps({"kind": "api_key", "vendor": "openai", "value": "mh-stored-s3cret"})
    )
    (state / "instances" / "i1" / "runtime-secrets.json").write_text(
        json.dumps({"management_key": "mh-management-s3cret", "gateway_token": "mh-gateway-s3cret"})
    )

    def send(message, extra=None):
        event = {"logentry": {"message": message}, "breadcrumbs": {"values": [{"message": message}]}}
        if extra is not None:
            event["extra"] = extra
        return json.dumps(sentry_integration.before_send(event, {}))

    first = send(
        "rejected mh-stored-s3cret and mh-management-s3cret (openai); short s3cr7! "
        + json.dumps({"note": 'lark"first\\s3cret'})
        + " "
        + repr('lark"first\\s3cret'),
        extra={"sessions": {"mh-gateway-s3cret": "active"}},
    )
    for secret in ("mh-stored-s3cret", "mh-management-s3cret", "mh-gateway-s3cret", "s3cr7", "first"):
        assert secret not in first
    assert "rejected [Filtered] and [Filtered] (openai); short [Filtered]!" in first

    (state / "credentials" / "cred_broken.json").write_text("{not json")
    config.lark.app_secret = "lark-rotated-s3cret"
    config.save()
    rotated = send("lark auth failed for lark-rotated-s3cret (cli_x)")
    assert "lark-rotated-s3cret" not in rotated
    assert "(cli_x)" in rotated


def test_before_send_keeps_request_records_to_their_origin(monkeypatch, tmp_path):
    """Request records keep method, origin and status, never paths, queries or bodies.

    The shapes are the ones the SDK attaches: an HTTP client breadcrumb, a
    subprocess breadcrumb and an incoming request.
    """
    import json

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    event = {
        "message": "request records",
        "request": {
            "method": "POST",
            "url": "http://127.0.0.1:5123/api/media/md-s3cret",
            "query_string": "state=st-s3cret",
            "data": {"content": "body-s3cret"},
            "cookies": "__Host-vibe_remote_session=ck-s3cret",
            "headers": {"x-forwarded-for": "203.0.113.10"},
            "env": {"REMOTE_ADDR": "203.0.113.10"},
        },
        "breadcrumbs": {
            "values": [
                {
                    "type": "http",
                    "category": "httplib",
                    "data": {
                        "http.method": "POST",
                        "url": "https://discord.com/api/webhooks/1/hk-s3cret",
                        "http.query": "wait=hq-s3cret",
                        "http.fragment": "fr-s3cret",
                        "http.response.status_code": 404,
                    },
                },
                {"type": "subprocess", "category": "subprocess", "message": "/usr/bin/git push https://x-access-token:gt-s3cret@github.com/o/r", "data": {"subprocess.cwd": "/srv/cw-s3cret"}},
            ]
        },
    }

    sent = sentry_integration.before_send(event, {})
    rendered = json.dumps(sent)

    assert "s3cret" not in rendered
    assert sent["request"] == {"method": "POST", "url": "http://127.0.0.1:5123/[Filtered]"}
    http_crumb, subprocess_crumb = sent["breadcrumbs"]["values"]
    assert http_crumb["data"]["url"] == "https://discord.com/[Filtered]"
    assert http_crumb["data"]["http.response.status_code"] == 404
    assert subprocess_crumb["message"] == "git"
    assert subprocess_crumb["data"] == {}


def test_performance_transactions_are_scrubbed_like_events(monkeypatch, tmp_path):
    """Transactions skip ``before_send``; their spans must not carry what events may not."""
    import sentry_sdk

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_SENTRY_DSN", "http://public@127.0.0.1:9/1")
    monkeypatch.setenv("VIBE_SENTRY_TRACES_SAMPLE_RATE", "1.0")
    sent = []

    class Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            sent.extend(item.get_bytes().decode("utf-8") for item in envelope.items)

    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda **options: real_init(transport=Capture(), **options))
    try:
        assert sentry_integration.init_sentry(_config(), component="service") is True
        with sentry_sdk.start_transaction(op="task", name="refresh"):
            with sentry_sdk.start_span(op="http.client", name="POST https://discord.com/api/webhooks/1/tx-s3cret") as span:
                span.set_data("url", "https://discord.com/api/webhooks/1/tx-s3cret")
                span.set_data("http.query", "wait=tq-s3cret")
            with sentry_sdk.start_span(op="subprocess", name="git push https://x:sp-s3cret@github.com/o/r"):
                pass
            with sentry_sdk.start_span(op="task", name="refresh lark app_secret=ts-s3cret"):
                pass
        sentry_sdk.flush(timeout=2)
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)

    (payload,) = [item for item in sent if '"type":"transaction"' in item]
    assert "s3cret" not in payload
    assert "https://discord.com/[Filtered]" in payload


@pytest.mark.parametrize("unit", ["\\", "a.", "x-api-"])
def test_scrub_data_stays_linear_on_pathological_text(unit):
    """Scrubbing runs synchronously on every log call, so it must never backtrack."""
    import time

    text = unit * (40_000 // len(unit))
    started = time.perf_counter()
    sentry_integration.scrub_data(text)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Failed to retrieve WSS URL: The request to the Slack API failed. "
            "(url: https://slack.com/api/apps.connections.open, status: 200)",
            "Failed to retrieve WSS URL: The request to the Slack API failed. "
            "(url: https://slack.com/api/apps.connections.open, status: 200)",
        ),
        # Two different reconnect failures share the filter's fingerprint only
        # through the method name, which appears inside the URL.
        (
            "The request to the Slack API failed. (url: https://slack.com/api/apps.connections.open, status: 200)",
            "The request to the Slack API failed. (url: https://slack.com/api/apps.connections.open, status: 503)",
        ),
    ],
)
def test_before_send_throttles_repeated_noisy_slack_socket_events(monkeypatch, first, second):
    monkeypatch.delenv("VIBE_SENTRY_NOISE_FILTERS", raising=False)
    monkeypatch.delenv("VIBE_SENTRY_NOISE_TTL_SECONDS", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()

    def event(formatted):
        return {"logger": "slack_sdk.socket_mode.aiohttp", "logentry": {"formatted": formatted}}

    assert sentry_integration.before_send(event(first), {}) is not None
    assert sentry_integration.before_send(event(second), {}) is None


def test_before_send_throttles_repeated_normal_errors(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_NOISE_FILTERS", raising=False)
    monkeypatch.delenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": "Failed to send result message: unexpected application bug"},
    }

    assert sentry_integration.before_send(dict(event), {}) is not None
    assert sentry_integration.before_send(dict(event), {}) is None


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "Failed to send result message: unexpected application bug",
            "Failed to send result message: different application bug",
        ),
        # Errors on one host differ only by path, which the sent event no longer carries.
        (
            "Upload failed: 404 url=https://api.example.com/v1/files",
            "Upload failed: 404 url=https://api.example.com/v1/messages",
        ),
    ],
)
def test_before_send_allows_distinct_normal_errors(monkeypatch, first, second):
    monkeypatch.delenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    first_event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": first},
    }
    second_event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": second},
    }

    assert sentry_integration.before_send(dict(first_event), {}) is not None
    assert sentry_integration.before_send(dict(second_event), {}) is not None


def test_before_send_allows_distinct_exception_values(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    first_event = {"exception": {"values": [{"type": "ValueError", "value": "invalid channel C123456789"}]}}
    second_event = {"exception": {"values": [{"type": "ValueError", "value": "invalid channel C987654321"}]}}

    assert sentry_integration.before_send(dict(first_event), {}) is not None
    assert sentry_integration.before_send(dict(second_event), {}) is not None
    assert sentry_integration.before_send(dict(first_event), {}) is None


def test_before_send_noise_filter_can_be_disabled(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_NOISE_FILTERS", "0")
    monkeypatch.setenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", "0")
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    event = {
        "logger": "core.watches",
        "logentry": {"formatted": "Managed watch reconcile failed: [Errno 28] No space left on device"},
    }

    assert sentry_integration.before_send(dict(event), {}) is not None
    assert sentry_integration.before_send(dict(event), {}) is not None


def test_before_send_rate_limit_can_be_raised(monkeypatch):
    monkeypatch.setenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", "2")
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": "Failed to send result message: recurring application bug"},
    }

    assert sentry_integration.before_send(dict(event), {}) is not None
    assert sentry_integration.before_send(dict(event), {}) is not None
    assert sentry_integration.before_send(dict(event), {}) is None


def test_event_rate_state_enforces_cache_limit(monkeypatch):
    monkeypatch.setattr(sentry_integration, "_EVENT_RATE_CACHE_LIMIT", 2)
    sentry_integration._EVENT_RATE_STATE.clear()

    for index in range(4):
        event = {
            "logger": "core.message_dispatcher",
            "logentry": {"formatted": f"unique application bug {index}"},
        }
        assert sentry_integration.before_send(event, {}) is not None

    assert len(sentry_integration._EVENT_RATE_STATE) == 2


def test_event_rate_state_evicts_oldest_entries(monkeypatch):
    monkeypatch.setattr(sentry_integration, "_EVENT_RATE_CACHE_LIMIT", 2)
    sentry_integration._EVENT_RATE_STATE.clear()
    sentry_integration._EVENT_RATE_STATE.update(
        {
            "z-oldest": (1.0, 1),
            "a-middle": (2.0, 1),
            "m-newest": (3.0, 1),
        }
    )

    sentry_integration._prune_event_rate_state(now=3.0, window_seconds=10.0)

    assert "z-oldest" not in sentry_integration._EVENT_RATE_STATE
    assert set(sentry_integration._EVENT_RATE_STATE) == {"a-middle", "m-newest"}


def test_before_send_rate_state_is_thread_safe(monkeypatch):
    monkeypatch.setattr(sentry_integration, "_EVENT_RATE_CACHE_LIMIT", 8)
    sentry_integration._EVENT_RATE_STATE.clear()

    def send_event(index: int) -> bool:
        event = {
            "logger": "core.message_dispatcher",
            "logentry": {"formatted": f"concurrent application bug {index}"},
        }
        return sentry_integration.before_send(event, {}) is not None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(send_event, range(64)))

    assert all(results)
    assert len(sentry_integration._EVENT_RATE_STATE) <= 8


def test_sentry_events_carry_no_frame_locals(monkeypatch, tmp_path):
    """An error event keeps its traceback but never the values its frames held.

    Frame locals carry what ``scrub_data`` cannot see into, such as a Vibey tool
    context's whole environment rendered as one repr string.
    """
    import json
    import logging
    import uuid

    import sentry_sdk

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    monkeypatch.setenv("VIBE_SENTRY_DSN", "http://public@127.0.0.1:9/1")
    events = []

    def capture(event, hint):
        events.append(event)
        return None  # dropped before any transport

    monkeypatch.setattr(sentry_integration, "before_send", capture)
    # Built at runtime: the event's source-context lines must not be what carries it.
    secret = f"sk-{uuid.uuid4().hex}"

    def tool_execute(arguments, env):
        raise RuntimeError("database is locked")

    try:
        assert sentry_integration.init_sentry(_config(), component="service") is True
        try:
            tool_execute({"path": "/etc/hosts"}, {"OPENAI_API_KEY": secret})
        except RuntimeError:
            logging.getLogger("tests.sentry_frame_locals").exception("Tool execution failed")
        sentry_sdk.flush(timeout=2)
    finally:
        sentry_sdk.get_client().close(timeout=0)
        sentry_sdk.get_global_scope().set_client(None)

    (event,) = events
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert [frame["function"] for frame in frames][-1] == "tool_execute"
    assert all("vars" not in frame for frame in frames)
    assert secret not in json.dumps(event, default=str)
