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

from config.v2_config import ModelHubSourceConfig, is_model_hub_model_id, is_model_hub_source_id

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
ExecutionMode = Literal["native", "executor", "service", "direct"]

HOSTED_TOOL_ASPECTS: Mapping[str, tuple[str, ...]] = {
    "web_search": ("call", "replay"),
    "x_search": ("call", "replay"),
    # An image call is an ordinary tool call and result, so only its call is evidence.
    "image_generate": ("call",),
    "web_fetch": ("call",),
}
EXECUTION_MODES: frozenset[str] = frozenset({"native", "executor", "service", "direct"})
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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
        """The exact shape ``payload()`` writes: ``{kind, name}`` for an effort, ``{kind, name, aspect}`` for a tool."""

        if not isinstance(payload, Mapping):
            raise ValueError("invalid evidence feature")
        keys = {"kind", "name", "aspect"} if payload.get("kind") == "hosted_tool" else {"kind", "name"}
        if set(payload) != keys:
            raise ValueError("invalid evidence feature")
        return cls(payload["kind"], payload["name"], payload.get("aspect"))

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
        # The shared identifier rules, so a damaged state file's record is unknown, never echoed by a read.
        if not is_model_hub_source_id(self.source_id) or not is_model_hub_model_id(self.model_id):
            raise ValueError("evidence names a Source id and a canonical model id")
        if not all(isinstance(value, str) and _DIGEST_RE.fullmatch(value) for value in (self.wire, self.source_identity)):
            raise ValueError("evidence wire and source identity are sha256 digests")
        if self.status == "unsupported":
            if self.mode is not None or not isinstance(self.detail, str):
                raise ValueError("a refusal carries its detail and no mode")
            if not 0 < len(self.detail) <= DETAIL_MAX_CHARS or not _encodable(self.detail):
                raise ValueError(f"evidence detail is encodable text of at most {DETAIL_MAX_CHARS} characters")
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
      write raises with nothing changed, so it never comes back after a restart;
    * a success is display history: memory takes every one at once, so every
      read is current, and the file at most hourly per key. Memory is the one
      source of success history; nothing projects it separately.
    """

    def __init__(self, path: Path, *, max_records: int = MAX_RECORDS):
        self.path = path
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: dict[tuple, EvidenceRecord] = {}
        self._by_hop: dict[tuple[str, str], list[EvidenceRecord]] = {}
        records = self._read_file()
        # The file's copy of each success, which throttles the next write of that key.
        self._persisted: dict[tuple, EvidenceRecord] = {
            key: record for key, record in records.items() if record.status == "verified"
        }
        self._remember(records)

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

    def _persist_locked(self, kept: Iterable[EvidenceRecord]) -> None:
        """Write the kept records to the file; raises OSError. Every writer goes through here."""

        kept = list(kept)
        write_state_document(self.path, [item.payload() for item in kept])
        self._persisted = {item.key: item for item in kept if item.status == "verified"}

    @staticmethod
    def _supersedes(record: EvidenceRecord, existing: Optional[EvidenceRecord], current: Mapping[str, str]) -> bool:
        """The one ordering rule every write follows: per key, the newest current record wins.

        Writes settle concurrently, so arrival order is not time order. ``record`` is always current: the writers
        drop one of a superseded identity or wire before they get here. It replaces a stored record that is no
        longer current at once (another identity, or for a refusal another wire, the one ``record`` proves
        current), and a current one only if it is newer.
        """

        return (
            existing is None
            or current.get(existing.source_id) != existing.source_identity
            or (record.status == "unsupported" and existing.wire != record.wire)
            or record.observed_at > existing.observed_at
        )

    @staticmethod
    def _live_locked(
        live: Callable[[], Iterable[ModelHubSourceConfig]],
    ) -> tuple[dict[str, ModelHubSourceConfig], dict[str, str]]:
        """The Sources as they are now, read under the store lock so no write prunes against an older snapshot."""

        sources = {source.id: source for source in live()}
        return sources, {source_id: identity_digest(source) for source_id, source in sources.items()}

    def write_refusal(
        self,
        record: EvidenceRecord,
        *,
        live: Callable[[], Iterable[ModelHubSourceConfig]],
        wire_for: Callable[[ModelHubSourceConfig, str], str],
    ) -> None:
        """Record a refusal: memory first, then the file; a failed write is logged, not raised."""

        self.write_refusals([record], live=live, wire_for=wire_for)

    def write_refusals(
        self,
        records: Iterable[EvidenceRecord],
        *,
        live: Callable[[], Iterable[ModelHubSourceConfig]],
        wire_for: Callable[[ModelHubSourceConfig, str], str],
    ) -> None:
        """Record refusals learned on requests that may have been admitted before a Source or model changed.

        Only a refusal of the Source as it is now, under the wire it renders now, is written: one learned under a
        superseded identity or wire describes nothing that routes, and must not replace a current one.
        """

        records = list(records)
        if any(record.status != "unsupported" for record in records):
            raise ValueError("write_refusals writes refusals only")
        with self._lock:
            sources, identities = self._live_locked(live)
            updated = dict(self._records)
            for record in records:
                source = sources.get(record.source_id)
                if (
                    source is None
                    or identities[record.source_id] != record.source_identity
                    or wire_for(source, record.model_id) != record.wire
                ):
                    logger.info("Model Hub dropped a refusal learned under a superseded Source identity or wire")
                    continue
                if self._supersedes(record, updated.get(record.key), identities):
                    updated[record.key] = record
            kept = self._write_locked(updated, identities)
            self._remember({item.key: item for item in kept})
            try:
                self._persist_locked(kept)
            except OSError:
                # Routing state: memory keeps the refusal even when the file does not.
                logger.warning("Model Hub could not persist capability evidence; it holds until restart")

    def note_success(
        self,
        record: EvidenceRecord,
        *,
        live: Callable[[], Iterable[ModelHubSourceConfig]],
        now: datetime,
    ) -> None:
        """Record a real success: memory at once, the file at most hourly per key.

        A success never replaces a refusal (they are separate keys), so a late
        success cannot re-enable what a concurrent refusal took away.
        """

        if record.status != "verified":
            raise ValueError("note_success writes successes only")
        with self._lock:
            _sources, identities = self._live_locked(live)
            if identities.get(record.source_id) != record.source_identity or not self._supersedes(
                record, self._records.get(record.key), identities
            ):
                return
            updated = dict(self._records)
            updated[record.key] = record
            kept = {item.key: item for item in self._write_locked(updated, identities)}
            self._remember(kept)
            persisted = self._persisted.get(record.key)
            # Throttled only while the file holds a recent success of this same Source identity: the first success
            # after an identity change is written at once, since the file's copy no longer counts.
            if (
                persisted is not None
                and persisted.source_identity == record.source_identity
                and (now - persisted.observed_at) < timedelta(seconds=POSITIVE_REFRESH_SECONDS)
            ):
                return
            try:
                self._persist_locked(kept.values())
            except OSError:
                logger.warning("Model Hub could not persist a hosted-tool success")

    def clear(
        self,
        matches: Callable[[EvidenceRecord], bool],
        *,
        live: Callable[[], Iterable[ModelHubSourceConfig]],
    ) -> int:
        """Remove the refusals ``matches`` selects: the file first, then memory; a failed write raises.

        Returns how many were removed; zero writes nothing.
        """

        with self._lock:
            _sources, identities = self._live_locked(live)
            removed = [
                key for key, record in self._records.items() if record.status == "unsupported" and matches(record)
            ]
            if not removed:
                return 0
            updated = {key: record for key, record in self._records.items() if key not in set(removed)}
            kept = self._write_locked(updated, identities)
            self._persist_locked(kept)
            self._remember({item.key: item for item in kept})
            return len(removed)
