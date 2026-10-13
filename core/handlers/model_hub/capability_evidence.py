"""Per-Source capability evidence (``model-hub-capability-model.md`` §3).

One record per (Source, model, feature). Absence of a record is ``unknown``,
and unknown is never negative (#2505): only explicit negative evidence excludes,
and only positive evidence enables a feature whose failure is terminal or
silent (a native hosted tool).

Two writers exist, and only they write here: a probe the user started, and the
gateway classifier's narrow rules on a real upstream error. This module owns
the persisted shape, its expiry and staleness, and how a record resolves for
routing. It decides nothing about requests itself.

Records follow Source identity: each one stores a digest of
``retry.source_identity`` at the time it was written, and a record whose
Source identity has since changed reads as absent and is pruned at the next
write.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from config.v2_config import ModelHubSourceConfig

from .retry import source_identity
from .state_file import write_state_document

EVIDENCE_VERSION = 1
MAX_RECORDS = 4000
OBSERVED_TTL_SECONDS = 7 * 24 * 3600
PROBE_REVERIFY_AFTER_SECONDS = 30 * 24 * 3600
DETAIL_MAX_CHARS = 64
# The engine stamp when the engine manifest cannot be read. It names no release,
# so probe evidence taken under it, or checked against it, never vouches for a wire.
UNKNOWN_ENGINE_STAMP = "unknown-engine"

FeatureKind = Literal["effort", "hosted_tool"]
EvidenceStatus = Literal["verified", "unsupported"]
EvidenceBasis = Literal["probe", "observed"]
RoutingStatus = Literal["verified", "unsupported", "unknown"]
Staleness = Literal["wire", "age"]

HOSTED_TOOL_ASPECTS: Mapping[str, tuple[str, ...]] = {
    "web_search": ("call", "replay"),
    "x_search": ("call", "replay"),
    # A generated image is never replayed verbatim, so only its call is evidence.
    "image_generation": ("call",),
}
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
    basis: EvidenceBasis
    observed_at: datetime
    engine: str
    shape: int
    source_identity: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("source_id", "model_id", "engine", "source_identity"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"evidence {name} must be a non-empty string")
        if self.status not in ("verified", "unsupported") or self.basis not in ("probe", "observed"):
            raise ValueError("invalid evidence status or basis")
        if self.basis == "observed" and (self.status != "unsupported" or not self.detail):
            raise ValueError("observed evidence is always unsupported and carries its detail")
        if self.detail is not None and (not isinstance(self.detail, str) or not 0 < len(self.detail) <= DETAIL_MAX_CHARS):
            raise ValueError(f"evidence detail is at most {DETAIL_MAX_CHARS} characters")
        if isinstance(self.shape, bool) or not isinstance(self.shape, int) or self.shape < 0:
            raise ValueError("evidence shape is a non-negative integer")
        if self.observed_at.tzinfo is None:
            raise ValueError("evidence time must be timezone-aware")

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.source_id, self.model_id, self.feature.kind, self.feature.name, self.feature.aspect or "")

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": EVIDENCE_VERSION,
            "source_id": self.source_id,
            "model_id": self.model_id,
            "feature": self.feature.payload(),
            "status": self.status,
            "basis": self.basis,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "engine": self.engine,
            "shape": self.shape,
            "source_identity": self.source_identity,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_payload(cls, payload: Any) -> "EvidenceRecord":
        if not isinstance(payload, Mapping) or payload.get("version") != EVIDENCE_VERSION:
            raise ValueError("unknown evidence record version")
        allowed = {
            "version", "source_id", "model_id", "feature", "status", "basis",
            "observed_at", "engine", "shape", "source_identity", "detail",
        }
        if set(payload) - allowed:
            raise ValueError("unknown evidence record field")
        feature = payload.get("feature")
        if not isinstance(feature, Mapping) or set(feature) - {"kind", "name", "aspect"}:
            raise ValueError("invalid evidence feature")
        observed_at = payload.get("observed_at")
        if not isinstance(observed_at, str) or not observed_at.endswith("Z"):
            raise ValueError("evidence time must be UTC")
        return cls(
            source_id=payload.get("source_id"),
            model_id=payload.get("model_id"),
            feature=EvidenceFeature(feature.get("kind"), feature.get("name"), feature.get("aspect")),
            status=payload.get("status"),
            basis=payload.get("basis"),
            observed_at=datetime.fromisoformat(observed_at[:-1] + "+00:00"),
            engine=payload.get("engine"),
            shape=payload.get("shape"),
            source_identity=payload.get("source_identity"),
            detail=payload.get("detail"),
        )


@dataclass(frozen=True)
class EvidenceView:
    """How one record resolves now.

    ``status`` is what routing uses. ``stale`` asks for a re-verify: ``wire``
    when the record was taken on another engine or probe shape, ``age`` when it
    is merely old. ``record`` is kept for display even when it no longer routes.
    """

    status: RoutingStatus
    stale: Optional[Staleness] = None
    record: Optional[EvidenceRecord] = None


UNKNOWN = EvidenceView("unknown")


def identity_digest(source: ModelHubSourceConfig) -> str:
    """Stable, credential-free digest of the Source identity that evidence follows."""

    raw = json.dumps(list(source_identity(source)), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve(
    record: Optional[EvidenceRecord],
    *,
    source: ModelHubSourceConfig,
    engine: str,
    shape: int,
    now: datetime,
) -> EvidenceView:
    """Resolve one record for routing (capability model §3.3, §3.4)."""

    if record is None or record.source_identity != identity_digest(source):
        return UNKNOWN
    age = (now - record.observed_at).total_seconds()
    if record.basis == "observed":
        # Relays change; an expired negative record falls back to the priors.
        return UNKNOWN if age > OBSERVED_TTL_SECONDS else EvidenceView("unsupported", None, record)
    wire_stale = (
        record.engine != engine
        or record.shape != shape
        or UNKNOWN_ENGINE_STAMP in (record.engine, engine)
    )
    stale: Optional[Staleness] = "wire" if wire_stale else "age" if age > PROBE_REVERIFY_AFTER_SECONDS else None
    if record.feature.kind == "hosted_tool" and wire_stale:
        # Evidence taken on another wire cannot vouch for this one, and a silent
        # native drop would never trip a narrow rule: route as unknown (proxy).
        return EvidenceView("unknown", stale, record)
    return EvidenceView(record.status, stale, record)


class CapabilityEvidenceStore:
    """The evidence records, held in memory and persisted on every write.

    Small: bounded by Sources x models x features. The controller is the only
    writer (probes and the classifier both run there; other processes reach it
    over RPC), so the file is read once, when the store is built at controller
    startup, and memory is authoritative afterwards. No lookup ever touches the
    disk, so none can block the loop.
    """

    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS):
        self.path = path
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: dict[tuple, EvidenceRecord] = {}
        self._by_hop: dict[tuple[str, str], list[EvidenceRecord]] = {}
        self._remember(self._read_file())

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
        # The index first: a reader that sees the new records also sees their index.
        self._by_hop = by_hop
        self._records = records

    def get(self, source_id: str, model_id: str, feature: EvidenceFeature) -> Optional[EvidenceRecord]:
        # Published snapshots are never mutated, so a reader needs no lock: a
        # write builds the next snapshot and swaps the references.
        return self._records.get((source_id, model_id, feature.kind, feature.name, feature.aspect or ""))

    def view(
        self,
        source: ModelHubSourceConfig,
        model_id: str,
        feature: EvidenceFeature,
        *,
        engine: str,
        shape: int,
        now: datetime,
    ) -> EvidenceView:
        return resolve(self.get(source.id, model_id, feature), source=source, engine=engine, shape=shape, now=now)

    def records(self) -> list[EvidenceRecord]:
        return list(self._records.values())

    def for_hop(self, source_id: str, model_id: str) -> list[EvidenceRecord]:
        """Every record of one (Source, model), without scanning the others."""

        return list(self._by_hop.get((source_id, model_id), ()))

    def write(self, record: EvidenceRecord, *, live: Iterable[ModelHubSourceConfig]) -> None:
        """Replace the record for its key; prune records of removed or re-identified Sources.

        Memory changes only once the file holds the new records.
        """

        identities = {source.id: identity_digest(source) for source in live}
        if identities.get(record.source_id) != record.source_identity:
            raise ValueError("evidence must be written for a live Source's current identity")
        with self._lock:
            records = {
                key: existing
                for key, existing in self._records.items()
                if identities.get(existing.source_id) == existing.source_identity
            }
            records[record.key] = record
            kept = sorted(records.values(), key=lambda item: item.observed_at)[-self.max_records :]
            write_state_document(self.path, [item.payload() for item in kept])
            self._remember({item.key: item for item in kept})
