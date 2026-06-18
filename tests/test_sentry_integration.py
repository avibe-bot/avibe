from __future__ import annotations

import sys
import types
import importlib
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    monkeypatch.setenv("VIBE_REMOTE_HOME", "/tmp/vibe-remote-home")
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
    monkeypatch.setenv("VIBE_REMOTE_HOME", "/tmp/vibe-remote-home")

    assert sentry_integration.detect_sentry_environment() == "integration"


def test_build_sentry_contexts_contains_debug_metadata(monkeypatch):
    monkeypatch.setenv("VIBE_REMOTE_HOME", "/tmp/vibe-remote-home")

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


def test_before_send_throttles_repeated_noisy_slack_socket_events(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_NOISE_FILTERS", raising=False)
    monkeypatch.delenv("VIBE_SENTRY_NOISE_TTL_SECONDS", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    event = {
        "logger": "slack_sdk.socket_mode.aiohttp",
        "logentry": {
            "formatted": (
                "Failed to retrieve WSS URL: The request to the Slack API failed. "
                "(url: https://slack.com/api/apps.connections.open, status: 200)"
            )
        },
    }

    assert sentry_integration.before_send(dict(event), {}) is not None
    assert sentry_integration.before_send(dict(event), {}) is None


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


def test_before_send_allows_distinct_normal_errors(monkeypatch):
    monkeypatch.delenv("VIBE_SENTRY_EVENT_RATE_LIMIT_PER_WINDOW", raising=False)
    sentry_integration._NOISY_EVENT_LAST_SEEN.clear()
    sentry_integration._EVENT_RATE_STATE.clear()
    first_event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": "Failed to send result message: unexpected application bug"},
    }
    second_event = {
        "logger": "core.message_dispatcher",
        "logentry": {"formatted": "Failed to send result message: different application bug"},
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
