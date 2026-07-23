"""Canonical, engine-independent Model Hub error classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .adapter import RawCallOutcome, RawOutcomeKind

ResolutionAction = Literal["return", "surface", "refresh", "fallback"]
ResolutionReason = Literal["quota_exhausted", "rate_limited", "server_error", "network"]

_SURFACE_PATTERNS = re.compile(
    r"(?:invalid[_ -]?(?:request|parameter)|validation[_ -]?error|context[_ -]?length|"
    r"unsupported[_ -]?(?:protocol|tool)|protocol[_ -]?(?:error|mismatch)|"
    r"tool[_ -]?(?:compat|choice|schema|use)|malformed[_ -]?(?:request|schema))",
    re.IGNORECASE,
)
_QUOTA_PATTERNS = re.compile(
    r"(?:quota[_ -]?(?:exhausted|exceeded)|insufficient[_ -]?(?:quota|credits)|"
    r"billing[_ -]?(?:limit|exhausted)|usage[_ -]?limit|credit[_ -]?balance)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ResolutionDecision:
    action: ResolutionAction
    reason: Optional[ResolutionReason] = None
    error_code: Optional[str] = None
    cooldown_seconds: int = 0


def _error_text(outcome: RawCallOutcome) -> str:
    return " ".join(value for value in (outcome.error_code, outcome.redacted_message) if isinstance(value, str))


def classify_outcome(
    outcome: RawCallOutcome,
    *,
    refresh_attempted: bool = False,
) -> ResolutionDecision:
    """Apply the signed taxonomy without persisting or exposing raw errors."""

    if outcome.kind == RawOutcomeKind.SUCCESS:
        return ResolutionDecision("return")

    # A partial stream is already externally observable. Any transparent retry
    # could duplicate tokens or tool calls, regardless of the failure category.
    if outcome.stream_started:
        return ResolutionDecision("surface", error_code="stream_interrupted")

    if outcome.kind in {RawOutcomeKind.NETWORK_ERROR, RawOutcomeKind.TIMEOUT}:
        return ResolutionDecision("fallback", reason="network", cooldown_seconds=30)
    if outcome.kind == RawOutcomeKind.PROTOCOL_ERROR:
        return ResolutionDecision("surface", error_code="upstream_protocol_error")

    if outcome.http_status == 401:
        if refresh_attempted:
            return ResolutionDecision("surface", error_code="upstream_unauthorized")
        return ResolutionDecision("refresh")

    error_text = _error_text(outcome)
    if _SURFACE_PATTERNS.search(error_text):
        return ResolutionDecision("surface", error_code="upstream_request_invalid")

    if _QUOTA_PATTERNS.search(error_text):
        return ResolutionDecision(
            "fallback",
            reason="quota_exhausted",
            cooldown_seconds=300,
        )
    if outcome.http_status == 429:
        return ResolutionDecision(
            "fallback",
            reason="rate_limited",
            cooldown_seconds=60,
        )
    if outcome.http_status is not None and 500 <= outcome.http_status < 600:
        return ResolutionDecision(
            "fallback",
            reason="server_error",
            cooldown_seconds=30,
        )
    return ResolutionDecision("surface", error_code="upstream_error")
