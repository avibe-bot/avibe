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

EventAgent = Literal["claude", "codex", "opencode", "system"]
EventKind = Literal["switch", "cooldown", "recover", "skip", "mapping_applied", "channel_switch"]
EventReason = Literal["quota_exhausted", "rate_limited", "server_error", "network", "recovery", "manual", "mapping"]
BillingNote = Literal["entered_metered", "left_metered"]

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
    model_id: str
    reason: EventReason
    human_zh: str
    human_en: str
    from_source: Optional[str] = None
    to_source: Optional[str] = None
    billing_note: Optional[BillingNote] = None

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
            "human_zh": self.human_zh,
            "human_en": self.human_en,
        }


def build_resolution_event(
    *,
    agent: EventAgent,
    kind: EventKind,
    model_id: str,
    reason: EventReason,
    from_source: Optional[str] = None,
    to_source: Optional[str] = None,
    from_label: Optional[str] = None,
    to_label: Optional[str] = None,
    billing_note: Optional[BillingNote] = None,
    now: Optional[datetime] = None,
) -> ResolutionEvent:
    safe_from = redact_credential_material(from_label or from_source or "source")
    safe_to = redact_credential_material(to_label or to_source or "source")
    reason_en = {
        "quota_exhausted": "quota exhausted",
        "rate_limited": "rate limited",
        "server_error": "upstream server unavailable",
        "network": "network unavailable",
        "recovery": "recovered",
        "manual": "manual change",
        "mapping": "model mapping applied",
    }[reason]
    reason_zh = {
        "quota_exhausted": "额度用完",
        "rate_limited": "请求受限",
        "server_error": "上游服务暂不可用",
        "network": "网络暂不可用",
        "recovery": "已恢复",
        "manual": "手动调整",
        "mapping": "已应用模型映射",
    }[reason]
    if kind == "switch":
        human_en = f"{safe_from}: {reason_en} -> switched to {safe_to}"
        human_zh = f"{safe_from}:{reason_zh} -> 已切到 {safe_to}"
    elif kind == "cooldown":
        human_en = f"{safe_from}: {reason_en}; cooling down"
        human_zh = f"{safe_from}:{reason_zh}，暂时冷却"
    elif kind == "recover":
        human_en = f"{safe_to}: recovered and available"
        human_zh = f"{safe_to}:已恢复可用"
    else:
        human_en = f"{safe_from}: {reason_en}"
        human_zh = f"{safe_from}:{reason_zh}"
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
