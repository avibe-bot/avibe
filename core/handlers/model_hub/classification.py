"""Canonical, engine-independent Model Hub error classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .adapter import RawCallOutcome, RawOutcomeKind

ResolutionAction = Literal["return", "surface", "refresh", "fallback"]
ResolutionReason = Literal[
    "quota_exhausted",
    "rate_limited",
    "server_error",
    "network",
    "credential_expired",
    "credential_revoked",
    "balance_exhausted",
    "account_banned",
    "permission_denied",
    "unclassified_error",
]

_SURFACE_PATTERNS = re.compile(
    r"(?:invalid[_ -]?(?:request|parameter)|validation[_ -]?error|context[_ -]?length|"
    r"unsupported[_ -]?(?:protocol|tool)|protocol[_ -]?(?:error|mismatch)|"
    r"tool[_ -]?(?:compat|choice|schema|use)|malformed[_ -]?(?:request|schema))",
    re.IGNORECASE,
)
_MODEL_SURFACE_PATTERNS = re.compile(
    r"(?:model[_ -]?(?:(?:is[_ -]?)?not[_ -]?(?:found|available|accessible)|"
    r"does[_ -]?not[_ -]?exist)|"
    r"unknown[_ -]?model|no[_ -]?such[_ -]?model)",
    re.IGNORECASE,
)
_MODEL_NOT_FOUND_ERROR_CODES = frozenset({"not_found_error"})
_REQUEST_SURFACE_ERROR_CODES = frozenset({"request_too_large"})
_REQUEST_FALLBACK_ERROR_CODES = frozenset({"permission_error"})
_QUOTA_PATTERNS = re.compile(
    r"(?:quota[_ -]?(?:exhausted|exceeded)|insufficient[_ -]?(?:quota|credits)|"
    r"billing[_ -]?(?:limit|exhausted)|usage[_ -]?limit|credit[_ -]?balance)",
    re.IGNORECASE,
)
_BANNED_PATTERNS = re.compile(
    r"(?:account[_ -]?(?:banned|suspended|disabled)|"
    r"(?:banned|suspended|disabled)[_ -]?account)",
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
            return ResolutionDecision(
                "fallback",
                reason="credential_expired",
            )
        return ResolutionDecision("refresh")

    error_text = _error_text(outcome)
    if outcome.http_status == 402:
        return ResolutionDecision(
            "fallback",
            reason="balance_exhausted",
        )
    normalized_error_code = str(outcome.error_code or "").strip().lower()
    model_not_found = _MODEL_SURFACE_PATTERNS.search(error_text) or (
        outcome.http_status == 404
        and normalized_error_code in _MODEL_NOT_FOUND_ERROR_CODES
    )
    if (
        normalized_error_code in _REQUEST_SURFACE_ERROR_CODES
        or _SURFACE_PATTERNS.search(error_text)
        or model_not_found
    ):
        return ResolutionDecision("surface", error_code="upstream_request_invalid")
    if normalized_error_code in _REQUEST_FALLBACK_ERROR_CODES:
        return ResolutionDecision("fallback", reason="permission_denied")

    if _QUOTA_PATTERNS.search(error_text):
        return ResolutionDecision(
            "fallback",
            reason="quota_exhausted",
            cooldown_seconds=300,
        )
    if outcome.http_status == 403:
        return ResolutionDecision(
            "fallback",
            reason=(
                "account_banned"
                if _BANNED_PATTERNS.search(error_text)
                else "credential_revoked"
            ),
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
    return ResolutionDecision("fallback", reason="unclassified_error")
