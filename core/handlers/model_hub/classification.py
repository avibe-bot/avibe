"""Canonical, engine-independent Model Hub error classification."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final, Literal, Mapping, Optional

from .adapter import RawCallOutcome, RawOutcomeKind
from .claude_rate_limits import claude_subscription_reset

ResolutionAction = Literal["return", "surface", "refresh", "fallback"]
MachineErrorFamily = Literal["auth", "request", "server", "transient", "terminal"]
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
    "plan_excludes_product",
    "spend_limit_reached",
    "unclassified_error",
]


@dataclass(frozen=True)
class SourceSettlementRule:
    """Authoritative source-state policy for one settled fallback reason."""

    status: Literal["cooldown", "needs_action", "error"]
    priority: int
    may_write_health: bool = True


# One strict authority order prevents late weaker verdicts from changing history.
SOURCE_SETTLEMENT_AUTHORITY: Mapping[str, SourceSettlementRule] = {
    "quota_exhausted": SourceSettlementRule("cooldown", 10),
    "rate_limited": SourceSettlementRule("cooldown", 10),
    "server_error": SourceSettlementRule("cooldown", 10),
    "network": SourceSettlementRule("cooldown", 10, may_write_health=False),
    "unclassified_error": SourceSettlementRule("error", 20),
    "credential_expired": SourceSettlementRule("needs_action", 30),
    "credential_revoked": SourceSettlementRule("needs_action", 30),
    "balance_exhausted": SourceSettlementRule("needs_action", 30),
    "account_banned": SourceSettlementRule("needs_action", 30),
    "plan_excludes_product": SourceSettlementRule("needs_action", 30),
    "spend_limit_reached": SourceSettlementRule("needs_action", 30),
}
_SOURCE_STATE_PRIORITY = {
    "active": 0,
    "standby": 0,
    **{rule.status: rule.priority for rule in SOURCE_SETTLEMENT_AUTHORITY.values()},
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

    family: MachineErrorFamily
    upstream_visible: bool
    specificity: int
    action: ResolutionAction | None
    reason: ResolutionReason | None
    error_code: str | None
    cooldown_seconds: int
    downstream_status: int | None


# This table is the sole owner of machine-code specificity and projection.
_MACHINE_ERROR_TAXONOMY: Final[Mapping[str, _MachineErrorRule]] = {
    "permission_error": _MachineErrorRule("terminal", True, 100, "surface", None, "request_incompatible", 0, 403),
    "engine_down": _MachineErrorRule("terminal", False, 100, "surface", None, "engine_down", 0, 502),
    "authentication_error": _MachineErrorRule("auth", True, 90, "refresh", None, None, 0, None),
    "invalid_api_key": _MachineErrorRule("auth", True, 90, "refresh", None, None, 0, None),
    "account_banned": _MachineErrorRule("auth", True, 90, "fallback", "account_banned", None, 0, None),
    "account_disabled": _MachineErrorRule("auth", True, 90, "fallback", "account_banned", None, 0, None),
    "account_suspended": _MachineErrorRule("auth", True, 90, "fallback", "account_banned", None, 0, None),
    "request_too_large": _MachineErrorRule("request", True, 80, "surface", None, "upstream_request_invalid", 0, 400),
    "invalid_parameter": _MachineErrorRule("request", True, 80, "surface", None, "upstream_request_invalid", 0, 400),
    "invalid_request_error": _MachineErrorRule(
        "request", True, 80, "surface", None, "upstream_request_invalid", 0, 400
    ),
    "validation_error": _MachineErrorRule("request", True, 80, "surface", None, "upstream_request_invalid", 0, 400),
    "context_length_exceeded": _MachineErrorRule(
        "request", True, 80, "surface", None, "upstream_request_invalid", 0, 400
    ),
    "model_not_found": _MachineErrorRule("request", True, 80, "surface", None, "upstream_request_invalid", 0, 400),
    "not_found_error": _MachineErrorRule("request", True, 80, "surface", None, "upstream_request_invalid", 0, 400),
    "rate_limit_error": _MachineErrorRule("transient", True, 80, "fallback", "rate_limited", None, 60, None),
    "rate_limit_exceeded": _MachineErrorRule("transient", True, 80, "fallback", "rate_limited", None, 60, None),
    "quota_exceeded": _MachineErrorRule("transient", True, 80, "fallback", "quota_exhausted", None, 300, None),
    "insufficient_quota": _MachineErrorRule("transient", True, 80, "fallback", "quota_exhausted", None, 300, None),
    # A ChatGPT subscription past its usage cap: a 429 whose envelope also names
    # the reset, which the engine client carries as retry advice.
    "usage_limit_reached": _MachineErrorRule("transient", True, 80, "fallback", "quota_exhausted", None, 300, None),
    "billing_error": _MachineErrorRule("transient", True, 80, "fallback", "balance_exhausted", None, 0, None),
    # A plan that does not include the product: Codex's 429 for a ChatGPT plan
    # without it, which only an upgrade ends (openai/codex api_bridge.rs).
    "usage_not_included": _MachineErrorRule("transient", True, 85, "fallback", "plan_excludes_product", None, 0, None),
    # OpenAI's billing 429s name their cause in the code, beside a type that may
    # be the generic insufficient_quota, so they outrank it. Its error-code guide
    # says retrying restores none of them: only credit, or a raised spend or
    # usage limit, does, so none is a cooldown.
    "credit_balance_exhausted": _MachineErrorRule("transient", True, 85, "fallback", "balance_exhausted", None, 0, None),
    "organization_spend_limit_exceeded": _MachineErrorRule(
        "transient", True, 85, "fallback", "spend_limit_reached", None, 0, None
    ),
    "project_spend_limit_exceeded": _MachineErrorRule(
        "transient", True, 85, "fallback", "spend_limit_reached", None, 0, None
    ),
    "organization_usage_limit_exceeded": _MachineErrorRule(
        "transient", True, 85, "fallback", "spend_limit_reached", None, 0, None
    ),
    "overloaded": _MachineErrorRule("transient", True, 80, "fallback", "server_error", None, 30, None),
    "overloaded_error": _MachineErrorRule("transient", True, 80, "fallback", "server_error", None, 30, None),
    "server_error": _MachineErrorRule("server", True, 70, "fallback", "server_error", None, 30, None),
    # CLIProxyAPI's Claude-shaped type for an upstream 504, and since v8 for a 408;
    # its OpenAI shape is type server_error with code request_timeout.
    "timeout_error": _MachineErrorRule("server", True, 70, "fallback", "server_error", None, 30, None),
    "internal_error": _MachineErrorRule("server", True, 70, "fallback", "server_error", None, 30, None),
    "api_error": _MachineErrorRule("server", True, 10, "fallback", "server_error", None, 30, None),
    "service_unavailable_error": _MachineErrorRule(
        "server", True, 70, "fallback", "server_error", None, 30, None
    ),
    "upstream_error": _MachineErrorRule("server", True, 70, "fallback", "server_error", None, 30, None),
}
MACHINE_ERROR_CODES: Final = frozenset(_MACHINE_ERROR_TAXONOMY)
UPSTREAM_MACHINE_ERROR_CODES: Final = frozenset(
    code for code, rule in _MACHINE_ERROR_TAXONOMY.items() if rule.upstream_visible
)
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
    # Derived upstream advice only; no header values cross classification.
    retry_at: datetime | None = None


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
    # Machine codes only. ``redacted_message`` is the engine client's own account
    # of how the call ended, not upstream text, so its wording must not decide a
    # class: "upstream returned a protocol error event" once read as a rejected
    # request.
    return " ".join(machine_error_codes(outcome))


def machine_error_codes(outcome: RawCallOutcome) -> tuple[str, ...]:
    """Collect and rank every raw machine-code candidate by the authority table."""

    def specificity(value: str) -> int:
        row = _MACHINE_ERROR_TAXONOMY.get(value)
        return row.specificity if row is not None else -1

    raw_values = (*outcome.error_candidates, outcome.error_type, outcome.error_code)
    candidates = {value.strip().lower() for value in raw_values if isinstance(value, str) and value.strip()}
    return tuple(
        sorted(
            candidates,
            key=lambda value: (-specificity(value), value),
        )
    )


def names_model_refusal(outcome: RawCallOutcome) -> bool:
    """Whether the upstream says it does not serve the requested model: a code naming it, or a 404 not_found_error."""

    return machine_codes_name_model(outcome) or (
        outcome.http_status == 404
        and any(code in _MODEL_NOT_FOUND_ERROR_CODES for code in machine_error_codes(outcome))
    )


def machine_codes_name_model(outcome: RawCallOutcome) -> bool:
    """Whether a machine code itself names the model as not served (``model_not_found``, ``unknown_model``)."""

    return bool(_MODEL_SURFACE_PATTERNS.search(_error_text(outcome)))


def _status_clears_request(http_status: int | None) -> bool:
    """Whether the status itself says the call failed for a reason other than the request.

    A timeout (408), a rate or quota limit (429) and a server or gateway failure
    (5xx) are the server's own account of why it did not serve the call. A
    request-family code beside one contradicts it rather than refining it:
    relays wrap their own timeouts and stalls in ``invalid_request_error``
    (#2260), so that code cannot show that no Source could serve the request.
    """

    return http_status in (408, 429) or (http_status is not None and 500 <= http_status <= 599)


def _classify_unstreamed(
    outcome: RawCallOutcome,
    *,
    refresh_attempted: bool = False,
) -> ResolutionDecision:
    clears_request = _status_clears_request(outcome.http_status)
    # Signed machine-code rows precede every transport and status heuristic,
    # except that a status clearing the request outranks the request family.
    machine_rows = (
        (_MACHINE_ERROR_TAXONOMY[code], code)
        for code in machine_error_codes(outcome)
        if code in _MACHINE_ERROR_TAXONOMY
        and not (clears_request and _MACHINE_ERROR_TAXONOMY[code].family == "request")
    )
    for machine_row, _machine_code in machine_rows:
        if machine_row.action is not None:
            if machine_row.action == "refresh" and refresh_attempted:
                return ResolutionDecision(
                    "fallback",
                    reason="credential_expired",
                )
            return ResolutionDecision(
                machine_row.action,
                reason=machine_row.reason,
                error_code=machine_row.error_code,
                cooldown_seconds=machine_row.cooldown_seconds,
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
    if not clears_request and (_SURFACE_PATTERNS.search(error_text) or names_model_refusal(outcome)):
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
            reason=("account_banned" if _BANNED_PATTERNS.search(error_text) else "credential_revoked"),
        )
    if outcome.http_status == 429:
        return ResolutionDecision(
            "fallback",
            reason="rate_limited",
            cooldown_seconds=60,
        )
    if outcome.http_status == 408 or (outcome.http_status is not None and 500 <= outcome.http_status < 600):
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
    if decision.reason == "rate_limited":
        retry_at = claude_subscription_reset(outcome)
        if retry_at is not None:
            decision = replace(decision, reason="quota_exhausted", cooldown_seconds=300, retry_at=retry_at)
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
    if decision.action == "refresh":
        # Credential capability belongs to the service boundary. Preserve the
        # refresh fact until that boundary can inspect the exact credential.
        return decision
    return decision
