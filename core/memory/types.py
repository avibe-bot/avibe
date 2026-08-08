"""Small caller-facing value types for the Memory module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from core.memory.presentation import MemoryStatusBuckets


MemoryKind = Literal["profile", "episode", "fact"]
MemoryContentKind = Literal["image", "audio", "doc", "pdf", "html", "email"]
MemoryOperationKind = Literal["add", "flush"]
MemorySettlementOutcome = Literal["succeeded", "rejected", "unknown", "manual_required"]
MemoryObservedOutcome = MemorySettlementOutcome | Literal["in_flight"]
MemorySettlementSource = Literal["add", "flush"]
MemoryFlushState = Literal["not_due", "due", "in_flight", "manual_required"]
MemoryFailureKind = Literal[
    "delivery_abandoned",
    "distillation_rejected",
    "result_unknown",
]
MemoryErrorCode = Literal[
    "memory_disabled",
    "memory_invalid_input",
    "memory_access_denied",
    "memory_input_too_large",
    "memory_queue_full",
    "memory_low_disk_space",
    "memory_store_unavailable",
    "memory_runtime_missing",
    "memory_runtime_unsupported",
    "memory_runtime_install_failed",
    "memory_reconcile_failed",
    "memory_restart_failed",
    "memory_sidecar_unavailable",
    "memory_provider_timeout",
    "memory_provider_response_invalid",
    "memory_processing_failed",
    "memory_clear_failed",
]

# Transport vocabulary, wider than the persistable one: errors such as
# ``memory_access_denied``, ``memory_reconcile_failed``, and
# ``memory_restart_failed`` never reach a stored ``last_error`` column.
CLOSED_MEMORY_ERROR_CODES = frozenset(
    {
        "memory_disabled",
        "memory_invalid_input",
        "memory_access_denied",
        "memory_input_too_large",
        "memory_queue_full",
        "memory_low_disk_space",
        "memory_store_unavailable",
        "memory_runtime_missing",
        "memory_runtime_unsupported",
        "memory_runtime_install_failed",
        "memory_reconcile_failed",
        "memory_restart_failed",
        "memory_sidecar_unavailable",
        "memory_provider_timeout",
        "memory_provider_response_invalid",
        "memory_processing_failed",
        "memory_clear_failed",
    }
)


def is_memory_error_code(value: object) -> bool:
    """Return whether *value* is a closed Memory error code."""

    return isinstance(value, str) and value in CLOSED_MEMORY_ERROR_CODES


@dataclass(frozen=True)
class ProviderSessionRef:
    """Canonical provider identity used by durable coordination state."""

    principal_id: str
    epoch: int
    project_ref: str
    session_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("principal_id", self.principal_id),
            ("project_ref", self.project_ref),
            ("session_id", self.session_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"provider session {name} must be non-empty")
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch < 0:
            raise ValueError("provider session epoch must be a non-negative integer")

    def as_tuple(self) -> tuple[str, int, str, str]:
        """Return the identity in its canonical ordering."""

        return (self.principal_id, self.epoch, self.project_ref, self.session_id)

    def as_dict(self) -> dict[str, str | int]:
        """Return the exact durable JSON projection."""

        return {
            "principal_id": self.principal_id,
            "epoch": self.epoch,
            "project_ref": self.project_ref,
            "session_id": self.session_id,
        }

    def serialize(self) -> str:
        """Serialize deterministically for Avibe-owned SQLite state."""

        return json.dumps(self.as_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    to_json = serialize

    @classmethod
    def deserialize(cls, value: str) -> "ProviderSessionRef":
        """Deserialize a reference without accepting an alternate key shape."""

        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            raise ValueError("invalid provider session reference") from None
        if not isinstance(payload, dict) or set(payload) != {
            "principal_id",
            "epoch",
            "project_ref",
            "session_id",
        }:
            raise ValueError("invalid provider session reference")
        return cls(
            principal_id=payload["principal_id"],
            epoch=payload["epoch"],
            project_ref=payload["project_ref"],
            session_id=payload["session_id"],
        )

    from_serialized = deserialize


@dataclass(frozen=True)
class MemorySessionState:
    """Durable coordinator scaffolding for one provider session."""

    provider_session_ref: ProviderSessionRef
    generation: int = 0
    first_unflushed_at: str | None = None
    last_add_ack_at: str | None = None
    due_at: str | None = None
    next_attempt_at: str | None = None
    flush_retry_count: int = 0
    flush_state: MemoryFlushState = "not_due"
    watermark: int = 0
    fence_epoch: int = 0
    fence_operation_id: str | None = None
    fence_owner: str | None = None
    fence_acquired_at: str | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class MemoryFlushToken:
    """Exact durable token acquired before one provider flush call."""

    provider_session_ref: ProviderSessionRef
    generation: int
    fence_epoch: int
    operation_id: str


@dataclass(frozen=True)
class MemorySettlementRecord:
    """Append-only Avibe evidence for an add or flush outcome."""

    provider_session_ref: ProviderSessionRef
    generation: int
    fence_epoch: int
    operation_id: str
    operation_kind: MemoryOperationKind
    outcome: MemorySettlementOutcome
    observed_at: str
    last_known_state: str | None = None
    last_observed_outcome: MemoryObservedOutcome | None = None
    request_id: str | None = None
    error_code: str | None = None
    watermark_before: int | None = None
    watermark_after: int | None = None
    settled_at: str | None = None
    confirmed_watermark_ms: int | None = None
    flush_state: MemoryFlushState | None = None
    source: MemorySettlementSource | None = None
    settlement_id: str = ""

    def __post_init__(self) -> None:
        for name, value in (
            ("generation", self.generation),
            ("fence_epoch", self.fence_epoch),
            ("watermark_before", self.watermark_before),
            ("watermark_after", self.watermark_after),
            ("confirmed_watermark_ms", self.confirmed_watermark_ms),
        ):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"settlement {name} must be a non-negative integer")
        if not self.operation_id:
            raise ValueError("settlement operation_id must be non-empty")
        if not self.settlement_id:
            object.__setattr__(self, "settlement_id", self.operation_id)


@dataclass(frozen=True)
class CaptureAttachment:
    """One Workbench-owned local file forwarded unchanged to the provider."""

    kind: MemoryContentKind
    name: str
    uri: str
    ext: str


@dataclass(frozen=True)
class CaptureRequest:
    source_message_id: str
    session_id: str
    principal_id: str
    project_id: str
    provenance: Literal["user_input", "agent"]
    text: str
    occurred_at_ms: int
    attachments: tuple[CaptureAttachment, ...] = ()


def encode_capture_attachments(attachments: tuple[CaptureAttachment, ...]) -> str | None:
    if not attachments:
        return None
    return json.dumps(
        [
            {
                "type": attachment.kind,
                "name": attachment.name,
                "uri": attachment.uri,
                "ext": attachment.ext,
            }
            for attachment in attachments
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_capture_attachments(payload: str | None) -> tuple[CaptureAttachment, ...] | None:
    if payload is None:
        return ()
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        return None
    attachments: list[CaptureAttachment] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "name", "uri", "ext"}:
            return None
        kind = item.get("type")
        name = item.get("name")
        uri = item.get("uri")
        ext = item.get("ext")
        if (
            kind not in {"image", "audio", "doc", "pdf", "html", "email"}
            or not isinstance(name, str)
            or not isinstance(uri, str)
            or not isinstance(ext, str)
        ):
            return None
        attachments.append(CaptureAttachment(kind=kind, name=name, uri=uri, ext=ext))
    return tuple(attachments)


@dataclass(frozen=True)
class CaptureAccepted:
    status: Literal["accepted"] = "accepted"


@dataclass(frozen=True)
class CaptureDuplicate:
    status: Literal["duplicate"] = "duplicate"


@dataclass(frozen=True)
class CaptureSkipped:
    reason: MemoryErrorCode
    status: Literal["skipped"] = "skipped"


@dataclass(frozen=True)
class OperationFailed:
    error: MemoryErrorCode
    status: Literal["failed"] = "failed"


CaptureReceipt: TypeAlias = CaptureAccepted | CaptureDuplicate | CaptureSkipped | OperationFailed


@dataclass(frozen=True)
class MemoryProfileExplicitInfo:
    """One directly stated profile fact from the provider."""

    description: str
    category: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class MemoryProfileTrait:
    """One inferred profile trait, keeping its basis distinct from evidence."""

    description: str
    trait: str | None = None
    basis: str | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class MemoryProfile:
    """The recognized, readable portion of an opaque provider profile."""

    summary: str | None = None
    explicit_info: tuple[MemoryProfileExplicitInfo, ...] = ()
    implicit_traits: tuple[MemoryProfileTrait, ...] = ()
    updated_at: str | None = None


@dataclass(frozen=True)
class MemoryItem:
    kind: MemoryKind
    text: str
    date: str | None = None
    profile: MemoryProfile | None = None


def memory_profile_payload(profile: MemoryProfile) -> dict[str, Any]:
    """Project a profile into the closed, JSON-ready Memory envelope."""

    return {
        "summary": profile.summary,
        "explicit_info": [
            {
                "description": info.description,
                "category": info.category,
                "evidence": info.evidence,
            }
            for info in profile.explicit_info
        ],
        "implicit_traits": [
            {
                "description": trait.description,
                "trait": trait.trait,
                "basis": trait.basis,
                "evidence": trait.evidence,
            }
            for trait in profile.implicit_traits
        ],
        "updated_at": profile.updated_at,
    }


def memory_item_payload(item: MemoryItem) -> dict[str, Any]:
    """Serialize one item without widening legacy item payloads with nulls."""

    payload: dict[str, Any] = {
        "kind": item.kind,
        "text": item.text,
        "date": item.date,
    }
    if item.profile is not None:
        payload["profile"] = memory_profile_payload(item.profile)
    return payload


@dataclass(frozen=True)
class MemoryItems:
    items: tuple[MemoryItem, ...] = ()
    warnings: tuple[MemoryErrorCode, ...] = ()
    status: Literal["ok"] = "ok"


MemoryResult: TypeAlias = MemoryItems | OperationFailed


@dataclass(frozen=True)
class MemoryStatus:
    state: Literal[
        "disabled",
        "starting",
        "ready",
        "syncing",
        "degraded",
        "down",
        "clearing",
        "error",
    ]
    pending: int = 0
    processing: int = 0
    awaiting_receipt: int = 0
    succeeded: int = 0
    receipt_unknown: int = 0
    distill_failed: int = 0
    dead: int = 0
    missed: int = 0
    queue_plaintext_bytes: int = 0
    provider_disk_bytes: int = 0
    last_success_at: str | None = None
    last_flush_observation: Literal["succeeded", "rejected", "unknown"] | None = None
    last_flush_status: Literal["extracted", "no_extraction"] | None = None
    last_flush_error_code: str | None = None
    last_flush_request_id: str | None = None
    last_flush_at: str | None = None
    processing_fault_kind: Literal["credential", "engine"] | None = None
    processing_fault_since: str | None = None
    processing_alert_active: bool = False
    error: MemoryErrorCode | None = None
    # Derived once from the counters above so every surface renders the same
    # six numbers; the raw counters stay published for callers that need them.
    buckets: MemoryStatusBuckets = MemoryStatusBuckets()
    # Discriminates a status body from the ``{"status": "failed"}`` envelope the
    # same routes return, matching every other result type in this module.
    status: Literal["ok"] = "ok"


@dataclass(frozen=True)
class MemoryFailureLogEntry:
    """One sanitized terminal failure observation retained by Avibe."""

    kind: MemoryFailureKind
    occurred_at: str
    error_code: str | None = None
    request_id: str | None = None
    attempts: int = 0


@dataclass(frozen=True)
class ClearCompleted:
    epoch: int
    status: Literal["completed"] = "completed"


ClearReceipt: TypeAlias = ClearCompleted | OperationFailed
