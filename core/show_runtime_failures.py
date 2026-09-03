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


class ShowRuntimeRecoveryAction(str, Enum):
    REPAIR = "repair"
    CHANGE_SETTING = "change_setting"
    NO_LOCAL_ACTION = "no_local_action"


@dataclass(frozen=True)
class ShowRuntimeFailureEvidence:
    dimension: ShowRuntimeFailureDimension
    reason: str | None
    provenance: str | None = None
    retryable: bool | None = None


@dataclass(frozen=True)
class ShowRuntimeFailureDeclaration:
    reason: str
    dimension: ShowRuntimeFailureDimension
    owning_artifact: str
    failure_class: ShowRuntimeFailureClass
    recovery_action: ShowRuntimeRecoveryAction
    user_owned: bool = False
    provenance: str | None = None
    retryable: bool | None = None


_FAILURE_DECLARATION_ROWS = (
    # Policy owns these settings; the manager never reinterprets an install
    # or runtime token as a policy decision.
    ShowRuntimeFailureDeclaration(
        "VIBE_INSTALL_SKIP_SHOW_RUNTIME",
        ShowRuntimeFailureDimension.POLICY,
        "install-policy",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "VIBE_SHOW_RUNTIME_AUTO_INSTALL",
        ShowRuntimeFailureDimension.POLICY,
        "install-policy",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_unavailable",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-admission",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_proxy_failed",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-request",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_url_timeout",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-start",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_process_unavailable",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-start",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_process_unavailable",
        ShowRuntimeFailureDimension.RUNTIME,
        "explicit-runtime-command",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_health_timeout",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-start",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_attempt_failed",
        ShowRuntimeFailureDimension.RUNTIME,
        "runtime-start",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_command_unavailable",
        ShowRuntimeFailureDimension.RUNTIME,
        "managed-runtime-command",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_command_invalid",
        ShowRuntimeFailureDimension.RUNTIME,
        "explicit-runtime-command",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_node_command_invalid",
        ShowRuntimeFailureDimension.RUNTIME,
        "node-configuration",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_command_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "explicit-runtime-command",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_node_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "node-installation",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_node_unsupported",
        ShowRuntimeFailureDimension.INSTALL,
        "node-installation",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_git_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "git-installation",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_npm_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "npm-installation",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_install_already_running",
        ShowRuntimeFailureDimension.INSTALL,
        "install-admission",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_install_guard_unavailable",
        ShowRuntimeFailureDimension.INSTALL,
        "install-admission",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_install_inspection_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "runtime-status",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.NO_LOCAL_ACTION,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_verification_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "verified-repair",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.NO_LOCAL_ACTION,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_start_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "verified-repair-candidate",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_prepare_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "verified-repair",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_legacy_archive_unavailable",
        ShowRuntimeFailureDimension.INSTALL,
        "legacy-archive-provider",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_install_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "managed-install",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_install_missing_bin",
        ShowRuntimeFailureDimension.INSTALL,
        "managed-install",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "packaged-manifest",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
        provenance="packaged",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "configured-manifest",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_invalid",
        ShowRuntimeFailureDimension.INSTALL,
        "packaged-manifest",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
        provenance="packaged",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_invalid",
        ShowRuntimeFailureDimension.INSTALL,
        "configured-manifest",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_download_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "manifest-provider",
        ShowRuntimeFailureClass.TRANSIENT,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_download_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "configured-manifest-url",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
        retryable=False,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_manifest_unavailable_offline",
        ShowRuntimeFailureDimension.INSTALL,
        "offline-policy",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "packaged-archive",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
        provenance="packaged",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_missing",
        ShowRuntimeFailureDimension.INSTALL,
        "configured-archive",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_probe_not_applicable",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_probe_unsupported",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_url_unsupported",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.PERMANENT,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_platform_unsupported",
        ShowRuntimeFailureDimension.INSTALL,
        "runtime-platform",
        ShowRuntimeFailureClass.PERMANENT,
        ShowRuntimeRecoveryAction.NO_LOCAL_ACTION,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_download_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.TRANSIENT,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_download_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "configured-archive-url",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        user_owned=True,
        provenance="configured",
        retryable=False,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_download_failed",
        ShowRuntimeFailureDimension.INSTALL,
        "packaged-archive-url",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
        provenance="packaged",
        retryable=False,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_unavailable_offline",
        ShowRuntimeFailureDimension.INSTALL,
        "offline-policy",
        ShowRuntimeFailureClass.CONFIGURED,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_size_mismatch",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.UNCLASSIFIED,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_archive_checksum_mismatch",
        ShowRuntimeFailureDimension.INSTALL,
        "archive-provider",
        ShowRuntimeFailureClass.CHECKSUM,
        ShowRuntimeRecoveryAction.REPAIR,
    ),
    ShowRuntimeFailureDeclaration(
        "runtime_source_unsupported",
        ShowRuntimeFailureDimension.INSTALL,
        "runtime-source-configuration",
        ShowRuntimeFailureClass.PERMANENT,
        ShowRuntimeRecoveryAction.CHANGE_SETTING,
        True,
    ),
)

if len({(row.reason, row.provenance, row.retryable) for row in _FAILURE_DECLARATION_ROWS}) != len(
    _FAILURE_DECLARATION_ROWS
):
    raise RuntimeError("Show Runtime failure evidence must have one declaration")

SHOW_RUNTIME_FAILURE_DECLARATIONS = {
    (row.reason, row.provenance, row.retryable): row for row in _FAILURE_DECLARATION_ROWS
}


def _failure_declaration(evidence: ShowRuntimeFailureEvidence) -> ShowRuntimeFailureDeclaration | None:
    keys = (
        (evidence.reason or "", evidence.provenance, evidence.retryable),
        (evidence.reason or "", evidence.provenance, None),
        (evidence.reason or "", None, evidence.retryable),
        (evidence.reason or "", None, None),
    )
    for key in dict.fromkeys(keys):
        declaration = SHOW_RUNTIME_FAILURE_DECLARATIONS.get(key)
        if declaration is not None and declaration.dimension is evidence.dimension:
            return declaration
    return None


def classify_show_runtime_failure(evidence: ShowRuntimeFailureEvidence) -> ShowRuntimeFailureClass:
    """Classify only what the owning failure evidence can prove."""
    declaration = _failure_declaration(evidence)
    return declaration.failure_class if declaration else ShowRuntimeFailureClass.UNCLASSIFIED


def show_runtime_recovery_action(
    evidence: ShowRuntimeFailureEvidence,
) -> ShowRuntimeRecoveryAction:
    """Publish the user obligation proved by the owning failure dimension."""
    declaration = _failure_declaration(evidence)
    return declaration.recovery_action if declaration else ShowRuntimeRecoveryAction.REPAIR
