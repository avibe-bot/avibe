"""Lightweight contracts shared by Memory's host-facing HTTP boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal


MAX_AGENTIC_TIMEOUT_SECONDS = 30.0
# The implementation currently proves a 40-second composite read bound. Keep
# the host transport outside it without importing the optional implementation.
PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS = 45.0

RecallMode = Literal["auto", "keyword", "vector", "hybrid", "agentic"]

# Host-owned capability projection shared by settings and Memory admission.
# Keep the released platform set in one place so host surfaces do not import
# optional implementation modules just to render availability.
IM_ATTACHMENT_CAPTURE_PLATFORMS = frozenset(
    {"slack", "discord", "telegram", "lark", "wechat"}
)


@dataclass(frozen=True)
class RecallPolicy:
    """Host-owned bounded recall policy accepted by Memory HTTP routes."""

    mode: RecallMode = "hybrid"
    max_results: int = 8
    include_profile: bool = True
    include_current_session: bool = False
    timeout_seconds: float | None = None
    max_model_calls: int | None = None
    cost_budget_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"auto", "keyword", "vector", "hybrid", "agentic"}:
            raise ValueError("invalid recall mode")
        if (
            isinstance(self.max_results, bool)
            or not isinstance(self.max_results, int)
            or not 1 <= self.max_results <= 20
        ):
            raise ValueError("invalid recall result limit")
        if type(self.include_profile) is not bool or type(self.include_current_session) is not bool:
            raise ValueError("invalid recall include policy")
        if self.mode == "agentic":
            if (
                self.timeout_seconds is None
                or isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(float(self.timeout_seconds))
                or not 0 < float(self.timeout_seconds) <= MAX_AGENTIC_TIMEOUT_SECONDS
                or isinstance(self.max_model_calls, bool)
                or not isinstance(self.max_model_calls, int)
                or not 1 <= self.max_model_calls <= 4
                or isinstance(self.cost_budget_tokens, bool)
                or not isinstance(self.cost_budget_tokens, int)
                or not 1 <= self.cost_budget_tokens <= 32_000
            ):
                raise ValueError("agentic recall requires bounded budgets")
        elif any(
            value is not None
            for value in (
                self.timeout_seconds,
                self.max_model_calls,
                self.cost_budget_tokens,
            )
        ):
            raise ValueError("non-agentic recall cannot carry agentic budgets")

    @classmethod
    def from_payload(cls, value: object) -> "RecallPolicy":
        if not isinstance(value, dict):
            raise ValueError("invalid recall policy")
        allowed = {
            "mode",
            "max_results",
            "include_profile",
            "include_current_session",
            "timeout_seconds",
            "max_model_calls",
            "cost_budget_tokens",
        }
        if not set(value).issubset(allowed):
            raise ValueError("invalid recall policy")
        mode = value.get("mode", "hybrid")
        budget_fields = {
            "timeout_seconds",
            "max_model_calls",
            "cost_budget_tokens",
        }
        if mode == "agentic":
            if not {"max_results", *budget_fields}.issubset(value):
                raise ValueError("agentic recall requires explicit budgets")
        elif set(value).intersection(budget_fields):
            raise ValueError("non-agentic recall cannot carry agentic budgets")
        return cls(
            mode=mode,
            max_results=value.get("max_results", 8),
            include_profile=value.get("include_profile", True),
            include_current_session=value.get("include_current_session", False),
            timeout_seconds=value.get("timeout_seconds"),
            max_model_calls=value.get("max_model_calls"),
            cost_budget_tokens=value.get("cost_budget_tokens"),
        )

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "mode": self.mode,
            "max_results": self.max_results,
            "include_profile": self.include_profile,
            "include_current_session": self.include_current_session,
        }
        if self.mode == "agentic":
            payload.update(
                timeout_seconds=self.timeout_seconds,
                max_model_calls=self.max_model_calls,
                cost_budget_tokens=self.cost_budget_tokens,
            )
        return payload


def is_memory_principal_id(value: object) -> bool:
    """Validate the opaque principal shape without importing Memory storage."""

    return (
        isinstance(value, str)
        and len(value) == 34
        and value.startswith("u-")
        and all(char in "0123456789abcdef" for char in value[2:])
    )

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
    "memory_wake_failed",
    "memory_runtime_busy",
    "memory_permission_denied",
    "memory_disk_unavailable",
    "memory_sidecar_unavailable",
    "memory_provider_timeout",
    "memory_provider_response_invalid",
    "memory_capability_unavailable",
    "memory_processing_failed",
    "memory_loss_confirmation_required",
    "memory_local_data_unusable",
    "memory_legacy_recovery_required",
    "memory_embedding_unavailable",
    "memory_llm_unavailable",
    "memory_rerank_unavailable",
    "memory_multimodal_unavailable",
    "memory_repair_failed",
    "memory_repair_not_required",
    "memory_delete_data_failed",
    "memory_reconfigure_failed",
    "memory_operation_in_progress",
    "memory_implementation_unavailable",
    "memory_implementation_incompatible",
]

# This transport vocabulary is wider than the persistable failure vocabulary.
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
        "memory_wake_failed",
        "memory_runtime_busy",
        "memory_permission_denied",
        "memory_disk_unavailable",
        "memory_sidecar_unavailable",
        "memory_provider_timeout",
        "memory_provider_response_invalid",
        "memory_capability_unavailable",
        "memory_processing_failed",
        "memory_loss_confirmation_required",
        "memory_local_data_unusable",
        "memory_legacy_recovery_required",
        "memory_embedding_unavailable",
        "memory_llm_unavailable",
        "memory_rerank_unavailable",
        "memory_multimodal_unavailable",
        "memory_repair_failed",
        "memory_repair_not_required",
        "memory_delete_data_failed",
        "memory_reconfigure_failed",
        "memory_operation_in_progress",
        "memory_implementation_unavailable",
        "memory_implementation_incompatible",
    }
)


def is_memory_error_code(value: object) -> bool:
    """Return whether *value* is a closed Memory transport error code."""

    return isinstance(value, str) and value in CLOSED_MEMORY_ERROR_CODES


class MemoryStoreUnavailableError(RuntimeError):
    """The optional Memory store cannot currently serve a host request."""


class MemoryRuntimeBusyError(RuntimeError):
    """The provider root already belongs to another live Memory runtime."""


class MemoryImplementationUnavailableError(RuntimeError):
    """The optional Memory implementation is absent or cannot be constructed."""


class MemoryImplementationIncompatibleError(RuntimeError):
    """The optional Memory implementation lacks the required runtime surface."""
