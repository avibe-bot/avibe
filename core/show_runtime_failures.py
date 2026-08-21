from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ShowRuntimeFailureClass(str, Enum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    CONFIGURED = "configured"
    CHECKSUM = "checksum"
    UNCLASSIFIED = "unclassified"


class ShowRuntimeFailureDimension(str, Enum):
    POLICY = "policy"
    INSTALL = "install"
    RUNTIME = "runtime"


class ShowRuntimeRetryDisposition(str, Enum):
    CONTINUOUS = "continuous"
    CONFIRMATION_PENDING = "confirmation_pending"
    MANUAL_ONLY = "manual_only"


class ShowRuntimeRecoveryAction(str, Enum):
    REPAIR = "repair"
    CHANGE_SETTING = "change_setting"
    NO_LOCAL_ACTION = "no_local_action"


@dataclass(frozen=True)
class ShowRuntimeFailureEvidence:
    dimension: ShowRuntimeFailureDimension
    reason: str | None


_TRANSIENT_FAILURES = frozenset(
    {
        "runtime_archive_download_failed",
        "runtime_manifest_download_failed",
    }
)

_CONFIGURED_FAILURES = frozenset(
    {
        "runtime_command_missing",
        "runtime_git_missing",
        "runtime_node_missing",
        "runtime_node_unsupported",
        "runtime_npm_missing",
        "runtime_start_command_unavailable",
    }
)

_PERMANENT_FAILURES = frozenset(
    {
        "runtime_archive_url_unsupported",
        "runtime_platform_unsupported",
        "runtime_source_unsupported",
    }
)


def classify_show_runtime_failure(evidence: ShowRuntimeFailureEvidence) -> ShowRuntimeFailureClass:
    """Classify only what the owning failure evidence can prove."""
    reason = evidence.reason
    if evidence.dimension is ShowRuntimeFailureDimension.POLICY:
        return ShowRuntimeFailureClass.CONFIGURED
    if reason == "runtime_archive_checksum_mismatch":
        return ShowRuntimeFailureClass.CHECKSUM
    if reason and reason.endswith("_unavailable_offline"):
        return ShowRuntimeFailureClass.CONFIGURED
    if reason in _CONFIGURED_FAILURES:
        return ShowRuntimeFailureClass.CONFIGURED
    if reason in _TRANSIENT_FAILURES:
        return ShowRuntimeFailureClass.TRANSIENT
    if reason in _PERMANENT_FAILURES:
        return ShowRuntimeFailureClass.PERMANENT
    return ShowRuntimeFailureClass.UNCLASSIFIED


def show_runtime_recovery_action(
    evidence: ShowRuntimeFailureEvidence,
) -> ShowRuntimeRecoveryAction:
    """Publish the user obligation proved by the owning failure dimension."""
    if evidence.dimension is ShowRuntimeFailureDimension.POLICY:
        return ShowRuntimeRecoveryAction.CHANGE_SETTING
    if evidence.reason == "runtime_platform_unsupported":
        return ShowRuntimeRecoveryAction.NO_LOCAL_ACTION
    failure_class = classify_show_runtime_failure(evidence)
    if failure_class is ShowRuntimeFailureClass.CONFIGURED or evidence.reason in {
        "runtime_archive_url_unsupported",
        "runtime_source_unsupported",
    }:
        return ShowRuntimeRecoveryAction.CHANGE_SETTING
    return ShowRuntimeRecoveryAction.REPAIR
