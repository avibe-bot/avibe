"""Hermetic recovery timing: fake clocks, DNS and connector processes only."""
from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

import pytest

from tests.test_remote_access_vibe_cloud import _config
from tests.test_tunnel_recovery import _metrics_sample, _quality, _setup_recovery, _StopMonitor
from tests.test_tunnel_recovery import isolated_recovery as isolated_recovery
from vibe import remote_access, tunnel_quality


class Clock:
    def __init__(self):
        self.now = 2_000_000_000.0

    def monotonic(self):
        return self.now

    time = monotonic
    perf_counter = monotonic


class Response:
    status_code = 200

    def close(self):
        pass


def test_dns_budget_never_blocks_poll_or_accumulates_workers(monkeypatch, caplog):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    calls, closed = [], []

    def slow_dns(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return []

    class Session:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            socket.getaddrinfo("fake.invalid", 443)
            return Response()

        def close(self):
            closed.append(True)

    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)
    monkeypatch.setattr(tunnel_quality, "time", clock)
    probe = tunnel_quality.BackgroundRequestPathProbe(Session)
    caplog.set_level("INFO")
    assert probe.poll("active", "https://fake.invalid/health") is None
    worker = probe._thread
    try:
        assert entered.wait(1)
        clock.now += 10.49
        assert probe.poll("active", "https://fake.invalid/health") is None
        clock.now += 0.01
        sample = probe.poll("active", "https://fake.invalid/health")
        assert sample.successes == (False, False, False)
        assert "duration_seconds=10.500 result=timeout" in caplog.text
        for _ in range(10):
            clock.now += 15
            assert probe.poll("active", "https://fake.invalid/health") is None
            assert probe._thread is worker
        assert len(calls) == 1
        probe.close()  # Closing the monitor is nonblocking too.
        assert closed == []  # Never close a session in use by the DNS worker.
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert closed == [True]
    assert len(calls) == 1  # No second/third request after the deadline.
    assert probe.poll("active", None) is None  # Late success is discarded.


