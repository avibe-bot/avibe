"""Bounded, credential-free persistence for Model Hub resolution events."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from vibe.i18n import t as i18n_t

EventAgent = Literal["claude", "codex", "opencode", "system"]
EventKind = Literal[
    "switch",
    "cooldown",
    "recover",
    "skip",
    "mapping_applied",
    "channel_switch",
    "needs_action",
    "supply_interrupted",
]
EventReason = Literal[
    "quota_exhausted",
    "rate_limited",
    "server_error",
    "network",
    "recovery",
    "manual",
    "mapping",
    "credential_expired",
    "credential_revoked",
    "balance_exhausted",
    "account_banned",
    "permission_denied",
    "unclassified_error",
    "no_enabled_source",
    "no_eligible_source",
    "model_unsupported",
]
BillingNote = Literal["entered_metered", "left_metered"]
EventSeverity = Literal["info", "action_required"]

_SELF_HEALING_REASONS = {
    "quota_exhausted",
    "rate_limited",
    "server_error",
    "network",
}
_NON_SELF_HEALING_REASONS = {
    "credential_expired",
    "credential_revoked",
    "balance_exhausted",
    "account_banned",
    "unclassified_error",
}
_REQUEST_SCOPED_REASONS = {"permission_denied"}
_STRUCTURAL_REASONS = {
    "no_enabled_source",
    "no_eligible_source",
    "model_unsupported",
}

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
            or reason not in _STRUCTURAL_REASONS
        ):
            raise ValueError("Invalid supply_interrupted event")
    elif reason in _STRUCTURAL_REASONS:
        raise ValueError("Structural reasons require supply_interrupted")
    if reason in _REQUEST_SCOPED_REASONS and kind != "switch":
        raise ValueError("Request-scoped reasons require a switch event")
    if kind == "needs_action" and (
        from_source is None
        or to_source is not None
        or reason not in _NON_SELF_HEALING_REASONS
    ):
        raise ValueError("Invalid needs_action event")
    if reason in _NON_SELF_HEALING_REASONS and kind in {"cooldown", "recover"}:
        raise ValueError("Non-self-healing reasons cannot cool down or recover")
    if kind == "cooldown" and reason not in _SELF_HEALING_REASONS:
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
            reason=i18n_t(f"modelHub.events.reason.{reason}", lang),
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
        return [item for item in payload if isinstance(item, dict) and not contains_credential_material(item)]

    def _write(self, payload: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload[-self.max_entries :], ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp_name = tmp.name
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, self.path)

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
