from __future__ import annotations

from vibe import tunnel_quality


def _sample(
    now: float,
    rtts: tuple[float, ...],
    *,
    connections: int = 4,
    timeout_losses: tuple[tuple[str, float], ...] = (),
) -> tunnel_quality.MetricsSample:
    return tunnel_quality.MetricsSample(
        sampled_at=now,
        ready=connections > 0,
        ha_connections=connections,
        edge_locations=("sin01", "sin02"),
        smoothed_rtt_ms=rtts,
        request_errors_total=0,
        packet_loss_total=sum(value for _, value in timeout_losses),
        closed_connections_total=0,
        timeout_packet_loss_by_connection=timeout_losses,
    )


def test_ra_tq_001_parse_cloudflared_metrics_extracts_quality_inputs() -> None:
    sample = tunnel_quality.parse_metrics(
        """
# TYPE cloudflared_tunnel_ha_connections gauge
cloudflared_tunnel_ha_connections 4
# TYPE cloudflared_tunnel_request_errors counter
cloudflared_tunnel_request_errors 42
cloudflared_tunnel_server_locations{connection_id="0",edge_location="sin12"} 1
cloudflared_tunnel_server_locations{connection_id="1",edge_location="sin20"} 1
quic_client_smoothed_rtt{conn_index="0"} 67
quic_client_smoothed_rtt{conn_index="1"} 82
# TYPE quic_client_lost_packets counter
quic_client_lost_packets{conn_index="0",reason="timeout"} 3
quic_client_lost_packets{conn_index="1",reason="timeout"} 5
quic_client_lost_packets{conn_index="2",reason="reordering"} 100
# TYPE quic_client_closed_connections counter
quic_client_closed_connections 2
""",
        now=100,
    )

    assert sample.ha_connections == 4
    assert sample.edge_locations == ("sin12", "sin20")
    assert sample.smoothed_rtt_ms == (67, 82)
    assert sample.request_errors_total == 42
    assert sample.packet_loss_total == 8
    assert sample.timeout_packet_loss_by_connection == (("0", 3), ("1", 5))
    assert sample.closed_connections_total == 2


def test_latency_grade_uses_median_and_worst_active_path() -> None:
    assert tunnel_quality.latency_grade({"min": 60, "median": 119, "max": 249}) == "good"
    assert tunnel_quality.latency_grade({"min": 60, "median": 120, "max": 249}) == "fair"
    assert tunnel_quality.latency_grade({"min": 60, "median": 190, "max": 400}) == "poor"
    assert tunnel_quality.latency_grade({"min": 60, "median": 349, "max": 700}) == "critical"
    assert tunnel_quality.latency_grade(None) == "unknown"


def test_packet_loss_rate_requires_timeout_growth_on_two_connections() -> None:
    evaluator = tunnel_quality.QualityEvaluator()
    evaluator.update(_sample(100, (70, 75, 80, 85), timeout_losses=(("0", 0), ("1", 0))))

    isolated = evaluator.update(_sample(160, (70, 75, 80, 85), timeout_losses=(("0", 12), ("1", 0))))

    assert isolated["packet_loss_per_minute"] == 0

    evaluator = tunnel_quality.QualityEvaluator()
    evaluator.update(_sample(200, (70, 75, 80, 85), timeout_losses=(("0", 0), ("1", 0))))
    distributed = evaluator.update(
        _sample(260, (70, 75, 80, 85), timeout_losses=(("0", 6), ("1", 5)))
    )

    assert distributed["packet_loss_per_minute"] == 11


def test_ra_tq_002_high_rtt_requires_three_minutes_before_recovery() -> None:
    evaluator = tunnel_quality.QualityEvaluator()
    now = 10_000.0
    for minute in range(15):
        snapshot = evaluator.update(_sample(now + minute * 60, (70, 75, 80, 85)))
    assert snapshot["baseline_median_rtt_ms"] == 77.5

    start = now + 15 * 60
    for index in range(11):
        snapshot = evaluator.update(_sample(start + index * 15, (210, 240, 260, 410)))
        assert snapshot["state"] == "healthy"

    snapshot = evaluator.update(_sample(start + 11 * 15, (210, 240, 260, 410)))
    assert snapshot["state"] == "degraded"
    assert snapshot["grade"] == "poor"
    assert evaluator.recovery_trigger(snapshot) == "latency"


def test_zero_ready_connections_degrades_immediately() -> None:
    evaluator = tunnel_quality.QualityEvaluator()

    snapshot = evaluator.update(_sample(100, (), connections=0))

    assert snapshot["state"] == "degraded"
    assert evaluator.recovery_trigger(snapshot) == "availability"


