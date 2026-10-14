"""Per-Source capability evidence (``model-hub-capability-model.md`` §3).

Evidence is passive: it is written only from real requests, never probed. At
most one record per (Source, model, feature, status):

* ``unsupported`` is routing state, written by a narrow rule on a real upstream
  refusal (``evidence_rules.py``). It has no time limit. It stops applying only
  when the user clears it, or when the wire stamp it was learned under no longer
  matches (the Source's rendered engine entry, the engine release or the Avibe
  release changed).
* ``verified`` is display history: a real response carried the feature. It
  never changes routing, survives wire changes, and records how the call ran.

Absence is ``unknown``, and unknown is never negative (#2505). Records follow
Source identity: a record whose Source identity has since changed reads as
absent and is pruned at the next write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Optional

from config.v2_config import ModelHubSourceConfig

from .retry import source_identity
from .state_file import write_state_document

logger = logging.getLogger(__name__)

EVIDENCE_VERSION = 2
MAX_RECORDS = 4000
DETAIL_MAX_CHARS = 64
# A positive record is rewritten at most this often per key: each write rewrites the file.
POSITIVE_REFRESH_SECONDS = 3600

FeatureKind = Literal["effort", "hosted_tool"]
EvidenceStatus = Literal["verified", "unsupported"]
RoutingStatus = Literal["unsupported", "unknown"]
ExecutionMode = Literal["native", "executor", "direct"]

HOSTED_TOOL_ASPECTS: Mapping[str, tuple[str, ...]] = {
    "web_search": ("call", "replay"),
    "x_search": ("call", "replay"),
    # An image call is an ordinary tool call and result, so only its call is evidence.
    "image_generate": ("call",),
}
EXECUTION_MODES: frozenset[str] = frozenset({"native", "executor", "direct"})
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@dataclass(frozen=True)
class EvidenceFeature:
    kind: FeatureKind
    name: str
    aspect: Optional[Literal["call", "replay"]] = None

    def __post_init__(self) -> None:
        if self.kind == "effort":
            if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name) or self.aspect is not None:
                raise ValueError("an effort feature is a name without an aspect")
            return
        if self.kind != "hosted_tool":
            raise ValueError(f"unknown evidence feature kind: {self.kind!r}")
        if self.name not in HOSTED_TOOL_ASPECTS or self.aspect not in HOSTED_TOOL_ASPECTS[self.name]:
            raise ValueError(f"unknown hosted-tool evidence: {self.name!r} {self.aspect!r}")

    @classmethod
    def effort(cls, name: str) -> "EvidenceFeature":
        return cls("effort", name)

    @classmethod
    def effort_or_none(cls, name: object) -> Optional["EvidenceFeature"]:
        """The effort feature, or None for a name evidence cannot hold (free-form efforts are allowed)."""

        return cls("effort", name) if isinstance(name, str) and _NAME_RE.fullmatch(name) else None

    @classmethod
    def hosted(cls, tool: str, aspect: Literal["call", "replay"]) -> "EvidenceFeature":
        return cls("hosted_tool", tool, aspect)

    @classmethod
    def from_payload(cls, payload: object) -> "EvidenceFeature":
        if not isinstance(payload, Mapping) or set(payload) - {"kind", "name", "aspect"}:
            raise ValueError("invalid evidence feature")
        return cls(payload.get("kind"), payload.get("name"), payload.get("aspect"))

    def payload(self) -> dict[str, str]:
        out = {"kind": self.kind, "name": self.name}
        if self.aspect is not None:
            out["aspect"] = self.aspect
        return out


@dataclass(frozen=True)
class EvidenceRecord:
    source_id: str
    model_id: str
    feature: EvidenceFeature
    status: EvidenceStatus
    observed_at: datetime
    wire: str
    source_identity: str
    detail: Optional[str] = None
    """``unsupported`` only: the HTTP status and upstream machine code, never upstream text."""
    mode: Optional[ExecutionMode] = None
    """``verified`` only: how the call ran."""

    def __post_init__(self) -> None:
        for name in ("source_id", "model_id", "wire", "source_identity"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"evidence {name} must be a non-empty string")
        if self.status == "unsupported":
            if self.mode is not None or not isinstance(self.detail, str):
                raise ValueError("a refusal carries its detail and no mode")
            if not 0 < len(self.detail) <= DETAIL_MAX_CHARS:
                raise ValueError(f"evidence detail is at most {DETAIL_MAX_CHARS} characters")
        elif self.status == "verified":
            if self.detail is not None or self.mode not in EXECUTION_MODES:
                raise ValueError("a success carries its mode and no detail")
        else:
            raise ValueError("invalid evidence status")
        if self.observed_at.tzinfo is None:
            raise ValueError("evidence time must be timezone-aware")

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return evidence_key(self.source_id, self.model_id, self.feature, self.status)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": EVIDENCE_VERSION,
            "source_id": self.source_id,
            "model_id": self.model_id,
            "feature": self.feature.payload(),
            "status": self.status,
            "basis": "observed",
            "observed_at": utc_text(self.observed_at),
            "wire": self.wire,
            "source_identity": self.source_identity,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        if self.mode is not None:
            out["mode"] = self.mode
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> "EvidenceRecord":
        # Records an rc build wrote (version 1: probe basis, engine and shape) do not validate and are pruned.
        if not isinstance(payload, Mapping) or payload.get("version") != EVIDENCE_VERSION:
            raise ValueError("unknown evidence record version")
        allowed = {
            "version", "source_id", "model_id", "feature", "status", "basis", "observed_at", "wire",
            "source_identity", "detail", "mode",
        }
        if set(payload) - allowed or payload.get("basis") != "observed":
            raise ValueError("unknown evidence record field")
        observed_at = payload.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at.endswith("Z"):
            raise ValueError("evidence time must be UTC")
        return cls(
            source_id=payload.get("source_id"),
            model_id=payload.get("model_id"),
            feature=EvidenceFeature.from_payload(payload.get("feature")),
            status=payload.get("status"),
            observed_at=datetime.fromisoformat(observed_at[:-1] + "+00:00"),
            wire=payload.get("wire"),
            source_identity=payload.get("source_identity"),
            detail=payload.get("detail"),
            mode=payload.get("mode"),
        )


def evidence_key(
    source_id: str, model_id: str, feature: EvidenceFeature, status: EvidenceStatus
) -> tuple[str, str, str, str, str, str]:
    return (source_id, model_id, feature.kind, feature.name, feature.aspect or "", status)


def utc_text(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def identity_digest(source: ModelHubSourceConfig) -> str:
    """Stable, credential-free digest of the Source identity that evidence follows."""

    raw = json.dumps(list(source_identity(source)), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def render_fields(source: ModelHubSourceConfig, model_id: str) -> tuple[Any, ...]:
    """What the engine entry renders for this (Source, model) beyond the Source identity.

    The renderer (``vibe/model_hub_runtime/config.py``) reads, per Source: the vendor, protocol, base URL and
    credential (whose auth scheme is fixed per credential ref), all in ``source_identity``; and, per model, the
    reasoning tiers it registers (``thinking.levels``), which decide whether the engine forwards or strips an
    effort. Those tiers are the only Source-config input the identity does not cover, so they join the stamp.
    Not here: which models are registered (membership does not change how a registered model is sent), the
    routing prefix and reload marker (engine-internal), and the text-only marking (from the models.dev copy, not
    Source config; it only replaces images in tool results). A future per-Source engine flag joins here.
    """

    listed = next((model for model in source.models if model.id == model_id), None)
    return (tuple(listed.reasoning_efforts) if listed is not None and not listed.retired else (),)


def wire_stamp(*, engine: str, avibe: str, source: ModelHubSourceConfig, model_id: str) -> str:
    """The wire a record is observed under: engine release, Avibe release, and the model's rendered fields."""

    raw = json.dumps(
        [engine, avibe, list(render_fields(source, model_id))], ensure_ascii=False, separators=(",", ":"), default=list,
    )
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LatestSuccess:
    """The latest real success of one hosted tool, for its status line."""

    source_id: str
    model_id: str
    mode: ExecutionMode
    at: datetime
    source_identity: str

    def payload(self) -> dict[str, str]:
        return {"source_id": self.source_id, "model_id": self.model_id, "mode": self.mode, "at": utc_text(self.at)}


