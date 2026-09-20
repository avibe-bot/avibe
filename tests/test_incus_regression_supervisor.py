from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import paths
from scripts import incus_regression
from vibe import runtime

# The supervisor lives in scripts/ (not an installed package); load it the same
# way test_incus_regression.py loads its sibling script.
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "incus_regression_supervisor.py"
SPEC = importlib.util.spec_from_file_location("incus_regression_supervisor", SCRIPT_PATH)
supervisor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


def _write_restart_status(status: dict) -> None:
    path = runtime.get_restart_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json(path, status)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("github", "archive"),
        ("github-source", "archive"),
        ("archive", "archive"),
        ("manifest-cache", "manifest-cache"),
        ("npm", "npm"),
    ],
)
def test_supervisor_normalizes_preserved_runtime_source_without_touching_other_env(
    monkeypatch,
    configured,
    expected,
):
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_SOURCE", configured)
    monkeypatch.delenv("VIBE_SHOW_RUNTIME_ARCHIVE_PATH", raising=False)
    monkeypatch.setenv("REGRESSION_UNRELATED_SETTING", "preserved")

    supervisor._normalize_show_runtime_source_environment()

    assert os.environ["VIBE_SHOW_RUNTIME_SOURCE"] == expected
    if expected == "archive":
        assert os.environ["VIBE_SHOW_RUNTIME_ARCHIVE_PATH"] == (
            "/home/avibe/.cache/avibe-regression/vibe-show-runtime-node.tgz"
        )
    else:
        assert "VIBE_SHOW_RUNTIME_ARCHIVE_PATH" not in os.environ
    assert os.environ["REGRESSION_UNRELATED_SETTING"] == "preserved"


def test_preserved_custom_archive_path_cannot_split_build_and_supervisor(
    monkeypatch,
):
    custom_path = "/srv/custom/show-runtime.tgz"
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_SOURCE", "archive")
    monkeypatch.setenv("VIBE_SHOW_RUNTIME_ARCHIVE_PATH", custom_path)
    monkeypatch.setenv("REGRESSION_SHOW_RUNTIME_ARCHIVE_PATH", custom_path)

    supervisor._normalize_show_runtime_source_environment()
    supervisor_path = os.environ["VIBE_SHOW_RUNTIME_ARCHIVE_PATH"]

    commands = []

    class RecordingRunner:
        def run(self, command, **kwargs):
            joined = " ".join(command)
            commands.append(joined)
            if 'printf "%s" "${VIBE_SHOW_RUNTIME_SOURCE:-}"' in joined:
                return subprocess.CompletedProcess(command, 0, stdout="archive")
            return subprocess.CompletedProcess(command, 0)

    target = incus_regression.RegressionTarget(
        target="master",
        slug="master",
        project="avr-master",
        instance="avibe-master",
        host_port=15130,
        ui_host="127.0.0.1",
        ui_port=5123,
    )

    incus_regression.prepare_show_runtime(RecordingRunner(), target, remote=None)

    build_command = next(command for command in commands if "git fetch --depth 1" in command)
    assert custom_path not in build_command
    assert f"export VIBE_SHOW_RUNTIME_ARCHIVE_PATH={supervisor_path}" in build_command


def test_restart_in_progress_true_while_job_pid_alive(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242, "supervisor_started_at": 1000.0})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(runtime, "process_create_time", lambda pid: 1000.0)

    assert supervisor._restart_in_progress() is True


def test_restart_in_progress_false_when_pid_reused(monkeypatch, tmp_path):
    # Pid is alive but its start time no longer matches what the job recorded —
    # the pid was reused (e.g. after a reboot) by an unrelated process, so the
    # restart is not actually in progress and recovery must proceed.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242, "supervisor_started_at": 1000.0})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(runtime, "process_create_time", lambda pid: 9999.0)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_when_job_pid_dead(monkeypatch, tmp_path):
    # The P2: a killed restart job or a reboot leaves ok=None + state=running with
    # a now-dead pid. The supervisor must treat it as stale, not in progress, so
    # it can exit nonzero and let systemd recover instead of looping "restarting".
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: False)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_without_recorded_pid(monkeypatch, tmp_path):
    # An older "running" status with no job pid can't be confirmed alive → stale.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "running"})

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_for_scheduled_restart(monkeypatch, tmp_path):
    # A delayed restart is only sleeping ("scheduled") and hasn't stopped the
    # service yet, so a crash during the delay must still be recovered — even
    # though the job process is alive.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": None, "state": "scheduled", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)

    assert supervisor._restart_in_progress() is False


def test_restart_in_progress_false_when_completed(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    _write_restart_status({"ok": True, "state": "succeeded", "supervisor_pid": 4242})
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: True)

    assert supervisor._restart_in_progress() is False


