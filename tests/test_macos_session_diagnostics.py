from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from vibe.macos_session_diagnostics import (
    CommandResult,
    DnsProbeState,
    MacOSSessionMonitor,
    MacOSSessionProbe,
    SessionContext,
    parse_gui_domain_asid,
    parse_launchctl_asid,
)


SERVICE_LAUNCHCTL_FIXTURE = """
pid/4242 = {
    type = pid
    security context = {
        uid = 501
        asid = 120001
    }
    task-special ports = {
        0x123 4 bootstrap (unknown)
    }
}
"""

GUI_LAUNCHCTL_FIXTURE = """
gui/501 = {
    type = login
    handle = 120001
    session = Aqua
    security context = {
        uid = 501
        asid = 120001
    }
    environment = {
        SECRET_THAT_MUST_NOT_BE_LOGGED => value
    }
}
"""


class FakeThread:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.started = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


class FakeProbe:
    service_pid = 4242
    service_uid = 501

    def __init__(self, contexts: list[SessionContext], dns_states: list[DnsProbeState]) -> None:
        self._contexts = iter(contexts)
        self._dns_states = iter(dns_states)
        self.context_calls = 0
        self.dns_calls = 0

    def capture_context(self) -> SessionContext:
        self.context_calls += 1
        return next(self._contexts)

    def capture_dns(self) -> DnsProbeState:
        self.dns_calls += 1
        return next(self._dns_states)


class ExplodingProbe:
    service_pid = 4242
    service_uid = 501

    def capture_context(self) -> SessionContext:
        raise RuntimeError("raw launchctl output must not escape")

    def capture_dns(self) -> DnsProbeState:
        raise RuntimeError("resolved address must not escape")


def context(
    *,
    console_user: str = "avibe",
    service_asid: int | None = 120001,
    gui_asid: int | None = 120001,
    gui_available: bool | None = True,
    gui_error: str | None = None,
) -> SessionContext:
    return SessionContext(
        service_pid=4242,
        service_uid=501,
        console_user=console_user,
        service_asid=service_asid,
        gui_asid=gui_asid,
        gui_domain_available=gui_available,
        gui_error_class=gui_error,
    )


def event_payloads(caplog) -> list[dict[str, object]]:
    prefix = "macos_session_diagnostic "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


def make_monitor(logger: logging.Logger, probe, *, dns_interval: float = 300.0):
    threads: list[FakeThread] = []

    def thread_factory(**kwargs):
        thread = FakeThread(**kwargs)
        threads.append(thread)
        return thread

    monitor = MacOSSessionMonitor(
        logger,
        platform="darwin",
        probe=probe,
        dns_interval=dns_interval,
        monotonic=lambda: 0.0,
        thread_factory=thread_factory,
    )
    return monitor, threads


def test_launchctl_snapshot_parsing_uses_security_context_fixture() -> None:
    assert parse_launchctl_asid(SERVICE_LAUNCHCTL_FIXTURE) == 120001
    assert parse_gui_domain_asid(GUI_LAUNCHCTL_FIXTURE) == 120001
    assert parse_gui_domain_asid(GUI_LAUNCHCTL_FIXTURE.replace("Aqua", "Background")) is None
    assert parse_launchctl_asid("security context format changed") is None


def test_probe_redacts_raw_launchctl_and_dns_results() -> None:
    outputs = {
        "pid/4242": CommandResult(0, SERVICE_LAUNCHCTL_FIXTURE),
        "gui/501": CommandResult(0, GUI_LAUNCHCTL_FIXTURE),
        "dns": CommandResult(0, "203.0.113.10 must be ignored"),
    }

    def runner(command, timeout):
        return outputs["dns" if command[-1] == "api.github.com" else command[-1]]

    probe = MacOSSessionProbe(
        service_pid=4242,
        service_uid=501,
        command_runner=runner,
        console_user_reader=lambda: ("avibe", None),
    )

    captured_context = probe.capture_context()
    captured_dns = probe.capture_dns()

    assert captured_context.asid_match is True
    assert captured_context.gui_domain_available is True
    assert captured_dns == DnsProbeState("healthy")
    assert "SECRET_THAT_MUST_NOT_BE_LOGGED" not in repr(captured_context)
    assert "203.0.113.10" not in repr(captured_dns)


