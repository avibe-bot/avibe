from __future__ import annotations

import json

from tests.test_remote_access_vibe_cloud import _config
from vibe import remote_access, runtime


def _quality(median: float) -> dict:
    return {
        "ha_connections": 4,
        "rtt_ms": {"min": median - 20, "median": median, "max": median + 40},
        "request_errors_per_minute": 0,
        "packet_loss_per_minute": 0,
    }


def _setup_recovery(monkeypatch, tmp_path, candidate_quality: dict):
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    config = _config()
    config.remote_access.vibe_cloud.tunnel_token = "tunnel-token"
    config.save()
    binary = "/usr/local/bin/cloudflared"
    active_pid = 111
    candidate_pid = 222
    alive = {active_pid, candidate_pid}
    remote_access._RECOVERY_CANCEL_EVENT.clear()
    remote_access._pid_path().parent.mkdir(parents=True, exist_ok=True)
    remote_access._pid_path().write_text(str(active_pid), encoding="utf-8")
    remote_access._write_state(active_pid, config, binary, "http://127.0.0.1:29001")

    monkeypatch.setattr(remote_access, "tunnel_quality_snapshot", lambda: _quality(250))
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_access, "_resolve_binary", lambda loaded: binary)
    monkeypatch.setattr(remote_access, "_allocate_metrics_url", lambda: "http://127.0.0.1:29002")
    monkeypatch.setattr(remote_access, "_wait_candidate_ready", lambda pid, url: True)
    monkeypatch.setattr(remote_access, "_candidate_average_snapshot", lambda url: candidate_quality)
    monkeypatch.setattr(runtime, "pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(runtime, "get_process_command", lambda pid: f"{binary} tunnel run")

    def spawn_background(args, pid_path, stdout_name, stderr_name, env=None):
        pid_path.write_text(str(candidate_pid), encoding="utf-8")
        return candidate_pid

    monkeypatch.setattr(runtime, "spawn_background", spawn_background)
    return config, active_pid, candidate_pid, alive


def test_ra_tq_004_candidate_promotes_before_active_drains(monkeypatch, tmp_path) -> None:
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    drained = []
    results = []

    def stop_pid(pid, timeout=8):
        drained.append((pid, timeout, remote_access._read_pid()))
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: results.append(result))

    remote_access._run_route_optimization(config, "latency")

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == candidate_pid
    assert state["active"]["pid"] == candidate_pid
    assert state["candidate"] is None
    assert drained == [(active_pid, remote_access.RECOVERY_DRAIN_SECONDS, candidate_pid)]
    assert results[0]["result"] == "improved"


def test_ra_tq_005_non_improving_candidate_keeps_active(monkeypatch, tmp_path) -> None:
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(220))
    stopped = []
    results = []

    def stop_pid(pid, timeout=8):
        stopped.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: results.append(result))

    remote_access._run_route_optimization(config, "latency")

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == active_pid
    assert state["active"]["pid"] == active_pid
    assert state["candidate"] is None
    assert stopped == [candidate_pid]
    assert results[0]["result"] == "no_improvement"


def test_ra_tq_008_restart_removes_orphan_candidate(monkeypatch, tmp_path) -> None:
    _, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    state["candidate"] = remote_access._connector_record(
        candidate_pid,
        "http://127.0.0.1:29002",
        stdout_path=remote_access._candidate_cloudflared_stdout_path(),
        stderr_path=remote_access._candidate_cloudflared_stderr_path(),
    )
    runtime.write_json(remote_access._state_path(), state)
    stopped = []

    def stop_pid(pid, timeout=8):
        stopped.append(pid)
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "stop_pid", stop_pid)
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)

    remote_access._reconcile_orphan_candidate()

    reconciled = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == active_pid
    assert reconciled["active"]["pid"] == active_pid
    assert reconciled["candidate"] is None
    assert stopped == [candidate_pid]


def test_ra_tq_009_restart_promotes_ready_candidate(monkeypatch, tmp_path) -> None:
    _, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    state["candidate"] = remote_access._connector_record(
        candidate_pid,
        "http://127.0.0.1:29002",
        stdout_path=remote_access._candidate_cloudflared_stdout_path(),
        stderr_path=remote_access._candidate_cloudflared_stderr_path(),
    )
    runtime.write_json(remote_access._state_path(), state)
    alive.discard(active_pid)

    class ReadyResponse:
        ok = True

    monkeypatch.setattr(remote_access.requests, "get", lambda *args, **kwargs: ReadyResponse())
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)

    remote_access._reconcile_orphan_candidate()

    reconciled = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == candidate_pid
    assert reconciled["active"]["pid"] == candidate_pid
    assert reconciled["candidate"] is None
