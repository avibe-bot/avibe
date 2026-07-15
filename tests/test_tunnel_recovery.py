from __future__ import annotations

import json
import time

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

    class ReadyResponse:
        ok = True

    monkeypatch.setattr(remote_access.requests, "get", lambda *args, **kwargs: ReadyResponse())

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
    assert state["draining"] is None
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


def test_optimize_route_reserves_single_candidate_atomically(monkeypatch, tmp_path) -> None:
    started = []
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(remote_access, "status", lambda config=None: {"running": True})
    monkeypatch.setattr(remote_access, "_fresh_active_comparison_snapshot", lambda *args, **kwargs: _quality(250))
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)
    monkeypatch.setattr(remote_access.threading, "Thread", FakeThread)
    with remote_access._RECOVERY_LOCK:
        remote_access._RECOVERY_THREAD = None
        remote_access._RECOVERY_STATE.clear()
        remote_access._RECOVERY_STATE.update(remote_access.tunnel_quality.empty_recovery())
        remote_access._RECOVERY_MANUAL_BYPASS_USED = False

    try:
        first = remote_access.optimize_route(trigger="manual")
        second = remote_access.optimize_route(trigger="manual")
    finally:
        with remote_access._RECOVERY_LOCK:
            remote_access._RECOVERY_THREAD = None

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "route_optimization_unavailable"
    assert len(started) == 1


def test_zero_connection_recovery_bypasses_attempt_budget_once(monkeypatch) -> None:
    now = time.time()
    first_thread = object()
    second_thread = object()
    with remote_access._RECOVERY_LOCK:
        remote_access._RECOVERY_THREAD = None
        remote_access._RECOVERY_ATTEMPTS[:] = [now - 10, now - 5]
        remote_access._RECOVERY_STATE["next_attempt_at"] = remote_access.tunnel_quality.utc_timestamp(now + 900)
        remote_access._RECOVERY_EMERGENCY_BYPASS_USED = False

    try:
        first = remote_access._reserve_recovery(first_thread, manual=False, emergency=True, now=now)
        with remote_access._RECOVERY_LOCK:
            remote_access._RECOVERY_THREAD = None
        second = remote_access._reserve_recovery(second_thread, manual=False, emergency=True, now=now)
    finally:
        with remote_access._RECOVERY_LOCK:
            remote_access._RECOVERY_THREAD = None
            remote_access._RECOVERY_ATTEMPTS.clear()
            remote_access._RECOVERY_STATE.update(remote_access.tunnel_quality.empty_recovery())
            remote_access._RECOVERY_EMERGENCY_BYPASS_USED = False

    assert first is True
    assert second is False


def test_optimize_route_does_not_start_without_fresh_active_sample(monkeypatch) -> None:
    monkeypatch.setattr(remote_access, "status", lambda config=None: {"running": True})
    monkeypatch.setattr(remote_access, "_fresh_active_comparison_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        remote_access.threading,
        "Thread",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("candidate thread must not start")),
    )

    result = remote_access.optimize_route(trigger="manual")

    assert result["ok"] is False
    assert result["error"] == "route_optimization_unavailable"


def test_manual_optimization_uses_active_availability_trigger(monkeypatch) -> None:
    now = time.time()
    quality = {
        **_quality(250),
        "state": "degraded",
        "ha_connections": 3,
        "sampled_at": remote_access.tunnel_quality.utc_timestamp(now),
        "rtt_ms": None,
    }
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self)

    monkeypatch.setattr(remote_access, "status", lambda config=None: {"running": True, "tunnel_quality": quality})
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)
    monkeypatch.setattr(remote_access.threading, "Thread", FakeThread)
    with remote_access._RECOVERY_LOCK:
        remote_access._RECOVERY_THREAD = None
        remote_access._RECOVERY_STATE.clear()
        remote_access._RECOVERY_STATE.update(remote_access.tunnel_quality.empty_recovery())

    try:
        result = remote_access.optimize_route(trigger="manual")
    finally:
        with remote_access._RECOVERY_LOCK:
            remote_access._RECOVERY_THREAD = None

    assert result["ok"] is True
    assert len(started) == 1
    assert started[0].kwargs["args"][1] == "availability"


def test_error_recovery_accepts_fresh_http2_snapshot_without_rtt() -> None:
    now = time.time()
    quality = {
        **_quality(250),
        "state": "degraded",
        "sampled_at": remote_access.tunnel_quality.utc_timestamp(now),
        "rtt_ms": None,
        "request_errors_per_minute": 3,
    }

    refreshed = remote_access._fresh_active_comparison_snapshot(
        {"tunnel_quality": quality},
        trigger="errors",
        now=now,
    )

    assert refreshed == quality


def test_stale_active_snapshot_is_refreshed_before_comparison(monkeypatch, tmp_path) -> None:
    _setup_recovery(monkeypatch, tmp_path, _quality(180))
    now = time.time()
    stale = {
        **_quality(250),
        "sampled_at": remote_access.tunnel_quality.utc_timestamp(
            now - remote_access.QUALITY_COMPARISON_MAX_AGE_SECONDS - 1
        ),
    }
    sample = remote_access.tunnel_quality.MetricsSample(
        sampled_at=now,
        ready=True,
        ha_connections=4,
        edge_locations=("sin01", "sin02"),
        smoothed_rtt_ms=(70, 80, 90, 100),
        request_errors_total=0,
        packet_loss_total=0,
        closed_connections_total=0,
    )
    monkeypatch.setattr(remote_access.tunnel_quality, "scrape_metrics", lambda url: sample)

    refreshed = remote_access._fresh_active_comparison_snapshot(
        {"tunnel_quality": stale},
        trigger="manual",
        now=now,
    )

    assert refreshed is not None
    assert refreshed["rtt_ms"]["median"] == 85.0