def test_initial_snapshot_is_single_compact_event_and_unchanged_poll_is_silent(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe([context(), context()], [DnsProbeState("healthy")])
    monitor, threads = make_monitor(logging.getLogger("test.macos.initial"), probe)

    monitor.start()
    assert probe.context_calls == 0
    assert probe.dns_calls == 0
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=1.0)

    payloads = event_payloads(caplog)
    assert len(payloads) == 1
    assert payloads[0] == {
        "asid_match": True,
        "console_user": "avibe",
        "dns_state": "healthy",
        "event": "macos_session_snapshot",
        "gui_asid": 120001,
        "gui_domain_available": True,
        "reason": ["service_start"],
        "service_asid": 120001,
        "service_pid": 4242,
        "service_uid": 501,
    }
    assert threads[0].started is True


def test_fast_user_switch_logs_only_console_user_transition(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe(
        [context(console_user="avibe"), context(console_user="other-user")],
        [DnsProbeState("healthy"), DnsProbeState("healthy")],
    )
    monitor, _ = make_monitor(logging.getLogger("test.macos.fus"), probe)

    monitor.start()
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=1.0)

    payloads = event_payloads(caplog)
    assert payloads[-1]["reason"] == ["console_user_change"]
    assert payloads[-1]["gui_domain_available"] is True
    assert payloads[-1]["asid_match"] is True
    assert probe.dns_calls == 2


def test_gui_domain_disappearance_and_recreation_are_edge_triggered(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe(
        [
            context(),
            context(gui_asid=None, gui_available=False, gui_error="absent"),
            context(gui_asid=None, gui_available=False, gui_error="absent"),
            context(gui_asid=120002, gui_available=True),
        ],
        [DnsProbeState("healthy"), DnsProbeState("healthy"), DnsProbeState("healthy")],
    )
    monitor, _ = make_monitor(logging.getLogger("test.macos.gui"), probe)

    monitor.start()
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=1.0)
    monitor.poll_once(now=2.0)
    monitor.poll_once(now=3.0)

    payloads = event_payloads(caplog)
    assert len(payloads) == 3
    assert payloads[1]["reason"] == ["gui_domain_change", "asid_match_change"]
    assert payloads[1]["gui_domain_available"] is False
    assert payloads[2]["reason"] == ["gui_domain_change", "asid_match_change"]
    assert payloads[2]["gui_asid"] == 120002
    assert payloads[2]["asid_match"] is False


def test_asid_mismatch_transition_is_logged(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe(
        [context(), context(gui_asid=120002)],
        [DnsProbeState("healthy"), DnsProbeState("healthy")],
    )
    monitor, _ = make_monitor(logging.getLogger("test.macos.asid"), probe)

    monitor.start()
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=1.0)

    transition = event_payloads(caplog)[-1]
    assert transition["reason"] == ["gui_domain_change", "asid_match_change"]
    assert transition["asid_match"] is False


def test_dns_failure_is_deduplicated_until_recovery(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe(
        [context(), context(), context(), context()],
        [
            DnsProbeState("healthy"),
            DnsProbeState("failed", "resolver_timeout"),
            DnsProbeState("failed", "resolver_timeout"),
            DnsProbeState("healthy"),
        ],
    )
    monitor, _ = make_monitor(logging.getLogger("test.macos.dns"), probe)

    monitor.start()
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=300.0)
    monitor.poll_once(now=600.0)
    monitor.poll_once(now=900.0)

    payloads = event_payloads(caplog)
    assert [payload["reason"] for payload in payloads] == [
        ["service_start"],
        ["dns_failed"],
        ["dns_recovered"],
    ]
    assert payloads[1]["dns_error_class"] == "resolver_timeout"
    assert "dns_error_class" not in payloads[2]


