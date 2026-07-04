from core.bind_security import BindAttemptLimiter


def test_bind_attempt_limiter_applies_backoff_lock_and_reset() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    limiter = BindAttemptLimiter(
        clock=clock,
        free_failures=1,
        backoff_base_seconds=10,
        backoff_max_seconds=30,
        lock_failures=4,
        lock_seconds=120,
    )
    key = {"platform": "slack", "user_id": "U1", "channel_id": "D1"}

    assert limiter.check(**key).allowed is True
    assert limiter.record_failure(**key).allowed is True

    decision = limiter.record_failure(**key)
    assert decision.allowed is False
    assert decision.retry_after_seconds == 10
    assert limiter.check(**key).retry_after_seconds == 10

    now += 10
    decision = limiter.record_failure(**key)
    assert decision.allowed is False
    assert decision.retry_after_seconds == 20

    now += 20
    decision = limiter.record_failure(**key)
    assert decision.allowed is False
    assert decision.retry_after_seconds == 120

    limiter.reset(**key)
    assert limiter.check(**key).allowed is True