def _controlled_probe(monkeypatch, clock, statuses=(200, 503, 204)):
    workers, requests = [], []

    class Thread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args, self.alive = target, args, True
            workers.append(self)

        def start(self):
            pass

        def is_alive(self):
            return self.alive

        def complete(self):
            self.target(*self.args)
            self.alive = False

    class Session:
        def get(self, url, **kwargs):
            status = statuses[len(requests) % len(statuses)]
            requests.append((url, kwargs))
            clock.now += 0.1
            return SimpleNamespace(status_code=status, close=lambda: None)

        def close(self):
            pass

    monkeypatch.setattr(tunnel_quality, "time", clock)
    monkeypatch.setattr(tunnel_quality, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    return tunnel_quality.BackgroundRequestPathProbe(Session), workers, requests


def test_probe_preserves_target_attempts_and_2xx_criteria(monkeypatch, caplog):
    clock = Clock()
    probe, workers, requests = _controlled_probe(monkeypatch, clock)
    caplog.set_level("INFO")
    url = "https://fake.invalid/health"
    assert probe.poll((111, url), url) is None
    workers[0].complete()
    sample = probe.poll((111, url), url)
    assert sample.successes == (True, False, True)
    assert sample.latency_ms == (100, 100, 100)
    assert len(requests) == 3
    assert all(target == url and kwargs["allow_redirects"] is False for target, kwargs in requests)
    assert "duration_seconds=0.300 result=failed successes=2 attempts=3" in caplog.text
    # The next window reuses the same persistent session and sampling criteria.
    workers[1].complete()
    assert probe.poll((111, url), url).successes == (True, False, True)
    assert len(requests) == 6
    probe.close()


def test_probe_timeout_preserves_completed_successes(monkeypatch):
    clock = Clock()
    probe, workers, _ = _controlled_probe(monkeypatch, clock)
    url = "https://fake.invalid/health"
    probe.poll("active", url)
    # One successful attempt completed; the second remains in DNS at budget end.
    with probe._lock:
        probe._samples.append(tunnel_quality.RequestPathSample(clock.now, (100,), (True,)))
    clock.now += 10.5
    sample = probe.poll("active", url)
    assert sample.successes == (True, False, False)
    assert probe._thread is workers[0]
    probe.close()


@pytest.mark.parametrize("invalidate", ["connector", "disabled", "protocol", "hostname"])
def test_probe_discards_stale_and_aba_results(monkeypatch, invalidate):
    clock = Clock()
    probe, workers, _ = _controlled_probe(monkeypatch, clock)
    original = (111, "quic", "https://fake.invalid/health")
    probe.poll(original, original[2])
    changed = {
        "connector": (222, original[1], original[2]),
        "disabled": original,
        "protocol": (111, "http2", original[2]),
        "hostname": (111, "quic", "https://other.invalid/health"),
    }[invalidate]
    assert probe.poll(changed, None if invalidate == "disabled" else changed[2]) is None
    workers[0].complete()
    assert probe.poll(original, original[2]) is None


@pytest.mark.parametrize("trigger,state,clears", [
    ("availability", "cooldown", True),
    ("latency", "cooldown", False),
    ("availability", "evaluating", False),
    ("availability", "draining", False),
])
def test_success_clears_only_availability_cooldown(monkeypatch, caplog, trigger, state, clears):
    deadline = tunnel_quality.utc_timestamp(2_000_000_060)
    remote_access._RECOVERY_STATE.update(
        state=state, last_trigger=trigger, next_attempt_at=deadline, attempt_count_window=5,
    )
    before = remote_access._recovery_payload()
    caplog.set_level("INFO")
    remote_access._clear_availability_cooldown(source="public_health_probe")
    if clears:
        assert remote_access._RECOVERY_STATE["next_attempt_at"] is None
        assert remote_access._RECOVERY_STATE["attempt_count_window"] == 0
        assert f"previous_cooldown_deadline={deadline}" in caplog.text
        assert "source=public_health_probe" in caplog.text
    else:
        assert remote_access._recovery_payload() == before


@pytest.mark.parametrize("success", [True, False])
def test_runtime_status_response_is_existing_recovery_signal(monkeypatch, success):
    config = _config()
    config.remote_access.vibe_cloud.backend_url = "https://backend.test"
    config.remote_access.vibe_cloud.instance_secret = "fake-instance-secret"
    monkeypatch.setattr(remote_access, "runtime_status_payload", lambda *a, **kw: {})
    monkeypatch.setattr(remote_access, "_replace_active_hostnames", lambda *a: None)
    remote_access._RECOVERY_STATE.update(
        state="cooldown", last_trigger="availability", attempt_count_window=5,
        next_attempt_at=tunnel_quality.utc_timestamp(2_000_000_060),
    )

    def request(*args, **kwargs):
        if not success:
            raise OSError("fake offline")
        return {}

    monkeypatch.setattr(remote_access, "_json_request", request)
    result = remote_access.report_runtime_status(config)
    assert result["ok"] is success
    assert (remote_access._RECOVERY_STATE["next_attempt_at"] is None) is success


@pytest.mark.parametrize("manual", [True, False])
def test_recovery_finish_logs_mode_and_deadline(monkeypatch, caplog, manual):
    monkeypatch.setattr(remote_access, "_observe_quality_context", lambda *a, **kw: True)
    caplog.set_level("INFO")
    remote_access._finish_recovery(
        trigger="availability", result="failed", previous_median=None, result_median=None, manual=manual,
    )
    deadline = remote_access._RECOVERY_STATE["next_attempt_at"]
    assert f"mode={'manual' if manual else 'automatic'} trigger=availability result=failed cooldown_deadline={deadline}" in caplog.text


@pytest.mark.parametrize("manual", [True, False])
def test_recovery_start_logs_intent_even_for_availability(monkeypatch, caplog, manual):
    started = []

    class Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs)

    monkeypatch.setattr(remote_access, "threading", SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(remote_access, "status", lambda *a, **kw: {"running": True})
    monkeypatch.setattr(remote_access, "_fresh_active_comparison_snapshot", lambda *a, **kw: {"ha_connections": 0})
    caplog.set_level("INFO")
    result = remote_access.optimize_route(trigger="manual" if manual else "availability")
    assert result["ok"]
    assert started[0]["kwargs"]["manual"] is manual
    assert f"mode={'manual' if manual else 'automatic'} trigger=availability cooldown_deadline=None" in caplog.text


@pytest.mark.parametrize("signal", [None, "public_health_probe", "runtime_status"])
def test_network_return_starts_candidate_within_cap_plus_tick(monkeypatch, tmp_path, signal):
    """Drive the real monitor/admission/worker path; only external IO is fake."""
    real_set = remote_access._set_recovery_state
    config, active_pid, candidate_pid, alive = _setup_recovery(monkeypatch, tmp_path, _quality(180))
    monkeypatch.setattr(remote_access, "_set_recovery_state", real_set)
    clock = Clock()
    started, probes = [], []
    network_back = clock.now + 1
    remote_access._observe_quality_context("quic")
    remote_access._RECOVERY_STATE.update(last_trigger="availability", attempt_count_window=9)
    monkeypatch.setattr(remote_access, "time", clock)
    remote_access._finish_recovery(trigger="availability", result="failed", previous_median=None, result_median=None)

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def close(self):
            pass

        def poll(self, context, url):
            probes.append(clock.now)
            if clock.now >= network_back and signal == "public_health_probe":
                return tunnel_quality.RequestPathSample(clock.now, (10,), (True,))
            return None  # Includes a DNS worker that never finishes.

    class Thread:
        def __init__(self, target, args, kwargs, **other):
            self.target, self.args, self.kwargs = target, args, kwargs

        def start(self):
            self.target(*self.args, **self.kwargs)

    def candidate(*args):
        started.append(clock.now)
        raise RuntimeError("fake candidate reached; no process spawned")

    def sleep(seconds):
        clock.now += seconds
        if signal == "runtime_status" and clock.now >= network_back:
            remote_access._clear_availability_cooldown(source="runtime_status")
        if started or clock.now > network_back + 75:
            raise _StopMonitor

    clock.sleep = sleep
    monkeypatch.setattr(remote_access, "threading", SimpleNamespace(Thread=Thread, current_thread=threading.current_thread))
    monkeypatch.setattr(tunnel_quality, "BackgroundRequestPathProbe", Probe)
    monkeypatch.setattr(tunnel_quality, "scrape_metrics", lambda url: _metrics_sample(connections=0))
    monkeypatch.setattr(remote_access, "_effective_protocol", lambda *a: "quic")
    monkeypatch.setattr(remote_access, "_start_candidate_connector", candidate)
    monkeypatch.setattr(remote_access, "_fresh_active_comparison_snapshot", lambda *a, **kw: {**_quality(250), "ha_connections": 0})
    monkeypatch.setattr(remote_access, "status", lambda *a, **kw: {"running": True, "enabled": True})
    monkeypatch.setattr(remote_access, "_reconcile_connector_lifecycle", lambda: None)
    monkeypatch.setattr(remote_access, "_persist_quality_snapshot", lambda *a, **kw: True)
    with pytest.raises(_StopMonitor):
        remote_access._quality_monitor_loop(15, remote_access._quality_state_path())
    assert len(started) == 1
    assert started[0] - network_back <= (15 if signal else 60 + 15)
    assert len(probes) >= 2  # Monitor keeps ticking even with no probe result.
    assert alive == {active_pid, candidate_pid}