def test_non_macos_monitor_is_noop(caplog) -> None:
    caplog.set_level(logging.INFO)
    probe = FakeProbe([context()], [DnsProbeState("healthy")])
    monitor = MacOSSessionMonitor(logging.getLogger("test.macos.noop"), platform="linux", probe=probe)

    monitor.start()
    monitor._initialize_once(now=0.0)
    monitor.poll_once(now=300.0)
    monitor.stop()

    assert probe.context_calls == 0
    assert probe.dns_calls == 0
    assert event_payloads(caplog) == []


def test_probe_failures_are_nonfatal_and_redacted(caplog) -> None:
    caplog.set_level(logging.INFO)
    monitor, threads = make_monitor(logging.getLogger("test.macos.failure"), ExplodingProbe())

    monitor.start()
    monitor._initialize_once(now=0.0)

    payload = event_payloads(caplog)[0]
    assert payload["reason"] == ["service_start"]
    assert payload["gui_domain_available"] is None
    assert payload["dns_state"] == "unknown"
    assert payload["gui_error_class"] == "probe_error"
    assert payload["dns_error_class"] == "probe_error"
    assert "raw launchctl" not in caplog.text
    assert "resolved address" not in caplog.text
    assert threads[0].started is True


def test_monitor_stop_joins_its_worker() -> None:
    probe = FakeProbe([context()], [DnsProbeState("healthy")])
    monitor, threads = make_monitor(logging.getLogger("test.macos.stop"), probe)
    monitor.start()
    monitor._initialize_once(now=0.0)

    monitor.stop()

    assert threads[0].joined is True


def test_service_main_owns_monitor_shutdown(monkeypatch) -> None:
    import config.v2_compat
    import core.controller
    import core.process_diagnostics
    import main as service_main
    import vibe.sentry_integration

    calls: list[str] = []

    class FakeMonitor:
        def stop(self) -> None:
            calls.append("diagnostics.stop")

    class FakeController:
        def __init__(self, config) -> None:
            calls.append("controller.init")

        def run(self) -> None:
            calls.append("controller.run")

    loaded_config = SimpleNamespace(
        runtime=SimpleNamespace(log_level="INFO", default_cwd="/tmp"),
        platform="avibe",
    )
    report = SimpleNamespace(imported=False, db_path="test.db", backup_path=None)

    monkeypatch.setattr(service_main, "acquire_service_instance_lock", lambda: calls.append("lock.acquire"))
    monkeypatch.setattr(service_main, "release_service_instance_lock", lambda: calls.append("lock.release"))
    monkeypatch.setattr(service_main, "load_config", lambda: loaded_config)
    monkeypatch.setattr(service_main, "setup_logging", lambda level: None)
    monkeypatch.setattr(service_main, "apply_claude_sdk_patches", lambda: None)
    monkeypatch.setattr(service_main, "prepare_sqlite_state", lambda loaded: report)
    monkeypatch.setattr(
        service_main,
        "start_macos_session_diagnostics",
        lambda logger: calls.append("diagnostics.start") or FakeMonitor(),
    )
    monkeypatch.setattr(vibe.sentry_integration, "init_sentry", lambda *args, **kwargs: None)
    monkeypatch.setattr(core.process_diagnostics, "log_process_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(core.controller, "Controller", FakeController)
    monkeypatch.setattr(config.v2_compat, "to_app_config", lambda loaded: loaded)
    monkeypatch.setattr(service_main.signal, "signal", lambda *args, **kwargs: None)

    service_main.main()

    assert calls.index("diagnostics.start") < calls.index("controller.run")
    assert calls.index("controller.run") < calls.index("diagnostics.stop")
