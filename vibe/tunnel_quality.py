"""Cloudflare Tunnel metrics parsing and shared quality evaluation."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import statistics
import time
from typing import Any

import requests
from prometheus_client.parser import text_string_to_metric_families


SAMPLE_INTERVAL_SECONDS = 15
RATE_WINDOW_SECONDS = 60
BASELINE_MINUTE_SAMPLES = 15
BASELINE_RETENTION_SAMPLES = 24 * 60
CANDIDATE_MIN_SAMPLES = 4


def utc_timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class MetricsSample:
    sampled_at: float
    ready: bool
    ha_connections: int
    edge_locations: tuple[str, ...]
    smoothed_rtt_ms: tuple[float, ...]
    request_errors_total: float
    packet_loss_total: float
    closed_connections_total: float
    timeout_packet_loss_by_connection: tuple[tuple[str, float], ...] = ()


def parse_metrics(text: str, *, ready: bool = True, now: float | None = None) -> MetricsSample:
    """Parse cloudflared Prometheus metrics without depending on sample order."""

    values: dict[str, list[Any]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            values.setdefault(sample.name, []).append(sample)

    def scalar(*names: str) -> float:
        for name in names:
            if name in values:
                return sum(float(sample.value) for sample in values[name])
        return 0.0

    rtt_values = sorted(
        float(sample.value)
        for sample in values.get("quic_client_smoothed_rtt", [])
        if math.isfinite(float(sample.value)) and float(sample.value) >= 0
    )
    locations = sorted(
        {
            str(sample.labels.get("edge_location", "")).lower()
            for sample in values.get("cloudflared_tunnel_server_locations", [])
            if float(sample.value) > 0 and sample.labels.get("edge_location")
        }
    )
    timeout_losses: dict[str, float] = {}
    loss_samples = values.get("quic_client_lost_packets_total") or values.get("quic_client_lost_packets") or []
    for sample in loss_samples:
        connection = sample.labels.get("conn_index")
        reason = str(sample.labels.get("reason") or "").lower()
        if connection is None or reason != "timeout":
            continue
        connection_key = str(connection)
        timeout_losses[connection_key] = timeout_losses.get(connection_key, 0.0) + float(sample.value)
    return MetricsSample(
        sampled_at=now if now is not None else time.time(),
        ready=ready,
        ha_connections=max(0, int(round(scalar("cloudflared_tunnel_ha_connections")))),
        edge_locations=tuple(locations[:4]),
        smoothed_rtt_ms=tuple(rtt_values[:4]),
        request_errors_total=max(
            0.0,
            scalar("cloudflared_tunnel_request_errors_total", "cloudflared_tunnel_request_errors"),
        ),
        packet_loss_total=max(0.0, sum(timeout_losses.values())),
        closed_connections_total=max(
            0.0,
            scalar("quic_client_closed_connections_total", "quic_client_closed_connections"),
        ),
        timeout_packet_loss_by_connection=tuple(sorted(timeout_losses.items())),
    )


def scrape_metrics(metrics_url: str, *, timeout: float = 0.5, now: float | None = None) -> MetricsSample:
    base_url = metrics_url.rstrip("/")
    ready_response = requests.get(f"{base_url}/ready", timeout=timeout)
    metrics_response = requests.get(f"{base_url}/metrics", timeout=timeout)
    metrics_response.raise_for_status()
    return parse_metrics(metrics_response.text, ready=ready_response.ok, now=now)


def rtt_stats(values: tuple[float, ...] | list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 1),
        "median": round(float(statistics.median(values)), 1),
        "max": round(max(values), 1),
    }


def latency_grade(rtt: dict[str, float] | None) -> str:
    if rtt is None:
        return "unknown"
    median = rtt["median"]
    maximum = rtt["max"]
    if median < 120 and maximum < 250:
        return "good"
    if median < 200 and maximum < 400:
        return "fair"
    if median < 350 and maximum < 700:
        return "poor"
    return "critical"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 1)


class QualityEvaluator:
    """Stateful evaluator shared by status, Doctor, reporting, and recovery."""

    def __init__(self) -> None:
        self._counter_samples: deque[MetricsSample] = deque(maxlen=8)
        self._baseline_samples: deque[tuple[float, float]] = deque(maxlen=BASELINE_RETENTION_SAMPLES)
        self._last_baseline_minute: int | None = None
        self._last_snapshot: dict[str, Any] | None = None
        self._latency_bad_samples = 0
        self._partial_samples = 0
        self._error_windows = 0
        self._loss_windows = 0
        self._metrics_failures = 0
        self._healthy_samples = 0

    @property
    def last_snapshot(self) -> dict[str, Any] | None:
        return self._last_snapshot

    @property
    def healthy_samples(self) -> int:
        return self._healthy_samples

    def reset_healthy_samples(self) -> None:
        self._healthy_samples = 0

    def export_state(self) -> dict[str, Any]:
        return {
            "baseline_samples": [[timestamp, value] for timestamp, value in self._baseline_samples],
            "last_baseline_minute": self._last_baseline_minute,
        }

    def load_state(self, payload: dict[str, Any], *, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - 24 * 60 * 60
        samples = payload.get("baseline_samples")
        if not isinstance(samples, list):
            return
        restored: list[tuple[float, float]] = []
        for item in samples[-BASELINE_RETENTION_SAMPLES:]:
            if not isinstance(item, list) or len(item) != 2:
                continue
            try:
                timestamp = float(item[0])
                value = float(item[1])
            except (TypeError, ValueError):
                continue
            if timestamp >= cutoff and value >= 0 and math.isfinite(value):
                restored.append((timestamp, value))
        self._baseline_samples = deque(restored, maxlen=BASELINE_RETENTION_SAMPLES)
        last_minute = payload.get("last_baseline_minute")
        self._last_baseline_minute = int(last_minute) if isinstance(last_minute, int) else None

    def baseline(self, now: float | None = None) -> float | None:
        cutoff = (now if now is not None else time.time()) - 24 * 60 * 60
        while self._baseline_samples and self._baseline_samples[0][0] < cutoff:
            self._baseline_samples.popleft()
        if len(self._baseline_samples) < BASELINE_MINUTE_SAMPLES:
            return None
        return _percentile([value for _, value in self._baseline_samples], 0.20)

    def _rates(self, sample: MetricsSample) -> tuple[float, float]:
        self._counter_samples.append(sample)
        cutoff = sample.sampled_at - RATE_WINDOW_SECONDS
        base = next((item for item in self._counter_samples if item.sampled_at >= cutoff), self._counter_samples[0])
        elapsed = sample.sampled_at - base.sampled_at
        if elapsed <= 0:
            return 0.0, 0.0
        scale = 60.0 / elapsed
        errors = max(0.0, sample.request_errors_total - base.request_errors_total) * scale
        current_losses = dict(sample.timeout_packet_loss_by_connection)
        base_losses = dict(base.timeout_packet_loss_by_connection)
        connection_losses = [
            max(0.0, current_losses[connection] - base_losses[connection]) * scale
            for connection in current_losses.keys() & base_losses.keys()
        ]
        losses = sum(connection_losses) if sum(loss > 0 for loss in connection_losses) >= 2 else 0.0
        return round(errors, 2), round(losses, 2)

    def update(
        self,
        sample: MetricsSample | None,
        *,
        connector_count: int = 1,
        recovery: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = now if now is not None else (sample.sampled_at if sample else time.time())
        recovery_payload = recovery or empty_recovery()
        previous_state = str((self._last_snapshot or {}).get("state") or "unknown")

        if sample is None:
            self._metrics_failures += 1
            state = "unknown" if self._metrics_failures >= 3 else previous_state
            if state not in {"healthy", "degraded", "recovering"}:
                state = "unknown"
            previous_connections = int((self._last_snapshot or {}).get("ha_connections") or 0)
            previous_locations = list((self._last_snapshot or {}).get("edge_locations") or [])
            snapshot = {
                "schema_version": 1,
                "state": "recovering" if recovery_payload["state"] in {"evaluating", "draining"} else state,
                "grade": "unknown",
                "sampled_at": utc_timestamp(current_time),
                "protocol": "unknown",
                "connector_count": max(0, connector_count),
                "ha_connections": previous_connections,
                "rtt_ms": None,
                "baseline_median_rtt_ms": self.baseline(current_time),
                "edge_locations": previous_locations,
                "window_seconds": RATE_WINDOW_SECONDS,
                "request_errors_per_minute": 0.0,
                "packet_loss_per_minute": 0.0,
                "recovery": recovery_payload,
            }
            self._last_snapshot = snapshot
            return snapshot

        self._metrics_failures = 0
        rtt = rtt_stats(sample.smoothed_rtt_ms)
        grade = latency_grade(rtt)
        errors_per_minute, loss_per_minute = self._rates(sample)

        self._latency_bad_samples = self._latency_bad_samples + 1 if grade in {"poor", "critical"} else 0
        self._partial_samples = self._partial_samples + 1 if sample.ha_connections < 4 else 0
        self._error_windows = self._error_windows + 1 if errors_per_minute >= 3 else 0
        self._loss_windows = self._loss_windows + 1 if loss_per_minute >= 10 else 0

        degraded = (
            not sample.ready
            or sample.ha_connections == 0
            or self._partial_samples >= 4
            or self._error_windows >= 2
            or self._loss_windows >= 2
            or self._latency_bad_samples >= 12
        )
        baseline = self.baseline(current_time)
        recovery_state = recovery_payload["state"]
        if recovery_state in {"evaluating", "draining"}:
            state = "recovering"
        elif degraded:
            state = "degraded"
            self._healthy_samples = 0
        elif previous_state in {"degraded", "recovering"}:
            healthy_rtt = (
                rtt is None or rtt["median"] < max(160.0, 1.5 * baseline)
                if baseline is not None
                else grade in {"good", "fair", "unknown"}
            )
            self._healthy_samples = self._healthy_samples + 1 if sample.ha_connections == 4 and errors_per_minute < 3 and loss_per_minute < 10 and healthy_rtt else 0
            state = "healthy" if self._healthy_samples >= 20 else "degraded"
        else:
            state = "healthy"
            self._healthy_samples += 1

        minute = int(current_time // 60)
        if (
            state == "healthy"
            and grade in {"good", "fair"}
            and sample.ha_connections == 4
            and errors_per_minute < 3
            and loss_per_minute < 10
            and rtt is not None
            and minute != self._last_baseline_minute
        ):
            self._baseline_samples.append((current_time, rtt["median"]))
            self._last_baseline_minute = minute
            baseline = self.baseline(current_time)

        snapshot = {
            "schema_version": 1,
            "state": state,
            "grade": grade,
            "sampled_at": utc_timestamp(current_time),
            "protocol": "quic" if rtt is not None else "http2" if sample.ready else "unknown",
            "connector_count": max(0, connector_count),
            "ha_connections": min(4, sample.ha_connections),
            "rtt_ms": rtt,
            "baseline_median_rtt_ms": baseline,
            "edge_locations": list(sample.edge_locations),
            "window_seconds": RATE_WINDOW_SECONDS,
            "request_errors_per_minute": errors_per_minute,
            "packet_loss_per_minute": loss_per_minute,
            "recovery": recovery_payload,
        }
        self._last_snapshot = snapshot
        return snapshot

    def recovery_trigger(self, snapshot: dict[str, Any] | None = None) -> str | None:
        current = snapshot or self._last_snapshot
        if not current or current.get("state") != "degraded":
            return None
        if int(current.get("ha_connections") or 0) < 4:
            return "availability"
        if float(current.get("request_errors_per_minute") or 0) >= 3 or float(current.get("packet_loss_per_minute") or 0) >= 10:
            return "errors"
        rtt = current.get("rtt_ms")
        if not isinstance(rtt, dict):
            return None
        median = float(rtt.get("median") or 0)
        maximum = float(rtt.get("max") or 0)
        baseline = current.get("baseline_median_rtt_ms")
        if baseline is None:
            return "latency" if median >= 250 or maximum >= 500 else None
        baseline_value = float(baseline)
        if median >= 350 or (median >= 200 and median >= 2 * baseline_value):
            return "latency"
        if maximum >= 700 or (maximum >= 400 and maximum >= 3 * baseline_value):
            return "latency"
        return None


def empty_recovery() -> dict[str, Any]:
    return {
        "state": "idle",
        "last_attempt_at": None,
        "last_trigger": None,
        "last_result": None,
        "previous_median_rtt_ms": None,
        "result_median_rtt_ms": None,
        "next_attempt_at": None,
        "attempt_count_window": 0,
    }


def candidate_is_better(active: dict[str, Any], candidate: dict[str, Any], *, trigger: str) -> bool:
    if int(candidate.get("ha_connections") or 0) < 4:
        return False
    if float(candidate.get("request_errors_per_minute") or 0) > 0:
        return False
    if float(candidate.get("packet_loss_per_minute") or 0) > float(active.get("packet_loss_per_minute") or 0):
        return False
    if trigger == "availability" and int(active.get("ha_connections") or 0) < 4:
        return True
    active_rtt = active.get("rtt_ms")
    candidate_rtt = candidate.get("rtt_ms")
    if trigger == "errors":
        if (
            isinstance(active_rtt, dict)
            and isinstance(candidate_rtt, dict)
            and float(candidate_rtt.get("max") or 0) > float(active_rtt.get("max") or 0)
        ):
            return False
        return (
            float(candidate.get("request_errors_per_minute") or 0)
            < float(active.get("request_errors_per_minute") or 0)
            or float(candidate.get("packet_loss_per_minute") or 0)
            < float(active.get("packet_loss_per_minute") or 0)
        )
    if not isinstance(active_rtt, dict) or not isinstance(candidate_rtt, dict):
        return False
    active_median = float(active_rtt.get("median") or 0)
    candidate_median = float(candidate_rtt.get("median") or 0)
    active_maximum = float(active_rtt.get("max") or 0)
    candidate_maximum = float(candidate_rtt.get("max") or 0)
    improvement = active_median - candidate_median
    return improvement >= 30 and candidate_median <= active_median * 0.75 and candidate_maximum <= active_maximum


def summarize_candidate_snapshots(snapshots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Conservatively summarize a candidate's evaluation window."""

    if len(snapshots) < CANDIDATE_MIN_SAMPLES:
        return None
    summary = dict(snapshots[-1])
    summary["ha_connections"] = min(int(snapshot.get("ha_connections") or 0) for snapshot in snapshots)
    summary["request_errors_per_minute"] = max(
        float(snapshot.get("request_errors_per_minute") or 0) for snapshot in snapshots
    )
    summary["packet_loss_per_minute"] = max(
        float(snapshot.get("packet_loss_per_minute") or 0) for snapshot in snapshots
    )
    rtt_samples = [snapshot.get("rtt_ms") for snapshot in snapshots]
    if all(isinstance(rtt, dict) for rtt in rtt_samples):
        typed_rtt_samples = [rtt for rtt in rtt_samples if isinstance(rtt, dict)]
        summary["rtt_ms"] = {
            "min": round(float(statistics.median(float(rtt["min"]) for rtt in typed_rtt_samples)), 1),
            "median": round(float(statistics.median(float(rtt["median"]) for rtt in typed_rtt_samples)), 1),
            "max": round(max(float(rtt["max"]) for rtt in typed_rtt_samples), 1),
        }
    else:
        summary["rtt_ms"] = None
    return summary