class CapabilityEvidenceStore:
    """The evidence records, held in memory and persisted to one small file.

    Bounded by Sources x models x features x two statuses. The controller is the
    only writer (the gateway's rules run there; other processes reach it over
    RPC), so the file is read once, when the store is built at controller
    startup, and memory is authoritative afterwards. Lookups never touch the
    disk. Writes rewrite the file, so callers run them off the loop.

    Persistence order differs by kind, and both are deliberate:

    * a refusal is routing state: memory takes it first, so the re-render that
      follows cannot declare the refused feature again, and a failed file write
      is logged rather than raised;
    * a clear is what the user is shown: the file takes it first, and a failed
      write raises with nothing changed, so it never comes back after a restart.
    """

    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS):
        self.path = path
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: dict[tuple, EvidenceRecord] = {}
        self._by_hop: dict[tuple[str, str], list[EvidenceRecord]] = {}
        self._latest: dict[str, LatestSuccess] = {}
        records = self._read_file()
        self._remember(records)
        for record in records.values():
            self._note_latest_locked(record)

    def _read_file(self) -> dict[tuple, EvidenceRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = []
        records: dict[tuple, EvidenceRecord] = {}
        for item in payload if isinstance(payload, list) else []:
            try:
                record = EvidenceRecord.from_payload(item)
            except (TypeError, ValueError):
                # An unreadable record is unknown, never negative.
                continue
            records[record.key] = record
        return records

    def _remember(self, records: dict[tuple, EvidenceRecord]) -> None:
        by_hop: dict[tuple[str, str], list[EvidenceRecord]] = {}
        for record in records.values():
            by_hop.setdefault((record.source_id, record.model_id), []).append(record)
        # Published snapshots are never mutated, so a reader needs no lock: a
        # write builds the next snapshot and swaps the references, index first.
        self._by_hop = by_hop
        self._records = records

    def _note_latest_locked(self, record: EvidenceRecord) -> None:
        if record.status != "verified" or record.feature.kind != "hosted_tool" or record.mode is None:
            return
        current = self._latest.get(record.feature.name)
        if current is None or record.observed_at > current.at:
            self._latest = {
                **self._latest,
                record.feature.name: LatestSuccess(
                    record.source_id, record.model_id, record.mode, record.observed_at, record.source_identity
                ),
            }

    def get(
        self, source_id: str, model_id: str, feature: EvidenceFeature, status: EvidenceStatus
    ) -> Optional[EvidenceRecord]:
        return self._records.get(evidence_key(source_id, model_id, feature, status))

    def refusal(
        self, source: ModelHubSourceConfig, model_id: str, feature: EvidenceFeature, *, wire: str
    ) -> Optional[EvidenceRecord]:
        """The refusal that applies to this hop now: same identity, same wire stamp; None otherwise."""

        record = self.get(source.id, model_id, feature, "unsupported")
        if record is None or record.source_identity != identity_digest(source) or record.wire != wire:
            return None
        return record

    def records(self) -> list[EvidenceRecord]:
        return list(self._records.values())

    def for_hop(self, source_id: str, model_id: str) -> list[EvidenceRecord]:
        """Every record of one (Source, model), without scanning the others."""

        return list(self._by_hop.get((source_id, model_id), ()))

    def latest_success(self, tool: str) -> Optional[LatestSuccess]:
        return self._latest.get(tool)

    def _write_locked(self, records: dict[tuple, EvidenceRecord], identities: Mapping[str, str]) -> list[EvidenceRecord]:
        """The records to keep: current identities only, within the bound, successes evicted before refusals.

        A refusal is routing state, so the bound takes display history first; only refusals beyond the bound on
        their own, which no real installation reaches, drop their oldest, with a warning.
        """

        def by_age(items: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
            return sorted(items, key=lambda item: item.observed_at)

        kept = [
            record for record in records.values() if identities.get(record.source_id) == record.source_identity
        ]
        if len(kept) <= self.max_records:
            return by_age(kept)
        refusals = by_age(record for record in kept if record.status == "unsupported")
        if len(refusals) > self.max_records:
            logger.warning("Model Hub capability evidence holds more refusals than its bound; the oldest are dropped")
            refusals = refusals[-self.max_records :]
        room = self.max_records - len(refusals)
        successes = by_age(record for record in kept if record.status == "verified")[-room:] if room else []
        return by_age([*refusals, *successes])

    def write_refusal(self, record: EvidenceRecord, *, live: Iterable[ModelHubSourceConfig]) -> None:
        """Record a refusal: memory first, then the file; a failed write is logged, not raised."""

        self.write_refusals([record], live=live)

    def write_refusals(self, records: Iterable[EvidenceRecord], *, live: Iterable[ModelHubSourceConfig]) -> None:
        records = list(records)
        identities = {source.id: identity_digest(source) for source in live}
        for record in records:
            if record.status != "unsupported":
                raise ValueError("write_refusals writes refusals only")
            if identities.get(record.source_id) != record.source_identity:
                raise ValueError("evidence must be written for a live Source's current identity")
        with self._lock:
            updated = dict(self._records)
            for record in records:
                updated[record.key] = record
            kept = self._write_locked(updated, identities)
            self._remember({item.key: item for item in kept})
            try:
                write_state_document(self.path, [item.payload() for item in kept])
            except OSError:
                # Routing state: memory keeps the refusal even when the file does not.
                logger.warning("Model Hub could not persist capability evidence; it holds until restart")

    def note_success(
        self,
        record: EvidenceRecord,
        *,
        live: Iterable[ModelHubSourceConfig],
        now: datetime,
    ) -> None:
        """Record a real success: the latest projection at once, the file at most hourly per key.

        A success never replaces a refusal (they are separate keys), so a late
        success cannot re-enable what a concurrent refusal took away.
        """

        if record.status != "verified":
            raise ValueError("note_success writes successes only")
        identities = {source.id: identity_digest(source) for source in live}
        if identities.get(record.source_id) != record.source_identity:
            return
        with self._lock:
            self._note_latest_locked(record)
            existing = self._records.get(record.key)
            if existing is not None and (now - existing.observed_at) < timedelta(seconds=POSITIVE_REFRESH_SECONDS):
                return
            updated = dict(self._records)
            updated[record.key] = record
            kept = self._write_locked(updated, identities)
            try:
                write_state_document(self.path, [item.payload() for item in kept])
            except OSError:
                logger.warning("Model Hub could not persist a hosted-tool success")
                return
            self._remember({item.key: item for item in kept})

    def clear(
        self,
        matches: Callable[[EvidenceRecord], bool],
        *,
        live: Iterable[ModelHubSourceConfig],
    ) -> int:
        """Remove the refusals ``matches`` selects: the file first, then memory; a failed write raises.

        Returns how many were removed; zero writes nothing.
        """

        identities = {source.id: identity_digest(source) for source in live}
        with self._lock:
            removed = [
                key for key, record in self._records.items() if record.status == "unsupported" and matches(record)
            ]
            if not removed:
                return 0
            updated = {key: record for key, record in self._records.items() if key not in set(removed)}
            kept = self._write_locked(updated, identities)
            write_state_document(self.path, [item.payload() for item in kept])
            self._remember({item.key: item for item in kept})
            return len(removed)