def test_partial_availability_triggers_recovery_after_four_samples() -> None:
    evaluator = tunnel_quality.QualityEvaluator()

    for index in range(3):
        snapshot = evaluator.update(_sample(100 + index * 15, (70, 75, 80), connections=3))
        assert snapshot["state"] == "healthy"

    snapshot = evaluator.update(_sample(145, (70, 75, 80), connections=3))

    assert snapshot["state"] == "degraded"
    assert evaluator.recovery_trigger(snapshot) == "availability"


def test_ra_tq_003_metrics_failure_becomes_unknown_after_45_seconds() -> None:
    evaluator = tunnel_quality.QualityEvaluator()
    evaluator.update(_sample(100, (70, 75, 80, 85)))

    assert evaluator.update(None, now=115)["state"] == "healthy"
    assert evaluator.update(None, now=130)["state"] == "healthy"
    assert evaluator.update(None, now=145)["state"] == "unknown"


def test_candidate_requires_material_absolute_and_relative_improvement() -> None:
    active = {
        "ha_connections": 4,
        "rtt_ms": {"median": 250, "max": 420},
        "request_errors_per_minute": 0,
        "packet_loss_per_minute": 0,
    }
    better = {
        "ha_connections": 4,
        "rtt_ms": {"median": 180, "max": 260},
        "request_errors_per_minute": 0,
        "packet_loss_per_minute": 0,
    }
    marginal = {**better, "rtt_ms": {"median": 215, "max": 300}}
    worse_tail = {**better, "rtt_ms": {"median": 180, "max": 430}}
    with_errors = {**better, "request_errors_per_minute": 1}

    assert tunnel_quality.candidate_is_better(active, better, trigger="latency") is True
    assert tunnel_quality.candidate_is_better(active, marginal, trigger="latency") is False
    assert tunnel_quality.candidate_is_better(active, worse_tail, trigger="latency") is False
    assert tunnel_quality.candidate_is_better(active, with_errors, trigger="latency") is False


def test_error_candidate_requires_error_or_loss_improvement_not_rtt_improvement() -> None:
    request_error_active = {
        "ha_connections": 4,
        "rtt_ms": {"median": 150, "max": 240},
        "request_errors_per_minute": 3,
        "packet_loss_per_minute": 0,
    }
    healthy_candidate = {
        "ha_connections": 4,
        "rtt_ms": {"median": 150, "max": 240},
        "request_errors_per_minute": 0,
        "packet_loss_per_minute": 0,
    }
    packet_loss_active = {**request_error_active, "request_errors_per_minute": 0, "packet_loss_per_minute": 10}
    reduced_loss_candidate = {**healthy_candidate, "packet_loss_per_minute": 4}
    worse_tail_candidate = {**healthy_candidate, "rtt_ms": {"median": 150, "max": 300}}

    assert tunnel_quality.candidate_is_better(request_error_active, healthy_candidate, trigger="errors") is True
    assert tunnel_quality.candidate_is_better(packet_loss_active, reduced_loss_candidate, trigger="errors") is True
    assert tunnel_quality.candidate_is_better(packet_loss_active, packet_loss_active, trigger="errors") is False
    assert tunnel_quality.candidate_is_better(request_error_active, worse_tail_candidate, trigger="errors") is False


def test_availability_candidate_restores_partial_connection_set_without_rtt() -> None:
    active = {
        "ha_connections": 3,
        "rtt_ms": None,
        "request_errors_per_minute": 0,
        "packet_loss_per_minute": 0,
    }
    candidate = {**active, "ha_connections": 4}

    assert tunnel_quality.candidate_is_better(active, candidate, trigger="availability") is True


def test_candidate_summary_requires_four_samples_and_keeps_worst_tail() -> None:
    snapshots = [
        {
            "ha_connections": 4,
            "rtt_ms": {"min": median - 20, "median": median, "max": maximum},
            "request_errors_per_minute": errors,
            "packet_loss_per_minute": loss,
        }
        for median, maximum, errors, loss in [
            (100, 140, 0, 0),
            (110, 150, 0, 1),
            (120, 210, 0, 0),
            (130, 170, 0, 0),
        ]
    ]

    assert tunnel_quality.summarize_candidate_snapshots(snapshots[:3]) is None
    summary = tunnel_quality.summarize_candidate_snapshots(snapshots)

    assert summary is not None
    assert summary["rtt_ms"] == {"min": 95.0, "median": 115.0, "max": 210.0}
    assert summary["ha_connections"] == 4
    assert summary["request_errors_per_minute"] == 0
    assert summary["packet_loss_per_minute"] == 1


def test_baseline_state_round_trips_without_raw_samples() -> None:
    evaluator = tunnel_quality.QualityEvaluator()
    for minute in range(15):
        evaluator.update(_sample(10_000 + minute * 60, (70, 75, 80, 85)))

    restored = tunnel_quality.QualityEvaluator()
    restored.load_state(evaluator.export_state(), now=10_000 + 15 * 60)

    assert restored.baseline(10_000 + 15 * 60) == 77.5
