from __future__ import annotations

import os

import pytest

from config import paths
from config.v2_config import V2Config
from core.web_ui_watchdog import CHECK_INTERVAL_SECONDS, WebUiWatchdog
from vibe import runtime

UI_COMMAND = "python3 -c from vibe.ui_server import run_ui_server; run_ui_server('127.0.0.1', 5199)"


class Processes:
    """The processes the host reports: pid -> command, or None when unreadable."""

    def __init__(self) -> None:
        self.commands: dict[int, str | None] = {}
        self.serving = False
        self.starts: list[tuple[str, int]] = []

    def start_ui(self, host, port, **kwargs):
        # Like the real start: the new pid is recorded before it returns.
        pid = 300 + len(self.starts) + 1
        self.starts.append((host, port))
        self.commands[pid] = UI_COMMAND
        paths.get_runtime_ui_pid_path().write_text(str(pid), encoding="utf-8")
        return pid

    def kill(self, pid: int) -> None:
        self.commands.pop(pid, None)


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A home whose service runs and whose recorded Web UI, pid 200, has died."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    config = V2Config.default()
    config.ui.setup_host = "127.0.0.1"
    config.ui.setup_port = 5199
    config.save(paths.get_config_path())
    runtime.write_status("running", "pid=100", 100, 200)
    paths.get_runtime_ui_pid_path().write_text("200", encoding="utf-8")

    processes = Processes()
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in processes.commands)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: processes.commands.get(pid))
    monkeypatch.setattr(runtime, "ui_server_healthy", lambda host, port, **kwargs: processes.serving)
    monkeypatch.setattr(runtime, "current_process_owns_service_instance", lambda: True)
    monkeypatch.setattr(runtime, "start_ui", processes.start_ui)
    return processes


def test_a_ui_gone_for_two_checks_is_started_again_with_the_status_kept(host) -> None:
    """The 2026-10-06 outage: the UI was killed and nothing started it while the service ran."""

    started_at = runtime.read_status()["started_at"]
    watchdog = WebUiWatchdog(stopping=lambda: False)

    assert watchdog.check(0.0) is None
    assert host.starts == []
    assert watchdog.check(CHECK_INTERVAL_SECONDS) == 301

    assert host.starts == [("127.0.0.1", 5199)]
    status = runtime.read_status()
    assert (status["state"], status["detail"], status["service_pid"], status["ui_pid"]) == (
        "running",
        "pid=100",
        100,
        301,
    )
    assert status["started_at"] == started_at


@pytest.mark.parametrize(
    ("recorded", "command", "started"),
    [
        pytest.param("200", None, True, id="recorded-pid-dead"),
        pytest.param(None, None, True, id="no-record"),
        pytest.param("200", "/usr/bin/some-other-program", True, id="pid-reused-by-another-program"),
        pytest.param("200", UI_COMMAND, False, id="ui-running"),
        # A command the host will not show: the UI may be running, and a second
        # one would die on its port with the record pointing at it.
        pytest.param("200", "", False, id="live-pid-command-unreadable"),
    ],
)
def test_only_a_ui_known_to_be_gone_is_started_again(host, recorded, command, started) -> None:
    if recorded is None:
        paths.get_runtime_ui_pid_path().unlink()
    if command is not None:
        host.commands[200] = command or None
    watchdog = WebUiWatchdog(stopping=lambda: False)

    watchdog.check(0.0)
    watchdog.check(CHECK_INTERVAL_SECONDS)

    assert bool(host.starts) is started


def _restart_job_running() -> None:
    runtime.write_json(
        runtime.get_restart_status_path(),
        {
            "ok": None,
            "state": "running",
            "supervisor_pid": os.getpid(),
            "supervisor_started_at": runtime.process_create_time(os.getpid()),
        },
    )


@pytest.mark.parametrize(
    ("stopping", "owner", "restart_job", "serving"),
    [
        pytest.param(True, True, False, False, id="service-stopping"),
        pytest.param(False, False, False, False, id="no-longer-the-service"),
        pytest.param(False, True, True, False, id="restart-job-owns-the-ui"),
        pytest.param(False, True, False, True, id="port-already-served"),
    ],
)
def test_no_ui_is_started_where_it_would_fight_another_owner(
    monkeypatch, host, stopping, owner, restart_job, serving
) -> None:
    monkeypatch.setattr(runtime, "current_process_owns_service_instance", lambda: owner)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == os.getpid() or pid in host.commands)
    if restart_job:
        _restart_job_running()
    host.serving = serving
    watchdog = WebUiWatchdog(stopping=lambda: stopping)

    for tick in range(4):
        assert watchdog.check(tick * CHECK_INTERVAL_SECONDS) is None

    assert host.starts == []


def test_a_ui_that_keeps_dying_is_retried_less_often_until_it_stays_up(host) -> None:
    watchdog = WebUiWatchdog(stopping=lambda: False)

    def tick(now: float) -> int | None:
        started = watchdog.check(now)
        if started is not None:
            # Each replacement dies before the next check sees it.
            host.kill(started)
        return started

    started = [now for now in range(0, 200, 10) if tick(float(now)) is not None]
    assert started == [10, 30, 70, 150]

    host.commands[999] = UI_COMMAND
    paths.get_runtime_ui_pid_path().write_text("999", encoding="utf-8")
    assert watchdog.check(200.0) is None
    host.kill(999)
    watchdog.check(210.0)
    assert watchdog.check(220.0) is not None
