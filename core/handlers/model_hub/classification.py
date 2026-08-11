"""Canonical, engine-independent Model Hub error classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal, Mapping, Optional

from .adapter import RawCallOutcome, RawOutcomeKind

ResolutionAction = Literal["return", "surface", "refresh", "fallback"]
TerminalOutcomeCategory = Literal[
    "served",
    "request_nonfallback",
    "fallback_source",
    "upstream_protocol",
]
ResolutionReason = Literal[
    "quota_exhausted",
    "rate_limited",
    "server_error",
    "network",
    "credential_expired",
    "credential_revoked",
    "balance_exhausted",
    "account_banned",
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
_MACHINE_ERROR_CODE_DECISIONS: dict[str, tuple[ResolutionAction, str]] = {
    "permission_error": ("surface", "request_incompatible"),
    "request_too_large": ("surface", "upstream_request_invalid"),
}
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


_TERMINAL_OUTCOME_CATEGORIES: Mapping[
    tuple[ResolutionAction, RawOutcomeKind, bool],
    TerminalOutcomeCategory,
] = {
    ("return", RawOutcomeKind.SUCCESS, False): "served",
    ("surface", RawOutcomeKind.HTTP_ERROR, False): "request_nonfallback",
    ("surface", RawOutcomeKind.PROTOCOL_ERROR, False): "upstream_protocol",
    ("surface", RawOutcomeKind.HTTP_ERROR, True): "fallback_source",
    ("surface", RawOutcomeKind.NETWORK_ERROR, True): "fallback_source",
    ("surface", RawOutcomeKind.TIMEOUT, True): "fallback_source",
}


def terminal_outcome_category(
    outcome: RawCallOutcome,
    decision: ResolutionDecision,
) -> TerminalOutcomeCategory:
    """Select a terminal row from positive classification facts only."""

    key = (decision.action, outcome.kind, decision.reason is not None)
    try:
        return _TERMINAL_OUTCOME_CATEGORIES[key]
    except KeyError as exc:
        raise AssertionError("unclassified terminal outcome") from exc


def _error_text(outcome: RawCallOutcome) -> str:
    return " ".join(value for value in (outcome.error_code, outcome.redacted_message) if isinstance(value, str))


def _classify_unstreamed(
    outcome: RawCallOutcome,
    *,
    refresh_attempted: bool = False,
) -> ResolutionDecision:
    if outcome.kind in {RawOutcomeKind.NETWORK_ERROR, RawOutcomeKind.TIMEOUT}:
        return ResolutionDecision("fallback", reason="network", cooldown_seconds=30)
    if outcome.kind == RawOutcomeKind.PROTOCOL_ERROR:
        return ResolutionDecision("surface", error_code="upstream_protocol_error")

    # Signed machine-code rows precede every status heuristic. HTTP status is
    # only fallback evidence when no canonical machine-code row matches.
    normalized_error_code = str(outcome.error_code or "").strip().lower()
    machine_row = _MACHINE_ERROR_CODE_DECISIONS.get(normalized_error_code)
    if machine_row is not None:
        action, error_code = machine_row
        return ResolutionDecision(action, error_code=error_code)

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
    model_not_found = _MODEL_SURFACE_PATTERNS.search(error_text) or (
        outcome.http_status == 404
        and normalized_error_code in _MODEL_NOT_FOUND_ERROR_CODES
    )
    if (
        _SURFACE_PATTERNS.search(error_text)
        or model_not_found
    ):
        return ResolutionDecision("surface", error_code="upstream_request_invalid")

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


def classify_outcome(
    outcome: RawCallOutcome,
    *,
    refresh_attempted: bool = False,
) -> ResolutionDecision:
    """Apply the signed taxonomy without persisting or exposing raw errors."""

    if outcome.kind == RawOutcomeKind.SUCCESS:
        return ResolutionDecision("return")

    decision = _classify_unstreamed(
        outcome,
        refresh_attempted=refresh_attempted,
    )
    if not outcome.stream_started:
        return decision
    # Output already reached the caller, so replay is terminal. Retain a
    # fallback-class Source reason for settlement before the terminal response.
    if decision.action == "fallback":
        return replace(
            decision,
            action="surface",
            error_code="stream_interrupted",
        )
    return decision
