"""Interpret the bounded Claude header projection, never raw response headers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

from .adapter import RawCallOutcome, RawOutcomeKind


_MAX_RESET_WINDOW = timedelta(days=7, hours=1)
_PREFIX = "anthropic-ratelimit-unified-"
_SHARED_WINDOWS = ("5h", "7d")
_ALLOWED = {"allowed", "allowed_warning"}


def _reset_deadline(raw: str | None, received: datetime) -> datetime | None:
    """Accept future Unix/aware ISO/HTTP dates within the recoverable window."""

    if not isinstance(raw, str) or not raw or len(raw) > 128 or not raw.isascii():
        return None
    try:
        upper = received + _MAX_RESET_WINDOW
        try:
            numeric = Decimal(raw)
        except InvalidOperation:
            try:
                deadline = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                deadline = parsedate_to_datetime(raw)
        else:
            # Bound before float conversion: finite-looking exponents and epoch
            # milliseconds must not overflow or turn into a far-future cooldown.
            if not numeric.is_finite() or not Decimal(received.timestamp()) < numeric <= Decimal(upper.timestamp()):
                return None
            deadline = datetime.fromtimestamp(float(numeric), timezone.utc)
        if deadline.tzinfo is not None and received < deadline <= upper:
            return deadline.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def claude_subscription_reset(outcome: RawCallOutcome) -> datetime | None:
    """Return the latest valid active shared-cap reset, or no refinement.

    L1 already restricts these facts to Claude Sources and an exact header
    allow-list. Shared-window rejection is stronger than an aggregate hint;
    overage/7d_oi alone cannot establish a Source-wide subscription cooldown.
    Missing/ambiguous evidence keeps ordinary 429 policy, including Retry-After.
    """

    received = outcome.response_received_at
    if (
        outcome.kind != RawOutcomeKind.HTTP_ERROR or outcome.http_status != 429
        or received is None or received.tzinfo is None
    ):
        return None
    headers = dict(outcome.claude_rate_limit_headers)
    statuses = {window: headers.get(f"{_PREFIX}{window}-status") for window in _SHARED_WINDOWS}
    active = {window for window, status in statuses.items() if status == "rejected"}
    rejected = headers.get(f"{_PREFIX}status") == "rejected"
    claim = headers.get(f"{_PREFIX}representative-claim", "").lower()
    if rejected and claim in _SHARED_WINDOWS and statuses[claim] is None:
        active.add(claim)

    resets = [headers.get(f"{_PREFIX}{window}-reset") for window in active]
    non_shared = (
        claim not in ("", *_SHARED_WINDOWS)
        or headers.get(f"{_PREFIX}7d_oi-status") == "rejected"
        or headers.get(f"{_PREFIX}overage-status") == "rejected"
        or bool(headers.get(f"{_PREFIX}overage-disabled-reason"))
    )
    # An aggregate reset can describe a billing period rather than the shared
    # subscription. Use it only with shared evidence and no conflicting claim.
    aggregate_shared = bool(active) or (
        rejected and not all(status in _ALLOWED for status in statuses.values())
    )
    claim_allowed = claim in _SHARED_WINDOWS and statuses[claim] in _ALLOWED
    if aggregate_shared and not non_shared and not claim_allowed:
        resets.append(headers.get(f"{_PREFIX}reset"))

    deadlines = [deadline for raw in resets if (deadline := _reset_deadline(raw, received)) is not None]
    return max(deadlines) if deadlines else None
