"""Small caller-facing value types for the Memory module."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, TypeAlias


MemoryKind = Literal["profile", "episode", "fact"]
MemoryContentKind = Literal["image", "audio", "doc", "pdf", "html", "email"]
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
    "memory_sidecar_unavailable",
    "memory_provider_timeout",
    "memory_provider_response_invalid",
    "memory_processing_failed",
    "memory_clear_failed",
]

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
class MemoryItem:
    kind: MemoryKind
    text: str
    date: str | None = None


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
