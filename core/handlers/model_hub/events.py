"""Bounded, credential-free persistence for Model Hub resolution events."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, TypeAlias, get_args

from vibe.i18n import t as i18n_t

from .state_file import write_state_document

EventAgent = Literal["claude", "codex", "opencode", "system"]
EventKind = Literal[
    "switch",
    "cooldown",
    "recover",
    "skip",
    "channel_switch",
    "needs_action",
    "supply_interrupted",
    "reasoning_efforts_override",
]
EventReason: TypeAlias = str
BillingNote = Literal["entered_metered", "left_metered"]
EventSeverity = Literal["info", "action_required"]

ReasonClass = Literal[
    "self_healing",
    "non_self_healing",
    "structural",
    "transition",
]

# The reason vocabulary, validation classes, event rendering, and locale parity
# all consume this table. The JSON schema and locale bundles are checked mirrors.
EVENT_REASON_AUTHORITY: dict[str, ReasonClass] = {
    "quota_exhausted": "self_healing",
    "rate_limited": "self_healing",
    "server_error": "self_healing",
    "network": "self_healing",
    "recovery": "transition",
    "manual": "transition",
    "upstream_tiers": "transition",
    "catalog_tiers": "transition",
    "credential_expired": "non_self_healing",
    "credential_revoked": "non_self_healing",
    "balance_exhausted": "non_self_healing",
    "account_banned": "non_self_healing",
    "unclassified_error": "non_self_healing",
    "no_enabled_source": "structural",
    "no_eligible_source": "structural",
    "route_unconfigured": "structural",
    "source_missing": "structural",
    "model_unsupported": "structural",
    "native_cli_unavailable": "structural",
}

SOURCE_DETAIL_EVENT_REASONS = {
    "models.source.cooldown.quota_exhausted": "quota_exhausted",
    "models.source.cooldown.rate_limited": "rate_limited",
    "models.source.cooldown.server_error": "server_error",
    "models.source.cooldown.network": "network",
    "models.source.cooldown.timeout": "network",
    "models.source.needs_action.oauth_expired": "credential_expired",
    "models.source.needs_action.credential_revoked": "credential_revoked",
    "models.source.needs_action.balance_exhausted": "balance_exhausted",
    "models.source.needs_action.account_banned": "account_banned",
    "models.source.error.unclassified": "unclassified_error",
}

# Released v5 records are normalized only at their persistence read boundary.
RETIRED_PERSISTED_REASON_DEGRADATIONS = {
    "permission_denied": "unclassified_error",
}


def degrade_persisted_event(event: dict) -> dict:
    degraded = dict(event)
    reason = degraded.get("reason")
    if isinstance(reason, str):
        degraded["reason"] = RETIRED_PERSISTED_REASON_DEGRADATIONS.get(
            reason,
            reason,
        )
    return degraded


def event_reason_label(reason: str, language: str) -> str:
    if reason not in EVENT_REASON_AUTHORITY:
        raise ValueError("Invalid resolution event reason")
    return i18n_t(f"modelHub.events.reason.{reason}", language)

_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\b(?:sk|rk|pk|sess|token)[-_][a-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?:authorization|api[_ -]?key|access[_ -]?token)\s*[:=]\s*"
        r"(?:sk[-_][a-z0-9_-]{8,}|[a-z0-9._~+/=-]{16,})"
    ),
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
)


def redact_credential_material(value: str) -> str:
    redacted = value
    for pattern in _CREDENTIAL_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def contains_credential_material(value: object) -> bool:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return any(pattern.search(rendered) for pattern in _CREDENTIAL_PATTERNS)


@dataclass(frozen=True)
class ResolutionEvent:
    id: str
    ts: str
    agent: EventAgent
    kind: EventKind
    model_id: Optional[str]
    reason: EventReason
    human_zh: str
    human_en: str
    from_source: Optional[str] = None
    to_source: Optional[str] = None
    billing_note: Optional[BillingNote] = None
    severity: Optional[EventSeverity] = None

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "agent": self.agent,
            "kind": self.kind,
            "model_id": self.model_id,
            "from_source": self.from_source,
            "to_source": self.to_source,
            "reason": self.reason,
            "billing_note": self.billing_note,
            "severity": self.severity,
            "human_zh": self.human_zh,
            "human_en": self.human_en,
        }


def build_resolution_event(
    *,
    agent: EventAgent,
    kind: EventKind,
    model_id: Optional[str],
    reason: EventReason,
    from_source: Optional[str] = None,
    to_source: Optional[str] = None,
    from_label: Optional[str] = None,
    to_label: Optional[str] = None,
    billing_note: Optional[BillingNote] = None,
    severity: Optional[EventSeverity] = None,
    now: Optional[datetime] = None,
) -> ResolutionEvent:
    if agent not in get_args(EventAgent):
        raise ValueError("Invalid resolution event agent")
    if kind not in get_args(EventKind):
        raise ValueError("Invalid resolution event kind")
    reason_class = EVENT_REASON_AUTHORITY.get(reason)
    if reason_class is None:
        raise ValueError("Invalid resolution event reason")
    if billing_note is not None and billing_note not in get_args(BillingNote):
        raise ValueError("Invalid resolution event billing note")
    if severity is not None and severity not in get_args(EventSeverity):
        raise ValueError("Invalid resolution event severity")
    action_required = kind in {"needs_action", "supply_interrupted"}
    expected_severity: EventSeverity = (
        "action_required" if action_required else "info"
    )
    severity = severity or expected_severity
    if severity != expected_severity:
        raise ValueError("Resolution event severity does not match its kind")
    if kind == "supply_interrupted":
        if (
            agent == "system"
            or model_id is None
            or from_source is not None
            or to_source is not None
            or reason_class != "structural"
        ):
            raise ValueError("Invalid supply_interrupted event")
    elif reason_class == "structural":
        raise ValueError("Structural reasons require supply_interrupted")
    if kind == "needs_action" and (
        from_source is None
        or to_source is not None
        or reason_class != "non_self_healing"
    ):
        raise ValueError("Invalid needs_action event")
    if reason_class == "non_self_healing" and kind in {"cooldown", "recover"}:
        raise ValueError("Non-self-healing reasons cannot cool down or recover")
    if kind == "cooldown" and reason_class != "self_healing":
        raise ValueError("Invalid cooldown reason")
    if kind == "channel_switch" and (
        from_source is None
        or to_source is None
        or from_source != to_source
    ):
        raise ValueError("Invalid channel_switch event")
    if kind == "switch" and (
        model_id is None or from_source is None or to_source is None
    ):
        raise ValueError("Invalid switch event")
    if kind == "reasoning_efforts_override" and (
        agent != "system"
        or model_id is None
        or from_source is None
        or to_source is not None
        or reason not in {"upstream_tiers", "catalog_tiers"}
    ):
        raise ValueError("Invalid reasoning_efforts_override event")
    if reason in {"upstream_tiers", "catalog_tiers"} and (
        kind != "reasoning_efforts_override"
    ):
        raise ValueError("Managed-tier reasons require reasoning_efforts_override")
    if kind in {"cooldown", "skip"} and (
        from_source is None or to_source is not None
    ):
        raise ValueError(f"Invalid {kind} event")
    if kind == "recover" and to_source is None:
        raise ValueError("Invalid recover event")
    if agent == "system" and kind not in {
        "cooldown",
        "recover",
        "skip",
        "needs_action",
        "channel_switch",
        "reasoning_efforts_override",
    }:
        raise ValueError("Invalid system event kind")
    if model_id is None and (
        agent != "system"
        or kind
        not in {"cooldown", "recover", "skip", "needs_action", "channel_switch"}
    ):
        raise ValueError("Null model_id requires a source-scoped system event")

    safe_from = redact_credential_material(from_label or from_source or "")
    safe_to = redact_credential_material(to_label or to_source or "")

    def render(lang: str) -> str:
        template = kind
        return i18n_t(
            f"modelHub.events.{template}",
            lang,
            from_source=safe_from or i18n_t("modelHub.events.sourceFallback", lang),
            to_source=safe_to or i18n_t("modelHub.events.sourceFallback", lang),
            reason=event_reason_label(reason, lang),
            model=model_id or "",
        )

    human_en = render("en")
    human_zh = render("zh")
    event = ResolutionEvent(
        id=f"evt_{uuid.uuid4().hex}",
        ts=(now or datetime.now(timezone.utc)).isoformat(),
        agent=agent,
        kind=kind,
        model_id=model_id,
        reason=reason,
        human_zh=human_zh[:200],
        human_en=human_en[:200],
        from_source=from_source,
        to_source=to_source,
        billing_note=billing_note,
        severity=severity,
    )
    if contains_credential_material(event.to_payload()):
        raise ValueError("Resolution event contains credential material")
    return event


class BoundedEventLog:
    def __init__(self, path: Path, *, max_entries: int = 500):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [
            degrade_persisted_event(item)
            for item in payload
            if isinstance(item, dict) and not contains_credential_material(item)
        ]

    def _write(self, payload: list[dict]) -> None:
        write_state_document(self.path, payload[-self.max_entries :])

    def append(self, event: ResolutionEvent) -> None:
        payload = event.to_payload()
        if contains_credential_material(payload):
            raise ValueError("Resolution event contains credential material")
        with self._lock:
            events = self._read()
            events.append(payload)
            self._write(events)

    def list(self, *, limit: int = 20, before: Optional[str] = None) -> list[dict]:
        bounded_limit = max(1, min(limit, 100))
        with self._lock:
            newest_first = list(reversed(self._read()))
        if before is not None:
            index = next((idx for idx, event in enumerate(newest_first) if event.get("id") == before), None)
            newest_first = newest_first[index + 1 :] if index is not None else []
        return newest_first[:bounded_limit]
