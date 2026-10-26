"""Shared Claude subscription windows refine only ordinary rate-limit outcomes."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from core.handlers.model_hub.adapter import RawCallOutcome, RawOutcomeKind
from core.handlers.model_hub.classification import classify_outcome


RECEIVED = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
FIVE_HOURS = RECEIVED + timedelta(hours=5)
WEEK = RECEIVED + timedelta(days=7)


def cap_outcome(headers):
    return RawCallOutcome(
        kind=RawOutcomeKind.HTTP_ERROR, http_status=429, error_code=None,
        error_type="rate_limit_error", redacted_message=None, stream_started=False,
        source_id="src_claudecap", model_id="claude-model",
        response_received_at=RECEIVED,
        claude_rate_limit_headers=tuple(
            (f"anthropic-ratelimit-unified-{name}", value) for name, value in headers.items()
        ),
    )


@pytest.mark.parametrize("headers,expected", [
    ({"5h-status": "rejected", "5h-reset": str(FIVE_HOURS.timestamp())}, FIVE_HOURS),
    ({"7d-status": "rejected", "7d-reset": str(WEEK.timestamp())}, WEEK),
    ({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat(),
      "7d-status": "rejected", "7d-reset": format_datetime(WEEK)}, WEEK),
    ({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat(),
      "7d-status": "allowed_warning", "7d-reset": WEEK.isoformat()}, FIVE_HOURS),
    ({"status": "rejected", "representative-claim": "5h", "5h-reset": FIVE_HOURS.isoformat(),
      "7d-reset": WEEK.isoformat()}, FIVE_HOURS),
    ({"status": "rejected", "representative-claim": "7d", "7d-reset": WEEK.isoformat()}, WEEK),
    ({"status": "rejected", "reset": WEEK.isoformat()}, WEEK),
    ({"5h-status": "rejected", "5h-reset": "bad",
      "7d-status": "rejected", "7d-reset": WEEK.isoformat()}, WEEK),
    ({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat(),
      "status": "rejected", "reset": WEEK.isoformat()}, WEEK),
    ({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat(),
      "status": "rejected", "representative-claim": "overage", "reset": WEEK.isoformat(),
      "overage-reset": WEEK.isoformat()}, FIVE_HOURS),
    ({"7d-status": "rejected", "7d-reset": (WEEK + timedelta(hours=1)).isoformat()},
     WEEK + timedelta(hours=1)),
    ({"5h-status": "rejected", "5h-reset": FIVE_HOURS.astimezone(timezone(timedelta(hours=8))).isoformat()},
     FIVE_HOURS),
    ({"5h-status": "rejected", "5h-reset": str(FIVE_HOURS.timestamp() + .25)},
     FIVE_HOURS + timedelta(seconds=.25)),
])
def test_only_active_shared_resets_refine_a_generic_429(headers, expected):
    """Regression: generic rate_limit_error used to ignore all unified evidence."""
    decision = classify_outcome(cap_outcome(headers))
    assert (decision.action, decision.reason, decision.cooldown_seconds) == ("fallback", "quota_exhausted", 300)
    assert decision.retry_at == expected


@pytest.mark.parametrize("reset", [
    "", "garbage", "-1", "0", "NaN", "Infinity", "1e9999", "9" * 129,
    "１２３", RECEIVED.isoformat(), (RECEIVED - timedelta(seconds=1)).isoformat(),
    (WEEK + timedelta(hours=1, seconds=1)).isoformat(),
    str(int(FIVE_HOURS.timestamp()) * 1000), FIVE_HOURS.replace(tzinfo=None).isoformat(),
])
def test_invalid_or_out_of_window_reset_preserves_the_rate_limit_fallback(reset):
    decision = classify_outcome(cap_outcome({"5h-status": "rejected", "5h-reset": reset}))
    assert (decision.reason, decision.cooldown_seconds) == ("rate_limited", 60)
    assert decision.retry_at is None


@pytest.mark.parametrize("headers", [
    {}, {"5h-reset": FIVE_HOURS.isoformat()},
    {"5h-status": "rejected"},
    {"status": "rejected", "5h-reset": FIVE_HOURS.isoformat()},  # no active-window witness
    {"5h-utilization": "1.0", "5h-reset": FIVE_HOURS.isoformat()},
    {"5h-status": "allowed", "5h-reset": FIVE_HOURS.isoformat()},
    {"status": "allowed", "reset": WEEK.isoformat()},
    {"status": "rejected", "representative-claim": "5h", "5h-status": "allowed",
     "5h-reset": FIVE_HOURS.isoformat(), "reset": FIVE_HOURS.isoformat()},
    {"status": "rejected", "5h-status": "allowed", "7d-status": "allowed_warning", "reset": WEEK.isoformat()},
    {"status": "rejected", "representative-claim": "overage", "reset": WEEK.isoformat()},
    {"status": "rejected", "overage-status": "rejected", "reset": WEEK.isoformat()},
    {"status": "rejected", "overage-disabled-reason": "billing", "reset": WEEK.isoformat()},
    {"status": "rejected", "7d_oi-status": "rejected", "7d_oi-reset": WEEK.isoformat(), "reset": WEEK.isoformat()},
    {"status": "rejected", "representative-claim": "unknown", "reset": WEEK.isoformat()},
])
def test_ambiguous_or_non_shared_rejection_never_sets_a_subscription_cooldown(headers):
    decision = classify_outcome(cap_outcome(headers))
    assert decision.reason == "rate_limited"
    assert decision.retry_at is None


@pytest.mark.parametrize("changes,reason", [
    ({"http_status": 503, "error_type": "server_error"}, "server_error"),
    ({"http_status": 200, "kind": RawOutcomeKind.SUCCESS}, None),
    ({"error_code": "usage_not_included"}, "plan_excludes_product"),
    ({"error_code": "credit_balance_exhausted"}, "balance_exhausted"),
    ({"error_code": "organization_spend_limit_exceeded"}, "spend_limit_reached"),
    ({"response_received_at": None}, "rate_limited"),
    ({"response_received_at": RECEIVED.replace(tzinfo=None)}, "rate_limited"),
])
def test_shared_header_evidence_does_not_override_other_contracts(changes, reason):
    outcome = replace(cap_outcome({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat()}), **changes)
    decision = classify_outcome(outcome)
    assert decision.reason == reason
    assert decision.retry_at is None


@pytest.mark.parametrize("error_type", [None, "rate_limit_error", "rate_limit_exceeded", "invalid_request_error"])
@pytest.mark.parametrize("stream_started", [False, True])
def test_refinement_keeps_status_fallback_and_post_output_terminal_semantics(error_type, stream_started):
    outcome = replace(
        cap_outcome({"5h-status": "rejected", "5h-reset": FIVE_HOURS.isoformat()}),
        error_type=error_type, stream_started=stream_started,
    )
    decision = classify_outcome(outcome)
    assert decision.reason == "quota_exhausted"
    assert decision.retry_at == FIVE_HOURS
    assert decision.action == ("surface" if stream_started else "fallback")
    assert decision.error_code == ("stream_interrupted" if stream_started else None)
