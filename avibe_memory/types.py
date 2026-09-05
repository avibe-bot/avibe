"""Small caller-facing value types for the Memory module."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from vibe.memory_contract import (
    CLOSED_MEMORY_ERROR_CODES,
    MAX_AGENTIC_TIMEOUT_SECONDS as MAX_AGENTIC_TIMEOUT_SECONDS,
    MAX_MEMORY_LIST_PAGE_SIZE as MAX_MEMORY_LIST_PAGE_SIZE,
    MAX_MEMORY_SEARCH_RESULTS as MAX_MEMORY_SEARCH_RESULTS,
    MemoryErrorCode,
    RecallPolicy,
    is_memory_error_code,
)

MemoryKind = Literal["profile", "episode", "fact"]
MemoryOrigin = Literal["user", "agent"]
RecallMode = Literal["auto", "keyword", "vector", "hybrid", "agentic"]
MemoryContentKind = Literal["image", "audio", "doc", "pdf", "html", "email"]
MemoryFailureKind = Literal[
    "boot_recovery",
    "delivery_abandoned",
    "distillation_rejected",
    "result_unknown",
]
@dataclass(frozen=True)
class MemoryPreflightDiagnostic:
    side: Literal["embedding", "llm", "rerank", "multimodal"]
    http_status: int | None = None
    provider_error_code: str | None = None
    message: str = "provider unavailable"

    def payload(self) -> dict[str, object]:
        return {
            "side": self.side,
            "http_status": self.http_status,
            "provider_error_code": self.provider_error_code,
            "message": self.message[:512],
        }


@dataclass(frozen=True)
class ProviderSessionRef:
    """Canonical provider identity; ``principal_id`` carries the Memory owner."""

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
        """Return the canonical identity ordering."""

        return (self.principal_id, self.epoch, self.project_ref, self.session_id)

    def serialize(self) -> str:
        """Serialize deterministically for Avibe-owned SQLite state."""

        return json.dumps(
            {
                "principal_id": self.principal_id,
                "epoch": self.epoch,
                "project_ref": self.project_ref,
                "session_id": self.session_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    to_json = serialize

    @classmethod
    def deserialize(cls, value: str) -> "ProviderSessionRef":
        """Deserialize only the canonical four-field identity shape."""

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
    attachment_config_generation: int | None = None
    sender_name: str | None = None


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
    captured_attachment_count: int = 0


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


MemoryWarningCode = Literal["memory_search_partial", "memory_search_truncated"]
MemoryListWarningCode = Literal["memory_list_partial", "memory_list_truncated"]
CLOSED_MEMORY_WARNING_CODES = frozenset(
    {
        "memory_search_partial",
        "memory_search_truncated",
        "memory_list_partial",
        "memory_list_truncated",
    }
)


def is_memory_warning_code(value: object) -> bool:
    return isinstance(value, str) and value in CLOSED_MEMORY_WARNING_CODES


@dataclass(frozen=True)
class MemoryItem:
    kind: MemoryKind
    text: str
    date: str | None = None
    profile: MemoryProfile | None = None
    project: str | None = None
    origin: Literal["user", "agent", "both"] | None = None


@dataclass(frozen=True)
class ProviderSearchItem:
    """Provider-only search metadata used to merge owner-scoped legs."""

    item: MemoryItem
    score: float | None
    episode_id: str | None
    timestamp: str | None
    provider_rank: int
    queried_owner: str


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
    if item.project is not None:
        payload["project"] = item.project
    if item.origin is not None:
        payload["origin"] = item.origin
    return payload


@dataclass(frozen=True)
class MemoryItems:
    items: tuple[MemoryItem, ...] = ()
    warnings: tuple[MemoryErrorCode | MemoryWarningCode, ...] = ()
    status: Literal["ok"] = "ok"


MemoryResult: TypeAlias = MemoryItems | OperationFailed


@dataclass(frozen=True)
class MemoryListItem:
    """One provider-neutral processed episode returned by a list read."""

    id: str
    subject: str
    summary: str
    body: str
    timestamp: str
    project: str
    kind: Literal["episode"] = "episode"
    origin: MemoryOrigin | None = None


@dataclass(frozen=True)
class MemoryListPage:
    """One exact 1-based provider page of processed episodes."""

    items: tuple[MemoryListItem, ...]
    page: int
    page_size: int
    count: int
    total_count: int
    warnings: tuple[MemoryListWarningCode, ...] = ()
    status: Literal["ok"] = "ok"


MemoryListResult: TypeAlias = MemoryListPage | OperationFailed


def memory_list_page_payload(result: MemoryListPage) -> dict[str, Any]:
    """Serialize a validated list page into the closed transport envelope."""

    return {
        "status": result.status,
        "items": [
            {
                "id": item.id,
                "kind": item.kind,
                "subject": item.subject,
                "summary": item.summary,
                "body": item.body,
                "timestamp": item.timestamp,
                "project": item.project,
                **({"origin": item.origin} if item.origin is not None else {}),
            }
            for item in result.items
        ],
        "page": result.page,
        "page_size": result.page_size,
        "count": result.count,
        "total_count": result.total_count,
        "warnings": list(result.warnings),
    }


@dataclass(frozen=True)
class RecallItems:
    items: tuple[MemoryItem, ...]
    requested_mode: RecallMode
    effective_mode: Literal["keyword", "vector", "hybrid", "agentic"]
    source: Literal["everos"] = "everos"
    current_session_overlay: bool = False
    watermark_ms: int | None = None
    freshness: Literal["unknown"] = "unknown"
    warnings: tuple[MemoryErrorCode | MemoryWarningCode, ...] = ()
    status: Literal["ok"] = "ok"


RecallResult: TypeAlias = RecallItems | OperationFailed


@dataclass(frozen=True)
class MemoryFailureLogEntry:
    """One sanitized terminal failure observation retained by Avibe."""

    id: str
    kind: MemoryFailureKind
    occurred_at: str
    error_code: str | None = None
    request_id: str | None = None
    attempts: int = 0
    state: str = "unknown"
    operation: str = "unknown"
    generation: int = 0
