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
    "engine_down",
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


@dataclass(frozen=True)
class SourceSettlementRule:
    """Authoritative source-state policy for one settled fallback reason."""

    status: Literal["cooldown", "needs_action", "error"]
    priority: int


# A needs_action state is credential-owned and outranks transient failures.
SOURCE_SETTLEMENT_AUTHORITY: Mapping[str, SourceSettlementRule] = {
    "quota_exhausted": SourceSettlementRule("cooldown", 10),
    "rate_limited": SourceSettlementRule("cooldown", 10),
    "server_error": SourceSettlementRule("cooldown", 10),
    "network": SourceSettlementRule("cooldown", 10),
    "credential_expired": SourceSettlementRule("needs_action", 20),
    "credential_revoked": SourceSettlementRule("needs_action", 20),
    "balance_exhausted": SourceSettlementRule("needs_action", 20),
    "account_banned": SourceSettlementRule("needs_action", 20),
    "unclassified_error": SourceSettlementRule("error", 10),
}
_SOURCE_STATE_PRIORITY: Mapping[str, int] = {
    "active": 0,
    "standby": 0,
    "cooldown": 10,
    "error": 10,
    "needs_action": 20,
}


def source_settlement_rule(reason: str) -> SourceSettlementRule:
    try:
        return SOURCE_SETTLEMENT_AUTHORITY[reason]
    except KeyError as exc:
        raise ValueError(f"unknown source settlement reason: {reason}") from exc


def source_settlement_allowed(existing_status: str, reason: str) -> bool:
    """Return whether a settled reason may replace the persisted source state."""

    incoming = source_settlement_rule(reason)
    return incoming.priority >= _SOURCE_STATE_PRIORITY.get(existing_status, 0)

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


@dataclass(frozen=True)
class _MachineErrorRule:
    """One machine-code row, including the downstream status projection."""

    specificity: int
    action: ResolutionAction | None
    error_code: str | None
    downstream_status: int | None


# This table is the sole owner of machine-code specificity and projection.
_MACHINE_ERROR_TAXONOMY: Mapping[str, _MachineErrorRule] = {
    "permission_error": _MachineErrorRule(100, "surface", "request_incompatible", 403),
    "engine_down": _MachineErrorRule(100, "surface", "engine_down", 502),
    "request_too_large": _MachineErrorRule(80, "surface", "upstream_request_invalid", 400),
    "api_error": _MachineErrorRule(0, None, None, None),
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
    downstream_status: int | None = None


_TERMINAL_OUTCOME_CATEGORIES: Mapping[
    tuple[ResolutionAction, RawOutcomeKind, bool],
    TerminalOutcomeCategory,
] = {
    ("return", RawOutcomeKind.SUCCESS, False): "served",
    ("surface", RawOutcomeKind.HTTP_ERROR, False): "request_nonfallback",
    ("surface", RawOutcomeKind.PROTOCOL_ERROR, False): "upstream_protocol",
    ("surface", RawOutcomeKind.NETWORK_ERROR, False): "engine_down",
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
    return " ".join((*machine_error_codes(outcome), outcome.redacted_message or ""))


def machine_error_codes(outcome: RawCallOutcome) -> tuple[str, ...]:
    """Collect and rank every raw machine-code candidate by the authority table."""

    def specificity(value: str) -> int:
        row = _MACHINE_ERROR_TAXONOMY.get(value)
        return row.specificity if row is not None else -1

    raw_values = (*outcome.error_candidates, outcome.error_type, outcome.error_code)
    candidates = {
        value.strip().lower()
        for value in raw_values
        if isinstance(value, str) and value.strip()
    }
    return tuple(
        sorted(
            candidates,
            key=lambda value: (-specificity(value), value),
        )
    )


def _classify_unstreamed(
    outcome: RawCallOutcome,
    *,
    refresh_attempted: bool = False,
) -> ResolutionDecision:
    # Signed machine-code rows precede every transport and status heuristic.
    machine_rows = (
        (_MACHINE_ERROR_TAXONOMY[code], code)
        for code in machine_error_codes(outcome)
        if code in _MACHINE_ERROR_TAXONOMY
    )
    for machine_row, _machine_code in machine_rows:
        if machine_row.action is not None:
            return ResolutionDecision(
                machine_row.action,
                error_code=machine_row.error_code,
                downstream_status=machine_row.downstream_status,
            )

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
    model_not_found = _MODEL_SURFACE_PATTERNS.search(error_text) or (
        outcome.http_status == 404
        and any(code in _MODEL_NOT_FOUND_ERROR_CODES for code in machine_error_codes(outcome))
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