def test_main_recovers_when_restart_leaves_unready_service(monkeypatch, tmp_path):
    # Codex P2: after a restart writes a new service pid that hangs (alive but
    # never acquires the lock) and the restart job then fails, the supervisor must
    # not adopt the unready pid and loop forever — it must exit nonzero so systemd
    # recovers the service. Old ready pid 100 is dead; the file now points at the
    # hung pid 200 (alive, not recorded); the UI (333) is alive; no restart active.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("200", encoding="utf-8")
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: False)
    launch_secrets: dict[str, str | None] = {}

    def start_service(*, wait_for_ready=True, memory_ui_secret=None):
        launch_secrets["service"] = memory_ui_secret
        return 100

    def start_ui(host, port, *, memory_ui_secret=None):
        launch_secrets["ui"] = memory_ui_secret
        return 333

    monkeypatch.setattr(runtime, "start_service", start_service)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", start_ui)
    # 100 was ready at startup; the hung 200 never records (no lock).
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 100)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {200, 333})
    monkeypatch.setattr(runtime, "stop_ui", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "stop_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    rc = supervisor.main()

    assert rc == 1
    assert runtime.read_status()["state"] == "error"
    assert launch_secrets["service"]
    assert launch_secrets["service"] == launch_secrets["ui"]


def test_main_backs_off_during_active_restart(monkeypatch, tmp_path):
    # Service is gone but a managed restart is in progress → the supervisor must
    # write "restarting" and keep waiting, never exit for systemd. Guards the
    # TOCTOU where the restart begins after the loop-top: the recovery branch
    # re-reads _restart_in_progress() before exiting.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: True)
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True, **kwargs: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port, **kwargs: 333)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: 100)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 100)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid == 333)  # service 100 dead, ui alive

    statuses: list[str] = []
    monkeypatch.setattr(runtime, "write_status", lambda state, *a, **k: statuses.append(state))

    class _Stop(Exception):
        pass

    def stop_on_sleep(_seconds):
        raise _Stop()

    monkeypatch.setattr(supervisor.time, "sleep", stop_on_sleep)

    with pytest.raises(_Stop):
        supervisor.main()

    assert "restarting" in statuses
    assert "error" not in statuses


def test_main_adopts_scoped_service_lock_holder(monkeypatch, tmp_path):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True, **kwargs: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port, **kwargs: 333)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 200)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {200, 333})

    waited: list[tuple[int, float]] = []

    def wait_for_ready(pid, timeout):
        waited.append((pid, timeout))
        return 200

    monkeypatch.setattr(runtime, "wait_for_service_ready", wait_for_ready)

    statuses: list[tuple[str, int | None, int | None]] = []
    monkeypatch.setattr(
        runtime,
        "write_status",
        lambda state, _message, service_pid=None, ui_pid=None: statuses.append((state, service_pid, ui_pid)),
    )

    class _Stop(Exception):
        pass

    def stop_on_sleep(_seconds):
        raise _Stop()

    monkeypatch.setattr(supervisor.time, "sleep", stop_on_sleep)

    with pytest.raises(_Stop):
        supervisor.main()

    assert waited == [(100, runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS)]
    assert statuses[0] == ("running", 200, 333)


def test_main_does_not_adopt_a_lock_holder_that_never_finished_starting(monkeypatch, tmp_path):
    # Round 8, finding 1. The failure this exists for: a restart spawns a new
    # service, that process takes the instance lock and then hangs inside its own
    # migration, and the restart job dies. Holding the lock makes
    # `service_pid_recorded` true, so adopting on that alone tracks the hung
    # generation as this supervisor's healthy service -- it loops forever, never
    # exits nonzero, and the unit's `Restart=on-failure` never fires. Readiness is
    # the question, and `service_instance_started` is the only thing that answers
    # it.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("200", encoding="utf-8")
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: False)
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True, **kwargs: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port, **kwargs: 333)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: 100)
    # 200 is alive and holds the lock -- and has not finished starting. 100, the
    # pid this supervisor started and had ready, is gone.
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 200)
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: False)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {200, 333})
    monkeypatch.setattr(runtime, "stop_ui", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "stop_service", lambda *args, **kwargs: None)
    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: None)

    assert supervisor._adoptable_service_pid(200) is False
    rc = supervisor.main()

    assert rc == 1
    assert runtime.read_status()["state"] == "error"


def test_main_adopts_the_replacement_once_it_finishes_starting(monkeypatch, tmp_path):
    # The other half of the same property, and the reason it is a deferral rather
    # than a refusal: the same pid, same lock, one iteration later, now reporting
    # that it finished starting -- and the supervisor tracks it instead of exiting
    # for systemd. A test that only asserted the refusal above would pass just as
    # well against a supervisor that never adopts anything.
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    paths.ensure_data_dirs()
    paths.get_runtime_pid_path().write_text("200", encoding="utf-8")
    paths.get_runtime_ui_pid_path().write_text("333", encoding="utf-8")

    monkeypatch.setattr(supervisor, "_config", lambda: SimpleNamespace(ui=SimpleNamespace(setup_port=8080)))
    monkeypatch.setattr(supervisor, "_reap_child", lambda pid: None)
    monkeypatch.setattr(supervisor, "_restart_in_progress", lambda: False)
    monkeypatch.setattr(runtime, "start_service", lambda wait_for_ready=True, **kwargs: 100)
    monkeypatch.setattr(runtime, "effective_ui_bind_host", lambda config: "127.0.0.1")
    monkeypatch.setattr(runtime, "start_ui", lambda host, port, **kwargs: 333)
    monkeypatch.setattr(runtime, "wait_for_service_ready", lambda pid, timeout: 100)
    monkeypatch.setattr(runtime, "service_pid_recorded", lambda pid: pid == 200)
    monkeypatch.setattr(runtime, "service_instance_started", lambda pid: pid == 200)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in {200, 333})
    monkeypatch.setattr(runtime, "stop_ui", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "stop_service", lambda *args, **kwargs: None)

    statuses: list[str] = []
    monkeypatch.setattr(runtime, "write_status", lambda state, *a, **k: statuses.append(state))

    class _Stop(Exception):
        pass

    monkeypatch.setattr(supervisor.time, "sleep", lambda _seconds: (_ for _ in ()).throw(_Stop()))

    assert supervisor._adoptable_service_pid(200) is True
    with pytest.raises(_Stop):
        supervisor.main()

    assert "error" not in statuses
