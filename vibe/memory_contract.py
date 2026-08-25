"""Lightweight contracts shared by Memory's host-facing HTTP boundary."""

from __future__ import annotations

from typing import Literal


MAX_AGENTIC_TIMEOUT_SECONDS = 30.0
# The implementation currently proves a 40-second composite read bound. Keep
# the host transport outside it without importing the optional implementation.
PROCESSING_RECORD_TRANSPORT_TIMEOUT_SECONDS = 45.0

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
    }
)


def is_memory_error_code(value: object) -> bool:
    """Return whether *value* is a closed Memory transport error code."""

    return isinstance(value, str) and value in CLOSED_MEMORY_ERROR_CODES


class MemoryStoreUnavailableError(RuntimeError):
    """The optional Memory store cannot currently serve a host request."""


class MemoryRuntimeCloseUnprovedError(RuntimeError):
    """Controller ownership remains fenced because runtime close was unproved."""
