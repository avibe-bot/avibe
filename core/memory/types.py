"""Small caller-facing value types for the Memory module."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from core.memory.presentation import MemoryStatusBuckets


MemoryKind = Literal["profile", "episode", "fact"]
MemoryContentKind = Literal["image", "audio", "doc", "pdf", "html", "email"]
MemoryOperationKind = Literal["add", "flush", "fingerprint_resolve"]
MemorySettlementSource = Literal[
    "add",
    "natural_boundary",
    "flush",
    "migration",
    "manual",
]
MemorySettlementOutcome = Literal[
    "succeeded",
    "rejected",
    "unknown",
    "manual_required",
    "committed",
    "not_committed",
    "settled_with_caveat",
]
MemoryObservedOutcome = MemorySettlementOutcome | Literal["in_flight"]
MemoryFlushState = Literal[
    "not_due",
    "due",
    "in_flight",
    "settled",
    "manual_required",
    "settled_with_caveat",
]
RecallMode = Literal["auto", "keyword", "vector", "hybrid", "agentic"]
RecallFreshness = Literal["eventual", "bounded", "session_overlay"]
MAX_RECALL_DECLARATIONS = 3
MAX_AGENTIC_RECALL_DECLARATIONS = 1
MAX_NON_AGENTIC_DECLARATION_RESULTS = 64
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
    """The canonical Avibe identity sent to and fenced around EverOS.

    ``session_id`` is the opaque provider session value.  Callers may retain a
    logical session id while constructing this value, but durable projections
    use the serialized four-part reference and never an ``app``-based key.
    """

    principal_id: str
    epoch: int
    project_ref: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise ValueError("provider session principal_id must be non-empty")
        if (
            isinstance(self.epoch, bool)
            or not isinstance(self.epoch, int)
            or self.epoch < 0
        ):
            raise ValueError("provider session epoch must be a non-negative integer")
        if not isinstance(self.project_ref, str) or not self.project_ref:
            raise ValueError("provider session project_ref must be non-empty")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("provider session session_id must be non-empty")

    def as_tuple(self) -> tuple[str, int, str, str]:
        """Return the identity in its canonical ordering."""

        return (self.principal_id, self.epoch, self.project_ref, self.session_id)

    def as_dict(self) -> dict[str, str | int]:
        """Return a JSON-ready projection with no origin ``app`` dimension."""

        return {
            "principal_id": self.principal_id,
            "epoch": self.epoch,
            "project_ref": self.project_ref,
            "session_id": self.session_id,
        }

    def serialize(self) -> str:
        """Serialize the reference deterministically for Avibe-owned SQLite."""

        return json.dumps(
            self.as_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    to_json = serialize

    @classmethod
    def deserialize(cls, value: str) -> "ProviderSessionRef":
        """Parse a canonical serialized reference without accepting extra fields."""

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
class CaptureTarget:
    """The trusted target for a later bounded-freshness recall."""

    session_ref: ProviderSessionRef
    target_generation: int
    target_watermark_ms: int


@dataclass(frozen=True)
class MemorySessionState:
    """Durable coordinator state for one canonical provider session."""

    provider_session_ref: ProviderSessionRef
    generation: int = 0
    first_unflushed_at: str | None = None
    last_add_ack_at: str | None = None
    due_at: str | None = None
    next_attempt_at: str | None = None
    flush_state: MemoryFlushState = "not_due"
    watermark: int = 0
    fence_epoch: int = 0
    fence_owner: str | None = None
    fence_acquired_at: str | None = None
    updated_at: str = ""


@dataclass(frozen=True)
class MemorySettlementRecord:
    """Append-only Avibe record of an add/flush outcome or manual decision."""

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
    actor: str | None = None
    decision: str | None = None
    evidence_ref: str | None = None
    settled_at: str | None = None
    confirmed_watermark_ms: int | None = None
    flush_state: MemoryFlushState | None = None
    source: MemorySettlementSource | None = None
    settlement_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class RecallBudget:
    """Per-run budget fields reserved for the later recall adapter."""

    limit: int = 10
    max_results: int | None = None
    freshness_timeout_seconds: int | None = None
    timeout_seconds: int | None = None
    max_model_calls: int | None = None
    cost_budget_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1:
            raise ValueError("recall declaration limit must be positive")
        for name, value in (
            ("max_results", self.max_results),
            ("freshness_timeout_seconds", self.freshness_timeout_seconds),
            ("timeout_seconds", self.timeout_seconds),
            ("max_model_calls", self.max_model_calls),
            ("cost_budget_tokens", self.cost_budget_tokens),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 1
            ):
                raise ValueError(f"recall declaration {name} must be positive")


@dataclass(frozen=True)
class RecallDeclaration:
    """One additional explicitly ordered recall run."""

    mode: Literal["keyword", "vector", "hybrid", "agentic"]
    budget: RecallBudget

    def __post_init__(self) -> None:
        if self.mode not in {"keyword", "vector", "hybrid", "agentic"}:
            raise ValueError("invalid recall declaration mode")
        if not isinstance(self.budget, RecallBudget):
            raise ValueError("recall declaration requires a RecallBudget")
        if (
            self.budget.max_results is not None
            and self.budget.max_results < self.budget.limit
        ):
            raise ValueError("declaration max_results must cover its limit")
        if self.mode != "agentic" and any(
            value is not None
            for value in (
                self.budget.max_results,
                self.budget.timeout_seconds,
                self.budget.max_model_calls,
                self.budget.cost_budget_tokens,
            )
        ):
            raise ValueError("agentic budget is only valid for agentic declarations")
        if self.mode == "agentic" and any(
            value is None
            for value in (
                self.budget.max_results,
                self.budget.timeout_seconds,
                self.budget.max_model_calls,
                self.budget.cost_budget_tokens,
            )
        ):
            raise ValueError("agentic declarations require complete budgets")


@dataclass(frozen=True)
class RecallPolicy:
    """Avibe-owned search policy shape reserved for the later recall phases."""

    mode: Literal["auto", "keyword", "vector", "hybrid", "agentic"] = "hybrid"
    limit: int = 10
    max_results: int | None = None
    freshness: RecallFreshness = "eventual"
    freshness_timeout_seconds: int | None = None
    wait_scope: ProviderSessionRef | None = None
    target_generation: int | None = None
    target_watermark_ms: int | None = None
    include_profile: bool = False
    filters: dict[str, Any] | None = None
    process_timeout_seconds: int = 30
    timeout_seconds: int | None = None
    max_model_calls: int | None = None
    cost_budget_tokens: int | None = None
    declarations: tuple[RecallDeclaration, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "keyword", "vector", "hybrid", "agentic"}:
            raise ValueError("invalid Memory recall mode")
        if self.freshness not in {"eventual", "bounded", "session_overlay"}:
            raise ValueError("invalid Memory recall freshness")
        if not isinstance(self.limit, int) or isinstance(self.limit, bool) or self.limit < 1:
            raise ValueError("recall limit must be positive")
        if self.max_results is not None:
            if (
                not isinstance(self.max_results, int)
                or isinstance(self.max_results, bool)
                or self.max_results < 1
            ):
                raise ValueError("recall max_results must be positive")
            if self.mode != "agentic":
                raise ValueError("max_results is only valid for agentic recall")
            if self.max_results < self.limit:
                raise ValueError("recall max_results must cover the recall limit")
        if (
            not isinstance(self.process_timeout_seconds, int)
            or isinstance(self.process_timeout_seconds, bool)
            or self.process_timeout_seconds < 1
        ):
            raise ValueError("recall process timeout must be positive")
        if self.freshness == "bounded":
            if (
                not isinstance(self.wait_scope, ProviderSessionRef)
                or self.target_generation is None
                or self.target_watermark_ms is None
            ):
                raise ValueError("bounded recall requires a complete session target")
            if (
                self.freshness_timeout_seconds is None
                or not isinstance(self.freshness_timeout_seconds, int)
                or isinstance(self.freshness_timeout_seconds, bool)
                or self.freshness_timeout_seconds < 1
            ):
                raise ValueError("bounded recall requires a positive freshness timeout")
        elif self.freshness == "session_overlay":
            if not isinstance(self.wait_scope, ProviderSessionRef):
                raise ValueError("session overlay recall requires a trusted session scope")
            if self.target_generation is not None or self.target_watermark_ms is not None:
                raise ValueError("generation and watermark targets are bounded-only")
            if self.freshness_timeout_seconds is not None:
                raise ValueError("freshness timeout is only valid for bounded recall")
        elif (
            self.wait_scope is not None
            or self.target_generation is not None
            or self.target_watermark_ms is not None
        ):
            raise ValueError("session target is only valid for bounded or overlay recall")
        elif self.freshness_timeout_seconds is not None:
            raise ValueError("freshness timeout is only valid for bounded recall")
        if self.target_generation is not None and (
            not isinstance(self.target_generation, int)
            or isinstance(self.target_generation, bool)
            or self.target_generation < 0
        ):
            raise ValueError("recall target generation must be non-negative")
        if self.target_watermark_ms is not None and (
            not isinstance(self.target_watermark_ms, int)
            or isinstance(self.target_watermark_ms, bool)
            or self.target_watermark_ms < 0
        ):
            raise ValueError("recall target watermark must be non-negative")
        if self.mode == "agentic":
            if any(
                value is None
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
                for value in (
                    self.max_results,
                    self.timeout_seconds,
                    self.max_model_calls,
                    self.cost_budget_tokens,
                )
            ):
                raise ValueError("agentic recall requires positive budgets")
        elif any(
            value is not None
            for value in (self.timeout_seconds, self.max_model_calls, self.cost_budget_tokens)
        ):
            raise ValueError("agentic budgets are only valid for agentic recall")
        if not isinstance(self.declarations, (tuple, list)):
            raise ValueError("recall declarations must be a sequence")
        declarations = tuple(self.declarations)
        object.__setattr__(self, "declarations", declarations)
        if len(declarations) > MAX_RECALL_DECLARATIONS:
            raise ValueError("recall declaration fan-out exceeded")
        if any(not isinstance(declaration, RecallDeclaration) for declaration in declarations):
            raise ValueError("recall declarations must contain RecallDeclaration values")
        agentic_declarations = [
            declaration
            for declaration in declarations
            if declaration.mode == "agentic"
        ]
        agentic_run_count = len(agentic_declarations) + (self.mode == "agentic")
        if agentic_run_count > MAX_AGENTIC_RECALL_DECLARATIONS:
            raise ValueError("recall agentic declaration fan-out exceeded")
        for declaration in declarations:
            budget = declaration.budget
            if budget.limit > self.limit:
                raise ValueError("declaration limit exceeds caller limit")
            if self.freshness == "bounded":
                if budget.freshness_timeout_seconds is None:
                    raise ValueError("bounded declarations require a freshness timeout")
                if budget.freshness_timeout_seconds > self.freshness_timeout_seconds:
                    raise ValueError("declaration freshness timeout exceeds caller timeout")
            elif budget.freshness_timeout_seconds is not None:
                raise ValueError("declaration freshness timeout is only valid for bounded recall")
        if (
            sum(
                declaration.budget.limit
                for declaration in declarations
                if declaration.mode != "agentic"
            )
            > MAX_NON_AGENTIC_DECLARATION_RESULTS
        ):
            raise ValueError("recall declaration result budget exceeded")
        total_timeout_seconds = (self.freshness_timeout_seconds or 0) + (
            self.timeout_seconds or 0
        )
        total_timeout_seconds += sum(
            (declaration.budget.freshness_timeout_seconds or 0)
            + (declaration.budget.timeout_seconds or 0)
            for declaration in declarations
        )
        if total_timeout_seconds > self.process_timeout_seconds:
            raise ValueError("recall declaration budget exceeds process timeout")


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
    app: str | None = None


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
    session_ref: ProviderSessionRef | None = field(default=None, compare=False)
    target_generation: int | None = field(default=None, compare=False)
    target_watermark_ms: int | None = field(default=None, compare=False)

    @property
    def target(self) -> CaptureTarget | None:
        if (
            self.session_ref is None
            or self.target_generation is None
            or self.target_watermark_ms is None
        ):
            return None
        return CaptureTarget(
            session_ref=self.session_ref,
            target_generation=self.target_generation,
            target_watermark_ms=self.target_watermark_ms,
        )


@dataclass(frozen=True)
class CaptureDuplicate:
    status: Literal["duplicate"] = "duplicate"
    session_ref: ProviderSessionRef | None = field(default=None, compare=False)
    target_generation: int | None = field(default=None, compare=False)
    target_watermark_ms: int | None = field(default=None, compare=False)

    @property
    def target(self) -> CaptureTarget | None:
        if (
            self.session_ref is None
            or self.target_generation is None
            or self.target_watermark_ms is None
        ):
            return None
        return CaptureTarget(
            session_ref=self.session_ref,
            target_generation=self.target_generation,
            target_watermark_ms=self.target_watermark_ms,
        )


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


# Short names are kept as public aliases so later coordinator modules can use
# the domain vocabulary without coupling callers to the storage projection's
# implementation prefix.
SessionFlushState = MemorySessionState
SettlementRecord = MemorySettlementRecord