def test_candidate_exit_before_promotion_keeps_active(monkeypatch, tmp_path) -> None:
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    results = []

    def candidate_exits(_url):
        alive.discard(candidate_pid)
        return _quality(180)

    monkeypatch.setattr(remote_access, "_candidate_average_snapshot", candidate_exits)
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: results.append(result))
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=8: alive.discard(pid) is None)

    remote_access._run_route_optimization(config, "latency")

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == active_pid
    assert state["active"]["pid"] == active_pid
    assert state["candidate"] is None
    assert results[0]["result"] == "failed"


def test_active_exit_during_evaluation_promotes_stable_candidate(monkeypatch, tmp_path) -> None:
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(220))
    results = []

    def active_exits(_url):
        alive.discard(active_pid)
        return _quality(220)

    monkeypatch.setattr(remote_access, "_candidate_average_snapshot", active_exits)
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: results.append(result))

    remote_access._run_route_optimization(config, "latency")

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert remote_access._read_pid() == candidate_pid
    assert state["active"]["pid"] == candidate_pid
    assert state["draining"] is None
    assert results[0]["trigger"] == "availability"
    assert results[0]["result"] == "improved"


def test_failed_drain_remains_tracked_for_reconcile(monkeypatch, tmp_path) -> None:
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    results = []
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=8: False)
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: results.append(result))

    remote_access._run_route_optimization(config, "latency")

    state = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert state["active"]["pid"] == candidate_pid
    assert state["draining"]["pid"] == active_pid
    assert results[0]["result"] == "improved"

    def stop_on_reconcile(pid, timeout=8):
        alive.discard(pid)
        return True

    monkeypatch.setattr(runtime, "stop_pid", stop_on_reconcile)
    remote_access._reconcile_draining_connector()
    reconciled = json.loads(remote_access._state_path().read_text(encoding="utf-8"))
    assert reconciled["draining"] is None
    assert active_pid not in alive


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


def test_restart_removes_candidate_recorded_only_in_pid_file(monkeypatch, tmp_path) -> None:
    _, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    remote_access._candidate_pid_path().write_text(str(candidate_pid), encoding="utf-8")
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
    assert not remote_access._candidate_pid_path().exists()
    assert stopped == [candidate_pid]


def test_restart_clears_candidate_pid_when_it_is_already_active(monkeypatch, tmp_path) -> None:
    _, active_pid, _, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    remote_access._candidate_pid_path().write_text(str(active_pid), encoding="utf-8")
    stopped = []
    recovery_results = []
    monkeypatch.setattr(runtime, "stop_pid", lambda pid, timeout=8: stopped.append(pid) or True)
    monkeypatch.setattr(remote_access, "_finish_reconciled_recovery", lambda result: recovery_results.append(result))

    remote_access._reconcile_orphan_candidate()

    assert active_pid in alive
    assert stopped == []
    assert not remote_access._candidate_pid_path().exists()
    assert recovery_results == ["improved"]


def test_startup_resets_inflight_recovery_without_connector(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path))
    with remote_access._RECOVERY_LOCK:
        remote_access._RECOVERY_STATE.clear()
        remote_access._RECOVERY_STATE.update(
            {
                **remote_access.tunnel_quality.empty_recovery(),
                "state": "evaluating",
                "last_trigger": "errors",
            }
        )
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)

    remote_access._normalize_orphaned_recovery_state()

    recovery = remote_access._recovery_payload()
    assert recovery["state"] == "cooldown"
    assert recovery["last_result"] == "failed"
    assert recovery["last_trigger"] == "errors"
    assert recovery["next_attempt_at"] is not None


def test_recovery_completion_restarts_healthy_reset_window(monkeypatch) -> None:
    evaluator = remote_access.tunnel_quality.QualityEvaluator()
    for index in range(120):
        evaluator.update(
            remote_access.tunnel_quality.MetricsSample(
                sampled_at=100 + index * 15,
                ready=True,
                ha_connections=4,
                edge_locations=("sin01", "sin02"),
                smoothed_rtt_ms=(70, 75, 80, 85),
                request_errors_total=0,
                packet_loss_total=0,
                closed_connections_total=0,
            )
        )
    monkeypatch.setattr(remote_access, "_QUALITY_EVALUATOR", evaluator)
    monkeypatch.setattr(remote_access, "_set_recovery_state", lambda **changes: changes)
    monkeypatch.setattr(remote_access, "_report_runtime_status_async", lambda *args, **kwargs: None)

    remote_access._finish_recovery(
        trigger="manual",
        result="no_improvement",
        previous_median=77.5,
        result_median=77.5,
    )

    assert evaluator.healthy_samples == 0


def test_reconciled_recovery_uses_only_contract_trigger_values(monkeypatch) -> None:
    captured = []
    with remote_access._RECOVERY_LOCK:
        remote_access._RECOVERY_STATE.clear()
        remote_access._RECOVERY_STATE.update(remote_access.tunnel_quality.empty_recovery())
    monkeypatch.setattr(remote_access, "_finish_recovery", lambda **result: captured.append(result))

    remote_access._finish_reconciled_recovery("failed")

    assert captured[0]["trigger"] is None


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
