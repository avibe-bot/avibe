import argparse
import asyncio
import contextlib
import errno
import getpass
import json
import logging
import math
import os
import platform
import select as select_module
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent
from typing import Any, Callable, Mapping, NamedTuple, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone_name
from sqlalchemy import select

from config import SettingsStore, paths
from config.atomic_io import write_atomic
from config.v2_config import V2Config
from core.scheduled_tasks import (
    AGENT_RUN_DELIVERY_QUEUE,
    AGENT_RUN_DELIVERY_STEER,
    BINDING_FOLLOWS_SESSION_METADATA_KEY,
    ScheduledTaskStore,
    TaskExecutionStore,
    UnresolvableSessionTarget,
    parse_scope_id,
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
from core.caller_context import caller_context_from_env, caller_resource_user_context
from core.command_runner import command_line_preview
from core.install_integrity import verify_python_environment, verify_site_packages
from core.vibe_agents import AgentArchivedEditError, AgentArchiveError, AgentNameValidationError, AgentReferenceRewriteError, VibeAgent, VibeAgentStore, iter_global_agent_files, parse_agent_file, validate_agent_backend
from core.watches import (
    DEFAULT_RETRY_EXIT_CODE,
    NO_EVENT_EXIT_CODE,
    WATCH_RECOVERY_ENTRY_TIMEOUT_SECONDS,
    WATCH_RECONCILE_INTERVAL_SECONDS,
    ManagedWatchStore,
    WatchRuntimeStateStore,
)
from vibe import __version__, api, runtime
from vibe.i18n import normalize_language, t as i18n_t
from vibe.restart_supervisor import schedule_restart
from vibe.screenshot import ScreenshotError, capture_screenshot
from vibe.upgrade import (
    CURRENT_VIBE_EXECUTABLE_ENV,
    LEGACY_PACKAGE_NAME,
    MemoryRequirementUnreadableError,
    PACKAGE_NAME,
    AtomicActivation,
    DEFERRED_ACTIVATION_TIMEOUT_SECONDS,
    RestartState,
    activate_installer_candidate,
    activate_upgrade_candidate,
    activation_block_reason,
    atomic_uv_install_root,
    atomic_upgrade_lock,
    build_upgrade_plan,
    cache_running_vibe_path,
    configured_memory_enabled,
    execute_upgrade_plan,
    defer_upgrade_activation,
    get_latest_version_info,
    get_safe_cwd,
    _launcher_generation,
    _candidate_python,
    launcher_is_current_process,
    restart_is_pending,
    restart_record_is_pending,
    discard_atomic_uv_install_generation,
    should_skip_show_runtime_prepare,
    UPGRADE_INSTALL_TIMEOUT_SECONDS,
    verify_upgrade_candidate,
)
from storage.db import create_sqlite_engine
from storage.background import (
    DefinitionWriteConflict,
    SQLiteBackgroundTaskStore,
    TASK_RETIREMENT_SCHEDULE_MISSED,
    TaskResumeBlocked,
    TaskScheduleRetired,
    compute_next_run_at,
    normalize_run_status,
)
from storage.models import agents, scope_settings, scopes
from storage.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    PageRequest,
    make_page_request,
    page_sequence,
    pagination_payload,
)
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
from storage.settings_service import make_scope_id

logger = logging.getLogger(__name__)
UV_TOOL_PACKAGE_NAMES = (PACKAGE_NAME, LEGACY_PACKAGE_NAME)
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSY_ENV_VALUES = {"0", "false", "no", "off"}
DOCTOR_RESTART_RESULT_RETENTION_SECONDS = 10 * 60
DOCTOR_RESTART_SEED_GRACE_SECONDS = 60.0
DOCTOR_REPAIR_TARGETS = (
    "home-migration",
    "stale-install-runtime",
    "duplicate-service-processes",
    "stale-restart-state",
    "askill",
    "avault",
    "model-hub-engine",
    "git-runtime",
    "memory-runtime",
    "show-runtime",
    "tmux",
)
DOCTOR_DEFAULT_REPAIR_TARGETS = DOCTOR_REPAIR_TARGETS[:4]
DOCTOR_DEPENDENCY_REPAIR_TARGETS = frozenset(DOCTOR_REPAIR_TARGETS[4:])
DOCTOR_REPAIR_DRY_RUN_I18N_KEYS = {
    "home-migration": "doctor.repair.dryHomeMigration",
    "stale-install-runtime": "doctor.repair.dryStaleInstall",
    "duplicate-service-processes": "doctor.repair.dryDuplicateProcesses",
    "stale-restart-state": "doctor.repair.dryStaleRestart",
    "askill": "doctor.repair.dryAskill",
    "avault": "doctor.repair.dryAvault",
    "model-hub-engine": "doctor.repair.dryModelHubEngine",
    "git-runtime": "doctor.repair.dryGitRuntime",
    "memory-runtime": "doctor.repair.dryMemoryRuntime",
    "show-runtime": "doctor.repair.dryShowRuntime",
    "tmux": "doctor.repair.dryTmux",
}

DOCTOR_DISPLAY_PROJECTIONS = {
    "tunnel_state": {
        "healthy": "doctor.value.tunnelStateHealthy",
        "degraded": "doctor.value.tunnelStateDegraded",
        "recovering": "doctor.value.tunnelStateRecovering",
        "unknown": "doctor.value.tunnelStateUnknown",
    },
    "tunnel_grade": {
        "good": "doctor.value.tunnelGradeGood",
        "fair": "doctor.value.tunnelGradeFair",
        "poor": "doctor.value.tunnelGradePoor",
        "critical": "doctor.value.tunnelGradeCritical",
        "unknown": "doctor.value.tunnelGradeUnknown",
    },
    "tunnel_protocol": {
        "quic": "doctor.value.tunnelProtocolQuic",
        "http2": "doctor.value.tunnelProtocolHttp2",
        "unknown": "doctor.value.tunnelProtocolUnknown",
    },
    "download_kind": {
        "http": "doctor.repair.dependencyDownloadHttp",
        "dns": "doctor.repair.dependencyDownloadDns",
        "tls": "doctor.repair.dependencyDownloadTls",
        "timeout": "doctor.repair.dependencyDownloadTimeout",
        "network": "doctor.repair.dependencyDownloadNetwork",
        "permission": "doctor.repair.dependencyDownloadPermission",
        "disk": "doctor.repair.dependencyDownloadDisk",
        "io": "doctor.repair.dependencyDownloadIo",
    },
    "repair_reason": {
        "askill_auto_install_unsupported": "doctor.repair.askillAutoInstallUnsupported",
        "askill_install_path_missing": "doctor.repair.askillInstallPathMissing",
        "askill_install_timeout": "doctor.repair.installTimeout",
        "askill_install_failed": "doctor.repair.installCommandFailed",
        "askill_install_error": "doctor.repair.installError",
        "avault_platform_unsupported": "doctor.repair.avaultPlatformUnsupported",
        "avault_checksum_mismatch": "doctor.repair.avaultChecksumMismatch",
        "avault_install_path_missing": "doctor.repair.avaultInstallPathMissing",
        "avault_install_failed": "doctor.repair.avaultInstallFailed",
        "avault_download_failed": "doctor.repair.dependencyArchiveDownloadFailed",
        "avault_p2_release_unavailable": "doctor.repair.avaultReleaseUnavailable",
        "git_runtime_unpublished": "doctor.repair.dependencyManifestUnavailable",
    },
    "repair_suffix": {
        "install_already_running": "doctor.repair.dependencyAlreadyRunning",
        "platform_unsupported": "doctor.repair.dependencyPlatformUnsupported",
        "manifest_missing": "doctor.repair.dependencyManifestMissing",
        "manifest_invalid": "doctor.repair.dependencyManifestInvalid",
        "manifest_unavailable": "doctor.repair.dependencyManifestUnavailable",
        "manifest_unavailable_offline": "doctor.repair.dependencyManifestUnavailable",
        "manifest_download_failed": "doctor.repair.dependencyManifestDownloadFailed",
        "manifest_url_unsupported": "doctor.repair.dependencyManifestUnavailable",
        "archive_unavailable": "doctor.repair.dependencyArchiveUnavailable",
        "archive_unavailable_offline": "doctor.repair.dependencyArchiveUnavailable",
        "archive_url_unsupported": "doctor.repair.dependencyArchiveUnavailable",
        "archive_download_failed": "doctor.repair.dependencyArchiveDownloadFailed",
        "archive_checksum_mismatch": "doctor.repair.dependencyArchiveVerificationFailed",
        "archive_size_mismatch": "doctor.repair.dependencyArchiveVerificationFailed",
        "binary_checksum_mismatch": "doctor.repair.dependencyBinaryVerificationFailed",
        "binary_not_runnable": "doctor.repair.dependencyBinaryNotRunnable",
        "binary_prepare_failed": "doctor.repair.dependencyBinaryPrepareFailed",
        "candidate_validation_failed": "doctor.repair.dependencyCandidateValidationFailed",
        "install_missing_binary": "doctor.repair.dependencyInstallMissingBinary",
        "install_failed": "doctor.repair.installError",
        "install_lock_failed": "doctor.repair.dependencyInstallLockFailed",
        "install_claim_failed": "doctor.repair.dependencyInstallClaimFailed",
        "install_target_changed": "doctor.repair.dependencyInstallTargetChanged",
        "pointer_write_failed": "doctor.repair.dependencyPointerWriteFailed",
        "codesign_missing": "doctor.repair.dependencyCodeSignMissing",
        "codesign_failed": "doctor.repair.dependencyCodeSignFailed",
        "codesign_verify_failed": "doctor.repair.dependencyCodeSignFailed",
        "xattr_failed": "doctor.repair.dependencyMetadataFailed",
    },
    "restart_state": {
        state.value: f"doctor.value.restartState{state.name.title()}" for state in RestartState
    },
    "show_runtime_provider": {
        "manifest-cache": "doctor.value.showRuntimeProviderManifest",
        "archive": "doctor.value.showRuntimeProviderArchive",
        "npm": "doctor.value.showRuntimeProviderNpm",
        "unknown": "doctor.value.showRuntimeProviderUnknown",
    },
}

DEFAULT_VAULT_APPROVAL_WAIT_SECONDS = 9 * 60
WATCH_STARTUP_STABLE_RUNNING_SECONDS = 1.5
WATCH_STARTUP_JITTER_BUFFER_SECONDS = 1.0


#: Commands whose trailing ``-- <command ...>`` tail is lifted out of argv BEFORE argparse
#: runs, mapped to ``(namespace attribute, first index searched for the separator)``.
#:
#: ``argparse.REMAINDER`` alone cannot carry these: it swallows everything after the first
#: non-flag token, so a legitimate option VALUE typed after the command (``-- ./sync.sh
#: --flag value``) would be eaten as part of the remainder for some orderings and dropped
#: for others. Lifting the tail here leaves the positional as a pure landing slot that
#: exists for the usage text, and keeps every flag on the command line parseable.
#:
#: The search index skips the fixed leading tokens (subcommand words plus any positional
#: id) so a definition id that happens to be ``--`` cannot be mistaken for the separator.
_POST_SEPARATOR_COMMAND_SPECS: dict[tuple[str, str], tuple[str, int]] = {
    ("watch", "update"): ("waiter_command", 3),
    ("task", "add"): ("command_argv", 2),
    ("task", "update"): ("command_argv", 3),
}


class VibeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        self.error_help_command = kwargs.pop("error_help_command", None)
        self.error_hint = kwargs.pop("error_hint", None)
        super().__init__(*args, **kwargs)

    def parse_args(self, args=None, namespace=None):
        parsed_args = list(sys.argv[1:] if args is None else args)
        lifted_attribute: str | None = None
        lifted_command: list[str] | None = None
        if self.prog == "vibe" and len(parsed_args) >= 2:
            spec = _POST_SEPARATOR_COMMAND_SPECS.get((parsed_args[0], parsed_args[1]))
            if spec is not None:
                attribute, search_from = spec
                separator_index = -1
                if len(parsed_args) > search_from:
                    try:
                        separator_index = parsed_args.index("--", search_from)
                    except ValueError:
                        separator_index = -1
                if separator_index >= 0:
                    lifted_attribute = attribute
                    lifted_command = ["--", *parsed_args[separator_index + 1 :]]
                    parsed_args = [*parsed_args[:separator_index]]

        parsed = super().parse_args(parsed_args, namespace)
        if lifted_attribute is not None:
            setattr(parsed, lifted_attribute, lifted_command)
        return parsed

    def error(self, message):
        payload = {
            "schema_version": 1,
            "ok": False,
            "kind": "error",
            "code": "invalid_arguments",
            "error": message,
            "usage": self.format_usage().strip(),
        }
        if self.error_hint:
            payload["hint"] = self.error_hint
        if self.error_help_command:
            payload["help_command"] = self.error_help_command
        self.exit(2, json.dumps(payload, indent=2) + "\n")


class TaskCliError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        hint: str | None = None,
        example: str | None = None,
        help_command: str | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.example = example
        self.help_command = help_command
        self.details = details or {}


class _LocalShowEventsTarget(NamedTuple):
    url: str
    verify_ui_pid: int | None = None


#: The reserved workspace-notifications session, refused at a CLI admission door.
#:
#: ONE CODE ACROSS TWO SURFACES. ``reserved_session`` is not new vocabulary invented for
#: the CLI: ``storage.workbench_sessions_service`` already raises it as
#: ``ReservedSessionError.code``, ``vibe/ui_server.py`` answers ``403 reserved_session``
#: for the DELETE, the PATCH and the messages POST, and ``ui/src/i18n`` renders one
#: ``errors.reserved_session`` entry for it. A coding agent driving ``vibe`` and a browser
#: driving the API now branch on the SAME token, which is the whole point of coding it:
#: the round-16 hole was reachable from the CLI precisely because the two surfaces did not
#: share a contract.
#:
#: ONLY ``reserved`` IS TYPED HERE, and the other three ``UnresolvableSessionTarget``
#: reasons deliberately stay on the generic path. The breadth was checked against the
#: vocabulary that actually exists rather than assumed:
#:
#: * ``session_archived`` does not exist in this CLI at all — it lives only on the Show
#:   and HTTP surfaces (``core/show_pages.py``, ``vibe/api.py``, ``vibe/ui_server.py``).
#:   Adding it here would be new vocabulary, not mirroring.
#: * ``session_not_found`` DOES exist here (``vibe session get`` / ``vibe session
#:   update``) but means something narrower: a ``LookupError`` from
#:   ``sessions_service.get_active_session``, which by its own comment folds ARCHIVED into
#:   not-found. Reusing it for ``reason == "missing"`` would give one token two
#:   incompatible meanings depending on which command emitted it — worse than a generic
#:   code, because a client cannot tell which one it got.
#: * this exception class already HAS a typed CLI code, ``invalid_session_id``, on the
#:   paths that route through ``_validate_session_id_target`` /
#:   ``_validate_callback_session_id``. Re-coding ``missing`` / ``archived`` / ``unusable``
#:   here would make a THIRD vocabulary for one class. Unifying those three is a real
#:   cleanup with its own blast radius; it is not this finding, and doing it silently
#:   inside a review round would be the larger change.
RESERVED_SESSION_CLI_CODE = "reserved_session"


def _reserved_session_cli_error(exc: "UnresolvableSessionTarget") -> TaskCliError:
    """Re-type the resolver refusal and localize its user-facing guidance."""

    try:
        lang = V2Config.load().language
    except Exception:
        lang = "en"
    return TaskCliError(
        str(exc),
        code=RESERVED_SESSION_CLI_CODE,
        hint=i18n_t("harness.notice.workspaceSessionReadOnly", lang),
        details={"session_id": exc.session_id, "reason": exc.reason},
    )


def _print_task_error(exc: Exception, *, help_command: str | None = None) -> None:
    # Re-typed BEFORE the ``TaskCliError`` branch, and here rather than in each command's
    # own ``except``, because this is the one printer every CLI admission door funnels its
    # broad handler through. ``cmd_agent_run``, ``cmd_task_add`` and ``cmd_watch_add`` all
    # reached ``resolve_session_id_target`` via ``_resolve_agent_for_target``, which does
    # not wrap — so all three reported ``task_command_failed`` and a client had nothing but
    # a prose string to branch on. Fixing it at the printer means a command added later
    # inherits the code instead of having to remember it, and adds no third copy of the
    # payload builder below.
    if isinstance(exc, UnresolvableSessionTarget) and exc.reason == "reserved":
        exc = _reserved_session_cli_error(exc)
    from storage.resource_access_service import (
        HARNESS_ACCESS_FORBIDDEN_CODE,
        ResourceAccessError,
    )

    if (
        isinstance(exc, ResourceAccessError)
        and exc.code == HARNESS_ACCESS_FORBIDDEN_CODE
    ):
        try:
            lang = V2Config.load().language
        except Exception:
            lang = "en"
        exc = TaskCliError(
            str(exc),
            code=exc.code,
            hint=i18n_t("harness.notice.remoteExecutionDisabled", lang),
            help_command=help_command,
        )
    if isinstance(exc, TaskCliError):
        payload = {
            "schema_version": 1,
            "ok": False,
            "kind": "error",
            "code": exc.code,
            "error": str(exc),
        }
        if exc.hint:
            payload["hint"] = exc.hint
        if exc.example:
            payload["example"] = exc.example
        if exc.help_command or help_command:
            payload["help_command"] = exc.help_command or help_command
        if exc.details:
            payload["details"] = exc.details
    else:
        payload = {
            "schema_version": 1,
            "ok": False,
            "kind": "error",
            "code": "task_command_failed",
            "error": str(exc),
        }
        if help_command:
            payload["help_command"] = help_command
    print(json.dumps(payload, indent=2), file=sys.stderr)


def _definition_conflict_cli_error(
    exc: DefinitionWriteConflict,
    *,
    help_command: str,
    details: dict | None = None,
) -> TaskCliError:
    """A refused full-row write, told to the user as a first-class command failure.

    The store writes EVERY column of a definition, so an update built from a read
    that a teardown has since invalidated is refused rather than applied (HFR-261).
    That refusal has to reach the user with its own code: without it the command
    would print the definition it *meant* to write and exit 0, while the stored row
    still holds whatever ``/new`` or the archive dialog put there.
    """

    return TaskCliError(
        str(exc),
        code="definition_write_conflict",
        hint=(
            "A /new clear or a Session archive reclaimed this definition while the "
            "update was being prepared. Re-read it and re-apply the change."
        ),
        help_command=help_command,
        details=details or {"definition_id": exc.definition_id},
    )


def _cli_payload(kind: str, **fields) -> dict:
    return {"schema_version": 1, "ok": True, "kind": kind, **fields}


def _print_cli_payload(kind: str, **fields) -> None:
    print(json.dumps(_cli_payload(kind, **fields), indent=2))


def _configured_trace_retention_days(language: str) -> int:
    """Read the persisted window for help text without loading/migrating config."""
    from storage import agent_events_retention as _retention

    try:
        config_path = paths.get_config_path()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        runtime = payload.get("runtime") if isinstance(payload, dict) else None
        value = runtime.get("agent_events_trace_retention_days") if isinstance(runtime, dict) else None
        from vibe.trace_retention_policy import validate_retention_days

        try:
            return validate_retention_days(value)
        except ValueError:
            pass
    except Exception:
        pass
    del language
    return _retention.DEFAULT_RETENTION_DAYS


def _configured_cli_language() -> str:
    """Read an optional configured language without creating or migrating state."""

    try:
        config_path = paths.get_config_path()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        language = payload.get("language") if isinstance(payload, dict) else None
        return normalize_language(language if isinstance(language, str) else None)
    except Exception:
        return "en"


def _memory_cli_language() -> str:
    return _configured_cli_language()


_MEMORY_CLI_RUNTIME_STATE_I18N_KEYS = {
    "disabled": "memory.cli.runtimeState.disabled",
    "starting": "memory.cli.runtimeState.starting",
    "running": "memory.cli.runtimeState.running",
    "degraded": "memory.cli.runtimeState.degraded",
    "needs_repair": "memory.cli.runtimeState.needsRepair",
}
_MEMORY_CLI_PROVIDER_STATE_I18N_KEYS = {
    "ok": "memory.cli.providerState.ok",
}
_MEMORY_CLI_ATTACHMENT_STATE_I18N_KEYS = {
    "ready": "memory.cli.attachmentCaptureState.ready",
    "not_configured": "memory.cli.attachmentCaptureState.notConfigured",
    "unavailable": "memory.cli.attachmentCaptureState.unavailable",
}
_MEMORY_CLI_REASON_I18N_KEYS = {
    "memory_disabled": "memory.cli.reason.memoryDisabled",
    "memory_invalid_input": "memory.cli.reason.invalidInput",
    "memory_access_denied": "memory.cli.reason.accessDenied",
    "memory_input_too_large": "memory.cli.reason.inputTooLarge",
    "memory_queue_full": "memory.cli.reason.queueFull",
    "memory_low_disk_space": "memory.cli.reason.lowDiskSpace",
    "memory_store_unavailable": "memory.cli.reason.storeUnavailable",
    "memory_runtime_missing": "memory.cli.reason.runtimeMissing",
    "memory_runtime_unsupported": "memory.cli.reason.runtimeUnsupported",
    "memory_runtime_install_failed": "memory.cli.reason.runtimeInstallFailed",
    "memory_reconcile_failed": "memory.cli.reason.reconcileFailed",
    "memory_wake_failed": "memory.cli.reason.wakeFailed",
    "memory_runtime_busy": "memory.cli.reason.runtimeBusy",
    "memory_permission_denied": "memory.cli.reason.permissionDenied",
    "memory_disk_unavailable": "memory.cli.reason.diskUnavailable",
    "memory_local_data_unusable": "memory.cli.reason.localDataUnusable",
    "memory_legacy_recovery_required": "memory.cli.reason.legacyRecoveryRequired",
    "memory_sidecar_unavailable": "memory.cli.reason.sidecarUnavailable",
    "memory_provider_timeout": "memory.cli.reason.providerTimeout",
    "memory_provider_response_invalid": "memory.cli.reason.providerResponseInvalid",
    "memory_capability_unavailable": "memory.cli.reason.capabilityUnavailable",
    "memory_processing_failed": "memory.cli.reason.processingFailed",
    "memory_loss_confirmation_required": "memory.cli.reason.lossConfirmationRequired",
    "memory_embedding_unavailable": "memory.cli.reason.embeddingUnavailable",
    "memory_llm_unavailable": "memory.cli.reason.llmUnavailable",
    "memory_rerank_unavailable": "memory.cli.reason.rerankUnavailable",
    "memory_multimodal_unavailable": "memory.cli.reason.multimodalUnavailable",
    "memory_repair_failed": "memory.cli.reason.repairFailed",
    "memory_repair_not_required": "memory.cli.reason.repairNotRequired",
    "memory_delete_data_failed": "memory.cli.reason.deleteDataFailed",
    "memory_reconfigure_failed": "memory.cli.reason.reconfigureFailed",
    "memory_operation_in_progress": "memory.cli.reason.operationInProgress",
    "memory_implementation_unavailable": "memory.cli.reason.implementationUnavailable",
    "memory_implementation_incompatible": "memory.cli.reason.implementationIncompatible",
}
_DOCTOR_MEMORY_REASON_I18N_KEYS = {
    "memory_runtime_install_requires_stopped_memory": "memory.cli.reason.runtimeInstallRequiresStoppedMemory",
    "memory_runtime_preparation_import_timeout": "memory.cli.reason.runtimePreparationImportTimeout",
    "memory_runtime_preparation_import_failed": "memory.cli.reason.runtimePreparationImportFailed",
    "memory_runtime_preparation_scrubber_timeout": "memory.cli.reason.runtimePreparationScrubberTimeout",
    "memory_runtime_preparation_scrubber_failed": "memory.cli.reason.runtimePreparationScrubberFailed",
    "memory_runtime_preparation_sync_contract_failed": "memory.cli.reason.runtimePreparationSyncContractFailed",
    "memory_runtime_preparation_failed": "memory.cli.reason.runtimePreparationFailed",
}


def _memory_cli_label(
    value: object,
    *,
    keys: dict[str, str],
    fallback_key: str,
    language: str,
) -> str:
    token = value.strip() if isinstance(value, str) else ""
    return i18n_t(keys.get(token, fallback_key), language)


def _print_memory_cli_error(operation: str, code: str, *, as_json: bool, language: str) -> int:
    payload = {
        "schema_version": 1,
        "ok": False,
        "kind": f"memory_{operation}",
        "code": code,
        "error": code,
    }
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        display_code = _memory_cli_label(
            code,
            keys=_MEMORY_CLI_REASON_I18N_KEYS,
            fallback_key="memory.cli.reason.unknown",
            language=language,
        )
        print(
            i18n_t("memory.cli.error", language, operation=operation, code=display_code),
            file=sys.stderr,
        )
    return 1


def _memory_cli_body(response: object, *, fallback: str) -> tuple[dict | None, str | None]:
    """Validate the closed controller response shape used by ``vibe memory``."""

    from vibe.memory_contract import is_memory_error_code

    if not isinstance(response, dict):
        return None, "memory_provider_response_invalid"
    body = response.get("body")
    if not isinstance(body, dict):
        return None, "memory_provider_response_invalid"
    error = body.get("error")
    if response.get("status_code") != 200 or body.get("status") == "failed":
        return None, error if is_memory_error_code(error) else fallback
    return body, None


def _print_memory_cli_human(operation: str, result: dict, *, language: str) -> None:
    if operation == "remember":
        print(i18n_t("memory.cli.remembered", language))
        return
    if operation == "status":
        runtime_state_label = _memory_cli_label(
            result.get("state"),
            keys=_MEMORY_CLI_RUNTIME_STATE_I18N_KEYS,
            fallback_key="memory.cli.runtimeState.unknown",
            language=language,
        )
        print(
            i18n_t(
                "memory.cli.status",
                language,
                state=runtime_state_label,
            )
        )
        health = result.get("health")
        if isinstance(health, dict):
            version = health.get("version")
            provider_state = health.get("status")
            provider_state_label = _memory_cli_label(
                provider_state,
                keys=_MEMORY_CLI_PROVIDER_STATE_I18N_KEYS,
                fallback_key="memory.cli.providerState.unknown",
                language=language,
            )
            print(
                i18n_t(
                    "memory.cli.provider",
                    language,
                    version=(
                        version
                        if isinstance(version, str) and version
                        else i18n_t("memory.cli.unknownVersion", language)
                    ),
                    state=provider_state_label,
                )
            )
        attachment_capture = result.get("attachment_capture")
        if isinstance(attachment_capture, dict):
            attachment_state_label = _memory_cli_label(
                attachment_capture.get("status"),
                keys=_MEMORY_CLI_ATTACHMENT_STATE_I18N_KEYS,
                fallback_key="memory.cli.attachmentCaptureState.unknown",
                language=language,
            )
            print(
                i18n_t(
                    "memory.cli.attachmentCapture",
                    language,
                    state=attachment_state_label,
                )
            )
        reason = result.get("reason")
        if isinstance(reason, str) and reason:
            reason_label = _memory_cli_label(
                reason,
                keys=_MEMORY_CLI_REASON_I18N_KEYS,
                fallback_key="memory.cli.reason.unknown",
                language=language,
            )
            print(i18n_t("memory.cli.sourceReason", language, reason=reason_label))
        return

    warnings = result.get("warnings")
    if operation == "list":
        if isinstance(warnings, list) and "memory_list_truncated" in warnings:
            print(i18n_t("memory.cli.listWarning.truncated", language), file=sys.stderr)
    elif (
        operation in {"search", "profile"}
        and isinstance(warnings, list)
        and "memory_search_partial" in warnings
    ):
        print(i18n_t("memory.cli.readWarning.partial", language), file=sys.stderr)

    items = result.get("items")
    if not isinstance(items, list) or not items:
        print(i18n_t("memory.cli.empty", language))
        return
    if operation == "list":
        for item in items:
            if not isinstance(item, dict):
                continue
            timestamp = item.get("timestamp")
            subject = item.get("subject")
            summary = item.get("summary")
            body = item.get("body")
            lines = [
                value
                for value in (subject, summary, body)
                if isinstance(value, str) and value
            ]
            if not lines:
                continue
            prefix = f"{timestamp} " if isinstance(timestamp, str) and timestamp else ""
            print(f"{prefix}{lines[0]}")
            for line in lines[1:]:
                print(line)
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        date = item.get("date")
        origin = item.get("origin")
        origin_prefix = ""
        if operation in {"search", "profile"} and origin in {"user", "agent", "both"}:
            origin_prefix = i18n_t(
                "memory.cli.originPrefix",
                language,
                origin=i18n_t(f"memory.cli.origin.{origin}", language),
            )
        date_prefix = f"{date} " if isinstance(date, str) and date else ""
        prefix = f"{origin_prefix}{date_prefix}"
        print(f"{prefix}{text}")


def cmd_memory(args) -> int:
    """Present direct Memory reads from the controller's verified UDS only."""

    from vibe import internal_client
    from core.caller_context import caller_context_from_env
    from vibe.memory_contract import (
        MAX_MEMORY_LIST_PAGE_SIZE,
        MAX_MEMORY_SEARCH_RESULTS,
    )

    operation = args.memory_command
    as_json = bool(getattr(args, "json", False))
    language = _memory_cli_language()
    query = ""
    if operation not in {"status", "profile", "list", "search", "remember"}:
        return _print_memory_cli_error("invalid", "memory_invalid_input", as_json=as_json, language=language)
    if operation == "search":
        query = args.query.strip() if isinstance(args.query, str) else ""
        if (
            not query
            or not isinstance(args.limit, int)
            or isinstance(args.limit, bool)
            or not 1 <= args.limit <= MAX_MEMORY_SEARCH_RESULTS
        ):
            return _print_memory_cli_error(operation, "memory_invalid_input", as_json=as_json, language=language)
    if operation == "list" and (
        not isinstance(args.page, int)
        or isinstance(args.page, bool)
        or args.page < 1
        or not isinstance(args.limit, int)
        or isinstance(args.limit, bool)
        or not 1 <= args.limit <= MAX_MEMORY_LIST_PAGE_SIZE
    ):
        return _print_memory_cli_error(operation, "memory_invalid_input", as_json=as_json, language=language)
    if operation == "remember":
        query = args.text if isinstance(args.text, str) else ""
        if not query.strip():
            return _print_memory_cli_error(operation, "memory_invalid_input", as_json=as_json, language=language)
    try:
        caller = caller_context_from_env()
        access = (
            {"caller_session_id": caller.session_id}
            if caller is not None
            else {}
        )
        if operation == "status":
            response = internal_client.memory_status_sync(**access)
        elif operation == "profile":
            response = internal_client.memory_profile_sync(**access)
        elif operation == "list":
            response = internal_client.memory_list_sync(
                page=args.page,
                limit=args.limit,
                project=getattr(args, "project", None),
                **access,
            )
        elif operation == "search":
            response = internal_client.memory_search_sync(
                query,
                args.limit,
                mode=args.mode,
                project=getattr(args, "project", None),
                **access,
            )
        else:
            response = internal_client.memory_remember_sync(
                query,
                project=getattr(args, "project", None),
                **access,
            )
    except internal_client.InternalServerUnavailable:
        return _print_memory_cli_error(operation, "memory_sidecar_unavailable", as_json=as_json, language=language)

    result, error = _memory_cli_body(response, fallback="memory_sidecar_unavailable")
    if error is not None:
        return _print_memory_cli_error(operation, error, as_json=as_json, language=language)
    assert result is not None
    if operation == "remember":
        outcome = result.get("status")
        if outcome not in {"accepted", "duplicate"}:
            code = result.get("reason") or result.get("error") or "memory_store_unavailable"
            return _print_memory_cli_error(operation, code, as_json=as_json, language=language)
    if as_json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "ok": True,
                    "kind": f"memory_{operation}",
                    "result": result,
                },
                indent=2,
            )
        )
    else:
        _print_memory_cli_human(operation, result, language=language)
    return 0


def cmd_skill(args) -> int:
    """List or load Skills through Avibe's live resolver."""

    from core.managed_skills import (
        load_skill,
        render_skill_content,
        render_skill_list,
        resolve_skills,
    )

    language = _configured_cli_language()

    if args.skill_command == "list":
        try:
            output = render_skill_list(
                resolve_skills(),
                page=args.page,
                more_notice=i18n_t(
                    "skill.cli.more",
                    language,
                    page=args.page + 1,
                ),
            )
        except ValueError:
            print(i18n_t("skill.cli.error.invalidPage", language), file=sys.stderr)
            return 1
        if output:
            print(output)
        return 0

    if args.skill_command == "load":
        allowed = resolve_skills()
        if args.name not in {skill.name for skill in allowed}:
            print(i18n_t("skill.cli.error.notFound", language, name=args.name), file=sys.stderr)
            return 1
        skill = load_skill(args.name, resolved_skills=allowed)
        if skill is None:
            print(i18n_t("skill.cli.error.notFound", language, name=args.name), file=sys.stderr)
            return 1
        print(render_skill_content(skill))
        return 0

    return 1


def cmd_debug_prompt(args) -> int:
    """Export the deterministic Prompt Studio source catalog."""

    if args.debug_command != "prompt" or args.prompt_debug_command != "export":
        return 1
    from core.prompt_studio_catalog import export_prompt_studio_catalog

    print(json.dumps(export_prompt_studio_catalog(), ensure_ascii=False, indent=2))
    return 0


def _add_pagination_args(parser, *, help_command: str) -> None:
    parser.add_argument("--page", type=int, help="Page number to return. Defaults to 1.")
    parser.add_argument(
        "--limit",
        type=int,
        help=f"Rows per page. Defaults to {DEFAULT_PAGE_LIMIT}; maximum {MAX_PAGE_LIMIT}.",
    )
    parser.error_help_command = help_command


def _add_vault_approval_wait_args(parser, *, default_seconds: int = DEFAULT_VAULT_APPROVAL_WAIT_SECONDS) -> None:
    parser.add_argument(
        "--approval-wait",
        type=_non_negative_float,
        metavar="SECONDS",
        help=(
            "Wait this many seconds for a protected approval before returning approval_wait_timeout "
            f"(default {default_seconds})."
        ),
    )
    parser.add_argument(
        "--no-approval-wait",
        action="store_true",
        help="Return approval_required immediately instead of waiting for browser approval.",
    )


def _page_request_from_args(args, *, help_command: str) -> PageRequest:
    try:
        return make_page_request(
            page=getattr(args, "page", None),
            limit=getattr(args, "limit", None),
        )
    except ValueError as exc:
        raise TaskCliError(str(exc), code="invalid_pagination", help_command=help_command) from exc


def _add_optional_arg(parts: list[str], flag: str, value: object) -> None:
    if value is not None and value != "":
        parts.extend([flag, str(value)])


def _next_command(parts: list[str], page_result) -> str | None:
    if page_result.next_page is None:
        return None
    command = [*parts, "--page", str(page_result.next_page), "--limit", str(page_result.limit)]
    return shlex.join(command)


def _pagination_message(page_payload: dict) -> str | None:
    if not page_payload.get("has_more"):
        return None
    next_command = page_payload.get("next_command")
    if next_command:
        return f"More records are available. Continue with: {next_command}"
    return "More records are available. Add --page to continue."


def _paginated_fields(page_result, *, command: list[str], include_next_command: bool = True) -> dict:
    page_payload = pagination_payload(
        page_result,
        next_command=_next_command(command, page_result) if include_next_command else None,
    )
    fields = {"pagination": page_payload}
    message = _pagination_message(page_payload)
    if message:
        fields["message"] = message
    return fields


def _print_definition_list_payload(
    page_result,
    *,
    payload_for_item,
    command: list[str],
) -> None:
    item_payloads = [payload_for_item(item) for item in page_result.items]
    _print_cli_payload(
        "run_definitions",
        definitions=item_payloads,
        **_paginated_fields(page_result, command=command),
    )


def _print_definition_payload(definition, **fields) -> None:
    _print_cli_payload("run_definition", definition=definition, **fields)


def _parse_cli_time_filter(value: str | None, *, field_name: str, help_command: str) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    suffix = raw[-1].lower()
    amount = raw[:-1]
    units = {
        "s": "seconds",
        "m": "minutes",
        "h": "hours",
        "d": "days",
    }
    if suffix in units and amount.isdigit():
        delta = timedelta(**{units[suffix]: int(amount)})
        return (datetime.now(timezone.utc) - delta).isoformat()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskCliError(
            f"{field_name} must be an ISO timestamp or a relative value like 30m, 6h, or 7d",
            code="invalid_time_filter",
            help_command=help_command,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def _task_examples_text() -> str:
    return dedent(
        """\
        Examples:
          vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
          vibe task update 12ab34cd56ef --cron '*/30 * * * *' --name 'Half-hour summary'
          vibe task run 12ab34cd56ef
          vibe task add --create-session --scope-id slack::channel::C123 --cron '*/5 * * * *' --message 'Tell a new joke each time.'
          vibe task add --create-session --scope-id slack::channel::C123 --at '2026-03-31T09:00:00+08:00' --message-file briefing.md
        """
    )


def _task_add_examples_text() -> str:
    return dedent(
        """\
        Session target:
          Use --session-id with the target Agent Session ID, for example sesk8m4q2p7x.
          Inside an Avibe Agent shell, tasks continue this conversation by default.

        Guidance:
          If this is your first time using this command, read this whole help entry before creating a task.
          `--session-id` chooses which Agent Session Avibe will continue using when the task runs.
          Use --create-session with --scope-id <scopes.id> to create a reusable Session in a specific existing scope.
          Use --create-session with --same-scope only from an Avibe Agent shell, where the caller Session scope is available.
          Use --cwd only for Sessions created by this task; existing target Sessions keep their own cwd.
          `--message` and `--message-file` provide the stored user message that will be sent each time the task runs.
          Use --cron for recurring jobs and --at for one-shot jobs.
          Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid. Prefer weekday names such as mon, tue, or sun when scheduling by day of week.
          --timezone controls how --cron and naive --at timestamps are interpreted.

        Command tasks:
          A command task runs a subprocess on schedule with NO Agent turn, so it needs no Session, Agent, or message.
          Use --shell for a shell command string, or pass the executable and its args after '--'.
          Failure handling is silent-success by default: a successful run stays quiet, and a failed run records a failure notice.
          Add --on-failure agent with --message to spend an Agent turn triaging a failed run instead.
          --timeout bounds one run; use 0 for no timeout.
          --cwd is where the command runs; without it, a Session-bound command follows that Session's directory
          and every other command records the directory you ran this from.

        Examples:
          vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
          vibe task add --create-session --scope-id slack::channel::C123 --cron '*/5 * * * *' --message 'Tell a new joke each time.'
          vibe task add --create-session --scope-id slack::channel::C123 --cron '0 9 * * *' --message 'Post a visible daily summary in this scope.'
          vibe task add --name nightly-sync --cron '0 3 * * *' --shell './scripts/sync.sh'
          vibe task add --cron '0 3 * * *' --shell './scripts/sync.sh' --on-failure agent --message 'The nightly sync failed. Diagnose it.'
        """
    )


def _task_update_examples_text() -> str:
    return dedent(
        """\
        You may update any subset of the stored task fields while keeping the same task ID.

        Common updates:
          vibe task update 12ab34cd56ef --name 'Morning summary'
          vibe task update 12ab34cd56ef --cron '*/30 * * * *'
          vibe task update 12ab34cd56ef --message 'Send a shorter summary.'
          vibe task update 12ab34cd56ef --session-id sesk8m4q2p7x
          vibe task update 12ab34cd56ef --create-session --scope-id slack::channel::C123
          vibe task update 12ab34cd56ef --reset-delivery
          vibe task update 12ab34cd56ef --shell './scripts/sync.sh --verbose'
          vibe task update 12ab34cd56ef --timeout 900

        Guidance:
          Unspecified fields keep their existing values.
          A command task and a message task are different kinds: remove and recreate the task to move between them, or to change --on-failure.
          Use --reset-delivery to return to following the session target directly.
          Use --same-scope or --scope-id when this task should create new Sessions in a specific scope.
          When changing schedule fields, pass either --cron or --at.
          Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid. Prefer weekday names such as mon, tue, or sun when scheduling by day of week.
          Use --clear-name if you want the task to stop storing a custom name.
        """
    )


def _hook_send_examples_text() -> str:
    return dedent(
        """\
        Deprecated:
          `vibe hook send` is a compatibility entrypoint.
          New automation should use `vibe agent run`.

        Session target:
          Use --session-id with the target Agent Session ID, for example sesk8m4q2p7x.

        Guidance:
          If this is your first time creating an async one-shot run, use `vibe agent run --help`.
          `vibe hook send` queues one deprecated asynchronous compatibility turn without persisting a scheduled task.
          `--session-id` chooses which Agent Session Avibe will continue using for that one async turn.
          Keep the current session id when the hook should continue in the same session.
          If no session id is available, trigger this from an active Avibe conversation instead of guessing.
          For new async one-shot work, prefer `vibe agent run`.
          `--message` and `--message-file` provide the one-shot async user message that will be queued immediately.

        Examples:
          vibe agent run --session-id sesk8m4q2p7x --no-callback --message 'The export finished. Share the summary.'
          vibe agent run --session-id sesk8m4q2p7x --no-callback --message 'Run the benchmark; I will inspect the run later.'
        """
    )


def _watch_examples_text() -> str:
    return dedent(
        """\
        Examples:
          vibe watch add --session-id sesk8m4q2p7x --name 'Wait for export' --message 'The export finished. Inspect it and continue.' --shell 'python3 scripts/wait_for_export.py'
          vibe watch add --create-session --scope-id slack::channel::C123 --message 'The CI job finished. Inspect the result.' -- python3 scripts/wait_for_ci.py --build 42
          vibe watch add --session-id sesk8m4q2p7x --forever --retry-exit-code 75 --retry-delay 60 --message 'The log pattern appeared. Continue from the result below.' --shell 'bash scripts/wait_for_log_pattern.sh'
          vibe watch list
          vibe watch show 12ab34cd56ef
          vibe watch pause 12ab34cd56ef
        """
    )


def _is_apple_silicon_host() -> bool:
    if platform.system().lower() != "darwin":
        return False
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return platform.machine().lower() in {"arm64", "aarch64"}
    return (result.stdout or "").strip() == "1"


def _binary_architecture(path: str | None) -> str | None:
    if not path:
        return None
    resolved_path = str(Path(path).resolve())
    try:
        result = subprocess.run(
            ["file", "-b", resolved_path],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output or None


def _architecture_token(text: str | None) -> str | None:
    normalized = (text or "").lower()
    if "arm64" in normalized or "arm64e" in normalized or "aarch64" in normalized:
        return "arm64"
    if "x86_64" in normalized or "x86-64" in normalized or "amd64" in normalized:
        return "x86_64"
    return None


def _runtime_architecture_items() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    language = _configured_cli_language()
    unknown_value = i18n_t("doctor.value.unknown", language)
    is_apple_silicon = _is_apple_silicon_host()
    host_arch = "Apple Silicon" if is_apple_silicon else platform.machine() or unknown_value
    python_arch = platform.machine() or unknown_value
    python_status = "warn" if is_apple_silicon and _architecture_token(python_arch) == "x86_64" else "pass"

    _add_doctor_item(
        items,
        python_status,
        i18n_t("doctor.item.pythonArchitecture", language, architecture=python_arch, executable=sys.executable),
    )
    if python_status == "warn":
        items[-1]["action"] = i18n_t("doctor.action.nativeArmPython", language)

    uv_path = shutil.which("uv")
    if uv_path:
        uv_arch_output = _binary_architecture(uv_path)
        uv_arch = _architecture_token(uv_arch_output) or "unknown"
        uv_display_arch = unknown_value if uv_arch == "unknown" else uv_arch
        uv_status = "warn" if is_apple_silicon and uv_arch in {"x86_64", "unknown"} else "pass"
        _add_doctor_item(
            items,
            uv_status,
            i18n_t("doctor.item.uvArchitecture", language, architecture=uv_display_arch, path=uv_path),
        )
        if is_apple_silicon and uv_arch == "x86_64":
            items[-1]["action"] = i18n_t("doctor.action.nativeArmUv", language)
        elif is_apple_silicon and uv_arch == "unknown":
            items[-1]["action"] = i18n_t("doctor.action.nativeUvWrapper", language)
    else:
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.uvMissing", language),
            i18n_t("doctor.action.uvMissing", language),
        )

    _add_doctor_item(
        items,
        "pass",
        i18n_t("doctor.item.hostArchitecture", language, architecture=host_arch),
    )
    return items


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except OSError:
        return None


def _path_points_to(path: Path, target: Path) -> bool:
    resolved_path = _safe_resolve(path)
    resolved_target = _safe_resolve(target)
    return resolved_path is not None and resolved_target is not None and resolved_path == resolved_target


def _home_migration_items() -> list[dict]:
    items: list[dict] = []
    language = _configured_cli_language()
    explicit_home = os.environ.get(paths.AVIBE_HOME_ENV)
    if explicit_home:
        _add_doctor_item(
            items,
            "pass",
            i18n_t(
                "doctor.item.explicitHome",
                language,
                path=Path(explicit_home).expanduser(),
            ),
            code="runtime.explicit_home",
        )
        return items

    avibe_home = Path.home() / paths.AVIBE_HOME_DIRNAME
    legacy_home = Path.home() / paths.LEGACY_HOME_DIRNAME
    avibe_present = avibe_home.exists() or avibe_home.is_symlink()
    legacy_present = legacy_home.exists() or legacy_home.is_symlink()

    if not avibe_present and not legacy_present:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.homeReady", language, path=avibe_home),
            code="runtime.home_ready",
        )
        return items

    if avibe_present:
        if legacy_home.is_symlink() and _path_points_to(legacy_home, avibe_home):
            _add_doctor_item(
                items,
                "pass",
                i18n_t(
                    "doctor.item.legacyHomeLinkHealthy",
                    language,
                    legacy_path=legacy_home,
                    active_path=avibe_home,
                ),
                code="runtime.legacy_home_link_ok",
            )
        elif not legacy_present:
            _add_doctor_item(
                items,
                "warn",
                i18n_t("doctor.item.legacyHomeLinkMissing", language, path=legacy_home),
                i18n_t("doctor.action.homeMigrationCreateLink", language),
                code="runtime.legacy_home_link_missing",
                repair_target="home-migration",
                repair_risk="low",
            )
        elif legacy_home.is_symlink():
            _add_doctor_item(
                items,
                "warn",
                i18n_t("doctor.item.legacyHomeLinkWrong", language, path=legacy_home),
                i18n_t("doctor.action.homeMigrationRecreateLink", language),
                code="runtime.legacy_home_link_wrong",
                repair_target="home-migration",
                repair_risk="low",
            )
        else:
            _add_doctor_item(
                items,
                "fail",
                i18n_t(
                    "doctor.item.homeConflict",
                    language,
                    active_path=avibe_home,
                    legacy_path=legacy_home,
                ),
                i18n_t("doctor.action.homeConflict", language),
                code="runtime.home_conflict",
            )
        return items

    if legacy_home.is_symlink():
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.legacyHomeWithoutCanonical", language, active_path=avibe_home),
            i18n_t("doctor.action.legacyHomeWithoutCanonical", language),
            code="runtime.legacy_home_symlink_without_canonical",
        )
        return items

    _add_doctor_item(
        items,
        "warn",
        i18n_t("doctor.item.legacyHomeUnmigrated", language, path=legacy_home),
        i18n_t("doctor.action.homeMigration", language),
        code="runtime.legacy_home_unmigrated",
        repair_target="home-migration",
        repair_risk="low",
    )
    return items


def _tool_family_from_text(text: str | None) -> str | None:
    candidates = [text] if text else []
    try:
        candidates.extend(shlex.split(text or "", posix=(os.name != "nt")))
    except ValueError:
        pass

    for candidate in candidates:
        normalized = (candidate or "").replace("\\", "/").lower()
        for package_name in (PACKAGE_NAME, LEGACY_PACKAGE_NAME):
            if f"/tools/{package_name}/" in normalized:
                return package_name
        if not candidate or not any(separator in candidate for separator in ("/", "\\")):
            continue
        resolved = _safe_resolve(Path(candidate))
        normalized_resolved = str(resolved or "").replace("\\", "/").lower()
        for package_name in (PACKAGE_NAME, LEGACY_PACKAGE_NAME):
            if f"/tools/{package_name}/" in normalized_resolved:
                return package_name
    return None


def _current_cli_install_family() -> str | None:
    candidates = [
        sys.executable,
        os.environ.get(CURRENT_VIBE_EXECUTABLE_ENV),
        *(str(path) for path in _path_entries_for_executable("vibe")[:1]),
    ]
    for candidate in candidates:
        family = _tool_family_from_text(candidate)
        if family:
            return family
    return None


def _service_install_family_items(*, detect_extra_processes: bool = True) -> list[dict]:
    items: list[dict] = []
    language = _configured_cli_language()
    current_family = _current_cli_install_family()
    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    service_pids = [pid for pid in [owner_pid] if pid]
    if detect_extra_processes:
        service_pids.extend(runtime.extra_service_process_pids(owner_pid=owner_pid))

    stale_pids: list[int] = []
    for pid in sorted(set(service_pids)):
        command = runtime.get_process_command(pid)
        service_family = _tool_family_from_text(command)
        if current_family == PACKAGE_NAME and service_family == LEGACY_PACKAGE_NAME:
            stale_pids.append(pid)

    if stale_pids:
        _add_doctor_item(
            items,
            "warn",
            i18n_t(
                "doctor.item.staleInstallProcess",
                language,
                pids=",".join(map(str, stale_pids)),
            ),
            i18n_t("doctor.action.staleInstallRuntime", language),
            code="runtime.stale_install_process",
            repair_target="stale-install-runtime",
            repair_risk="medium",
        )
    elif owner_pid and current_family:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.installMismatchNone", language, pid=owner_pid),
        )
    elif owner_pid:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.installMismatchSkipped", language, pid=owner_pid),
        )
    else:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.installMismatchAbsent", language))
    return items


def _restart_status_is_stale(payload: dict, path: Path) -> bool:
    try:
        state = RestartState(payload.get("state"))
    except (TypeError, ValueError):
        return False

    if state.retention == "seed":
        return not restart_record_is_pending(payload, path, grace_seconds=DOCTOR_RESTART_SEED_GRACE_SECONDS)

    if state.retention == "result":
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > DOCTOR_RESTART_RESULT_RETENTION_SECONDS
    return False


def _restart_failure_summary(payload: dict, language: str) -> str:
    """Describe a recorded restart failure on the single line doctor prints.

    Why it failed is the entire value of the item, so the recorded error is
    carried through rather than summarized away, with its whitespace collapsed
    because the report prints one line per item.
    """

    raw_state = payload.get("state") or RestartState.UNKNOWN.value
    pairs = (
        (
            "doctor.value.restartSummaryState",
            _doctor_display_value(raw_state, "restart_state", language),
        ),
        (
            "doctor.value.restartSummaryError",
            " ".join(str(payload.get("error") or "").split()),
        ),
        ("doctor.value.restartSummaryTrigger", payload.get("trigger")),
        ("doctor.value.restartSummaryJobId", payload.get("job_id")),
        ("doctor.value.restartSummaryLog", payload.get("log_path")),
    )
    return " ".join(i18n_t(key, language, value=value) for key, value in pairs if value)


def _restart_state_items() -> list[dict]:
    items: list[dict] = []
    language = _configured_cli_language()
    restart_path = runtime.get_restart_status_path()
    payload = runtime.read_json(restart_path) or {}
    if not payload:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.restartStateAbsent", language),
            code="runtime.restart_state_absent",
        )
        return items

    # Both clauses are read now, from what is on disk and what is running now.
    # Nothing here asks what was true at some earlier moment, which is the whole
    # design: an earlier version of this decided downtime from a liveness snapshot
    # the supervisor had stamped into the record, and every interleaving between
    # taking that snapshot and acting on it was a way to get the answer wrong. A
    # rule with no remembered observation in it has no such window.
    #
    # The cost is bounded and in the safe direction. A restart that failed without
    # stopping the old service -- the spawn path never stops anything -- leaves a
    # record that outlives its relevance, so after that service is later stopped on
    # purpose this reports a failure that is history. Both halves of the sentence
    # are still true, and `vibe start` ends it. Suppressing it instead would mean
    # trusting a remembered snapshot to stay true, and for a diagnostic the
    # asymmetry decides it: a stale `fail` is a true statement with a self-clearing
    # next step, while a wrong `pass` is the eight-day silent outage in #1567
    # sitting behind a green health check.
    #
    # Note what the action must not say, which is the original defect: never offer
    # the marker-deleting repair here. The reader may be genuinely down, and that
    # command both destroys the only record of why and makes doctor pass again --
    # an operator following it would silence their own health check.
    #
    # This has to be read before the staleness branch below, because terminal
    # metadata goes stale after DOCTOR_RESTART_RESULT_RETENTION_SECONDS, and that
    # branch offers a repair that deletes the marker -- on a still-down instance,
    # the reason it is down.
    #
    # Liveness asks `verified_service_running`, never the broader
    # `service_process_running`. The broad one reports whatever occupies this data
    # dir, which is the right question for refusing a second start and the wrong
    # one here: a pid reserved by a process that never acquired the lock is the
    # wreckage of a failed start, not a recovery, and reading it as one would
    # suppress the very failure it came from. Nor does holding the lock make a
    # process a service, because the lock is taken before the database is
    # migrated -- the generation that hung mid-migration in #1567 held it for
    # eight days -- so the owner also requires the holder's own published start.
    #
    # What that leaves the reader is a process `start_service` refuses to start
    # past, because it asks the broad question -- so the action has to cover it,
    # and `_service_lifecycle_items` cannot be the one to do that here: its
    # extra-process item is behind `--deep` and the default run is exactly where a
    # reader of this lands.
    #
    # Which is the whole discipline for the text below. Every sentence of procedure
    # is a claim about control flow this item does not own, and each one is
    # separately falsifiable: earlier revisions deferred to an item that is not
    # rendered by default, and then told the reader to start again after a repair
    # that starts the service itself. So it names each command once, in order, and
    # says the one thing the reader cannot see -- that the repair brings the
    # service up -- because that is what stops them from running start twice and
    # reading `ServiceAlreadyRunningError` as a failed recovery.
    #
    # The occupier decides which command, and only one of them can be prescribed
    # blind: `duplicate-service-processes` stops what the scan sees beside the lock
    # owner, so it reaches a holder whose record answers no pid and skips one that
    # answers its own -- and a holder stuck mid-startup is exactly the second kind.
    # `vibe stop` is what covers that one. Anything beyond naming both is a
    # prediction, and the commands report their own outcomes.
    if payload.get("ok") is False and not runtime.verified_service_running():
        _add_doctor_item(
            items,
            "fail",
            i18n_t(
                "doctor.item.restartFailed",
                language,
                summary=_restart_failure_summary(payload, language),
            ),
            i18n_t("doctor.action.restartFailed", language),
            code="runtime.restart_failed",
        )
        return items

    if _restart_status_is_stale(payload, restart_path):
        state = payload.get("state") or "unknown"
        _add_doctor_item(
            items,
            "warn",
            i18n_t(
                "doctor.item.staleRestartState",
                language,
                state=_doctor_display_value(state, "restart_state", language),
            ),
            i18n_t("doctor.action.staleRestartState", language),
            code="runtime.stale_restart_state",
            repair_target="stale-restart-state",
            repair_risk="low",
            restart_state=state,
        )
    else:
        state = payload.get("state") or "unknown"
        _add_doctor_item(
            items,
            "pass",
            i18n_t(
                "doctor.item.restartStateCurrent",
                language,
                state=_doctor_display_value(state, "restart_state", language),
            ),
            restart_state=state,
        )
    return items


def _service_lifecycle_items(*, detect_extra_processes: bool = True) -> list[dict]:
    items: list[dict] = []
    language = _configured_cli_language()
    missing_value = i18n_t("doctor.value.missing", language)
    pid_path = paths.get_runtime_pid_path()
    recorded_pid: int | None = None
    try:
        recorded_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        recorded_pid = None

    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    lock_holder_pid = runtime.service_lock_holder_pid()
    status = runtime.read_status()
    status_pid = status.get("service_pid")

    if owner_pid:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.serviceLockOwner", language, pid=owner_pid),
            code="runtime.service_lock_owner",
        )
    elif lock_holder_pid:
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.serviceLockUnverified", language, pid=lock_holder_pid),
            i18n_t("doctor.action.serviceLockUnverified", language),
            code="runtime.unverified_service_lock",
        )
    else:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.serviceLockFree", language),
            code="runtime.service_lock_free",
        )

    if owner_pid and recorded_pid != owner_pid:
        _add_doctor_item(
            items,
            "warn",
            i18n_t(
                "doctor.item.servicePidfileMismatch",
                language,
                pidfile=recorded_pid or missing_value,
                owner=owner_pid,
            ),
            i18n_t("doctor.action.servicePidfileMismatch", language),
            code="runtime.service_pidfile_mismatch",
        )
    elif recorded_pid:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.servicePidfile", language, pid=recorded_pid))
    else:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.servicePidfileAbsent", language))

    if owner_pid and status_pid != owner_pid:
        _add_doctor_item(
            items,
            "warn",
            i18n_t(
                "doctor.item.statusPidMismatch",
                language,
                status=status_pid or missing_value,
                owner=owner_pid,
            ),
            i18n_t("doctor.action.statusPidMismatch", language),
            code="runtime.status_pid_mismatch",
        )
    elif status_pid:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.statusPid", language, pid=status_pid))
    else:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.statusPidAbsent", language))

    if detect_extra_processes:
        extra_service_pids = runtime.extra_service_process_pids(owner_pid=owner_pid)
        unverified_service_pids = runtime.extra_service_process_pids(
            owner_pid=owner_pid,
            include_unverified=True,
        )
        unverified_service_pids = [pid for pid in unverified_service_pids if pid not in set(extra_service_pids)]
        if extra_service_pids:
            _add_doctor_item(
                items,
                "warn",
                i18n_t(
                    "doctor.item.extraServiceProcess",
                    language,
                    pids=",".join(map(str, extra_service_pids)),
                ),
                i18n_t("doctor.action.duplicateServiceProcesses", language),
                code="runtime.extra_service_process",
                repair_target="duplicate-service-processes",
                repair_risk="medium",
            )
        elif unverified_service_pids:
            _add_doctor_item(
                items,
                "warn",
                i18n_t(
                    "doctor.item.unverifiedServiceProcess",
                    language,
                    pids=",".join(map(str, unverified_service_pids)),
                ),
                i18n_t("doctor.action.unverifiedServiceProcess", language),
                code="runtime.unverified_service_process",
            )
        else:
            _add_doctor_item(items, "pass", i18n_t("doctor.item.noExtraServiceProcess", language))
    else:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.deepScanSkipped", language),
            i18n_t("doctor.action.deepScanSkipped", language),
            code="runtime.deep_service_process_scan_skipped",
        )

    return items


def _show_git_checkpoint_items() -> list[dict]:
    try:
        if runtime.resolve_service_owner_pid(include_starting=False):
            from core.show_git import show_git_checkpointing_active

            available = show_git_checkpointing_active()
        else:
            from core.git_binary import resolve_git

            available = resolve_git() is not None
    except Exception:
        available = False
    if available:
        return []
    return [
        {
            "status": "warn",
            "message": i18n_t("doctor.item.showGitUnavailable", _configured_cli_language()),
            "code": "runtime.show_git_unavailable",
        }
    ]


def _remote_examples_text() -> str:
    return dedent(
        """\
        Examples:
          vibe remote
          vibe remote status
          vibe remote start
          vibe remote stop
          vibe remote pair vrp_abc123
        """
    )


def _remote_pair_examples_text() -> str:
    return dedent(
        """\
        Guidance:
          This is the direct pairing command for users who already have a pairing key.
          For the guided setup flow, run `vibe remote`.
          If you omit the pairing key, the CLI prompts for it without echoing it to the terminal.
          Pairing saves the remote-access config and then starts the managed tunnel automatically.
          The pairing key is one-time use; create a fresh key from the Avibe Cloud console if it fails.

        Examples:
          vibe remote
          vibe remote pair vrp_abc123
          vibe remote pair --device-name "Mac Studio"
          vibe remote pair --backend-url https://avibe.bot
        """
    )


def _show_examples_text() -> str:
    markdown_help = i18n_t("show.markdown.help", _configured_cli_language())
    return dedent(
        """\
        A Show Page is one session-scoped visual page that Avibe serves through the Web UI / Avibe Cloud tunnel.
        One Agent Session has exactly one Show Page.

        Agent-readable representation:
          __MARKDOWN_HELP__

        Commands:
          list     List existing Show Pages across sessions.
          path     Create or resolve the local workspace.
          status   Inspect local path, visibility, active URL, and share state.
          update   Switch visibility, set a custom public link, rotate share links, or take the page offline.
          mark     Add an assistant mark event to the session.
          reply    Reply to a dispatched page annotation at its original anchor.
          marks    List active assistant marks.
          unmark   Resolve active assistant marks by id or target.
          event    Record a generic annotation-layer event.
          annotate Control the page's annotation overlay.

        Visibility:
          private  Authenticated Web UI URL under /show/<session-id>/.
          public   Short unauthenticated share URL under /p/<share-id>/.
          offline  URL access is revoked; local files remain.

        Examples:
          vibe show list
          vibe show list --visibility public
          vibe show path --session-id sesk8m4q2p7x
          vibe show status --session-id sesk8m4q2p7x --json
          vibe show update --session-id sesk8m4q2p7x --visibility public
          vibe show update --session-id sesk8m4q2p7x --visibility offline
          vibe show mark mark-default-summary --session-id sesk8m4q2p7x --message "Review this summary."
          vibe show reply show_evt_1a2b3c4d --message "The source changed in W30."
          vibe show marks --json
          vibe show unmark mark_1 mark-default-summary
          vibe show event --session-id sesk8m4q2p7x --event-json @./show-event.json --json
          vibe show annotate --session-id sesk8m4q2p7x --on --mode screenshot

        More:
          vibe show list --help
          vibe show path --help
          vibe show status --help
          vibe show update --help
          vibe show mark --help
          vibe show reply --help
          vibe show marks --help
          vibe show unmark --help
          vibe show event --help
          vibe show annotate --help
        """
    ).replace("__MARKDOWN_HELP__", markdown_help)


def _show_path_examples_text() -> str:
    return dedent(
        """\
        Returns the directory where the agent should write a React/Vite Show Page.
        The directory is created if needed. On first creation, Avibe writes src/App.tsx,
        src/styles.css, index.html, and a sample api/health.ts handler.

        First-run workflow:
          1. Run: vibe show path --session-id sesk8m4q2p7x
          2. Write or update src/App.tsx in the returned path.
          3. Share the active URL if the command output includes one.
          4. Run `vibe show update --session-id sesk8m4q2p7x --visibility public` only when the user asks for a shareable public link.
        """
    )


def _show_status_examples_text() -> str:
    return dedent(
        """\
        Shows the current Show Page state without creating a new page.

        Fields include:
          path, visibility, active_url, private_url, public_url, share_id, offline, created_at, updated_at.

        Use --json when another program or agent will consume the result.
        """
    )


def _show_update_examples_text() -> str:
    return dedent(
        """\
        Change the current Show Page state.

        Examples:
          vibe show update --session-id sesk8m4q2p7x --visibility public
          vibe show update --session-id sesk8m4q2p7x --share-id q3-roadmap
          vibe show update --session-id sesk8m4q2p7x --visibility private
          vibe show update --session-id sesk8m4q2p7x --visibility offline
          vibe show update --session-id sesk8m4q2p7x --rotate-share

        Notes:
          private uses the authenticated /show/<session-id>/ URL.
          public uses a short /p/<share-id>/ URL and disables the private path.
          offline takes the page down without deleting local files.
          --share-id sets a custom /p/<share-id>/ suffix (3-64 chars, unique);
            allowed only while public, and it replaces the previous public URL.
          --rotate-share is allowed only while the page is public.
        """
    )


def _show_mark_examples_text() -> str:
    return dedent(
        """\
        Add an assistant-authored mark event to the session's Show Page event stream.
        The mark is also projected into the session transcript as an assistant message.

        Target should be a short mark id or selector understood by the Show Page, usually
        a value produced by @avibe/show-sdk's mark helpers.

        Examples:
          vibe show mark mark-default-summary --session-id sesk8m4q2p7x --message "Review this summary."
          vibe show mark summary --session-id sesk8m4q2p7x --scope default --message-file ./comment.txt --json
          vibe show mark --target summary --body "Legacy option aliases still work."
        """
    )


def _show_event_examples_text() -> str:
    return dedent(
        """\
        Record any Show Page event supported by the annotation layer.

        Examples:
          vibe show event --session-id sesk8m4q2p7x --type assistant.page.updated --event-json '{"summary":"Updated the plan."}'
          vibe show event --session-id sesk8m4q2p7x --event-json @./show-event.json --json
        """
    )


def _show_annotate_examples_text() -> str:
    return dedent(
        """\
        Control the annotation overlay through the Show Page event stream.
        Control events do not create transcript messages or dispatch Agent turns.

        Examples:
          vibe show annotate --session-id sesk8m4q2p7x --on
          vibe show annotate --session-id sesk8m4q2p7x --on --mode screenshot
          vibe show annotate --session-id sesk8m4q2p7x --mode smart
          vibe show annotate --session-id sesk8m4q2p7x --off --json
        """
    )


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _watch_add_examples_text() -> str:
    return dedent(
        """\
        Session target:
          Use --session-id with the target Agent Session ID, for example sesk8m4q2p7x.
          Inside an Avibe Agent shell, watches follow up in this conversation by default.

        Guidance:
          If this is your first time using this command, read this whole help entry before creating a watch.
          Use a watch when a script should wait in the background and send a follow-up when it detects an event or reaches a terminal failure.
          `--session-id` chooses which Agent Session Avibe will continue using for follow-up messages from the watch.
          Use --create-session with --scope-id <scopes.id> to create a reusable Session in a specific existing scope.
          Use --create-session with --same-scope only from an Avibe Agent shell, where the caller Session scope is available.
          Prefer --message or --message-file for follow-up instructions; --prefix is legacy-compatible.
          Terminal failures also send a follow-up and disable the watch.
          In either mode, an allowed `--retry-exit-code` keeps waiting. A once Watch stops after its first event.
          A forever Watch waits for each event's Agent Run to finish before re-arming, then applies a five-second safety delay.
          Exit 0 must mean one new reportable event, not a condition that merely remains true. Repeated rapid successes automatically pause the Watch and send the Agent a repair message.
          Waiter exit codes: 0 detected an event and sends the follow-up; 124 timed out and is terminal unless explicitly allowed for retry;
          64 PLUS the line 'avibe-watch: no-event' on stderr means the cycle ran and found nothing worth reporting,
          so a once Watch ends and a forever Watch re-arms WITHOUT an Agent turn. A once waiter that is still waiting must use a retry exit code.
          Any other non-zero is a failure.
          The marker is required: 64 alone is also sysexits EX_USAGE, so a bare 64 stays a failure and stops the watch.
          Use it in waiters whose normal outcome is uninteresting, such as green CI.
          Pass either --shell '<command>' or a command after '--'.
          --timeout applies to each cycle. --lifetime-timeout applies to the whole Watch lifetime across retries and re-arms.

        Examples:
          vibe watch add --session-id sesk8m4q2p7x --message 'The export finished. Inspect it and continue.' --shell 'python3 scripts/wait_for_export.py'
          vibe watch add --create-session --scope-id slack::channel::C123 --message 'The export finished.' -- bash -lc 'sleep 120; echo done'
          vibe watch add --session-id sesk8m4q2p7x --forever --timeout 600 --lifetime-timeout 86400 --retry-exit-code 75 --retry-delay 30 --message 'PR #153 changed. Inspect it and continue.' -- uv run --no-project scripts/wait_pr.py --repo avibe-bot/avibe --pr 153
        """
    )


def _agent_run_examples_text() -> str:
    return dedent(
        """\
        Session target:
          Use --session-id to continue an existing Agent Session.
          The default is P1: steer an active native Turn, start when idle, or fall back to the durable P3 queue.
          Add --queue to persist this Run as P3 behind the active Turn.
          --send-now explicitly selects the same P1 content delivery for an existing Session.
          To promote the exact existing P3 queue head without a new message, use: vibe session send-now <session-id>
          Inspect queued work with: vibe session queue list <session-id>
          Remove one exact queued row with: vibe session queue remove <session-id> <message-id>
          Omit --session-id/--fork-self/--fork-session to create a background Session for --agent.
          Inside an Agent shell it inherits the caller scope and invocation cwd; outside one it is standalone with its own Show workspace.
          Use --same-scope to explicitly place a new Session in the caller/source Session's scope.
          Use --scope-id <scopes.id> to place a new Session in a specific existing scope.
          New and forked delegated Sessions are background by default; pass --visible to make them user-facing and enable outward delivery.
          --cwd only applies to new Sessions; existing Sessions keep their own cwd.

        Callback:
          Agent runs are async by default. From an Avibe Agent shell, they return their final result to this conversation by default.
          From a normal terminal, pass --callback-session-id or --no-callback for async runs.
          Pass --no-callback only when you intentionally want no automatic follow-up.
          Pass --callback-session-id only when the final result should return somewhere else.
          Pass --sync when the CLI should wait for the run result in the terminal.

        Forking:
          --fork-self forks this current Session.
          --fork-session <session-id> creates a new Avibe Agent Session and asks the native backend to fork the source native session on the first turn.
          Forks keep the same backend, scope, and cwd as the source Session. Passing --agent is allowed only when that Agent uses the same backend.
          --agent, --model, and --reasoning-effort may override the forked Session's Agent/model/effort.
          Do not combine fork flags with --session-id or --create-session.

        Avibe Agent shell examples:
          vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'
          vibe agent run --agent release-reviewer --visible --message 'Review this project in a visible sibling Session.'
          vibe agent run --session-id sesk8m4q2p7x --send-now --message 'Apply this correction in the current turn.'
          vibe session queue list sesk8m4q2p7x
          vibe session queue remove sesk8m4q2p7x msg_queued123
          vibe session send-now sesk8m4q2p7x

        Normal terminal examples:
          vibe agent run --sync --agent release-reviewer --message 'Review the latest CI result and print it here.'
          vibe agent run --agent release-reviewer --callback-session-id sescaller456 --message 'Review the latest CI result and report back.'
          vibe agent run --agent release-reviewer --no-callback --message 'Run a background experiment; I will inspect the run later.'

        Fork examples:
          vibe agent run --fork-self --message 'Explore this alternate fix from the current context.'
          vibe agent run --fork-session sesk8m4q2p7x --agent reviewer --model gpt-5.4 --reasoning-effort high --message 'Review the forked context.'
        """
    )


def _add_hidden_task_alias(task_subparsers, alias: str, parser) -> None:
    alias_parser = task_subparsers.add_parser(
        alias,
        help=argparse.SUPPRESS,
        parents=[parser],
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    alias_parser.error_help_command = getattr(parser, "error_help_command", None)
    alias_parser.error_hint = getattr(parser, "error_hint", None)
    task_subparsers._choices_actions = [  # type: ignore[attr-defined]
        action for action in task_subparsers._choices_actions if action.dest != alias  # type: ignore[attr-defined]
    ]


def _write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _read_json(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pid_alive(pid):
    return runtime.pid_alive(pid)


def _in_ssh_session() -> bool:
    """Best-effort detection for SSH sessions."""
    return any(os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


def _open_browser(url: str) -> bool:
    """Open a URL in the default browser (best effort).

    Returns True if a launch attempt was made successfully.
    """
    try:
        import webbrowser

        if webbrowser.open(url):
            return True
    except Exception:
        pass

    # Fallbacks for environments where webbrowser isn't configured.
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", url])
            return True
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", url])
            return True
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", url])
            return True
    except Exception:
        pass

    return False


def _default_config():
    # Single source of truth lives in ``core.services.settings`` so the CLI's
    # seed-on-first-run default and the UI's read-side default (GET /api/config
    # on a fresh install) can never drift apart.
    from core.services import settings as settings_service

    return settings_service.default_config()


def _ensure_config():
    # Routed through ``core.services.settings`` so the UI server, CLI, and
    # future internal RPC pick up the same config-file lifecycle. The
    # default-factory keeps the CLI-only "seed on first run" behavior.
    from core.services import settings as settings_service

    return settings_service.load_config(default_factory=_default_config)


def _write_status(state, detail=None):
    payload = {
        "state": state,
        "detail": detail,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(paths.get_runtime_status_path(), payload)


def _spawn_background(
    args,
    pid_path,
    stdout_name: str = "service_stdout.log",
    stderr_name: str = "service_stderr.log",
):
    return runtime.spawn_background(args, pid_path, stdout_name, stderr_name)


def _stop_process(pid_path):
    return runtime.stop_process(pid_path)


def _render_status():
    return runtime.render_status()


def _default_timezone_name() -> str:
    try:
        return get_localzone_name()
    except Exception:
        tz = datetime.now().astimezone().tzinfo
        key = getattr(tz, "key", None)
        if key:
            return str(key)
    return "UTC"


def _resolve_prompt_input(args, *, help_command: str, example_command: str) -> str:
    if getattr(args, "prompt", None) is not None or getattr(args, "prompt_file", None) is not None:
        raise TaskCliError(
            "--prompt is deprecated; use --message instead",
            code="deprecated_prompt_argument",
            hint="Use --message for the user message sent to the Agent, or --message-file for file input.",
            example=f"{example_command} --message 'Share the hourly summary.'",
            help_command=help_command,
        )
    return _resolve_message_input(args, help_command=help_command, example_command=example_command)


def _resolve_message_input(args, *, help_command: str, example_command: str) -> str:
    if getattr(args, "prompt", None) is not None or getattr(args, "prompt_file", None) is not None:
        raise TaskCliError(
            "--prompt is deprecated; use --message instead",
            code="deprecated_prompt_argument",
            hint="Use --message for the user message sent to the Agent, or --message-file for file input.",
            example=f"{example_command} --message 'Share the hourly summary.'",
            help_command=help_command,
        )
    message = (getattr(args, "message", None) or "").strip()
    message_file = getattr(args, "message_file", None)
    if message and message_file:
        raise TaskCliError(
            "use either --message or --message-file",
            code="conflicting_message_inputs",
            hint="Pass inline text with --message or load it from disk with --message-file, but not both.",
            help_command=help_command,
        )
    if message:
        return message
    if getattr(args, "message", None) is not None:
        raise TaskCliError(
            "message text cannot be empty",
            code="empty_message",
            hint="Provide non-empty text after --message, or use --message-file with a readable text file.",
            help_command=help_command,
        )
    if message_file:
        try:
            content = Path(message_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TaskCliError(
                f"failed to read message file: {exc}",
                code="message_file_read_failed",
                hint="Use --message-file with a readable UTF-8 text file.",
                example=f"{example_command} --message-file briefing.md",
                help_command=help_command,
                details={"message_file": message_file},
            ) from exc
        if not content:
            raise TaskCliError(
                "message file is empty",
                code="empty_message",
                hint="Put the message text in the file, or pass it directly with --message.",
                example=f"{example_command} --message 'Share the hourly summary.'",
                help_command=help_command,
                details={"message_file": message_file},
            )
        return content
    raise TaskCliError(
        "one of --message or --message-file is required",
        code="missing_message",
        hint="Pass inline text with --message or load it from disk with --message-file.",
        help_command=help_command,
    )


def _resolve_optional_message_input(
    args,
    *,
    help_command: str,
    example_command: str,
    legacy_prefix: Optional[str] = None,
) -> Optional[str]:
    if getattr(args, "prompt", None) is not None or getattr(args, "prompt_file", None) is not None:
        raise TaskCliError(
            "--prompt is deprecated; use --message instead",
            code="deprecated_prompt_argument",
            hint="Use --message for the user message sent to the Agent, or --message-file for file input.",
            example=f"{example_command} --message 'Review the waiter output.'",
            help_command=help_command,
        )
    has_message = getattr(args, "message", None) is not None or getattr(args, "message_file", None) is not None
    has_prefix = legacy_prefix is not None
    if has_message and has_prefix:
        raise TaskCliError(
            "use either --message/--message-file or --prefix, not both",
            code="conflicting_message_inputs",
            hint="Use --message for new watches. --prefix is only a compatibility alias.",
            help_command=help_command,
        )
    if has_message:
        return _resolve_message_input(args, help_command=help_command, example_command=example_command)
    return legacy_prefix


def _resolve_legacy_prompt_input(args, *, help_command: str, example_command: str) -> str:
    prompt = (getattr(args, "prompt", None) or "").strip()
    prompt_file = getattr(args, "prompt_file", None)
    if prompt and prompt_file:
        raise TaskCliError(
            "use either --prompt or --prompt-file",
            code="conflicting_prompt_inputs",
            hint="Pass inline text with --prompt or load it from disk with --prompt-file, but not both.",
            help_command=help_command,
        )
    if prompt:
        return prompt
    if getattr(args, "prompt", None) is not None:
        raise TaskCliError(
            "prompt text cannot be empty",
            code="empty_prompt",
            hint="Provide non-empty text after --prompt, or use --prompt-file with a readable text file.",
            help_command=help_command,
        )
    if prompt_file:
        try:
            content = Path(prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TaskCliError(
                f"failed to read prompt file: {exc}",
                code="prompt_file_read_failed",
                hint="Use --prompt-file with a readable UTF-8 text file.",
                example=f"{example_command} --prompt-file briefing.md",
                help_command=help_command,
                details={"prompt_file": prompt_file},
            ) from exc
        if not content:
            raise TaskCliError(
                "prompt file is empty",
                code="empty_prompt",
                hint="Put the prompt text in the file, or pass it directly with --prompt.",
                example=f"{example_command} --prompt 'Share the hourly summary.'",
                help_command=help_command,
                details={"prompt_file": prompt_file},
            )
        return content
    raise TaskCliError(
        "one of --prompt or --prompt-file is required",
        code="missing_prompt",
        hint="Pass inline text with --prompt or load it from disk with --prompt-file.",
        help_command=help_command,
    )


def _normalize_run_at(value: str, timezone_name: str) -> str:
    dt = datetime.fromisoformat(value)
    tz = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)
    return dt.isoformat()


def _normalize_task_name(value: Optional[str], *, allow_none: bool = True) -> Optional[str]:
    if value is None:
        return None if allow_none else ""
    normalized = value.strip()
    if not normalized:
        raise TaskCliError(
            "task name cannot be empty",
            code="empty_task_name",
            hint="Pass a short non-empty name, or omit --name.",
        )
    return normalized


def _normalize_watch_name(value: Optional[str], *, help_command: str = "vibe watch add --help") -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise TaskCliError(
            "watch name cannot be empty",
            code="empty_watch_name",
            hint="Pass a short non-empty name, or omit --name.",
            help_command=help_command,
        )
    return normalized


def _resolve_existing_cwd(value: str, *, help_command: str, code: str, label: str) -> str:
    resolved = Path(value).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise TaskCliError(
            f"{label} cwd does not exist: {value}",
            code=code,
            hint="Point --cwd to an existing directory, or omit it to use the invocation directory.",
            help_command=help_command,
            details={"cwd": value},
        )
    return str(resolved)


def _resolve_watch_cwd(value: Optional[str], *, help_command: str, default_to_invocation: bool = False) -> Optional[str]:
    if not value:
        return os.getcwd() if default_to_invocation else None
    return _resolve_existing_cwd(value, help_command=help_command, code="invalid_watch_cwd", label="watch")


def _validate_watch_timing(
    *,
    timeout_seconds: float,
    retry_delay_seconds: float,
    lifetime_timeout_seconds: float,
    help_command: str,
) -> None:
    if timeout_seconds < 0:
        raise TaskCliError(
            "--timeout must be >= 0",
            code="invalid_watch_timeout",
            hint="Use 0 for no per-cycle timeout, or a positive number of seconds.",
            help_command=help_command,
            details={"timeout": timeout_seconds},
        )
    if retry_delay_seconds < 0:
        raise TaskCliError(
            "--retry-delay must be >= 0",
            code="invalid_watch_retry_delay",
            hint="Use 0 to retry immediately, or a positive number of seconds.",
            help_command=help_command,
            details={"retry_delay": retry_delay_seconds},
        )
    if lifetime_timeout_seconds < 0:
        raise TaskCliError(
            "--lifetime-timeout must be >= 0",
            code="invalid_watch_lifetime_timeout",
            hint="Use 0 for no overall lifetime limit, or a positive number of seconds.",
            help_command=help_command,
            details={"lifetime_timeout": lifetime_timeout_seconds},
        )


def _task_message_preview(message: str, *, max_chars: int = 72) -> str:
    compact = " ".join((message or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _task_command_fields(task) -> tuple[Optional[str], list[str], dict]:
    """``(shell_command, command, metadata)`` from a stored row or a read projection."""

    if isinstance(task, Mapping):
        shell_command = task.get("shell_command")
        command = task.get("command") or []
        metadata = task.get("metadata")
    else:
        shell_command = task.shell_command
        command = task.command or []
        metadata = task.metadata
    return (
        str(shell_command) if shell_command else None,
        [str(item) for item in command] if isinstance(command, list) else [],
        metadata if isinstance(metadata, dict) else {},
    )


def _task_kind(task) -> str:
    """``"command"`` when this definition runs a subprocess, else ``"message"``."""

    shell_command, command, _metadata = _task_command_fields(task)
    return "command" if (shell_command or command) else "message"


def _task_on_failure(task) -> str:
    _shell_command, _command, metadata = _task_command_fields(task)
    value = str(metadata.get("on_failure") or "none").strip().lower()
    return value or "none"


def _task_command_preview(task) -> str:
    shell_command, command, _metadata = _task_command_fields(task)
    return command_line_preview(shell_command, command)


def _task_display_name(task) -> str:
    # A command task stores no message, so the command itself is the only human-readable
    # label the list has; without this fallback those rows rendered with an empty name.
    return task.name or _task_message_preview(task.prompt) or _task_command_preview(task)


def _task_state(task) -> str:
    if _is_failed_one_shot(task):
        return "failed"
    if _is_completed_one_shot(task):
        return "completed"
    if task.enabled:
        return "active"
    return "paused"


def _task_last_status(task) -> str:
    """Historical compatibility field; never used to determine lifecycle.

    Deliberately three-valued and identical to ``_task_projection_last_status``.
    An earlier revision of this change added a fourth value, ``degraded``, here —
    before #1061 demoted the field. Keeping that would have put new semantics on a
    field declared compatibility-only AND made the two spellings of ``last_status``
    disagree about their own vocabulary. Health is reported in its own fields,
    computed once in ``_enrich_definitions`` and read back by everything else.
    """

    if task.last_run_at and task.last_error:
        return "failed"
    if task.last_run_at:
        return "succeeded"
    return "never_run"


def _task_next_run_at(task) -> Optional[str]:
    return compute_next_run_at(
        enabled=task.enabled,
        schedule_type=task.schedule_type,
        cron=task.cron,
        run_at=task.run_at,
        timezone_name=task.timezone,
    )


def _task_schedule_summary(task) -> str:
    if isinstance(task, Mapping):
        schedule_type = str(task.get("schedule_type") or "")
        cron = task.get("cron")
        run_at = task.get("run_at")
    else:
        schedule_type = task.schedule_type
        cron = task.cron
        run_at = task.run_at
    if schedule_type == "cron":
        return f"cron:{cron}" if cron else "cron"
    if schedule_type == "at":
        return f"at:{run_at}" if run_at else "at"
    return schedule_type


def _task_payload(task, *, brief: bool = False):
    """The stored row alone, for the one case that has no projection to read.

    Carries no health: derived health is a fact about ``agent_runs`` that only
    ``_enrich_definitions`` computes, and a payload builder that answered it from
    a second query of its own is how the CLI came to disagree with every other
    surface. Callers reach this only when the read-back finds nothing — the
    file-backed store has no run history at all — where degrading to the stored
    fields beats inventing a badge.
    """

    derived = {
        "display_name": _task_display_name(task),
        "message_preview": _task_message_preview(task.prompt),
        "state": _task_state(task),
        "last_status": _task_last_status(task),
        "next_run_at": _task_next_run_at(task),
        "schedule_summary": _task_schedule_summary(task),
        # Command-task facts. ``kind`` is what every surface branches on, and
        # ``on_failure`` is carried even for message tasks so the key never has to be
        # probed for existence.
        "kind": _task_kind(task),
        "on_failure": _task_on_failure(task),
        "command_preview": _task_command_preview(task),
    }
    if brief:
        return {
            "id": task.id,
            "name": task.name,
            "display_name": derived["display_name"],
            "kind": derived["kind"],
            "last_exit_code": task.last_exit_code,
            "state": derived["state"],
            "last_status": derived["last_status"],
            # ``last_error`` used to be dropped here, which left the list a user
            # actually runs unable to say WHY anything was failing.
            "last_error": task.last_error,
            "next_run_at": derived["next_run_at"],
            "schedule_type": task.schedule_type,
            "schedule_summary": derived["schedule_summary"],
            "session_id": task.session_id,
            "session_key": task.session_key,
            "agent_name": task.agent_name,
            "post_to": task.post_to,
            "deliver_key": task.deliver_key,
            "timezone": task.timezone,
            "enabled": task.enabled,
        }
    payload = task.to_dict()
    payload.update(derived)
    return payload


_CANONICAL_DEFINITION_FIELDS = (
    "lifecycle_state",
    "lifecycle_detail",
    "lifecycle_finished_at",
    "next_run_at",
    "waiting_since",
    "running_since",
)

#: Derived failure health, forwarded verbatim beside the canonical lifecycle
#: fields — NOT folded into ``lifecycle_state`` and NOT smuggled into
#: ``last_status``.
#:
#: Health and lifecycle are ORTHOGONAL axes. A cron that fails every night is
#: ``waiting`` between fires and ``failing`` the whole time; that combination is
#: precisely the case this exists to surface. Folding health into
#: ``lifecycle_state`` would need a fifth value in a closed four-value vocabulary
#: the Workbench switches on three ways (icon, pill class, i18n key), or would lose
#: one of the two axes.
#:
#: ``last_status`` is the other wrong home: it is explicitly a compatibility field
#: that never determines lifecycle, so putting new semantics there would leave the
#: canonical projection unable to see that a definition is broken — which is the
#: original defect one layer up.
#:
#: These ride the same store row as the canonical fields, because the health query
#: is computed in ``_enrich_definitions`` — the one chokepoint every list, show and
#: Workbench read already passes through.
_DEFINITION_FAILURE_FIELDS = (
    "health",
    "consecutive_failures",
    "recent_failures",
    # Watches keep waiter health above and expose their downstream Agent Run
    # history independently. Tasks omit these keys.
    "processing_health",
    "processing_consecutive_failures",
    "processing_recent_failures",
    # The one field that says WHY, dropped from the brief list payload before.
    "last_error",
    "resume_blocked",
)


def _task_projection_state(task: Mapping[str, object]) -> str:
    """Compatibility ``state`` derived from the canonical lifecycle fields."""

    lifecycle_state = task.get("lifecycle_state")
    if lifecycle_state in {"waiting", "running"}:
        return "active"
    if lifecycle_state == "finished":
        lifecycle_detail = task.get("lifecycle_detail")
        if lifecycle_detail == "canceled":
            return "canceled"
        if lifecycle_detail in {"timeout", "error", "missed"}:
            return "failed"
        if lifecycle_detail == "normal":
            return "completed"
        return "unknown"
    if lifecycle_state == "paused":
        return "paused"
    return "unknown"


def _task_projection_last_status(task: Mapping[str, object]) -> str:
    """Historical compatibility field; never used to determine lifecycle."""

    if task.get("last_run_at") and task.get("last_error"):
        return "failed"
    if task.get("last_run_at"):
        return "succeeded"
    return "never_run"


def _task_projection_payload(task: Mapping[str, object], *, brief: bool = False) -> dict:
    prompt = str(task.get("prompt") or "")
    name = task.get("name")
    command_preview = _task_command_preview(task)
    derived = {
        "display_name": str(name) if name else (_task_message_preview(prompt) or command_preview),
        "message_preview": _task_message_preview(prompt),
        "state": _task_projection_state(task),
        "last_status": _task_projection_last_status(task),
        "schedule_summary": _task_schedule_summary(task),
        "kind": _task_kind(task),
        "on_failure": _task_on_failure(task),
        "command_preview": command_preview,
    }
    if brief:
        payload = {
            "id": task.get("id"),
            "name": name,
            "display_name": derived["display_name"],
            "kind": derived["kind"],
            "last_exit_code": task.get("last_exit_code"),
            "state": derived["state"],
            "last_status": derived["last_status"],
            "schedule_type": task.get("schedule_type"),
            "schedule_summary": derived["schedule_summary"],
            "session_id": task.get("session_id"),
            "session_key": task.get("session_key"),
            "agent_name": task.get("agent_name"),
            "post_to": task.get("post_to"),
            "deliver_key": task.get("deliver_key"),
            "timezone": task.get("timezone"),
            "enabled": task.get("enabled"),
        }
        payload.update({field: task.get(field) for field in _CANONICAL_DEFINITION_FIELDS})
        payload.update({field: task.get(field) for field in _DEFINITION_FAILURE_FIELDS})
        return payload
    payload = dict(task)
    payload.update(derived)
    return payload


def _task_store() -> ScheduledTaskStore:
    return ScheduledTaskStore()


@contextlib.contextmanager
def _definition_read_store():
    """Own the canonical read projection store used by CLI read commands."""

    store = SQLiteBackgroundTaskStore()
    try:
        yield store
    finally:
        store.close()


def _read_definition_projection(
    read: Callable[[SQLiteBackgroundTaskStore], Optional[dict[str, Any]]],
) -> Optional[dict[str, Any]]:
    """Read one definition back through ``_enrich_definitions``, or ``None``.

    Never raises: a mutation that succeeded must still report the row it wrote
    even if the projection cannot be read, so callers degrade to the stored
    fields instead of turning a completed write into a failed command.
    """

    try:
        with _definition_read_store() as store:
            return read(store)
    except Exception:
        logger.debug("definition projection read-back failed", exc_info=True)
        return None


def _task_mutation_payload(task) -> dict:
    """The projected row a task mutation just wrote.

    Create / pause / resume / update answer with exactly what ``vibe task show``
    prints next, because they read the same enriched row rather than deriving
    anything themselves. That is the whole contract: the mutation response is the
    first — and for an agent, often the only — thing anyone sees, so a definition
    that is already failing has to say so there in the same words.

    ``None`` means there is no projection to read (the file-backed store keeps no
    ``agent_runs`` history), which degrades to the stored row.
    """

    projected = _read_definition_projection(lambda store: store.get_scheduled_task(task.id))
    if projected is None:
        return _task_payload(task)
    return _task_projection_payload(projected)


def _task_request_store() -> TaskExecutionStore:
    return TaskExecutionStore()


def _agent_store() -> VibeAgentStore:
    return VibeAgentStore()


def _ensure_cli_sqlite_state() -> None:
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config

    ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))


def _guard_cli_default_state_migration() -> None:
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()


def _primary_platform() -> str:
    try:
        return _ensure_config().platform
    except Exception:
        return "slack"


def _watch_store() -> ManagedWatchStore:
    return ManagedWatchStore()


def _watch_runtime_store() -> WatchRuntimeStateStore:
    return WatchRuntimeStateStore()


def _supported_task_platforms() -> set[str]:
    # ``avibe`` (the web workbench) is ALWAYS available as an in-process platform,
    # even though it's not in the configured IM platform list — so scheduled
    # tasks / watches can target a workbench session. Include it unconditionally.
    platforms = {"avibe"}
    try:
        config = _ensure_config()
    except Exception:
        return platforms
    enabled = getattr(config, "enabled_platforms", None)
    if callable(enabled):
        return platforms | set(enabled())
    platforms.add(getattr(config, "platform", "slack"))
    return platforms


def _is_completed_one_shot(task) -> bool:
    return (
        task.schedule_type == "at"
        and bool(task.retired_at)
        and bool(task.last_run_at)
        and not task.last_error
        and task.retirement_reason != TASK_RETIREMENT_SCHEDULE_MISSED
    )


def _is_failed_one_shot(task) -> bool:
    return (
        task.schedule_type == "at"
        and bool(task.retired_at)
        and (
            task.retirement_reason == TASK_RETIREMENT_SCHEDULE_MISSED
            or (bool(task.last_run_at) and bool(task.last_error))
        )
    )


def _parse_validated_session_key(
    session_key: str,
    *,
    help_command: str,
) -> object:
    try:
        parsed = parse_session_key(session_key)
    except ValueError as exc:
        raise TaskCliError(
            str(exc),
            code="invalid_session_key",
            hint="Use <platform>::<channel|user>::<id>[::thread::<thread_id>]. Prefer a threadless key unless the command must reply in one specific thread.",
            example="slack::channel::C123",
            help_command=help_command,
            details={"session_key": session_key},
        ) from exc

    supported_platforms = _supported_task_platforms()
    if parsed.platform not in supported_platforms:
        supported_text = ", ".join(sorted(supported_platforms)) or "none"
        raise TaskCliError(
            f"unsupported task platform: {parsed.platform}",
            code="unsupported_platform",
            hint="Choose a platform that is enabled in Avibe before sending the request.",
            example="slack::channel::C123",
            help_command=help_command,
            details={
                "requested_platform": parsed.platform,
                "configured_platforms": sorted(supported_platforms),
                "configured_platforms_text": supported_text,
            },
        )
    if parsed.platform == "avibe":
        # A bare avibe session KEY carries no agent_session_id, so a dispatched
        # reply can't attach to a workbench session (persist_agent_message can't
        # resolve a project scope) — target workbench sessions by --session-id.
        raise TaskCliError(
            "avibe workbench sessions must be targeted with --session-id, not --session-key",
            code="avibe_requires_session_id",
            hint="A workbench session key has no agent session id, so the reply wouldn't attach to the Chat. Pass the session id via --session-id.",
            example="--session-id ses3chKBjP5hy",
            help_command=help_command,
            details={"session_key": session_key},
        )
    return parsed


def _validate_session_id_target(
    session_id: str,
    *,
    help_command: str,
) -> object:
    try:
        resolved = resolve_session_id_target(session_id)
    except ValueError as exc:
        raise TaskCliError(
            str(exc),
            code="invalid_session_id",
            hint="Use a valid Agent Session ID. Inside an Avibe Agent shell, commands that continue this conversation can use the default target.",
            example="sesk8m4q2p7x",
            help_command=help_command,
            details={"session_id": session_id},
        ) from exc

    supported_platforms = _supported_task_platforms()
    if resolved.session_key.platform not in supported_platforms:
        supported_text = ", ".join(sorted(supported_platforms)) or "none"
        raise TaskCliError(
            f"unsupported task platform: {resolved.session_key.platform}",
            code="unsupported_platform",
            hint="Choose a session whose platform is enabled in Avibe before sending the request.",
            example="sesk8m4q2p7x",
            help_command=help_command,
            details={
                "requested_platform": resolved.session_key.platform,
                "configured_platforms": sorted(supported_platforms),
                "configured_platforms_text": supported_text,
            },
        )
    return resolved.session_key


def _resolve_session_target_args(
    args,
    *,
    required: bool,
    help_command: str,
) -> tuple[Optional[str], str]:
    session_id = (getattr(args, "session_id", None) or "").strip()
    session_key = (getattr(args, "session_key", None) or "").strip()
    if session_id and session_key:
        raise TaskCliError(
            "use either --session-id or --session-key, not both",
            code="conflicting_session_target",
            hint="Use --session-id for new commands.",
            help_command=help_command,
        )
    if required and not session_id and not session_key:
        raise TaskCliError(
            "one of --session-id or --session-key is required",
            code="missing_session_target",
            hint="Run from an Avibe Agent shell to continue this conversation by default, or pass --session-id for the target Session.",
            example="vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'",
            help_command=help_command,
        )
    return session_id or None, session_key


def _default_session_id_from_caller(caller_context) -> Optional[str]:
    if caller_context is None:
        return None
    session_id = (getattr(caller_context, "session_id", None) or "").strip()
    if not session_id:
        return None
    return session_id


def _apply_caller_session_default(args, caller_context, *, purpose: str) -> Optional[dict[str, str]]:
    if (getattr(args, "session_id", None) or "").strip():
        return None
    if (getattr(args, "session_key", None) or "").strip():
        return None
    if bool(getattr(args, "create_session", False)) or bool(getattr(args, "create_session_per_run", False)):
        return None
    if (getattr(args, "fork_session", None) or "").strip():
        return None
    default_session_id = _default_session_id_from_caller(caller_context)
    if not default_session_id:
        return None
    setattr(args, "session_id", default_session_id)
    return {
        "code": "session_defaulted_to_caller",
        "message": f"{purpose} defaulted to this Agent Session.",
        "session_id": default_session_id,
    }


def _resolve_show_session_id(args, *, help_command: str) -> tuple[str, Optional[dict[str, str]]]:
    caller_context = caller_context_from_env()
    notice = _apply_caller_session_default(args, caller_context, purpose="Show Page session")
    session_id = (getattr(args, "session_id", None) or "").strip()
    if not session_id:
        raise TaskCliError(
            "Show Page session id is required outside an Avibe Agent environment.",
            code="missing_session_target",
            hint="Run this command from an Avibe Agent shell, or pass --session-id for the target Show Page.",
            help_command=help_command,
        )
    return session_id, notice


def _resolve_caller_session_id(args, *, purpose: str, help_command: str) -> tuple[str, Optional[dict[str, str]]]:
    caller_context = caller_context_from_env()
    notice = _apply_caller_session_default(args, caller_context, purpose=purpose)
    session_id = (getattr(args, "session_id", None) or "").strip()
    if not session_id:
        raise TaskCliError(
            f"{purpose} id is required outside an Avibe Agent environment.",
            code="missing_session_target",
            hint="Run this command from an Avibe Agent shell, or pass the target Session ID positionally.",
            help_command=help_command,
        )
    return session_id, notice


def _require_caller_session_id(caller_context, *, purpose: str, help_command: str) -> str:
    session_id = _default_session_id_from_caller(caller_context)
    if session_id:
        return session_id
    raise TaskCliError(
        f"{purpose} requires an Avibe Agent caller Session.",
        code="missing_caller_session",
        hint="Run this command from an Avibe Agent shell, or pass an explicit Session ID.",
        help_command=help_command,
    )


def _default_run_id_from_caller(caller_context) -> Optional[str]:
    if caller_context is None:
        return None
    run_id = (getattr(caller_context, "run_id", None) or "").strip()
    if not run_id:
        return None
    return run_id


def _resolve_caller_run_id(args, *, purpose: str, help_command: str) -> tuple[str, Optional[dict[str, str]]]:
    run_id = (getattr(args, "run_id", None) or "").strip()
    if run_id:
        return run_id, None
    default_run_id = _default_run_id_from_caller(caller_context_from_env())
    if not default_run_id:
        raise TaskCliError(
            f"{purpose} id is required outside an Avibe Agent run environment.",
            code="missing_run_target",
            hint="Pass the run id explicitly, or run this command from an Avibe Agent shell where AVIBE_RUN_ID is injected.",
            help_command=help_command,
        )
    setattr(args, "run_id", default_run_id)
    return default_run_id, {
        "code": "run_defaulted_to_caller",
        "message": f"{purpose} defaulted to the caller Run from AVIBE_RUN_ID.",
        "run_id": default_run_id,
    }


def _parse_validated_scope_id(scope_id: str, *, help_command: str):
    try:
        target = parse_scope_id(scope_id)
    except ValueError as exc:
        raise TaskCliError(
            str(exc),
            code="invalid_scope_id",
            hint="Pass a Scope ID from the scopes table, for example avibe::project::proj_123.",
            example="avibe::project::proj_abc123",
            help_command=help_command,
            details={"scope_id": scope_id},
        ) from exc

    supported_platforms = _supported_task_platforms()
    if target.platform not in supported_platforms:
        supported_text = ", ".join(sorted(supported_platforms)) or "none"
        raise TaskCliError(
            f"unsupported scope platform: {target.platform}",
            code="unsupported_platform",
            hint="Choose a scope whose platform is enabled in Avibe before sending the request.",
            example="avibe::project::proj_abc123",
            help_command=help_command,
            details={
                "requested_platform": target.platform,
                "configured_platforms": sorted(supported_platforms),
                "configured_platforms_text": supported_text,
            },
        )
    return target


def _validate_existing_scope_id(scope_id: str, *, help_command: str):
    target = _parse_validated_scope_id(scope_id, help_command=help_command)
    _ensure_cli_sqlite_state()
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            found = conn.execute(
                select(scopes.c.id, scope_settings.c.enabled)
                .select_from(scopes.outerjoin(scope_settings, scope_settings.c.scope_id == scopes.c.id))
                .where(scopes.c.id == target.session_scope)
                .limit(1)
            ).mappings().first()
    finally:
        engine.dispose()
    if found is None:
        raise TaskCliError(
            f"scope id not found: {target.session_scope}",
            code="scope_not_found",
            hint="Pass an existing Scope ID, or use --same-scope from an Avibe Agent Session.",
            help_command=help_command,
            details={"scope_id": target.session_scope},
        )
    if target.platform == "avibe" and target.scope_type == "project" and found["enabled"] is not None and not bool(found["enabled"]):
        raise TaskCliError(
            f"scope id is archived: {target.session_scope}",
            code="scope_archived",
            hint="Choose an active Workbench project scope.",
            help_command=help_command,
            details={"scope_id": target.session_scope},
        )
    return target


def _scope_id_from_session_id(session_id: str, *, help_command: str) -> Optional[str]:
    resolved = resolve_session_id_target(session_id)
    return resolved.scope_id


def _require_scope_id_from_session_id(session_id: str, *, help_command: str) -> str:
    scope_id = _scope_id_from_session_id(session_id, help_command=help_command)
    if scope_id is None:
        raise TaskCliError(
            f"session has no scope: {session_id}",
            code="standalone_session_has_no_scope",
            hint="Use --scope-id to choose a scope, or omit --same-scope to create another standalone Session.",
            help_command=help_command,
            details={"session_id": session_id},
        )
    return scope_id


def _legacy_scope_key_from_target(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return parse_scope_id(value).session_scope
    except ValueError:
        try:
            return parse_session_key(value).session_scope
        except ValueError:
            return ""


def _resolve_agent_run_scope_key(args, *, caller_context, source_session_id: Optional[str]) -> Optional[str]:
    raw_scope_id = (getattr(args, "scope_id", None) or "").strip()
    if raw_scope_id:
        return _validate_existing_scope_id(raw_scope_id, help_command="vibe agent run --help").session_scope
    if bool(getattr(args, "same_scope", False)):
        if source_session_id:
            return _require_scope_id_from_session_id(source_session_id, help_command="vibe agent run --help")
        caller_session_id = _require_caller_session_id(
            caller_context,
            purpose="--same-scope",
            help_command="vibe agent run --help",
        )
        return _require_scope_id_from_session_id(caller_session_id, help_command="vibe agent run --help")
    if source_session_id:
        # Leave placement implicit so reserve_forked_session inherits the
        # source Session's scope (including standalone) and preserves its
        # anchor semantics. Most importantly, do not fall through to a caller
        # in another project. --scope-id remains the opt-in move.
        return None
    if caller_context is not None:
        try:
            return _scope_id_from_session_id(
                caller_context.session_id,
                help_command="vibe agent run --help",
            )
        except ValueError:
            # A stale/injected caller id that is not present in this state DB is
            # not a usable placement context; fall back to standalone creation.
            return None
    return None


def _resolve_definition_scope_key(args, *, caller_context, help_command: str) -> Optional[str]:
    raw_scope_id = (getattr(args, "scope_id", None) or "").strip()
    same_scope = bool(getattr(args, "same_scope", False))
    legacy_deliver_key = (getattr(args, "deliver_key", None) or "").strip()
    if raw_scope_id and same_scope:
        raise TaskCliError(
            "use either --same-scope or --scope-id, not both",
            code="conflicting_scope_target",
            hint="Use --same-scope to reuse the caller scope, or --scope-id to place the new Session explicitly.",
            help_command=help_command,
        )
    if legacy_deliver_key and (raw_scope_id or same_scope):
        raise TaskCliError(
            "use either the legacy delivery target or the new scope placement flags, not both",
            code="conflicting_scope_target",
            hint="Use --scope-id or --same-scope for new Agent-facing commands.",
            help_command=help_command,
        )
    if raw_scope_id:
        return _validate_existing_scope_id(raw_scope_id, help_command=help_command).session_scope
    if same_scope:
        caller_session_id = _require_caller_session_id(
            caller_context,
            purpose="--same-scope",
            help_command=help_command,
        )
        return _require_scope_id_from_session_id(caller_session_id, help_command=help_command)
    if legacy_deliver_key:
        return _parse_validated_session_key(legacy_deliver_key, help_command=help_command).session_scope
    return None


def _definition_metadata_with_scope(
    caller_context,
    *,
    scope_id: Optional[str],
    session_workdir: Optional[str] = None,
) -> dict:
    metadata = _definition_creation_metadata_from_caller(caller_context)
    if scope_id:
        metadata["session_scope_id"] = scope_id
    if session_workdir:
        metadata["session_workdir"] = session_workdir
    return metadata


def _scope_id_payload_from_session(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    try:
        return resolve_session_id_target(session_id).scope_id
    except ValueError:
        return None


def _validate_callback_session_id(session_id: str, *, help_command: str) -> None:
    try:
        resolve_session_id_target(session_id)
    except ValueError as exc:
        raise TaskCliError(
            str(exc),
            code="invalid_session_id",
            hint="Pass an existing Agent Session ID as the callback target.",
            help_command=help_command,
            details={"session_id": session_id},
        ) from exc


def _resolve_runs_list_session_filter(args) -> Optional[str]:
    explicit_session_id = (getattr(args, "session_id", None) or "").strip()
    current_session = bool(getattr(args, "current_session", False))
    if explicit_session_id and current_session:
        raise TaskCliError(
            "use either --session-id or --current-session, not both",
            code="conflicting_session_filter",
            hint="Use --current-session to resolve AVIBE_SESSION_ID, or pass a specific --session-id.",
            help_command="vibe runs list --help",
        )
    if explicit_session_id:
        return explicit_session_id
    if current_session:
        return _require_caller_session_id(
            caller_context_from_env(),
            purpose="--current-session",
            help_command="vibe runs list --help",
        )
    return None


def _validate_delivery_args(
    *,
    session_key: str,
    session_id: Optional[str] = None,
    post_to: Optional[str],
    deliver_key: Optional[str],
    help_command: str,
):
    if post_to and deliver_key:
        raise TaskCliError(
            "use only one delivery override",
            code="conflicting_delivery_target",
            hint="Prefer --scope-id or --same-scope for new Agent-facing commands.",
            help_command=help_command,
        )

    if session_id:
        session_target = _validate_session_id_target(session_id, help_command=help_command)
    else:
        session_target = _parse_validated_session_key(session_key, help_command=help_command)
    delivery_target = None
    if deliver_key:
        delivery_target = _parse_validated_session_key(deliver_key, help_command=help_command)
        if delivery_target.platform != session_target.platform:
            raise TaskCliError(
                "legacy delivery target must use the same platform as the session target",
                code="invalid_delivery_target",
                hint="Keep session memory and delivery on the same IM platform. Change only the channel, user, or thread target.",
                help_command=help_command,
                details={
                    "session_platform": session_target.platform,
                    "delivery_platform": delivery_target.platform,
                },
            )
    elif post_to == "thread" and not session_target.thread_id:
        raise TaskCliError(
            "thread delivery override requires a thread-bound session target or explicit delivery target",
            code="invalid_delivery_target",
            hint="Use a thread-bound Agent Session ID, or keep delivery following the Session target.",
            help_command=help_command,
            details={"session_id": session_id, "session_key": session_key, "post_to": post_to},
        )
    return session_target, delivery_target


def _validate_delivery_override_for_target(
    session_target,
    *,
    post_to: Optional[str],
    deliver_key: Optional[str],
    help_command: str,
):
    delivery_target = None
    if deliver_key:
        delivery_target = _parse_validated_session_key(deliver_key, help_command=help_command)
        if delivery_target.platform != session_target.platform:
            raise TaskCliError(
                "legacy delivery target must use the same platform as the session target",
                code="invalid_delivery_target",
                hint="Keep session memory and delivery on the same IM platform. Change only the channel, user, or thread target.",
                help_command=help_command,
                details={
                    "session_platform": session_target.platform,
                    "delivery_platform": delivery_target.platform,
                },
            )
    elif post_to == "thread" and not session_target.thread_id:
        raise TaskCliError(
            "thread delivery override requires a thread-bound session target",
            code="invalid_delivery_target",
            hint="Use a thread-bound Agent Session ID, or keep delivery following the created Session target.",
            help_command=help_command,
            details={"post_to": post_to},
        )
    return session_target, delivery_target


def _collect_target_warnings(*targets) -> list[dict]:
    from core.services import settings as settings_service

    lark_targets = [target for target in targets if target is not None and target.platform == "lark" and target.is_dm]
    if not lark_targets:
        return []
    store = settings_service.get_settings_store()
    warnings: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for target in lark_targets:
        dedupe_key = (target.platform, target.scope_type, target.scope_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        bound_user = store.get_user(target.scope_id, platform="lark")
        if bound_user is None:
            warnings.append(
                {
                    "code": "lark_user_not_bound",
                    "message": "The target Lark user is not bound in Avibe yet; delivery may fail at runtime.",
                    "details": {"session_key": target.to_key(include_thread=False)},
                }
            )
        elif not getattr(bound_user, "dm_chat_id", ""):
            warnings.append(
                {
                    "code": "lark_dm_chat_unbound",
                    "message": "The target Lark user has no dm_chat_id binding yet; delivery may fail at runtime.",
                    "details": {"session_key": target.to_key(include_thread=False)},
                }
            )

    return warnings


def _validate_agent_name_arg(agent_name: Optional[str]) -> Optional[str]:
    value = (agent_name or "").strip()
    if not value:
        return None
    _agent_store().require_enabled(value)
    return value


class _ScopeRoutingTarget(NamedTuple):
    agent_name: Optional[str]
    agent_id: Optional[str]


class _AgentTargetResolution(NamedTuple):
    agent: Optional[VibeAgent]
    requires_enabled_write_guard: bool
    preserves_existing_reference: bool = False


def _agent_write_guard_ids(
    resolution: _AgentTargetResolution,
) -> tuple[Optional[str], Optional[str]]:
    agent = resolution.agent
    if agent is None:
        return None, None
    if resolution.requires_enabled_write_guard:
        return agent.id, None
    if getattr(resolution, "preserves_existing_reference", False):
        return None, agent.id
    return None, None


def _resolve_scope_routing_target(session_key: str) -> _ScopeRoutingTarget:
    if not session_key:
        return _ScopeRoutingTarget(None, None)
    try:
        parsed = parse_scope_id(session_key)
    except ValueError:
        try:
            parsed = parse_session_key(session_key)
        except ValueError:
            return _ScopeRoutingTarget(None, None)
    scope_id = make_scope_id(parsed.platform, parsed.scope_type, parsed.scope_id)
    _ensure_cli_sqlite_state()
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(scope_settings.c.agent_name, agents.c.id.label("agent_id"))
                .select_from(
                    scope_settings.outerjoin(agents, agents.c.name == scope_settings.c.agent_name)
                )
                .where(scope_settings.c.scope_id == scope_id)
                .limit(1)
            ).first()
            if row is None:
                return _ScopeRoutingTarget(None, None)
            agent_name = str(row.agent_name).strip() if row.agent_name else None
            agent_id = str(row.agent_id).strip() if row.agent_id else None
            return _ScopeRoutingTarget(agent_name, agent_id)
    finally:
        engine.dispose()


def _resolve_scope_agent_name(session_key: str) -> Optional[str]:
    return _resolve_scope_routing_target(session_key).agent_name


def _resolve_agent_target(
    *,
    agent_name: Optional[str],
    session_id: Optional[str],
    session_key: str,
    help_command: str,
    existing_agent_reference: bool = False,
) -> _AgentTargetResolution:
    store = _agent_store()
    try:
        requested = None
        if agent_name:
            requested = (
                store.require_reference(agent_name)
                if existing_agent_reference
                else store.require_enabled(agent_name)
            )
        if session_id:
            target = resolve_session_id_target(session_id)
            session_agent = (
                store.require_reference_by_id(target.agent_id)
                if target.agent_id
                else store.require_reference(target.agent_name)
                if target.agent_name
                else None
            )
            if requested is not None and session_agent is not None and requested.name != session_agent.name:
                raise TaskCliError(
                    "agent does not match the existing session agent",
                    code="agent_session_agent_mismatch",
                    hint="Omit --agent when continuing an existing Session, or pass the same Agent name already bound to that Session.",
                    details={
                        "agent": requested.name,
                        "session_id": session_id,
                        "session_agent": session_agent.name,
                    },
                    help_command=help_command,
                )
            if requested is not None and target.agent_backend and requested.backend != target.agent_backend:
                raise TaskCliError(
                    "agent backend does not match the existing session backend",
                    code="agent_session_backend_mismatch",
                    hint="Use an Agent with the same backend as the Session, or create a new Session.",
                    details={
                        "agent": requested.name,
                        "agent_backend": requested.backend,
                        "session_id": session_id,
                        "session_backend": target.agent_backend,
                    },
                    help_command=help_command,
                )
            return _AgentTargetResolution(
                session_agent or requested,
                requested is not None and not existing_agent_reference,
                requested is None or existing_agent_reference,
            )

        if requested is not None:
            return _AgentTargetResolution(
                requested,
                not existing_agent_reference,
                existing_agent_reference,
            )

        if session_key:
            scope_target = _resolve_scope_routing_target(session_key)
            if scope_target.agent_name:
                return _AgentTargetResolution(
                    (
                        store.require_reference_by_id(scope_target.agent_id)
                        if scope_target.agent_id
                        else store.require_reference(scope_target.agent_name)
                    ),
                    False,
                    True,
                )

        default_agent = store.get_default_agent()
        return _AgentTargetResolution(default_agent, default_agent is not None)
    finally:
        store.close()


def _resolve_agent_for_target(
    *,
    agent_name: Optional[str],
    session_id: Optional[str],
    session_key: str,
    help_command: str,
    existing_agent_reference: bool = False,
):
    return _resolve_agent_target(
        agent_name=agent_name,
        session_id=session_id,
        session_key=session_key,
        help_command=help_command,
        existing_agent_reference=existing_agent_reference,
    ).agent


def _resolve_agent_for_session_reservation(
    *,
    agent_name: Optional[str],
    agent_id: Optional[str] = None,
    deliver_key: str,
    help_command: str,
) -> Optional[VibeAgent]:
    resolved_agent_name = agent_name
    scope_target = _ScopeRoutingTarget(None, None)
    if not resolved_agent_name:
        scope_target = _resolve_scope_routing_target(deliver_key)
        resolved_agent_name = scope_target.agent_name

    store = _agent_store()
    try:
        if agent_id:
            return store.require_reference_by_id(agent_id)
        if scope_target.agent_id:
            return store.require_reference_by_id(scope_target.agent_id)
        if resolved_agent_name:
            return store.require_reference(resolved_agent_name)
        return store.get_default_agent()
    finally:
        store.close()


def _resolve_watch_command(args, *, help_command: str) -> tuple[list[str], Optional[str]]:
    shell_command = (getattr(args, "shell", None) or "").strip()
    raw_command = list(getattr(args, "waiter_command", []) or [])
    if raw_command and raw_command[0] == "--":
        raw_command = raw_command[1:]

    if shell_command and raw_command:
        raise TaskCliError(
            "use either --shell or a command after '--', not both",
            code="conflicting_watch_command_inputs",
            hint="Pass a shell string with --shell, or pass the executable and its args after '--'.",
            help_command=help_command,
        )
    if shell_command:
        return [], shell_command
    if raw_command:
        return raw_command, None
    raise TaskCliError(
        "one of --shell or a command after '--' is required",
        code="missing_watch_command",
        hint="Pass a shell command with --shell, or add the watcher executable and its args after '--'.",
        help_command=help_command,
    )


def _resolve_task_command(args, *, help_command: str) -> tuple[list[str], Optional[str], bool]:
    """Command inputs for a scheduled task, or ``([], None, False)`` for a message task.

    Same two input shapes as ``_resolve_watch_command`` — ``--shell`` XOR a trailing
    ``-- argv`` — with one deliberate difference: neither being present is NOT an
    error here, because a task may instead carry a stored message for an Agent.
    """

    raw_shell = getattr(args, "shell", None)
    shell_command = (raw_shell or "").strip()
    raw_command = list(getattr(args, "command_argv", None) or [])
    argv_present = bool(raw_command)
    if raw_command and raw_command[0] == "--":
        raw_command = raw_command[1:]

    if shell_command and raw_command:
        raise TaskCliError(
            "use either --shell or a command after '--', not both",
            code="conflicting_task_command_inputs",
            hint="Pass a shell string with --shell, or pass the executable and its args after '--'.",
            help_command=help_command,
        )
    if raw_shell is not None and not shell_command:
        raise TaskCliError(
            "--shell cannot be empty",
            code="empty_task_command",
            hint="Pass the shell command to run on schedule, for example --shell './scripts/sync.sh'.",
            help_command=help_command,
        )
    if argv_present and not raw_command:
        raise TaskCliError(
            "a command is required after '--'",
            code="empty_task_command",
            hint="Add the executable and its args after '--', or use --shell for a shell command string.",
            help_command=help_command,
        )
    if shell_command:
        return [], shell_command, True
    if raw_command:
        return raw_command, None, True
    return [], None, False


def _watch_command_preview(watch) -> str:
    return command_line_preview(watch.shell_command, watch.command)


def _watch_display_name(watch) -> str:
    return watch.name or _watch_command_preview(watch)


def _watch_state(watch, runtime_entry: Optional[dict[str, object]]) -> str:
    if runtime_entry and runtime_entry.get("running"):
        return "running"
    if watch.enabled and watch.mode == "forever":
        return "armed"
    if watch.enabled:
        return "pending"
    if watch.last_error:
        return "failed"
    if watch.last_event_at:
        return "completed"
    return "paused"


def _watch_payload(watch, runtime_entry: Optional[dict[str, object]], *, brief: bool = False) -> dict:
    derived = {
        "display_name": _watch_display_name(watch),
        "command_preview": _watch_command_preview(watch),
        "state": _watch_state(watch, runtime_entry),
        "runtime": runtime_entry or {},
    }
    if brief:
        return {
            "id": watch.id,
            "name": watch.name,
            "display_name": derived["display_name"],
            "state": derived["state"],
            "mode": watch.mode,
            "session_id": watch.session_id,
            "session_key": watch.session_key,
            "agent_name": watch.agent_name,
            "message_preview": _task_message_preview(getattr(watch, "message", None) or watch.prefix or ""),
            "timeout_seconds": watch.timeout_seconds,
            "lifetime_timeout_seconds": watch.lifetime_timeout_seconds,
            "enabled": watch.enabled,
            "last_event_at": watch.last_event_at,
            "last_error": watch.last_error,
        }
    payload = watch.to_dict()
    payload.update(derived)
    return payload


def _watch_projection_state(watch: Mapping[str, object]) -> str:
    """Compatibility ``state`` derived from canonical lifecycle and liveness."""

    lifecycle_state = watch.get("lifecycle_state")
    if lifecycle_state == "running":
        return "running"
    if lifecycle_state == "waiting":
        if watch.get("process_alive") is True:
            return "running"
        return "armed" if watch.get("mode") == "forever" else "pending"
    if lifecycle_state == "finished":
        return "failed" if watch.get("lifecycle_detail") in {"timeout", "error"} else "completed"
    if lifecycle_state == "paused":
        return "paused"
    return "unknown"


def _watch_projection_payload(watch: Mapping[str, object], *, brief: bool = False) -> dict:
    shell_command = str(watch.get("shell_command") or "")
    command = watch.get("command")
    command_values = [str(value) for value in command] if isinstance(command, list) else []
    command_preview = shell_command or shlex.join(command_values)
    name = watch.get("name")
    derived = {
        "display_name": str(name) if name else _task_message_preview(command_preview, max_chars=120),
        "command_preview": _task_message_preview(command_preview, max_chars=120),
        "state": _watch_projection_state(watch),
    }
    if brief:
        payload = {
            "id": watch.get("id"),
            "name": name,
            "display_name": derived["display_name"],
            "state": derived["state"],
            "mode": watch.get("mode"),
            "session_id": watch.get("session_id"),
            "session_key": watch.get("session_key"),
            "agent_name": watch.get("agent_name"),
            "message_preview": _task_message_preview(
                str(watch.get("message") or watch.get("prefix") or "")
            ),
            "timeout_seconds": watch.get("timeout_seconds"),
            "lifetime_timeout_seconds": watch.get("lifetime_timeout_seconds"),
            "enabled": watch.get("enabled"),
            "last_event_at": watch.get("last_event_at"),
            "last_error": watch.get("last_error"),
            "process_alive": watch.get("process_alive"),
        }
        payload.update({field: watch.get(field) for field in _CANONICAL_DEFINITION_FIELDS})
        # Watches get health on the same terms as tasks: ``_enrich_definitions``
        # computes it for both, and a watch whose hook fails nightly is exactly as
        # invisible as a task that does.
        payload.update({field: watch.get(field) for field in _DEFINITION_FAILURE_FIELDS})
        return payload
    payload = dict(watch)
    payload.update(derived)
    return payload


def _watch_mutation_payload(watch, runtime_entry: Optional[dict[str, object]]) -> dict:
    """The projected row a watch mutation just wrote.

    The twin of ``_task_mutation_payload``, and the reason watches needed one:
    they never had a health lookup of their own, so every create / pause /
    resume / update answered with no health at all while the same watch showed
    ``failing`` in ``vibe watch list``. Reading the projection back gives both
    definition types the one answer instead of one answer each.

    ``runtime_entry`` remains the fallback's liveness source; the projected row
    carries its own ``runtime`` and ``process_alive`` from the same heartbeat
    rows the Workbench reads.
    """

    projected = _read_definition_projection(lambda store: store.get_watch(watch.id))
    if projected is None:
        return _watch_payload(watch, runtime_entry)
    return _watch_projection_payload(projected)


def _agent_payload(agent, *, brief: bool = False) -> dict:
    payload = agent.to_dict()
    if brief:
        return {
            "id": payload["id"],
            "name": payload["name"],
            "display_name": payload["display_name"],
            "backend": payload["backend"],
            "model": payload["model"],
            "reasoning_effort": payload["reasoning_effort"],
            "enabled": payload["enabled"],
            "archived": payload["archived"],
            "archived_at": payload["archived_at"],
            "source": payload["source"],
            "updated_at": payload["updated_at"],
        }
    return payload


def _run_payload(run: dict, *, brief: bool = False) -> dict:
    normalized = dict(run)
    normalized["status"] = normalize_run_status(normalized.get("status"))
    activity_at = normalized.get("last_activity_at") or normalized.get("started_at")
    activity_basis = (
        "output"
        if normalized.get("last_activity_at")
        else ("start" if activity_at else None)
    )
    if brief:
        return {
            "id": normalized.get("id"),
            "run_type": normalized.get("run_type") or normalized.get("request_type"),
            "status": normalized.get("status"),
            "agent_name": normalized.get("agent_name"),
            "session_id": normalized.get("session_id"),
            "definition_id": normalized.get("definition_id") or normalized.get("task_id"),
            "created_at": normalized.get("created_at"),
            "started_at": normalized.get("started_at"),
            "last_activity_at": activity_at,
            "activity_basis": activity_basis,
            "activity_age_seconds": _seconds_since_iso(activity_at),
            "completed_at": normalized.get("completed_at"),
            "error": normalized.get("error"),
            "callback_session_id": normalized.get("callback_session_id"),
            "callback_status": normalized.get("callback_status"),
            "callback_run_id": normalized.get("callback_run_id"),
        }
    return normalized


def cmd_harness_status(_args) -> int:
    """Print one bounded operational snapshot across Harness work types."""

    from core.services.harness_status import build_harness_status
    from vibe import internal_client

    try:
        language = _configured_cli_language()
        request_store = _task_request_store()
        sqlite_store = request_store.sqlite_backend
        if sqlite_store is None:
            raise RuntimeError(i18n_t("harness.cli.error.sqliteRequired", language))
        fetch_limit = MAX_PAGE_LIMIT + 1
        raw_runs = sqlite_store.list_active_runs(limit=fetch_limit)
        raw_watches = sqlite_store.list_enabled_definitions(
            "watch",
            limit=fetch_limit,
        )
        raw_tasks = sqlite_store.list_enabled_definitions(
            "scheduled",
            limit=fetch_limit,
        )
        runs_truncated = len(raw_runs) > MAX_PAGE_LIMIT

        try:
            response = asyncio.run(
                internal_client.list_running_agents(
                    run_ids=[str(row.get("id")) for row in raw_runs if row.get("id")]
                )
            )
            body = response.get("body") if isinstance(response, dict) else None
            runtime_snapshot = dict(body) if isinstance(body, dict) else {}
            status_code = response.get("status_code") if isinstance(response, dict) else None
            runtime_snapshot["available"] = status_code == 200 and bool(
                runtime_snapshot.get("ok")
            )
            if not runtime_snapshot["available"]:
                runtime_snapshot["error"] = i18n_t(
                    "harness.cli.error.controllerStatus",
                    language,
                    status=status_code,
                )
        except internal_client.InternalServerTimeout:
            runtime_snapshot = {
                "available": False,
                "error": i18n_t("harness.cli.error.controllerTimeout", language),
            }
        except internal_client.InternalServerUnavailable:
            runtime_snapshot = {
                "available": False,
                "error": i18n_t("harness.cli.error.controllerUnavailable", language),
            }

        # Ownership is a point-in-time controller fact. Keep only Runs that were
        # active on both sides of that snapshot so a Run completing during the
        # request cannot be mislabeled as owner-missing.
        active_run_ids_after = sqlite_store.active_run_ids(
            row.get("id") for row in raw_runs
        )
        raw_runs = [
            row for row in raw_runs if str(row.get("id")) in active_run_ids_after
        ]

        snapshot = build_harness_status(
            runs=raw_runs[:MAX_PAGE_LIMIT],
            watches=raw_watches[:MAX_PAGE_LIMIT],
            tasks=raw_tasks[:MAX_PAGE_LIMIT],
            runtime_snapshot=runtime_snapshot,
            truncated={
                "runs": runs_truncated,
                "watches": len(raw_watches) > MAX_PAGE_LIMIT,
                "tasks": len(raw_tasks) > MAX_PAGE_LIMIT,
            },
        )
        _print_cli_payload("harness_status", **snapshot)
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe harness status --help")
        return 1


def _seconds_since_iso(timestamp: object) -> float | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    text = timestamp.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        started_at = datetime.fromisoformat(text)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())


def _watch_recovery_entry_count(runtime_store: WatchRuntimeStateStore) -> int:
    try:
        runtime_state = runtime_store.load_for_recovery()
    except Exception:
        return 1
    runtime_watches = runtime_state.get("watches") if isinstance(runtime_state, dict) else None
    if not isinstance(runtime_watches, dict):
        return 1
    return sum(
        1
        for entry in runtime_watches.values()
        if isinstance(entry, dict)
        and entry.get("running") is True
        and isinstance(entry.get("pid"), int)
        and not isinstance(entry.get("pid"), bool)
        and entry["pid"] > 0
    )


def _default_watch_startup_timeout_seconds(
    *,
    stable_running_seconds: float = WATCH_STARTUP_STABLE_RUNNING_SECONDS,
    recovery_entry_count: int = 0,
) -> float:
    recovery_budget = max(0, recovery_entry_count) * WATCH_RECOVERY_ENTRY_TIMEOUT_SECONDS
    return (
        WATCH_RECONCILE_INTERVAL_SECONDS
        + recovery_budget
        + stable_running_seconds
        + WATCH_STARTUP_JITTER_BUFFER_SECONDS
    )


def _wait_for_watch_startup(
    store: ManagedWatchStore,
    runtime_store: WatchRuntimeStateStore,
    watch_id: str,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = 0.1,
    stable_running_seconds: float = WATCH_STARTUP_STABLE_RUNNING_SECONDS,
):
    inspect_command = f"vibe watch show {watch_id}"
    if timeout_seconds is None:
        timeout_seconds = _default_watch_startup_timeout_seconds(
            stable_running_seconds=stable_running_seconds,
            recovery_entry_count=_watch_recovery_entry_count(runtime_store),
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        store.maybe_reload()
        watch = store.get_watch(watch_id)
        if watch is None:
            raise TaskCliError(
                f"watch '{watch_id}' could not be verified because it disappeared during startup",
                code="watch_startup_failed",
                hint="Recreate the watch, then inspect its first-cycle state before reporting that monitoring is active.",
                example=inspect_command,
                help_command=inspect_command,
                details={"watch_id": watch_id},
            )
        runtime_entry = runtime_store.load().get("watches", {}).get(watch_id)
        if watch.last_error and not watch.enabled:
            raise TaskCliError(
                f"watch '{watch.name or watch.id}' failed during startup and has already been disabled",
                code="watch_startup_failed",
                hint="Inspect the stored watch error, fix the waiter or its dependencies, then recreate the watch if monitoring should continue.",
                example=inspect_command,
                help_command=inspect_command,
                details={"watch": _watch_mutation_payload(watch, runtime_entry)},
            )
        # NO_EVENT_EXIT_CODE is a clean finish too: a once watch whose first cycle
        # already decided there is nothing to report has completed, not hung.
        if (
            watch.mode == "once"
            and watch.last_finished_at
            and not watch.last_error
            and watch.last_exit_code in (0, NO_EVENT_EXIT_CODE)
        ):
            return watch, runtime_entry
        if runtime_entry and runtime_entry.get("running"):
            stable_for = _seconds_since_iso(runtime_entry.get("started_at")) or _seconds_since_iso(watch.last_started_at)
            if stable_for is not None and stable_for >= stable_running_seconds:
                return watch, runtime_entry
        time.sleep(poll_interval_seconds)

    store.maybe_reload()
    watch = store.get_watch(watch_id)
    runtime_entry = runtime_store.load().get("watches", {}).get(watch_id)
    if watch is not None and watch.last_error and not watch.enabled:
        raise TaskCliError(
            f"watch '{watch.name or watch.id}' failed during startup and has already been disabled",
            code="watch_startup_failed",
            hint="Inspect the stored watch error, fix the waiter or its dependencies, then recreate the watch if monitoring should continue.",
            example=inspect_command,
            help_command=inspect_command,
            details={"watch": _watch_mutation_payload(watch, runtime_entry)},
        )
    raise TaskCliError(
        f"watch '{watch_id}' was created but startup was not confirmed within {timeout_seconds:.0f} second(s)",
        code="watch_startup_unconfirmed",
        hint="Confirm that the Avibe service is running, then inspect the watch state before reporting that monitoring is active.",
        example=inspect_command,
        help_command=inspect_command,
        details={"watch": _watch_mutation_payload(watch, runtime_entry) if watch is not None else {"id": watch_id}},
    )


#: Flags that only mean something for a definition that talks to an Agent Session.
#: Attribute name -> the flag as the user typed it, for the refusal message.
_TASK_SESSION_FLAG_ARGS: tuple[tuple[str, str], ...] = (
    ("session_id", "--session-id"),
    ("session_key", "--session-key"),
    ("create_session", "--create-session"),
    ("create_session_per_run", "--create-session-per-run"),
    ("same_scope", "--same-scope"),
    ("scope_id", "--scope-id"),
    ("agent", "--agent"),
    ("post_to", "--post-to"),
    ("deliver_key", "--deliver-key"),
)

_TASK_MESSAGE_FLAG_ARGS: tuple[tuple[str, str], ...] = (
    ("message", "--message"),
    ("message_file", "--message-file"),
    ("prompt", "--prompt"),
    ("prompt_file", "--prompt-file"),
)


def _explicit_flag_names(args, candidates: tuple[tuple[str, str], ...]) -> list[str]:
    """Which of ``candidates`` the user actually passed, in declaration order."""

    present: list[str] = []
    for attribute, flag in candidates:
        value = getattr(args, attribute, None)
        if isinstance(value, bool):
            if value:
                present.append(flag)
        elif value is not None and str(value).strip():
            present.append(flag)
    return present


def _reject_session_flags_for_command_task(args, *, help_command: str) -> None:
    """A pure command task has no Session, so Session-shaped flags are inert config."""

    offenders = _explicit_flag_names(args, _TASK_SESSION_FLAG_ARGS)
    if not offenders:
        return
    raise TaskCliError(
        "a pure command task has no Agent session; use --on-failure agent to attach one",
        code="session_flags_with_command_task",
        hint=(
            "Drop "
            + ", ".join(offenders)
            + ", or add --on-failure agent so a failed run has an Agent Session to report into."
        ),
        example="vibe task add --cron '0 3 * * *' --shell './scripts/sync.sh'",
        help_command=help_command,
        details={"flags": offenders},
    )


def _validate_task_timeout(timeout: Optional[float], *, help_command: str) -> None:
    # ``isfinite`` and not just ``>= 0``: ``float("inf") >= 0`` is True, so ``--timeout
    # inf`` used to be stored and then waited on forever, while every reader of the
    # definition -- JSON output, the Workbench -- rejects a non-finite number and shows
    # the default instead. The documented spelling for "no timeout" is 0.
    if timeout is None or (math.isfinite(timeout) and timeout >= 0):
        return
    raise TaskCliError(
        "--timeout must be a finite number of seconds >= 0",
        code="invalid_timeout",
        hint="Use 0 for no timeout, or a positive number of seconds.",
        help_command=help_command,
        details={"timeout": timeout},
    )


def _validate_task_action_matrix(
    args,
    *,
    has_command: bool,
    requested_on_failure: Optional[str],
    effective_on_failure: str,
    help_command: str,
) -> None:
    """Which combinations of message, command and failure policy are creatable.

    One place rather than scattered guards, because the interesting failures are all
    CROSS-flag: a policy or a timeout that nothing would consume, a message no Agent
    would ever read, and Session flags on a definition that has no Session at all.
    """

    timeout = getattr(args, "timeout", None)
    if not has_command:
        if requested_on_failure is not None:
            raise TaskCliError(
                "--on-failure only applies to command tasks",
                code="on_failure_requires_command",
                hint="Add --shell or a command after '--', or drop --on-failure.",
                example="vibe task add --cron '0 3 * * *' --shell './scripts/sync.sh' --on-failure agent --message 'Diagnose the failure.'",
                help_command=help_command,
            )
        if timeout is not None:
            raise TaskCliError(
                "--timeout only applies to command tasks",
                code="timeout_requires_command",
                hint="Add --shell or a command after '--', or drop --timeout.",
                help_command=help_command,
            )
    _validate_task_timeout(timeout, help_command=help_command)
    message_flags = _explicit_flag_names(args, _TASK_MESSAGE_FLAG_ARGS)
    if has_command and message_flags and effective_on_failure == "none":
        raise TaskCliError(
            "a stored message needs an Agent to read it",
            code="message_without_consumer",
            hint="Add --on-failure agent so the message is sent when a run fails, or drop the message.",
            example="vibe task add --cron '0 3 * * *' --shell './scripts/sync.sh' --on-failure agent --message 'The nightly sync failed. Diagnose it.'",
            help_command=help_command,
            details={"flags": message_flags},
        )
    if not has_command and not message_flags:
        raise TaskCliError(
            "one of --message, --message-file, --shell, or a command after '--' is required",
            code="missing_task_action",
            hint=(
                "Use --message for a scheduled Agent turn, or --shell for a scheduled command "
                "that runs with no Agent involved."
            ),
            example="vibe task add --cron '0 3 * * *' --shell './scripts/sync.sh'",
            help_command=help_command,
        )


def cmd_task_add(args):
    reserved_session_id: Optional[str] = None
    try:
        caller_context = caller_context_from_env()
        caller_user_context = caller_resource_user_context(caller_context)
        command, shell_command, has_command = _resolve_task_command(
            args,
            help_command="vibe task add --help",
        )
        requested_on_failure = getattr(args, "on_failure", None)
        effective_on_failure = requested_on_failure or "none"
        # A command task that escalates keeps the whole Session/Agent flow below,
        # because its failure notice runs a real Agent turn. A PURE command task has
        # no Session at all, so every Session-shaped input is refused instead of
        # silently stored.
        is_pure_command = has_command and effective_on_failure == "none"
        _validate_task_action_matrix(
            args,
            has_command=has_command,
            requested_on_failure=requested_on_failure,
            effective_on_failure=effective_on_failure,
            help_command="vibe task add --help",
        )
        schedule_type = "cron" if args.cron else "at"
        if is_pure_command:
            _reject_session_flags_for_command_task(args, help_command="vibe task add --help")
            # Deliberately BEFORE any caller default is applied: inside an Avibe Agent
            # shell ``AVIBE_SESSION_ID`` would otherwise bind this task to the calling
            # conversation, and a pure cron command could not be created from chat at all.
            session_default_notice = None
            session_policy = None
            session_id = None
            session_key = ""
            agent_name = None
            expected_enabled_agent_id = None
            expected_reference_agent_id = None
            scope_key = None
            message = ""
            session_target = None
            delivery_target = None
            raw_cwd = (getattr(args, "cwd", None) or "").strip()
            cwd = (
                _resolve_existing_cwd(
                    raw_cwd,
                    help_command="vibe task add --help",
                    code="cwd_not_found",
                    label="task",
                )
                if raw_cwd
                else os.getcwd()
            )
            session_workdir = None
        else:
            session_default_notice = _apply_caller_session_default(
                args,
                caller_context,
                purpose="Task target Session",
            )
            session_policy = _validate_definition_session_policy(
                args,
                schedule_type=schedule_type,
                help_command="vibe task add --help",
                allow_caller_session_default=caller_context is not None,
            )
            scope_key = _resolve_definition_scope_key(args, caller_context=caller_context, help_command="vibe task add --help")
            # Optional only for an escalating command task, where the Agent turn is a
            # failure notice and the stored message is extra triage guidance.
            message = (
                _resolve_prompt_input(
                    args,
                    help_command="vibe task add --help",
                    example_command="vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *'",
                )
                if not has_command or _explicit_flag_names(args, _TASK_MESSAGE_FLAG_ARGS)
                else ""
            )
            session_id, session_key = _resolve_session_target_args(
                args,
                required=session_policy == "existing",
                help_command="vibe task add --help",
            )
            cwd = _resolve_definition_session_cwd(
                explicit_cwd=getattr(args, "cwd", None),
                existing_cwd=None,
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args),
                has_command=has_command,
                help_command="vibe task add --help",
            )
            session_workdir = cwd
            cwd = _command_definition_spawn_cwd(
                cwd,
                has_command=has_command,
                session_policy=session_policy,
                explicit_cwd=getattr(args, "cwd", None),
                help_command="vibe task add --help",
            )
            agent_resolution = _resolve_agent_target(
                agent_name=getattr(args, "agent", None),
                session_id=session_id,
                session_key=session_key or scope_key or "",
                help_command="vibe task add --help",
            )
            agent = agent_resolution.agent
            agent_name = agent.name if agent else None
            expected_enabled_agent_id, expected_reference_agent_id = _agent_write_guard_ids(
                agent_resolution
            )
            if session_policy == "create_once":
                session_id = _reserve_definition_session(
                    agent_name=agent_name,
                    agent_id=agent.id if agent else None,
                    deliver_key=scope_key or "",
                    # The SESSION half, captured before the command's overwrote it. The
                    # two agree here today, but reading the command's answer is what
                    # made the update path place a Session in a subprocess directory,
                    # and the same line is the one that would do it here.
                    workdir=session_workdir,
                    help_command="vibe task add --help",
                    require_enabled_agent=expected_enabled_agent_id is not None,
                    expected_reference_agent_id=expected_reference_agent_id,
                )
                reserved_session_id = session_id
            session_target, delivery_target = _validate_definition_delivery_target(
                session_policy=session_policy,
                session_id=session_id,
                session_key=session_key,
                post_to=getattr(args, "post_to", None),
                deliver_key=getattr(args, "deliver_key", None),
                scope_key=scope_key,
                help_command="vibe task add --help",
            )
        metadata = _definition_metadata_with_scope(
            caller_context,
            scope_id=scope_key,
            # ``session_workdir`` describes a Session this definition will create, which
            # is a different question from ``cwd`` -- where its command runs. A pure
            # command task creates no Session at all, and a per-run one lets its Session
            # keep whatever workdir creation resolves (see
            # ``_command_definition_spawn_cwd``, which answers only the command's half).
            session_workdir=session_workdir,
        )
        if has_command:
            metadata["on_failure"] = effective_on_failure
        timezone_name = args.timezone or _default_timezone_name()
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise TaskCliError(
                f"invalid timezone: {timezone_name}",
                code="invalid_timezone",
                hint="Use a valid IANA timezone such as UTC, Asia/Shanghai, or America/Los_Angeles.",
                example="Asia/Shanghai",
                help_command="vibe task add --help",
                details={"timezone": timezone_name},
            ) from exc
        store = _task_store()

        if args.cron:
            try:
                CronTrigger.from_crontab(args.cron, timezone=timezone)
            except ValueError as exc:
                raise TaskCliError(
                    f"invalid cron expression: {args.cron}",
                    code="invalid_cron",
                    hint="Use standard 5-field crontab format: minute hour day-of-month month day-of-week.",
                    example="0 * * * *",
                    help_command="vibe task add --help",
                    details={"cron": args.cron},
                ) from exc
            task = store.add_task(
                name=_normalize_task_name(getattr(args, "name", None)),
                session_key=session_key,
                session_id=session_id,
                post_to=args.post_to,
                deliver_key=args.deliver_key,
                prompt=message,
                schedule_type="cron",
                agent_name=agent_name,
                session_policy=session_policy,
                cwd=cwd,
                cron=args.cron,
                timezone_name=timezone_name,
                shell_command=shell_command or None,
                command=command or None,
                timeout_seconds=getattr(args, "timeout", None),
                metadata=metadata,
                expected_enabled_agent_id=expected_enabled_agent_id,
                expected_reference_agent_id=expected_reference_agent_id,
                user_context=caller_user_context,
            )
        else:
            try:
                run_at = _normalize_run_at(args.at, timezone_name)
            except ValueError as exc:
                raise TaskCliError(
                    f"invalid --at timestamp: {args.at}",
                    code="invalid_run_at",
                    hint="Use ISO 8601, for example 2026-03-31T09:00:00+08:00 or 2026-03-31T09:00:00.",
                    example="2026-03-31T09:00:00+08:00",
                    help_command="vibe task add --help",
                    details={"at": args.at, "timezone": timezone_name},
                ) from exc
            task = store.add_task(
                name=_normalize_task_name(getattr(args, "name", None)),
                session_key=session_key,
                session_id=session_id,
                post_to=args.post_to,
                deliver_key=args.deliver_key,
                prompt=message,
                schedule_type="at",
                agent_name=agent_name,
                session_policy=session_policy,
                cwd=cwd,
                run_at=run_at,
                timezone_name=timezone_name,
                shell_command=shell_command or None,
                command=command or None,
                timeout_seconds=getattr(args, "timeout", None),
                metadata=metadata,
                expected_enabled_agent_id=expected_enabled_agent_id,
                expected_reference_agent_id=expected_reference_agent_id,
                user_context=caller_user_context,
            )
        reserved_session_id = None
        warnings = _collect_target_warnings(session_target, delivery_target)
        task_payload = _task_mutation_payload(task)
        payload_fields = {
            "warnings": warnings,
        }
        if session_default_notice:
            payload_fields["session_default_notice"] = session_default_notice
        _print_definition_payload(task_payload, **payload_fields)
        return 0
    except Exception as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="task creation failed before its Session reservation was adopted",
            )
        _print_task_error(exc, help_command="vibe task add --help")
        return 1


def cmd_task_list(
    *,
    include_finished: bool = False,
    brief: bool = True,
    page_request: PageRequest = PageRequest(),
):
    with _definition_read_store() as store:
        page_result = store.list_scheduled_tasks_page(
            page_request=page_request,
            include_successful_finished=include_finished,
            enabled_first=True,
        )
    command = ["vibe", "task", "list"]
    if include_finished:
        command.append("--include-finished")
    _print_definition_list_payload(
        page_result,
        payload_for_item=lambda task: _task_projection_payload(task, brief=brief),
        command=command,
    )
    return 0


def cmd_task_show(task_id: str):
    with _definition_read_store() as store:
        task = store.get_scheduled_task(task_id)
    if task is None:
        _print_task_error(
            TaskCliError(
                f"task '{task_id}' not found",
                code="task_not_found",
                hint="Use 'vibe task list' to find a valid task ID before calling show.",
                help_command="vibe task list",
                details={"task_id": task_id},
            )
        )
        return 1
    task_payload = _task_projection_payload(task)
    _print_definition_payload(task_payload)
    return 0


def cmd_task_set_enabled(task_id: str, enabled: bool):
    store = _task_store()
    task = store.get_task(task_id)
    if task is None:
        action = "resume" if enabled else "pause"
        _print_task_error(
            TaskCliError(
                f"task '{task_id}' not found",
                code="task_not_found",
                hint=f"Use 'vibe task list' to find a valid task ID before calling {action}.",
                help_command="vibe task list",
                details={"task_id": task_id},
            )
        )
        return 1
    try:
        updated = store.set_enabled(task_id, enabled)
    except TaskResumeBlocked as exc:
        lang = _memory_cli_language()
        _print_task_error(
            TaskCliError(
                i18n_t("error.taskOwnerUnavailable.message", lang),
                code=exc.code,
                hint=i18n_t("error.taskOwnerUnavailable.hint", lang, id=task_id),
                help_command=f"vibe task remove {task_id}",
                details={
                    "task_id": task_id,
                    "owner_session_id": exc.owner_session_id,
                },
            )
        )
        return 1
    except TaskScheduleRetired as exc:
        lang = _memory_cli_language()
        _print_task_error(
            TaskCliError(
                i18n_t("error.taskScheduleRetired.message", lang),
                code=exc.code,
                hint=i18n_t("error.taskScheduleRetired.hint", lang, id=task_id),
                help_command=f"vibe task update {task_id} --help",
                details={"task_id": task_id},
            )
        )
        return 1
    except DefinitionWriteConflict as exc:
        # Pause/resume is also a full-row write, so it is refused when a teardown
        # changed the definition first. Reporting the switch as flipped would be a lie
        # about a row this command did not write.
        _print_task_error(
            _definition_conflict_cli_error(
                exc,
                help_command="vibe task list",
                details={"task_id": task_id},
            )
        )
        return 1
    task_payload = _task_mutation_payload(updated)
    _print_definition_payload(task_payload)
    return 0


def cmd_task_remove(task_id: str):
    store = _task_store()
    removed = store.remove_task(task_id)
    if not removed:
        _print_task_error(
            TaskCliError(
                f"task '{task_id}' not found",
                code="task_not_found",
                hint="Use 'vibe task list' to find a valid task ID before calling remove.",
                help_command="vibe task list",
                details={"task_id": task_id},
            )
        )
        return 1
    _print_cli_payload("run_definition", removed_id=task_id)
    return 0


def _explicit_task_command_update_flags(args) -> list[str]:
    """Command-shaped update flags the user passed, in declaration order."""

    present: list[str] = []
    if getattr(args, "shell", None) is not None:
        present.append("--shell")
    if getattr(args, "command_argv", None) is not None:
        present.append("--")
    if getattr(args, "on_failure", None) is not None:
        present.append("--on-failure")
    if getattr(args, "timeout", None) is not None:
        present.append("--timeout")
    return present


def _resolve_definition_name_update(args, task, *, help_command: str) -> Optional[str]:
    if getattr(args, "name", None) is not None and getattr(args, "clear_name", False):
        raise TaskCliError(
            "use either --name or --clear-name, not both",
            code="conflicting_name_update",
            hint="Pass a new name with --name, or remove the stored name with --clear-name.",
            help_command=help_command,
        )
    if getattr(args, "clear_name", False):
        return None
    if getattr(args, "name", None) is not None:
        return _normalize_task_name(args.name)
    return task.name


def _resolve_definition_schedule_update(
    args,
    task,
    *,
    help_command: str,
) -> tuple[str, Optional[str], Optional[str], str]:
    """``(schedule_type, cron, run_at, timezone_name)`` for one stored definition."""

    timezone_name = args.timezone or task.timezone
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise TaskCliError(
            f"invalid timezone: {timezone_name}",
            code="invalid_timezone",
            hint="Use a valid IANA timezone such as UTC, Asia/Shanghai, or America/Los_Angeles.",
            example="Asia/Shanghai",
            help_command=help_command,
            details={"timezone": timezone_name},
        ) from exc

    if args.cron and args.at:
        raise TaskCliError(
            "use either --cron or --at when updating the schedule",
            code="conflicting_schedule_inputs",
            hint="Pass only one schedule update flag at a time.",
            help_command=help_command,
        )
    if args.cron:
        try:
            CronTrigger.from_crontab(args.cron, timezone=timezone)
        except ValueError as exc:
            raise TaskCliError(
                f"invalid cron expression: {args.cron}",
                code="invalid_cron",
                hint="Use standard 5-field crontab format: minute hour day-of-month month day-of-week.",
                example="0 * * * *",
                help_command=help_command,
                details={"cron": args.cron},
            ) from exc
        return "cron", args.cron, None, timezone_name
    if args.at:
        try:
            run_at = _normalize_run_at(args.at, timezone_name)
        except ValueError as exc:
            raise TaskCliError(
                f"invalid --at timestamp: {args.at}",
                code="invalid_run_at",
                hint="Use ISO 8601, for example 2026-03-31T09:00:00+08:00 or 2026-03-31T09:00:00.",
                example="2026-03-31T09:00:00+08:00",
                help_command=help_command,
                details={"at": args.at, "timezone": timezone_name},
            ) from exc
        return "at", None, run_at, timezone_name
    return task.schedule_type, task.cron, task.run_at, timezone_name


def _merge_task_command_update(
    args,
    task,
    *,
    help_command: str,
) -> tuple[Optional[str], Optional[list[str]], Optional[float]]:
    """Stored command fields with the provided flags applied.

    ``update_command_fields`` is all-or-nothing across the three columns, so an edit
    that names only one of them still has to send the other two back unchanged.
    """

    shell_command = task.shell_command
    command = task.command
    if getattr(args, "shell", None) is not None or getattr(args, "command_argv", None) is not None:
        resolved_command, resolved_shell, _has_command = _resolve_task_command(args, help_command=help_command)
        shell_command = resolved_shell
        command = resolved_command or None
    requested_timeout = getattr(args, "timeout", None)
    _validate_task_timeout(requested_timeout, help_command=help_command)
    timeout_seconds = requested_timeout if requested_timeout is not None else task.timeout_seconds
    return shell_command, command, timeout_seconds


def _cmd_task_update_pure_command(args, store, task) -> int:
    """Update a command task that has no Session, Agent, or delivery target.

    Kept off the main update path on purpose: that path resolves an Agent, defaults a
    Session policy to ``existing`` and validates a delivery target, all of which would
    invent bindings for a definition that deliberately has none — and would drop the
    stored ``cwd`` on the way, because ``existing`` Sessions own their own workdir.
    """

    help_command = "vibe task update --help"
    _reject_session_flags_for_command_task(args, help_command=help_command)
    name = _resolve_definition_name_update(args, task, help_command=help_command)
    schedule_type, cron, run_at, timezone_name = _resolve_definition_schedule_update(
        args,
        task,
        help_command=help_command,
    )
    shell_command, command, timeout_seconds = _merge_task_command_update(
        args,
        task,
        help_command=help_command,
    )
    raw_cwd = (getattr(args, "cwd", None) or "").strip()
    cwd = (
        _resolve_existing_cwd(raw_cwd, help_command=help_command, code="cwd_not_found", label="task")
        if raw_cwd
        else task.cwd
    )
    metadata = dict(task.metadata or {})
    if getattr(args, "on_failure", None) is not None:
        metadata["on_failure"] = args.on_failure

    changes = {
        "name": name,
        "schedule_type": schedule_type,
        "cwd": cwd,
        "cron": cron,
        "run_at": run_at,
        "timezone": timezone_name,
        "metadata": metadata,
        "shell_command": shell_command,
        "command": command,
        "timeout_seconds": timeout_seconds,
    }
    current = {
        "name": task.name,
        "schedule_type": task.schedule_type,
        "cwd": task.cwd,
        "cron": task.cron,
        "run_at": task.run_at,
        "timezone": task.timezone,
        "metadata": task.metadata,
        "shell_command": task.shell_command,
        "command": task.command,
        "timeout_seconds": task.timeout_seconds,
    }
    if changes == current:
        raise TaskCliError(
            "no task fields were changed",
            code="no_task_changes",
            hint="Pass at least one field to update, such as --name, --cron, --shell, or --timeout.",
            help_command=help_command,
            details={"task_id": args.task_id},
        )

    updated = store.update_task(
        args.task_id,
        name=name,
        session_key=task.session_key,
        session_id=task.session_id,
        prompt=task.prompt,
        schedule_type=schedule_type,
        agent_name=task.agent_name,
        session_policy=task.session_policy,
        post_to=task.post_to,
        deliver_key=task.deliver_key,
        cwd=cwd,
        update_cwd=True,
        cron=cron,
        run_at=run_at,
        timezone_name=timezone_name,
        shell_command=shell_command,
        command=command,
        timeout_seconds=timeout_seconds,
        update_command_fields=True,
        metadata=metadata,
    )
    _print_definition_payload(_task_mutation_payload(updated), warnings=[])
    return 0


def cmd_task_update(args):
    reserved_session_id: Optional[str] = None
    try:
        store = _task_store()
        task = store.get_task(args.task_id)
        if task is None:
            raise TaskCliError(
                f"task '{args.task_id}' not found",
                code="task_not_found",
                hint="Use 'vibe task list' to find a valid task ID before calling update.",
                help_command="vibe task list",
                details={"task_id": args.task_id},
            )

        # A definition is either a scheduled Agent message or a scheduled command, and
        # the two shapes are not convertible in place: switching would leave the other
        # shape's columns (a session binding, or a command line) stored as dead config.
        command_flags = _explicit_task_command_update_flags(args)
        message_flags = _explicit_flag_names(args, _TASK_MESSAGE_FLAG_ARGS)
        #
        # Scoped to ``on_failure=none``, because only THERE is a message a mode switch.
        # An escalating command task already stores one -- it is the guidance the
        # failure turn carries, and ``cmd_task_add`` REQUIRES that mode for a message
        # to be legal beside a command (``message_without_consumer``). Rejecting it for
        # every command task forbade the exact shape the add path blesses, so the
        # guidance could only be reworded by deleting and recreating the task.
        if task.has_command and message_flags and task.on_failure == "none":
            raise TaskCliError(
                "this task runs a command, so its message inputs cannot be changed",
                code="task_mode_immutable",
                hint="Remove this task and create it again to move between command and message tasks.",
                help_command="vibe task update --help",
                details={"task_id": args.task_id, "kind": "command", "flags": message_flags},
            )
        if not task.has_command and command_flags:
            raise TaskCliError(
                "this task sends a stored message, so command inputs cannot be added",
                code="task_mode_immutable",
                hint="Remove this task and create it again with --shell to make it a command task.",
                help_command="vibe task update --help",
                details={"task_id": args.task_id, "kind": "message", "flags": command_flags},
            )
        requested_on_failure = getattr(args, "on_failure", None)
        if requested_on_failure is not None and requested_on_failure != task.on_failure:
            raise TaskCliError(
                f"cannot change failure handling from '{task.on_failure}' to '{requested_on_failure}'",
                code="task_mode_immutable",
                hint=(
                    "Escalation decides whether this task owns an Agent Session at all. "
                    "Remove this task and create it again with the failure handling you want."
                ),
                help_command="vibe task update --help",
                details={
                    "task_id": args.task_id,
                    "on_failure": task.on_failure,
                    "requested_on_failure": requested_on_failure,
                },
            )
        if task.has_command and task.on_failure == "none":
            return _cmd_task_update_pure_command(args, store, task)

        if getattr(args, "reset_delivery", False) and (
            getattr(args, "post_to", None) is not None
            or getattr(args, "deliver_key", None) is not None
            or getattr(args, "scope_id", None) is not None
            or bool(getattr(args, "same_scope", False))
        ):
            raise TaskCliError(
                "use either --reset-delivery or a new delivery flag, not both",
                code="conflicting_delivery_target",
                hint="Pass --reset-delivery to clear delivery overrides, or pass --scope-id/--same-scope to replace placement.",
                help_command="vibe task update --help",
            )
        caller_context = caller_context_from_env()
        caller_user_context = caller_resource_user_context(caller_context)
        scope_arg_present = (getattr(args, "scope_id", None) is not None) or bool(getattr(args, "same_scope", False))
        if scope_arg_present and not (
            bool(getattr(args, "create_session", False)) or bool(getattr(args, "create_session_per_run", False))
        ):
            raise TaskCliError(
                "scope placement flags only apply when creating Sessions",
                code="scope_without_session_creation",
                hint="Use --create-session or --create-session-per-run with --scope-id/--same-scope, or omit the scope placement flag.",
                help_command="vibe task update --help",
            )
        requested_scope_key = _resolve_definition_scope_key(
            args,
            caller_context=caller_context,
            help_command="vibe task update --help",
        )
        session_id_update, session_key_update = _resolve_session_target_args(
            args,
            required=False,
            help_command="vibe task update --help",
        )
        if session_id_update is not None:
            session_id = session_id_update
            session_key = ""
        elif session_key_update:
            session_id = None
            session_key = session_key_update
        else:
            session_id = task.session_id
            session_key = task.session_key
        if getattr(args, "reset_delivery", False):
            post_to = None
            deliver_key = None
        else:
            requested_post_to = getattr(args, "post_to", None)
            requested_deliver_key = getattr(args, "deliver_key", None)
            if requested_post_to is not None:
                post_to = requested_post_to
                deliver_key = None
            elif requested_deliver_key is not None:
                post_to = None
                deliver_key = requested_deliver_key
            else:
                post_to = task.post_to
                deliver_key = task.deliver_key
        metadata = dict(task.metadata or {})
        if requested_scope_key:
            metadata["session_scope_id"] = requested_scope_key
        elif scope_arg_present:
            metadata.pop("session_scope_id", None)

        name = _resolve_definition_name_update(args, task, help_command="vibe task update --help")

        # Rejected rather than silently resolved, exactly as ``--name`` /
        # ``--clear-name`` above. The two flags mean opposite things and the pair had
        # no single sensible reading: ``--clear-agent`` won for ``agent_name`` (→
        # None) while the mere PRESENCE of ``--agent`` set
        # ``explicit_agent_requested``, which POPS the follow-the-session marker. The
        # definition then looked like "no Agent pinned and not following its
        # Session", so the resolve below wrote today's scope / default Agent back as
        # a hard pin — the exact regression the marker exists to prevent (HFR-245),
        # reachable in one command.
        if getattr(args, "agent", None) is not None and getattr(args, "clear_agent", False):
            raise TaskCliError(
                "use either --agent or --clear-agent, not both",
                code="conflicting_agent_update",
                hint=(
                    "Pin an Agent with --agent, or hand Agent authority back to the bound "
                    "Session with --clear-agent."
                ),
                help_command="vibe task update --help",
            )
        if getattr(args, "clear_agent", False):
            agent_name = None
        elif getattr(args, "agent", None) is not None:
            agent_name = _validate_agent_name_arg(args.agent)
        else:
            agent_name = task.agent_name

        # "Follow the bound Session's Agent" is a durable state (set by a reset
        # rebind, or by ``--clear-agent``), not merely a missing ``agent_name``.
        # An explicit ``--agent`` is the user pinning again, so it ends the state.
        explicit_agent_requested = getattr(args, "agent", None) is not None
        if explicit_agent_requested:
            metadata.pop(BINDING_FOLLOWS_SESSION_METADATA_KEY, None)
        elif getattr(args, "clear_agent", False):
            metadata[BINDING_FOLLOWS_SESSION_METADATA_KEY] = True
        follows_session_agent = bool(metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY))

        message_changed = any(
            getattr(args, name, None) is not None
            for name in ("message", "message_file", "prompt", "prompt_file")
        )
        message = (
            _resolve_prompt_input(
                args,
                help_command="vibe task update --help",
                example_command=f"vibe task update {args.task_id}",
            )
            if message_changed
            else task.prompt
        )

        schedule_type, cron, run_at, timezone_name = _resolve_definition_schedule_update(
            args,
            task,
            help_command="vibe task update --help",
        )

        session_policy = _definition_session_policy_for_update(
            args,
            current_policy=task.session_policy,
            current_schedule_type=task.schedule_type,
            next_schedule_type=schedule_type,
            help_command="vibe task update --help",
        )
        explicit_cwd = getattr(args, "cwd", None)
        _reject_inert_create_once_cwd_update(
            explicit_cwd=explicit_cwd,
            current_policy=task.session_policy,
            current_session_id=task.session_id,
            create_session=bool(getattr(args, "create_session", False)),
            has_command=task.has_command,
            help_command="vibe task update --help",
        )
        # This edit is not placing a Session: the definition is bound to one that owns
        # its own directory, or has already reserved its reusable one and passed no
        # --create-session to replace it. For a message task that leaves ``--cwd``
        # nothing to do, and the two refusals above say so. For a command task it
        # leaves exactly one question -- where the subprocess runs -- so the flag is
        # resolved as the command's alone and the Session's half is read back from
        # storage untouched. Without the branch, every path below writes SOME answer
        # into ``session_workdir``, and for a command task that answer is the command's
        # directory: an unrelated ``--name`` edit on a reserved definition wrote the
        # subprocess directory into ``metadata["session_workdir"]``.
        command_only_cwd = task.has_command and (
            session_policy == "existing"
            or (
                session_policy == "create_once"
                and bool(task.session_id)
                and not getattr(args, "create_session", False)
            )
        )
        if command_only_cwd:
            session_workdir = _stored_session_workdir(task, metadata)
            cwd = _resolve_command_only_cwd(
                explicit_cwd,
                stored_cwd=task.cwd,
                help_command="vibe task update --help",
            )
        elif session_policy == "existing":
            cwd = _resolve_definition_session_cwd(
                explicit_cwd=explicit_cwd,
                existing_cwd=None,
                session_policy=session_policy,
                has_command=task.has_command,
                help_command="vibe task update --help",
            )
        elif explicit_cwd is not None:
            cwd = _resolve_definition_session_cwd(
                explicit_cwd=explicit_cwd,
                existing_cwd=None,
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args),
                help_command="vibe task update --help",
            )
        elif getattr(args, "create_session", False) or getattr(args, "create_session_per_run", False):
            cwd = _resolve_definition_session_cwd(
                explicit_cwd=None,
                existing_cwd=_stored_session_workdir(task, metadata),
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args) or bool(str(metadata.get("session_scope_id") or "").strip()),
                help_command="vibe task update --help",
            )
        else:
            # Nothing in this edit asks about directories, so each half carries forward
            # from where that half is stored. For a message task ``task.cwd`` IS the
            # Session's answer; for a command task it is the command's alone, and
            # reading it here promoted it into ``metadata["session_workdir"]`` -- so a
            # plain ``--name`` on a per-run definition that deliberately left its
            # Sessions unplaced (SCT-047) pinned every future one to the directory
            # ``task add`` happened to be typed in, and a ``create_once`` definition
            # that had not reserved yet reserved there. The command half is untouched:
            # ``_command_definition_spawn_cwd`` falls back to ``stored_cwd`` below.
            cwd = _stored_session_workdir(task, metadata) if task.has_command else task.cwd
        if not command_only_cwd:
            session_workdir = cwd
            cwd = _command_definition_spawn_cwd(
                cwd,
                has_command=task.has_command,
                session_policy=session_policy,
                explicit_cwd=explicit_cwd,
                stored_cwd=task.cwd,
                help_command="vibe task update --help",
            )
        scope_key = requested_scope_key or str(metadata.get("session_scope_id") or "").strip() or _legacy_scope_key_from_target(deliver_key)
        if session_policy == "create_once" and not scope_key:
            raise TaskCliError(
                "--scope-id or --same-scope is required when a stored definition creates one reusable Session",
                code="missing_delivery_target",
                hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
                help_command="vibe task update --help",
            )
        agent_resolution = _AgentTargetResolution(None, False)
        if follows_session_agent and not explicit_agent_requested:
            # Deliberately resolves NOTHING. Re-resolving here would write today's
            # scope/default Agent back onto a definition whose Agent authority now
            # belongs to its bound Session, and the pin wins over the Session row at
            # dispatch -- so an unrelated ``--name`` edit would silently move every
            # future fire onto a different Agent.
            pass
        elif agent_name is None and session_policy != "existing":
            agent_resolution = _resolve_agent_target(
                agent_name=None,
                session_id=None,
                session_key=scope_key,
                help_command="vibe task update --help",
            )
            agent = agent_resolution.agent
            agent_name = agent.name if agent else None
        elif agent_name is not None or session_id or session_key:
            agent_resolution = _resolve_agent_target(
                agent_name=agent_name,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe task update --help",
                existing_agent_reference=not explicit_agent_requested,
            )
            agent = agent_resolution.agent
            agent_name = agent.name if agent else None
        expected_enabled_agent_id, expected_reference_agent_id = _agent_write_guard_ids(
            agent_resolution
        )
        if session_policy == "create_once" and (
            getattr(args, "create_session", False) or not session_id
        ):
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                agent_id=agent.id if agent else None,
                deliver_key=scope_key,
                # The SESSION half, captured before ``_command_definition_spawn_cwd``
                # overwrote ``cwd`` with the command's. Reading ``cwd`` here handed the
                # replacement Session the directory the user picked for a subprocess,
                # so it stopped inheriting from its Scope -- the same defect
                # ``_stored_session_workdir`` closes one branch earlier, through the
                # one line that could reintroduce it.
                workdir=session_workdir,
                help_command="vibe task update --help",
                require_enabled_agent=expected_enabled_agent_id is not None,
                expected_reference_agent_id=expected_reference_agent_id,
            )
            reserved_session_id = session_id
            session_key = ""
        if session_policy == "existing":
            metadata.pop("session_workdir", None)
        elif session_workdir:
            # The Session's half of the answer, not the command's: a per-run command
            # records the invocation directory in ``cwd`` above without pinning the
            # Session that escalation creates.
            metadata["session_workdir"] = session_workdir
        else:
            metadata.pop("session_workdir", None)
        session_target, delivery_target = _validate_definition_update_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=post_to,
            deliver_key=deliver_key,
            scope_key=scope_key,
            help_command="vibe task update --help",
        )

        # An escalating command task reaches here because it owns a Session for its
        # failure notice; only its command columns need merging, and a message task
        # sends all three back as ``None`` with the write gate closed.
        if task.has_command:
            shell_command, command, timeout_seconds = _merge_task_command_update(
                args,
                task,
                help_command="vibe task update --help",
            )
        else:
            shell_command = None
            command = None
            timeout_seconds = None

        changes = {
            "name": name,
            "session_id": session_id,
            "session_key": session_key,
            "prompt": message,
            "agent_name": agent_name,
            "session_policy": session_policy,
            "schedule_type": schedule_type,
            "post_to": post_to,
            "deliver_key": deliver_key,
            "cwd": cwd,
            "cron": cron,
            "run_at": run_at,
            "timezone": timezone_name,
            "metadata": metadata,
            "shell_command": shell_command,
            "command": command,
            "timeout_seconds": timeout_seconds,
        }
        current = {
            "name": task.name,
            "session_id": task.session_id,
            "session_key": task.session_key,
            "prompt": task.prompt,
            "agent_name": task.agent_name,
            "session_policy": task.session_policy,
            "schedule_type": task.schedule_type,
            "post_to": task.post_to,
            "deliver_key": task.deliver_key,
            "cwd": task.cwd,
            "cron": task.cron,
            "run_at": task.run_at,
            "timezone": task.timezone,
            "metadata": task.metadata,
            "shell_command": task.shell_command,
            "command": task.command,
            "timeout_seconds": task.timeout_seconds,
        }
        if changes == current:
            raise TaskCliError(
                "no task fields were changed",
                code="no_task_changes",
                hint="Pass at least one field to update, such as --name, --cron, --message, --session-id, or --scope-id.",
                help_command="vibe task update --help",
                details={"task_id": args.task_id},
            )

        updated = store.update_task(
            args.task_id,
            name=name,
            session_key=session_key,
            session_id=session_id,
            prompt=message,
            schedule_type=schedule_type,
            agent_name=agent_name,
            session_policy=session_policy,
            post_to=post_to,
            deliver_key=deliver_key,
            cwd=cwd,
            update_cwd=True,
            cron=cron,
            run_at=run_at,
            timezone_name=timezone_name,
            shell_command=shell_command,
            command=command,
            timeout_seconds=timeout_seconds,
            update_command_fields=task.has_command,
            metadata=metadata,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
            user_context=caller_user_context,
        )
        reserved_session_id = None
        warnings = _collect_target_warnings(session_target, delivery_target)
        task_payload = _task_mutation_payload(updated)
        _print_definition_payload(task_payload, warnings=warnings)
        return 0
    except DefinitionWriteConflict as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="task update failed before its Session reservation was adopted",
            )
        _print_task_error(
            _definition_conflict_cli_error(
                exc,
                help_command="vibe task update --help",
                details={"task_id": getattr(args, "task_id", exc.definition_id)},
            )
        )
        return 1
    except Exception as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="task update failed before its Session reservation was adopted",
            )
        _print_task_error(exc, help_command="vibe task update --help")
        return 1


def cmd_task_run(task_id: str):
    store = _task_store()
    task = store.get_task(task_id)
    if task is None:
        _print_task_error(
            TaskCliError(
                f"task '{task_id}' not found",
                code="task_not_found",
                hint="Use 'vibe task list' to find a valid task ID before calling run.",
                help_command="vibe task list",
                details={"task_id": task_id},
            )
        )
        return 1
    request = _task_request_store().enqueue_task_run(task.id, task=task)
    _print_cli_payload(
        "agent_run",
        accepted=True,
        execution_id=request.id,
        run_id=request.id,
        request_type=request.request_type,
        task_id=task.id,
        definition={"id": task.id, "definition_type": "scheduled"},
        run={
            "id": request.id,
            "status": "queued",
            "run_type": request.request_type,
            "definition_id": task.id,
            "agent_name": task.agent_name,
            "session_id": task.session_id,
            "session_policy": task.session_policy,
        },
    )
    return 0


def cmd_hook_send(args):
    try:
        session_id, session_key = _resolve_session_target_args(
            args,
            required=True,
            help_command="vibe hook send --help",
        )
        session_target, delivery_target = _validate_delivery_args(
            session_id=session_id,
            session_key=session_key,
            post_to=getattr(args, "post_to", None),
            deliver_key=getattr(args, "deliver_key", None),
            help_command="vibe hook send --help",
        )
        message = _resolve_prompt_input(
            args,
            help_command="vibe hook send --help",
            example_command="vibe hook send --session-id sesk8m4q2p7x",
        )
        agent_resolution = _resolve_agent_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key,
            help_command="vibe hook send --help",
        )
        agent = agent_resolution.agent
        request = _task_request_store().enqueue_hook_send(
            session_key=session_key,
            session_id=session_id,
            post_to=args.post_to,
            deliver_key=args.deliver_key,
            prompt=message,
            agent_name=agent.name if agent else None,
            agent_id=agent.id if agent else None,
            run_type="agent_run",
            source_kind="cli",
            expected_enabled_agent_id=(
                agent.id
                if agent is not None and agent_resolution.requires_enabled_write_guard
                else None
            ),
            expected_reference_agent_id=(
                agent.id
                if agent is not None
                and getattr(agent_resolution, "preserves_existing_reference", False)
                else None
            ),
        )
        warnings = _collect_target_warnings(session_target, delivery_target)
        _print_cli_payload(
            "agent_run",
            accepted=True,
            execution_id=request.id,
            run_id=request.id,
            request_type=request.request_type,
            session_id=session_id,
            session_key=session_key,
            post_to=args.post_to,
            deliver_key=args.deliver_key,
            deprecation_warning=(
                "vibe hook send is deprecated; use `vibe agent run --session-id <session-id> "
                "--no-callback --message ...` for the same fire-and-forget behavior, or pass "
                "`--callback-session-id <session-id>` when the async run should report back."
            ),
            run={
                "id": request.id,
                "status": "queued",
                "run_type": request.request_type,
                "agent_name": agent.name if agent else None,
                "session_id": session_id,
            },
            warnings=warnings,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe hook send --help")
        return 1


def _read_optional_text(path: str | None, *, field_name: str) -> str | None:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        raise TaskCliError(
            f"failed to read {field_name} file: {exc}",
            code=f"{field_name}_file_read_failed",
            details={f"{field_name}_file": path},
        ) from exc


def _parse_metadata_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except ValueError as exc:
        raise TaskCliError("metadata must be valid JSON", code="invalid_metadata_json") from exc
    if not isinstance(payload, dict):
        raise TaskCliError("metadata JSON must be an object", code="invalid_metadata_json")
    return payload


def _add_json_noop(parser) -> None:
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def cmd_agent_list(args):
    try:
        page_request = _page_request_from_args(args, help_command="vibe agent list --help")
        store = _agent_store()
        backend = getattr(args, "backend", None)
        disabled_only = bool(getattr(args, "disabled", False))
        include_disabled = disabled_only or bool(getattr(args, "include_disabled", False))
        agents = store.list_agents(include_disabled=include_disabled)
        if backend:
            agents = [agent for agent in agents if agent.backend == backend]
        if disabled_only:
            agents = [agent for agent in agents if not agent.enabled]
        result = page_sequence(agents, page_request)
        command = ["vibe", "agent", "list"]
        _add_optional_arg(command, "--backend", backend)
        if disabled_only:
            command.append("--disabled")
        elif include_disabled:
            command.append("--include-disabled")
        _print_cli_payload(
            "agents",
            agents=[_agent_payload(agent, brief=True) for agent in result.items],
            **_paginated_fields(result, command=command),
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe agent list --help")
        return 1


def cmd_agent_show(args):
    try:
        agent = _agent_store().require(args.name)
        _print_cli_payload("agent", agent=_agent_payload(agent))
        return 0
    except Exception as exc:
        _print_task_error(TaskCliError(str(exc), code="agent_not_found", details={"agent": args.name}))
        return 1


def cmd_agent_default(args):
    try:
        store = _agent_store()
        if store.get(args.name) is None:
            try:
                backend = validate_agent_backend(args.name)
            except ValueError:
                backend = None
            if backend:
                store.sync_builtin_default_agent(backend=backend, backend_enabled=True)
        store.set_default_agent_name(args.name)
        agent = store.require(args.name)
        _print_cli_payload("default_agent", default_agent_name=agent.name, agent=_agent_payload(agent, brief=True))
        return 0
    except Exception as exc:
        _print_task_error(exc)
        return 1


def _agent_models_current(agent, options: dict) -> dict:
    """Echo an Agent's currently-set model/effort and whether they remain valid."""
    by_value = {entry.get("value"): entry for entry in options.get("models") or []}
    model = agent.model
    effort = agent.reasoning_effort
    # An OpenCode Agent may store a bare model id that routes through the configured
    # default provider; normalize it to the catalog's provider/model key before lookup
    # so a valid bare-id Agent is not reported as unknown.
    resolved = model
    if model and model not in by_value and "/" not in model:
        default_provider = options.get("default_provider")
        if default_provider and f"{default_provider}/{model}" in by_value:
            resolved = f"{default_provider}/{model}"
    model_known: bool | None = (resolved in by_value) if model else None
    effort_valid: bool | None = None
    if effort and model and model_known:
        effort_valid = effort in (by_value[resolved].get("reasoning_efforts") or [])
    valid = not (model_known is False or effort_valid is False)
    return {
        "model": model,
        "reasoning_effort": effort,
        "model_known": model_known,
        "reasoning_effort_valid": effort_valid,
        "valid": valid,
    }


def cmd_agent_models(args):
    try:
        name = getattr(args, "name", None)
        backend_arg = getattr(args, "backend", None)
        provider = getattr(args, "provider", None)
        model = getattr(args, "model", None)
        if bool(name) == bool(backend_arg):
            raise TaskCliError(
                "provide exactly one of <name> or --backend",
                code="invalid_agent_models_target",
                hint="Pass an Agent name to use its backend, or --backend to query a backend directly.",
                help_command="vibe agent models --help",
            )
        agent = None
        if name:
            try:
                agent = _agent_store().require(name)
            except Exception as exc:
                raise TaskCliError(str(exc), code="agent_not_found", details={"agent": name}) from exc
            backend = agent.backend
        else:
            backend = validate_agent_backend(backend_arg)
        if provider and backend != "opencode":
            raise TaskCliError(
                f"--provider is only supported for the opencode backend, not '{backend}'",
                code="provider_not_supported",
                hint="Providers are an OpenCode concept; drop --provider for claude/codex.",
                help_command="vibe agent models --help",
            )
        options = api.agent_model_options(backend, provider=provider)
        if not options.get("ok"):
            raise TaskCliError(
                options.get("error") or "failed to load model options",
                code="agent_models_unavailable",
                details={"backend": backend},
                help_command="vibe agent models --help",
            )
        # `current` validity is checked against the full set; --model only narrows
        # the displayed list, so an Agent's real model is never hidden from it.
        current = _agent_models_current(agent, options) if agent else None
        models = options.get("models", [])
        providers = options.get("providers")
        provider_rows = providers if isinstance(providers, list) else []
        if model:
            models = [entry for entry in models if entry.get("value") == model]
            matching_provider_ids = {
                entry.get("provider") for entry in models if entry.get("provider")
            }
            provider_rows = [
                entry for entry in provider_rows if entry.get("id") in matching_provider_ids
            ]
        catalog_rows = [("provider", entry) for entry in provider_rows]
        catalog_rows.extend(("model", entry) for entry in models)
        page_request = _page_request_from_args(args, help_command="vibe agent models --help")
        result = page_sequence(catalog_rows, page_request)
        page_providers = [entry for kind, entry in result.items if kind == "provider"]
        page_models = [entry for kind, entry in result.items if kind == "model"]
        command = ["vibe", "agent", "models"]
        if name:
            command.append(name)
        else:
            _add_optional_arg(command, "--backend", backend_arg)
        _add_optional_arg(command, "--provider", provider)
        _add_optional_arg(command, "--model", model)
        _print_cli_payload(
            "agent_models",
            agent=agent.name if agent else None,
            backend=backend,
            current=current,
            providers=page_providers if providers is not None else None,
            models=page_models,
            source=options.get("source"),
            live=options.get("live", False),
            notes=options.get("notes"),
            **_paginated_fields(result, command=command),
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe agent models --help")
        return 1


def _agent_value_warning_fields(agent) -> dict:
    """Best-effort, non-fatal warnings when an Agent's model/effort is unknown.

    Cheap (file-based) check for claude/codex only; OpenCode availability is
    live (needs the OpenCode server) so it is skipped here to keep create/update
    fast — ``vibe agent models`` is the place for the full OpenCode check.
    """
    if agent.backend not in ("claude", "codex"):
        return {}
    if not agent.model and not agent.reasoning_effort:
        return {}
    try:
        options = api.agent_model_options(agent.backend)
    except Exception:
        return {}
    if not options.get("ok"):
        return {}
    by_value = {entry.get("value"): entry for entry in options.get("models") or []}
    model_unknown = bool(agent.model) and agent.model not in by_value
    warnings: list[str] = []
    if model_unknown:
        warnings.append(
            f"model '{agent.model}' is not in the known {agent.backend} model list; "
            "it may be a typo or newer than the catalog"
        )
    if agent.reasoning_effort:
        if agent.model and not model_unknown:
            allowed = set(by_value[agent.model].get("reasoning_efforts") or [])
            scope = f"model '{agent.model}'"
        else:
            # model unset or unknown: accept any effort valid for some model of this backend
            # (Codex efforts are backend-wide; Claude's widest set still lives in some model)
            allowed = set()
            for entry in by_value.values():
                allowed.update(entry.get("reasoning_efforts") or [])
            scope = f"backend '{agent.backend}'"
        if allowed and agent.reasoning_effort not in allowed:
            warnings.append(f"reasoning_effort '{agent.reasoning_effort}' is not valid for {scope}")
    if not warnings:
        return {}
    return {
        "warnings": warnings,
        "hint": f"Run `vibe agent models {agent.name}` to list valid models and reasoning efforts.",
    }


def cmd_agent_create(args):
    try:
        system_prompt = args.system_prompt
        if args.system_prompt_file:
            system_prompt = _read_optional_text(args.system_prompt_file, field_name="system_prompt")
        agent = _agent_store().create(
            name=args.name,
            backend=validate_agent_backend(args.backend),
            description=args.description,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            system_prompt=system_prompt,
            metadata=_parse_metadata_json(args.metadata),
            enabled=not bool(getattr(args, "disabled", False)),
        )
        _print_cli_payload("agent", agent=_agent_payload(agent), **_agent_value_warning_fields(agent))
        return 0
    except AgentNameValidationError as exc:
        try:
            lang = V2Config.load().language
        except Exception:
            lang = "en"
        key = f"error.agentNameValidation.{exc.code}"
        _print_task_error(
            TaskCliError(
                i18n_t(f"{key}.message", lang, agent=exc.agent_name),
                code=exc.code,
                hint=i18n_t(f"{key}.hint", lang, agent=exc.agent_name),
                details={"agent": exc.agent_name},
            )
        )
        return 1
    except Exception as exc:
        _print_task_error(exc)
        return 1


def cmd_agent_update(args):
    try:
        kwargs: dict[str, object] = {}
        if args.description is not None:
            kwargs["description"] = args.description
        if args.clear_description:
            kwargs["description"] = None
        if args.model is not None:
            kwargs["model"] = args.model
        if args.clear_model:
            kwargs["model"] = None
        if args.reasoning_effort is not None:
            kwargs["reasoning_effort"] = args.reasoning_effort
        if args.clear_reasoning_effort:
            kwargs["reasoning_effort"] = None
        if args.system_prompt is not None:
            kwargs["system_prompt"] = args.system_prompt
        if args.system_prompt_file:
            kwargs["system_prompt"] = _read_optional_text(args.system_prompt_file, field_name="system_prompt")
        if args.clear_system_prompt:
            kwargs["system_prompt"] = None
        if args.metadata is not None:
            kwargs["metadata"] = _parse_metadata_json(args.metadata)
        if getattr(args, "enable", False):
            kwargs["enabled"] = True
        if getattr(args, "disable", False):
            kwargs["enabled"] = False
        if not kwargs:
            raise TaskCliError(
                "no agent fields were changed",
                code="no_agent_changes",
                hint="Pass at least one editable field. Agent name and backend are immutable.",
            )
        agent = _agent_store().update(args.name, **kwargs)
        _print_cli_payload("agent", agent=_agent_payload(agent), **_agent_value_warning_fields(agent))
        return 0
    except AgentArchivedEditError as exc:
        _print_task_error(_agent_archived_edit_cli_error(exc))
        return 1
    except Exception as exc:
        _print_task_error(exc)
        return 1


def cmd_agent_set_enabled(args, *, enabled: bool):
    try:
        agent = _agent_store().set_enabled(args.name, enabled)
        _print_cli_payload("agent", agent=_agent_payload(agent))
        return 0
    except AgentArchivedEditError as exc:
        _print_task_error(_agent_archived_edit_cli_error(exc))
        return 1
    except Exception as exc:
        _print_task_error(exc)
        return 1


def _agent_archived_edit_cli_error(exc: AgentArchivedEditError) -> TaskCliError:
    try:
        lang = V2Config.load().language
    except Exception:
        lang = "en"
    key = f"error.agentLifecycle.{exc.code}"
    return TaskCliError(
        i18n_t(f"{key}.message", lang, agent=exc.agent_name),
        code=exc.code,
        hint=i18n_t(f"{key}.hint", lang, agent=exc.agent_name),
        details={"agent": exc.agent_name},
    )


def _agent_reference_rewrite_cli_error(exc: AgentReferenceRewriteError) -> TaskCliError:
    try:
        lang = V2Config.load().language
    except Exception:
        lang = "en"
    key = f"error.agentLifecycle.{exc.code}"
    return TaskCliError(
        i18n_t(f"{key}.message", lang),
        code=exc.code,
        hint=i18n_t(f"{key}.hint", lang),
    )


def cmd_agent_remove(args):
    try:
        store = _agent_store()
        try:
            archived = store.archive(args.name)
        except AgentArchiveError as exc:
            try:
                lang = V2Config.load().language
            except Exception:
                lang = "en"
            raise TaskCliError(
                i18n_t(
                    f"error.agentArchive.{exc.code}.message",
                    lang,
                    agent=exc.agent_name,
                ),
                code=exc.code,
                hint=i18n_t(
                    f"error.agentArchive.{exc.code}.hint",
                    lang,
                    agent=exc.agent_name,
                ),
                details={"agent": args.name},
            ) from exc
        except AgentReferenceRewriteError as exc:
            raise _agent_reference_rewrite_cli_error(exc) from exc
        if archived is None:
            raise TaskCliError(f"agent '{args.name}' not found", code="agent_not_found", details={"agent": args.name})
        _print_cli_payload(
            "agent",
            removed_agent=archived.original_name,
            archived_agent=_agent_payload(archived.agent, brief=True),
            references=archived.references,
            default_agent_name=archived.default_agent_name,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc)
        return 1


def cmd_agent_import(args):
    try:
        candidates = []
        skipped = []
        if args.file:
            if args.name or args.all:
                raise TaskCliError(
                    "--name and --all are only valid with --from",
                    code="invalid_agent_import_filter",
                    help_command="vibe agent import --help",
                )
            if not args.backend:
                raise TaskCliError(
                    "--backend is required when importing an arbitrary file",
                    code="missing_agent_backend",
                    hint="Pass --backend codex, --backend claude, or --backend opencode.",
                )
            candidates.append(parse_agent_file(Path(args.file), backend=args.backend))
        else:
            if args.name and args.all:
                raise TaskCliError(
                    "use either --name or --all, not both",
                    code="invalid_agent_import_filter",
                    help_command="vibe agent import --help",
                )
            for path, backend in iter_global_agent_files(args.from_source):
                try:
                    candidate = parse_agent_file(path, backend=backend)
                except Exception as exc:
                    skipped.append({"source_ref": str(path), "reason": "invalid", "error": str(exc)})
                    continue
                if args.name and candidate.name != args.name:
                    continue
                candidates.append(candidate)
            if args.name and not candidates:
                raise TaskCliError(
                    f"agent '{args.name}' was not found in {args.from_source} global agents",
                    code="agent_import_source_not_found",
                    details={"source": args.from_source, "name": args.name},
                )
        result = _agent_store().import_candidates(candidates)
        _print_cli_payload(
            "agents",
            imported=[_agent_payload(agent, brief=True) for agent in result.imported],
            skipped=skipped + result.skipped,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc)
        return 1


def _validate_run_session_policy(args, *, help_command: str) -> str:
    session_id = (getattr(args, "session_id", None) or "").strip()
    fork_session = (getattr(args, "fork_session", None) or "").strip()
    fork_self = bool(getattr(args, "fork_self", False))
    create_session = bool(getattr(args, "create_session", False))
    create_per_run = bool(getattr(args, "create_session_per_run", False))
    same_scope = bool(getattr(args, "same_scope", False))
    scope_id = (getattr(args, "scope_id", None) or "").strip()
    deliver_key = (getattr(args, "deliver_key", None) or "").strip()
    agent_name = (getattr(args, "agent", None) or "").strip()
    visibility = (getattr(args, "visibility", None) or "").strip()
    if bool(getattr(args, "async_run", False)) and bool(getattr(args, "sync_run", False)):
        raise TaskCliError(
            "use either --async or --sync, not both",
            code="conflicting_wait_policy",
            hint="Agent runs are async by default. Pass --sync only when the CLI should wait.",
            help_command=help_command,
        )
    if _agent_run_is_async(args) and getattr(args, "wait_timeout", None) is not None:
        raise TaskCliError(
            "use --sync with --wait-timeout",
            code="conflicting_wait_policy",
            hint="Agent runs are async by default. Pass --sync when the CLI should wait, or remove --wait-timeout.",
            help_command=help_command,
        )
    if (getattr(args, "callback_session_id", None) or "").strip() and bool(getattr(args, "no_callback", False)):
        raise TaskCliError(
            "use either --callback-session-id or --no-callback, not both",
            code="conflicting_callback_policy",
            hint="Pass --callback-session-id to receive a follow-up, or --no-callback to intentionally inspect the run later.",
            help_command=help_command,
        )
    if same_scope and scope_id:
        raise TaskCliError(
            "use either --same-scope or --scope-id, not both",
            code="conflicting_scope_placement",
            hint="Use --same-scope to reuse the caller/source scope, or --scope-id to place the new Session explicitly.",
            help_command=help_command,
        )
    if deliver_key and (same_scope or scope_id):
        raise TaskCliError(
            "use either the legacy delivery target or the new scope placement flags, not both",
            code="conflicting_scope_placement",
            hint="Use --scope-id or --same-scope for new Agent-facing commands.",
            help_command=help_command,
        )
    if fork_self and fork_session:
        raise TaskCliError(
            "use either --fork-self or --fork-session, not both",
            code="conflicting_session_policy",
            hint="Use --fork-self from inside an Avibe Agent shell, or pass an explicit --fork-session.",
            help_command=help_command,
        )
    if fork_self and session_id:
        raise TaskCliError(
            "use --fork-self without --session-id",
            code="conflicting_session_policy",
            hint="--fork-self resolves the source Session from AVIBE_SESSION_ID.",
            help_command=help_command,
        )
    if (fork_session or fork_self) and (session_id or create_session or create_per_run):
        raise TaskCliError(
            "use fork without --session-id or session creation flags",
            code="conflicting_session_policy",
            hint="Fork creates a new Session from the source Session.",
            help_command=help_command,
        )
    if not (fork_session or fork_self) and (
        (getattr(args, "model", None) or "").strip()
        or (getattr(args, "reasoning_effort", None) or "").strip()
    ):
        raise TaskCliError(
            "--model and --reasoning-effort are only valid with forked Sessions",
            code="fork_override_without_fork",
            hint="Use --agent, --model, and --reasoning-effort as overrides when forking a Session.",
            help_command=help_command,
        )
    if session_id and (same_scope or scope_id):
        raise TaskCliError(
            "scope placement flags only apply when creating a new Session",
            code="scope_with_existing_session",
            hint="An existing --session-id keeps its original scope.",
            help_command=help_command,
        )
    if session_id and visibility:
        raise TaskCliError(
            "visibility options only apply when creating or forking a Session",
            code="visibility_with_existing_session",
            hint="Use `vibe session update --visible|--hidden` (or `--visibility ...`) to change an existing Session.",
            help_command=help_command,
        )
    if session_id and (create_session or create_per_run):
        raise TaskCliError(
            "use either --session-id or --create-session, not both",
            code="conflicting_session_policy",
            help_command=help_command,
        )
    if create_session and create_per_run:
        raise TaskCliError(
            "use either --create-session or --create-session-per-run, not both",
            code="conflicting_session_policy",
            help_command=help_command,
        )
    if create_per_run:
        raise TaskCliError(
            "--create-session-per-run is only valid on stored recurring definitions",
            code="invalid_session_policy",
            hint="Use --create-session for a one-shot agent run.",
            help_command=help_command,
        )
    if fork_session or fork_self:
        return "fork"
    if create_session:
        return "create"
    if session_id:
        return "existing"
    return "none"


def _validate_definition_session_policy(
    args,
    *,
    schedule_type: str | None,
    help_command: str,
    allow_caller_session_default: bool = False,
) -> str:
    session_id = (getattr(args, "session_id", None) or "").strip()
    session_key = (getattr(args, "session_key", None) or "").strip()
    create_session = bool(getattr(args, "create_session", False))
    create_per_run = bool(getattr(args, "create_session_per_run", False))
    deliver_key = (getattr(args, "deliver_key", None) or "").strip()
    scope_id = (getattr(args, "scope_id", None) or "").strip()
    same_scope = bool(getattr(args, "same_scope", False))
    specified = sum(1 for value in (bool(session_id or session_key), create_session, create_per_run) if value)
    if specified > 1:
        raise TaskCliError(
            "use exactly one session policy",
            code="conflicting_session_policy",
            hint="Use --session-id, --create-session, or --create-session-per-run, but not more than one.",
            help_command=help_command,
        )
    if create_per_run and schedule_type == "at":
        raise TaskCliError(
            "--create-session-per-run is invalid for one-shot tasks",
            code="invalid_session_policy",
            hint="Use --create-session for a one-shot task because it only runs once.",
            help_command=help_command,
        )
    if (scope_id or same_scope) and not (create_session or create_per_run):
        raise TaskCliError(
            "scope placement flags only apply when creating Sessions",
            code="scope_without_session_creation",
            hint="Use --create-session or --create-session-per-run with --scope-id/--same-scope, or omit the scope placement flag.",
            help_command=help_command,
        )
    if create_session and not (deliver_key or scope_id or same_scope):
        raise TaskCliError(
            "--scope-id or --same-scope is required when a stored definition creates one reusable Session",
            code="missing_delivery_target",
            hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
            help_command=help_command,
        )
    if create_session:
        return "create_once"
    if create_per_run:
        return "create_per_run"
    if session_id or session_key:
        return "existing"
    if allow_caller_session_default:
        return "existing"
    raise TaskCliError(
        "one session policy is required",
        code="missing_session_policy",
        hint=(
            "Use --session-id to continue a Session, or --create-session with --same-scope/--scope-id to create one. "
            "Inside an Avibe Agent shell, this can continue the current conversation by default."
        ),
        help_command=help_command,
    )


def _definition_session_policy_for_update(
    args,
    *,
    current_policy: Optional[str],
    current_schedule_type: str,
    next_schedule_type: str,
    help_command: str,
) -> str:
    create_session = bool(getattr(args, "create_session", False))
    create_per_run = bool(getattr(args, "create_session_per_run", False))
    session_id = (getattr(args, "session_id", None) or "").strip()
    session_key = (getattr(args, "session_key", None) or "").strip()
    scope_id = (getattr(args, "scope_id", None) or "").strip()
    same_scope = bool(getattr(args, "same_scope", False))
    if create_session and create_per_run:
        raise TaskCliError(
            "use either --create-session or --create-session-per-run, not both",
            code="conflicting_session_policy",
            help_command=help_command,
        )
    if (session_id or session_key) and (create_session or create_per_run):
        raise TaskCliError(
            "use either --session-id or session creation, not both",
            code="conflicting_session_policy",
            help_command=help_command,
        )
    if create_per_run and next_schedule_type == "at":
        raise TaskCliError(
            "--create-session-per-run is invalid for one-shot tasks",
            code="invalid_session_policy",
            hint="Use --create-session for a one-shot task because it only runs once.",
            help_command=help_command,
        )
    if (scope_id or same_scope) and not (create_session or create_per_run):
        raise TaskCliError(
            "scope placement flags only apply when creating Sessions",
            code="scope_without_session_creation",
            hint="Use --create-session or --create-session-per-run with --scope-id/--same-scope, or omit the scope placement flag.",
            help_command=help_command,
        )
    if create_session:
        return "create_once"
    if create_per_run:
        return "create_per_run"
    if session_id or session_key:
        return "existing"
    if current_policy == "create_per_run" and current_schedule_type != next_schedule_type and next_schedule_type == "at":
        raise TaskCliError(
            "--create-session-per-run is invalid for one-shot tasks",
            code="invalid_session_policy",
            hint="Use --create-session when converting this definition to a one-shot task.",
            help_command=help_command,
        )
    return current_policy or "existing"


def _reject_inert_create_once_cwd_update(
    *,
    explicit_cwd: Optional[str],
    current_policy: Optional[str],
    current_session_id: Optional[str],
    create_session: bool,
    has_command: bool = False,
    help_command: str,
) -> None:
    """Refuse a ``--cwd`` that would change nothing, for the tasks where it changes nothing.

    A reusable Session that has already been reserved owns its workdir, so for a
    message task the flag is inert and saying so beats accepting it silently. A command
    task in the same position still has the other question to answer -- where its
    subprocess runs -- and this refusal reached it first, so the only way to repoint a
    nightly command was to replace an escalation Session that had nothing to do with
    the request. Same rule as ``_resolve_definition_session_cwd``'s, softened the same
    way and for the same reason.
    """

    if explicit_cwd is None or create_session or has_command:
        return
    if current_policy == "create_once" and current_session_id:
        raise TaskCliError(
            "--cwd cannot update an already-created reusable Session",
            code="cwd_with_existing_session",
            hint="Pass --create-session with --cwd to reserve a replacement Session, or omit --cwd because the existing Session keeps its own workdir.",
            help_command=help_command,
        )


def _validate_definition_update_delivery_target(
    *,
    session_policy: str,
    session_id: Optional[str],
    session_key: str,
    post_to: Optional[str],
    deliver_key: Optional[str],
    scope_key: Optional[str],
    help_command: str,
):
    return _validate_definition_delivery_target(
        session_policy=session_policy,
        session_id=session_id,
        session_key=session_key,
        post_to=post_to,
        deliver_key=deliver_key,
        scope_key=scope_key,
        help_command=help_command,
    )


def _validate_definition_delivery_target(
    *,
    session_policy: str,
    session_id: Optional[str],
    session_key: str,
    post_to: Optional[str],
    deliver_key: Optional[str],
    scope_key: Optional[str],
    help_command: str,
):
    if session_policy == "create_per_run":
        if not scope_key:
            if post_to or deliver_key:
                raise TaskCliError(
                    "delivery overrides require a scoped Session target",
                    code="missing_delivery_target",
                    hint="Add --scope-id/--same-scope, or omit delivery overrides for a standalone background Session.",
                    help_command=help_command,
                )
            return None, None
        session_target = _parse_validated_scope_id(scope_key, help_command=help_command)
        return _validate_delivery_override_for_target(
            session_target,
            post_to=post_to,
            deliver_key=deliver_key,
            help_command=help_command,
        )
    return _validate_delivery_args(
        session_id=session_id,
        session_key=session_key,
        post_to=post_to,
        deliver_key=deliver_key,
        help_command=help_command,
    )


def _resolve_run_cwd(
    args,
    *,
    session_policy: str,
    scoped_session: bool = False,
    invocation_cwd_default: bool = False,
    help_command: str,
) -> Optional[str]:
    """Working directory for a session this run RESERVES.

    An explicit ``--cwd`` must exist and always wins for blank session creation.
    Without it, explicit scope placement snapshots the scope default. A new
    delegated Session follows the caller shell cwd; a caller-less standalone
    Session gets its own Show workspace from the reservation service.
    Existing and forked sessions keep their own cwd, so ``--cwd`` is an error.
    """
    raw = (getattr(args, "cwd", None) or "").strip()
    if session_policy in {"existing", "fork"}:
        if raw:
            raise TaskCliError(
                "--cwd only applies when this run creates a blank session",
                code="cwd_with_existing_session",
                hint="Existing and forked Sessions keep their own working directory.",
                help_command=help_command,
            )
        return None
    if raw:
        resolved = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(resolved):
            raise TaskCliError(
                f"--cwd directory does not exist: {resolved}",
                code="cwd_not_found",
                hint="Point --cwd to an existing directory, or omit it to use the session target's default workdir.",
                help_command=help_command,
            )
        return resolved
    if scoped_session:
        return None
    return os.getcwd() if invocation_cwd_default else None


def _resolve_definition_session_cwd(
    *,
    explicit_cwd: Optional[str],
    existing_cwd: Optional[str],
    session_policy: str,
    scoped_session: bool = False,
    has_command: bool = False,
    help_command: str,
) -> Optional[str]:
    """Where a Session this definition CREATES should run. Never the command's answer.

    The ``existing`` refusal states a rule that is still true -- a Session bound here
    owns its own directory, and a definition pointing at one must not rewrite it. What
    it must not also do is refuse the OTHER question. A command task binds to an
    existing Session for a reason unrelated to where it runs (``--on-failure agent``
    needs somewhere to escalate), so the refusal landed on ``--cwd`` as collateral and
    left the command with no way to say where it spawns.

    ``has_command`` therefore softens the refusal rather than widening the return: this
    still answers only the Session question, and answers it ``None``, so nothing writes
    a workdir onto a Session that owns one. ``_command_definition_spawn_cwd`` picks the
    flag up as the command's half.
    """

    raw = (explicit_cwd or "").strip()
    if session_policy == "existing":
        if raw and not has_command:
            raise TaskCliError(
                "--cwd only applies when this definition creates new Sessions",
                code="cwd_with_existing_session",
                hint="An existing target Session keeps its own working directory.",
                help_command=help_command,
            )
        return None
    if raw:
        return _resolve_existing_cwd(raw, help_command=help_command, code="cwd_not_found", label="task")
    if existing_cwd:
        return existing_cwd
    if scoped_session:
        return None
    if session_policy == "create_per_run":
        return None
    return os.getcwd()


def _command_definition_spawn_cwd(
    session_cwd: Optional[str],
    *,
    has_command: bool,
    session_policy: str,
    explicit_cwd: Optional[str] = None,
    stored_cwd: Optional[str] = None,
    help_command: str = "vibe task add --help",
) -> Optional[str]:
    """Where this definition's COMMAND runs, given where its Session would run.

    ``_resolve_definition_session_cwd`` answers a Session question, and declines to
    answer it for ``create_per_run``: that Session does not exist yet, so creation
    resolves its workdir later from the Scope or the runtime default. A command cannot
    wait for that. It runs on the next tick from the definition's ``cwd``, and with
    nothing recorded there it fell through to the ``~/.avibe`` fallback -- so
    ``--shell './scripts/sync.sh'`` ran from the product state directory, where the
    relative path is missing and a relative write lands in persisted state.

    The invocation directory is the answer, for the same reason a pure command task
    already records it: it is where the user was standing when they described the
    command. Returned only for the policy that leaves the command with no other source
    -- an ``existing`` binding is read live from its Session at fire time, and
    ``create_once`` reserves its Session immediately -- and never over an explicit
    ``--cwd``.

    ``explicit_cwd`` is that flag arriving for the one policy whose Session question
    answers ``None`` on purpose. Every other policy has already folded it into
    ``session_cwd``, where it is BOTH answers at once: a created Session and its
    command run in the same place. An ``existing`` binding is the case where the two
    genuinely differ -- the Session keeps its directory, the command gets the one the
    user named -- so the flag is resolved here, under the same ``cwd_not_found`` check
    every other policy gives it, and stored as the definition's ``cwd``. Fire time
    already prefers that over the bound Session's live workdir
    (``_bound_session_workdir``), so omitting the flag keeps today's
    inherit-from-Session behaviour untouched.

    ``stored_cwd`` is what an UPDATE must not drop. A bound definition resolves its
    Session question to ``None`` on every edit, and the update path persists that with
    ``update_cwd=True`` -- so without this, renaming an escalating command task would
    silently un-pin the directory it was created with, and the next fire would go back
    to following the Session. Only the explicit flag replaces it, and it outranks the
    invocation directory for every policy rather than only for ``existing``: a policy
    change is not a request to move the command, and re-stamping the directory the
    UPDATE happened to run from is the same silent relocation in a different lane.
    It outranks ``session_cwd`` for the same reason. The two are equal wherever
    ``--cwd`` set both, and differ exactly where the user separated them -- a bound
    command moved to B while its Session keeps A -- so reading the Session half first
    let a later ``--create-session*`` carry A forward onto the command and move it
    back. ``stored_cwd`` defaults to ``None``, so ``task add`` -- which has nothing
    stored yet -- resolves exactly as it did before.
    """

    if not has_command:
        return session_cwd
    raw = (explicit_cwd or "").strip()
    if raw:
        # The flag is the command's answer wherever it arrives. A creating policy has
        # already folded it into ``session_cwd`` -- same directory, both halves -- so
        # resolving it again here returns the same path; an ``existing`` binding answers
        # its Session question ``None`` on purpose, and this is the only place the flag
        # can be picked up at all.
        return _resolve_existing_cwd(raw, help_command=help_command, code="cwd_not_found", label="task")
    if stored_cwd:
        # Before ``session_cwd``, because the two can legitimately differ and only one of
        # them is an answer to this question. Once ``--cwd`` moves a bound command to B
        # while its Session keeps A, a later ``--create-session*`` carrying A forward
        # resolved the command to A as well -- moving it back, with nothing in the edit
        # asking to. Same rule as ``_resolve_command_only_cwd``'s and SCT-051's: only the
        # explicit flag replaces a stored directory.
        return stored_cwd
    if session_cwd:
        return session_cwd
    if session_policy == "create_per_run":
        return os.getcwd()
    return None


def _resolve_command_only_cwd(
    explicit_cwd: Optional[str],
    *,
    stored_cwd: Optional[str],
    help_command: str,
) -> Optional[str]:
    """``--cwd`` for an edit that answers the command question and nothing else.

    Under the same ``cwd_not_found`` check every other policy gives the flag. Omitting
    it keeps the stored directory rather than re-deriving one, because a definition
    whose Session is already settled has no other source to fall back to and the
    invocation directory of an unrelated edit is not an answer anybody asked for.
    """

    raw = (explicit_cwd or "").strip()
    if raw:
        return _resolve_existing_cwd(raw, help_command=help_command, code="cwd_not_found", label="task")
    return stored_cwd


def _stored_session_workdir(task, metadata: Optional[dict]) -> Optional[str]:
    """The SESSION half of what a definition already stores, for a policy change.

    Retargeting at ``--create-session``/``--create-session-per-run`` without naming a
    directory carries forward the one the definition already had. For a message task
    ``task.cwd`` IS that directory: the Session question and the run question are the
    same question, answered once at ``task add``.

    For a command task they can differ, and since the ``existing`` refusal was softened
    they routinely do -- ``task.cwd`` is where the COMMAND runs. The Session half lives
    in ``metadata["session_workdir"]``, or is deliberately absent: a bound definition
    never had one, and a per-run definition leaves it unset on purpose so an
    escalation's Session follows its Scope (SCT-047). Reading ``task.cwd`` for it
    promotes a directory the user picked for a subprocess into a Session placement they
    never asked for, and the newly created Session stops inheriting from its Scope with
    nothing in the edit saying so. ``_command_definition_spawn_cwd`` keeps the command
    half from its own ``stored_cwd``, so the two survive the retarget separately.
    """

    if not task.has_command:
        return task.cwd
    return str((metadata or {}).get("session_workdir") or "").strip() or None


def _has_modern_scope_target(args) -> bool:
    return bool((getattr(args, "scope_id", None) or "").strip()) or bool(getattr(args, "same_scope", False))


def _session_creation_metadata_from_caller(caller_context) -> dict:
    if caller_context is None:
        return {}
    return {
        "created_by": {
            "kind": "caller_context",
            "caller": caller_context.to_metadata(),
        }
    }


def _definition_creation_metadata_from_caller(caller_context) -> dict:
    return _session_creation_metadata_from_caller(caller_context)


def _agent_run_source_from_caller(caller_context) -> tuple[str, Optional[str], Optional[str], dict]:
    if caller_context is None:
        return "cli", None, None, {}
    metadata = {"caller_context": caller_context.to_metadata()}
    return "agent", caller_context.session_id, caller_context.run_id, metadata


def _agent_run_is_async(args) -> bool:
    return not bool(getattr(args, "sync_run", False))


def _resolve_callback_session_id(args, caller_context, *, target_session_id: Optional[str] = None):
    explicit_callback = (getattr(args, "callback_session_id", None) or "").strip() or None
    no_callback = bool(getattr(args, "no_callback", False))
    is_async = _agent_run_is_async(args)
    if explicit_callback:
        return explicit_callback, {
            "code": "callback_explicit",
            "message": (
                "Callback route recorded for this Agent Run."
                if not is_async
                else "Async callback will be sent to the explicit Session."
            ),
            "callback_session_id": explicit_callback,
        }
    if no_callback:
        message = (
            "Started async Agent Run without a callback. This run will not post its final result back into a "
            "Session automatically. Track it with `vibe runs show <run-id>` or by polling/listing runs for the "
            "target Session. To receive a follow-up message next time, use `--callback-session-id <session-id>` "
            "or run from a resolved caller context so Avibe can default the callback to the current Session."
        )
        if not is_async:
            message = (
                "Recorded an explicit no-callback policy. This synchronous run will not send a callback if it later "
                "detaches into an asynchronous background run."
            )
        return None, {
            "code": "async_run_without_callback",
            "message": message,
        }
    if caller_context is not None:
        caller_session_id = caller_context.session_id
        if target_session_id and target_session_id == caller_session_id:
            return None, {
                "code": "async_self_run_without_callback",
                "message": (
                    "Started Agent Run on the caller Session without a callback. "
                    "The target Session will receive the run result directly, so Avibe did not create a duplicate callback turn."
                ),
            }
        return caller_context.session_id, {
            "code": "callback_defaulted_to_caller_session",
            "message": (
                "Async callback defaulted to this conversation."
                if is_async
                else "Callback route defaulted to this conversation."
            ),
            "callback_session_id": caller_session_id,
        }
    if not is_async:
        return None, None
    raise TaskCliError(
        "This async Agent Run has no callback target.",
        code="missing_async_callback",
        hint=(
            "Pass --callback-session-id <session-id> to send the final result back to a specific Agent Session, "
            "or pass --no-callback to run without an automatic follow-up and inspect the result later with "
            "`vibe runs show <run-id>` or by polling/listing runs for the target Session. "
            "`--callback-session-id` identifies the Session that should receive the delegated run's final result."
        ),
        help_command="vibe agent run --help",
    )


def _reserve_cli_session(
    *,
    agent,
    scope_key: Optional[str],
    workdir: Optional[str] = None,
    metadata: Optional[dict] = None,
    session_anchor_target=None,
    visibility: str = "background",
) -> str:
    # Route through ``core.services.sessions`` so the CLI shares the same
    # business API as the UI server and the future N3 internal endpoint;
    # see docs/plans/workbench-dispatch-architecture.md §6 (C2).
    from core.services import sessions as sessions_service

    if scope_key:
        target = _parse_validated_scope_id(scope_key, help_command="vibe agent run --help")
        anchor_target = session_anchor_target or target
        session_anchor = _session_anchor_with_suffix(anchor_target, suffix="run")
        session_id = sessions_service.reserve_agent_session(
            scope_key=target.session_scope,
            agent_backend=agent.backend,
            session_anchor=session_anchor,
            agent_id=agent.id,
            agent_name=agent.name,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            workdir=workdir,
            visibility=visibility,
            metadata={"scope_placement": "explicit", **dict(metadata or {})},
            require_enabled_agent=True,
        )
    else:
        session_anchor = f"standalone_{uuid4().hex[:12]}"
        session_id = sessions_service.reserve_standalone_agent_session(
            agent_backend=agent.backend,
            session_anchor=session_anchor,
            agent_id=agent.id,
            agent_name=agent.name,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            workdir=workdir,
            visibility=visibility,
            metadata=metadata,
            require_enabled_agent=True,
        )
    if not session_id:
        raise TaskCliError(
            "failed to reserve a new Agent Session ID",
            code="session_reservation_failed",
            help_command="vibe agent run --help",
        )
    return session_id


def _session_anchor_with_suffix(target, *, suffix: str) -> str:
    return f"{session_anchor_for_target(target)}:{suffix}_{uuid4().hex[:12]}"


def _sync_run_detached(run_payload: dict) -> bool:
    return (
        run_payload.get("wait_state") == "detached"
        or run_payload.get("handoff_reason") == "wait_limit_reached"
        or normalize_run_status(run_payload.get("status")) not in {"succeeded", "failed", "canceled"}
    )


def _reserve_forked_cli_session(
    *,
    source_session_id: str,
    agent_name: Optional[str],
    model: Optional[str],
    reasoning_effort: Optional[str],
    scope_key: Optional[str],
    visibility: str,
):
    from core.services.session_fork import (
        SESSION_AGENT_UNAVAILABLE_CODE,
        SESSION_AGENT_UNAVAILABLE_I18N_KEY,
        SessionForkError,
        reserve_forked_session,
    )

    try:
        return reserve_forked_session(
            source_session_id=source_session_id,
            agent_name=agent_name or None,
            model=model,
            reasoning_effort=reasoning_effort,
            scope_id=scope_key,
            visibility=visibility,
            db_path=paths.get_sqlite_state_path(),
        )
    except SessionForkError as exc:
        if exc.code == SESSION_AGENT_UNAVAILABLE_CODE:
            try:
                lang = V2Config.load().language
            except Exception:
                lang = "en"
            key = SESSION_AGENT_UNAVAILABLE_I18N_KEY
            raise TaskCliError(
                i18n_t(f"{key}.message", lang),
                code=exc.code,
                hint=i18n_t(f"{key}.hint", lang),
                help_command="vibe agent run --help",
                details={"source_session_id": source_session_id, **exc.details},
            ) from exc
        raise TaskCliError(
            str(exc),
            code="session_fork_failed",
            hint="Fork requires a bound source Session and, when overriding --agent, the same backend.",
            help_command="vibe agent run --help",
            details={"source_session_id": source_session_id},
        ) from exc


def _reserve_definition_session(
    *,
    agent_name: Optional[str],
    agent_id: Optional[str] = None,
    deliver_key: str,
    help_command: str,
    workdir: Optional[str] = None,
    require_enabled_agent: bool = False,
    expected_reference_agent_id: Optional[str] = None,
) -> str:
    from core.services import sessions as sessions_service

    try:
        target = _parse_validated_scope_id(deliver_key, help_command=help_command)
    except TaskCliError:
        target = _parse_validated_session_key(deliver_key, help_command=help_command)
    agent = _resolve_agent_for_session_reservation(
        agent_name=agent_name,
        agent_id=agent_id,
        deliver_key=deliver_key,
        help_command=help_command,
    )
    if agent is None:
        raise TaskCliError(
            "no enabled default Agent is available for session creation",
            code="default_agent_unavailable",
            hint="Create or enable a default Agent before creating sessions without --agent.",
            help_command=help_command,
        )
    agent_backend = agent.backend
    session_anchor = _session_anchor_with_suffix(target, suffix="definition")
    session_id = sessions_service.reserve_agent_session(
        scope_key=target.session_scope,
        agent_backend=agent_backend,
        session_anchor=session_anchor,
        agent_id=agent.id if agent else None,
        agent_name=agent.name if agent else None,
        model=agent.model if agent else None,
        reasoning_effort=agent.reasoning_effort if agent else None,
        workdir=workdir,
        visibility="foreground",
        require_enabled_agent=require_enabled_agent,
        expected_reference_agent_id=expected_reference_agent_id,
    )
    if not session_id:
        raise TaskCliError(
            "failed to reserve a new Agent Session ID",
            code="session_reservation_failed",
            help_command=help_command,
        )
    return session_id


def _release_cli_session_reservation(session_id: str, *, reason: str) -> bool:
    """Release only the unadopted Session reserved by a failed CLI mutation."""

    from storage.sessions_service import SQLiteSessionsService

    service: Optional[SQLiteSessionsService] = None
    try:
        service = SQLiteSessionsService(paths.get_sqlite_state_path())
        return service.release_reserved_agent_session(session_id, reason=reason)
    except Exception:
        logger.exception(
            "Could not release the reserved Agent Session %s after a failed CLI mutation",
            session_id,
        )
        return False
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                logger.exception(
                    "Could not close the Session store after releasing reservation %s",
                    session_id,
                )


def cmd_agent_run(args):
    reserved_session_id: Optional[str] = None
    try:
        caller_context = caller_context_from_env()
        visibility = (getattr(args, "visibility", None) or "background").strip()
        run_async = _agent_run_is_async(args)
        message = _resolve_message_input(
            args,
            help_command="vibe agent run --help",
            example_command="vibe agent run --agent default",
        )
        session_policy = _validate_run_session_policy(args, help_command="vibe agent run --help")
        delivery_intent = (
            AGENT_RUN_DELIVERY_QUEUE
            if bool(getattr(args, "queue", False))
            else AGENT_RUN_DELIVERY_STEER
        )
        if bool(getattr(args, "send_now", False)) and session_policy != "existing":
            raise TaskCliError(
                "--send-now requires an existing Agent Session",
                code="send_now_requires_existing_session",
                hint="Pass --session-id <session-id>, or omit --send-now for a new or forked Session.",
                help_command="vibe agent run --help",
            )
        agent_name = (args.agent or "").strip()
        if session_policy in {"create", "none"} and not agent_name:
            raise TaskCliError(
                "--agent is required when running without an existing --session-id",
                code="missing_agent",
                hint="Pass --agent with the Avibe Agent name to run.",
                help_command="vibe agent run --help",
            )
        source_session_id = (args.fork_session or "").strip() or None
        if bool(getattr(args, "fork_self", False)):
            source_session_id = _require_caller_session_id(
                caller_context,
                purpose="--fork-self",
                help_command="vibe agent run --help",
            )
            setattr(args, "fork_session", source_session_id)
        if session_policy == "none" and (args.deliver_key or args.post_to):
            raise TaskCliError(
                "delivery options require an explicit Session target",
                code="delivery_target_without_session_policy",
                hint="Use --same-scope or --scope-id for new Session placement.",
                help_command="vibe agent run --help",
            )
        if session_policy == "fork" and args.post_to:
            raise TaskCliError(
                "delivery options require an existing Session target",
                code="delivery_target_without_session_policy",
                hint="Fork creates a new Session. Use scope placement flags for where it lives; callback controls where results return.",
                help_command="vibe agent run --help",
            )
        session_id = (args.session_id or "").strip() or None
        session_key = ""
        scope_key = (
            _resolve_agent_run_scope_key(
                args,
                caller_context=caller_context,
                source_session_id=source_session_id,
            )
            if session_policy != "existing"
            else None
        )
        has_resolved_caller_placement = False
        if caller_context is not None and session_policy != "existing":
            try:
                resolve_session_id_target(caller_context.session_id)
                has_resolved_caller_placement = True
            except ValueError:
                pass
        legacy_reservation_target = None
        if not scope_key and (args.deliver_key or "").strip():
            # Hidden legacy compatibility: external docs and prompts should use
            # --scope-id/--same-scope, while old callers still map into the same
            # internal placement field.
            legacy_reservation_target = _parse_validated_session_key(args.deliver_key, help_command="vibe agent run --help")
            scope_key = legacy_reservation_target.session_scope
        run_cwd = _resolve_run_cwd(
            args,
            session_policy=session_policy,
            scoped_session=bool(
                _has_modern_scope_target(args) or (args.deliver_key or "").strip()
            ),
            invocation_cwd_default=has_resolved_caller_placement
            and not _has_modern_scope_target(args)
            and not (args.deliver_key or "").strip(),
            help_command="vibe agent run --help",
        )
        agent = _agent_store().require_enabled(agent_name) if agent_name else None
        fork_result = None
        session_metadata = _session_creation_metadata_from_caller(caller_context)
        if session_policy in {"existing", "fork"} and session_id:
            target = resolve_session_id_target(session_id)
            session_key = target.session_key.to_key()
            agent = _resolve_agent_for_target(
                agent_name=agent_name or None,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe agent run --help",
            )
        if session_policy == "existing" and (args.post_to or args.deliver_key):
            _validate_delivery_args(
                session_id=session_id,
                session_key=session_key,
                post_to=args.post_to,
                deliver_key=args.deliver_key,
                help_command="vibe agent run --help",
            )
        callback_session_id, callback_notice = _resolve_callback_session_id(args, caller_context, target_session_id=session_id)
        if callback_session_id:
            _validate_callback_session_id(callback_session_id, help_command="vibe agent run --help")
        legacy_deliver_key = args.deliver_key
        if (getattr(args, "same_scope", False) or (getattr(args, "scope_id", None) or "").strip()) and legacy_deliver_key != scope_key:
            legacy_deliver_key = None
        if session_policy == "create":
            session_id = _reserve_cli_session(
                agent=agent,
                scope_key=scope_key,
                workdir=run_cwd,
                metadata=session_metadata,
                session_anchor_target=legacy_reservation_target,
                visibility=visibility,
            )
            reserved_session_id = session_id
        elif session_policy == "none":
            session_id = _reserve_cli_session(
                agent=agent,
                scope_key=scope_key,
                workdir=run_cwd,
                metadata=session_metadata,
                visibility=visibility,
            )
            reserved_session_id = session_id
        elif session_policy == "fork":
            fork_result = _reserve_forked_cli_session(
                source_session_id=source_session_id or "",
                agent_name=agent_name or None,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                scope_key=scope_key,
                visibility=visibility,
            )
            session_id = fork_result.session_id
            reserved_session_id = session_id
            if agent_name:
                agent = _agent_store().require_enabled(agent_name)
        if session_id and not session_key:
            target = resolve_session_id_target(session_id)
            session_key = target.session_key.to_key()
            agent = _resolve_agent_for_target(
                agent_name=agent_name or None,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe agent run --help",
            )
        if (session_policy in {"create", "none"} or fork_result) and args.post_to:
            _validate_delivery_args(
                session_id=session_id,
                session_key=session_key,
                post_to=args.post_to,
                deliver_key=None,
                help_command="vibe agent run --help",
            )
        source_kind, source_actor, parent_run_id, provenance_metadata = _agent_run_source_from_caller(caller_context)
        if fork_result:
            provenance_metadata = {
                **provenance_metadata,
                "session_fork": fork_result.fork.to_metadata(),
            }
        request_store = _task_request_store()
        request = request_store.enqueue_agent_run(
            agent_name=agent.name if agent else None,
            agent_id=agent.id if agent else None,
            agent_backend=agent.backend if agent else None,
            model=fork_result.model if fork_result else (agent.model if agent else None),
            reasoning_effort=(
                fork_result.reasoning_effort if fork_result else (agent.reasoning_effort if agent else None)
            ),
            session_policy=session_policy,
            session_key=session_key,
            session_id=session_id,
            post_to=args.post_to,
            deliver_key=legacy_deliver_key,
            message=message,
            source_kind=source_kind,
            source_actor=source_actor,
            parent_run_id=parent_run_id,
            callback_session_id=callback_session_id,
            callback_active=run_async,
            delivery_intent=delivery_intent,
            metadata=provenance_metadata or None,
            expected_enabled_agent_id=(agent.id if agent is not None and bool(agent_name) else None),
        )
        reserved_session_id = None
        resolved_scope_id = _scope_id_payload_from_session(session_id)
        payload = {
            "accepted": True,
            "request_type": request.request_type,
            "run_id": request.id,
            "execution_id": request.id,
            "agent": agent.name if agent else None,
            "session_policy": session_policy,
            "session_id": session_id,
            "scope_id": resolved_scope_id,
            "visibility": target.visibility if session_id else visibility,
            "deliver_key": legacy_deliver_key,
            "callback_session_id": callback_session_id,
            "async": run_async,
            "caller_context": caller_context.to_metadata() if caller_context else None,
            "callback_notice": callback_notice,
            "run": {
                "id": request.id,
                "status": "queued",
                "run_type": request.request_type,
                "agent_name": agent.name if agent else None,
                "session_id": session_id,
                "scope_id": resolved_scope_id,
                "visibility": target.visibility if session_id else visibility,
                "callback_session_id": callback_session_id,
                "source_kind": source_kind,
                "source_actor": source_actor,
                "parent_run_id": parent_run_id,
            },
        }
        if bool(getattr(args, "send_now", False)) or delivery_intent != AGENT_RUN_DELIVERY_STEER:
            payload["delivery_intent"] = delivery_intent
            payload["run"]["delivery_intent"] = delivery_intent
        if fork_result:
            payload["forked_from_session_id"] = fork_result.fork.source_session_id
        if fork_result:
            payload["run"]["forked_from_session_id"] = fork_result.fork.source_session_id
        if not run_async:
            run_payload = _wait_for_run_result(request_store, request.id, wait_timeout=args.wait_timeout)
            if callback_session_id and _sync_run_detached(run_payload):
                request_store.mark_callback_pending(request.id)
                run_payload["callback_status"] = "pending"
            payload["run"] = run_payload
        _print_cli_payload("agent_run", **payload)
        return 0
    except Exception as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="Agent Run enqueue failed before its Session reservation was adopted",
            )
        _print_task_error(exc, help_command="vibe agent run --help")
        return 1


def _wait_for_run_result(store: TaskExecutionStore, run_id: str, *, wait_timeout: Optional[float]) -> dict:
    started = time.monotonic()
    max_wait = wait_timeout if wait_timeout is not None else 1800.0
    while True:
        run = store.get_run(run_id)
        if run and normalize_run_status(run.get("status")) in {"succeeded", "failed", "canceled"}:
            return _run_payload(run)
        elapsed = time.monotonic() - started
        if elapsed >= max_wait:
            run = run or {"id": run_id}
            run["wait_state"] = "detached"
            run["handoff_reason"] = "wait_limit_reached"
            run["wait_elapsed_seconds"] = round(elapsed, 3)
            run["accepted"] = True
            run["async"] = True
            return _run_payload(run)
        time.sleep(0.25)


def cmd_runs_list(args):
    try:
        page_request = _page_request_from_args(args, help_command="vibe runs list --help")
        created_after = _parse_cli_time_filter(
            getattr(args, "created_after", None),
            field_name="--created-after",
            help_command="vibe runs list --help",
        )
        created_before = _parse_cli_time_filter(
            getattr(args, "created_before", None),
            field_name="--created-before",
            help_command="vibe runs list --help",
        )
        result = _task_request_store().list_runs_page(
            status=getattr(args, "status", None),
            run_type=getattr(args, "type", None),
            agent_name=getattr(args, "agent", None),
            agent_backend=getattr(args, "backend", None),
            session_id=_resolve_runs_list_session_filter(args),
            definition_id=getattr(args, "definition_id", None),
            created_after=created_after,
            created_before=created_before,
            query=getattr(args, "query", None),
            page_request=page_request,
            newest_first=True,
        )
        command = ["vibe", "runs", "list"]
        _add_optional_arg(command, "--status", getattr(args, "status", None))
        _add_optional_arg(command, "--type", getattr(args, "type", None))
        _add_optional_arg(command, "--agent", getattr(args, "agent", None))
        _add_optional_arg(command, "--backend", getattr(args, "backend", None))
        _add_optional_arg(command, "--session-id", getattr(args, "session_id", None))
        if getattr(args, "current_session", False):
            command.append("--current-session")
        _add_optional_arg(command, "--definition-id", getattr(args, "definition_id", None))
        _add_optional_arg(command, "--created-after", created_after)
        _add_optional_arg(command, "--created-before", created_before)
        _add_optional_arg(command, "--q", getattr(args, "query", None))
        if getattr(args, "brief", False):
            command.append("--brief")
        payload = {
            "runs": [_run_payload(run, brief=True) for run in result.items],
            **_paginated_fields(result, command=command),
        }
        _print_cli_payload("agent_runs", **payload)
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe runs list --help")
        return 1


def cmd_runs_show(args):
    try:
        run_id, run_default_notice = _resolve_caller_run_id(
            args,
            purpose="Run",
            help_command="vibe runs show --help",
        )
    except Exception as exc:
        _print_task_error(exc, help_command="vibe runs show --help")
        return 1
    run = _task_request_store().get_run(run_id)
    if run is None:
        _print_task_error(TaskCliError(f"run '{run_id}' not found", code="run_not_found", details={"run_id": run_id}))
        return 1
    run_payload = _run_payload(run)
    session_runtime = _live_session_runtime_for_run(run_payload)
    if session_runtime is not None:
        run_payload["session_runtime"] = session_runtime
    payload_fields = {"run": run_payload}
    if run_default_notice:
        payload_fields["run_default_notice"] = run_default_notice
    _print_cli_payload("agent_run", **payload_fields)
    return 0


def _live_session_runtime_for_run(run: dict) -> dict | None:
    """Attach authoritative controller ownership to an active Session Run."""

    if normalize_run_status(run.get("status")) not in {"queued", "running"}:
        return None
    session_id = _run_session_id(run)
    if not session_id:
        return None
    from vibe import internal_client

    try:
        result = asyncio.run(internal_client.turn_state(session_id))
    except internal_client.InternalServerTimeout:
        return {"available": False, "reason": "controller_probe_timeout"}
    except internal_client.InternalServerUnavailable:
        return {"available": False, "reason": "controller_unavailable"}
    except Exception:
        logger.debug("Failed to inspect live Session owner for Run %s", run.get("id"), exc_info=True)
        return {"available": False, "reason": "controller_probe_failed"}
    body = result.get("body") if isinstance(result, dict) else None
    if result.get("status_code") != 200 or not isinstance(body, dict):
        return {"available": False, "reason": "controller_probe_rejected"}
    return {"available": True, **body}


def _run_type(run: dict | None) -> str:
    if not isinstance(run, dict):
        return ""
    return str(run.get("run_type") or run.get("request_type") or "").strip()


def _run_session_id(run: dict | None) -> str:
    if not isinstance(run, dict):
        return ""
    return str(run.get("session_id") or "").strip()


def _should_attempt_live_run_cancel(run: dict | None) -> bool:
    if not isinstance(run, dict):
        return False
    return (
        _run_type(run) == "agent_run"
        and normalize_run_status(run.get("status")) == "running"
        and bool(_run_session_id(run))
    )


def _recorded_only_cancel_result(*, reason_code: str, detail: object | None = None) -> dict:
    result = {
        "code": "cancel_request_recorded_only",
        "live_cancel_attempted": reason_code not in {"not_running_agent_run", "missing_session_id"},
        "live_cancel_confirmed": False,
        "reason_code": reason_code,
        "message": "Cancel request was recorded, but no live backend turn was stopped.",
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _record_live_cancel_fallback(
    store: TaskExecutionStore,
    run_id: str,
    *,
    reason_code: str,
    detail: object | None = None,
) -> dict:
    store.cancel_run(run_id)
    return _recorded_only_cancel_result(reason_code=reason_code, detail=detail)


def _initial_cancel_result(run: dict | None) -> dict:
    if not isinstance(run, dict):
        return _recorded_only_cancel_result(reason_code="run_not_found")
    status = normalize_run_status(run.get("status"))
    if status == "queued":
        return {
            "code": "queued_canceled",
            "live_cancel_attempted": False,
            "live_cancel_confirmed": False,
            "message": "Queued run was canceled before it started.",
        }
    if _run_type(run) != "agent_run" or status != "running":
        return _recorded_only_cancel_result(reason_code="not_running_agent_run")
    if not _run_session_id(run):
        return _recorded_only_cancel_result(reason_code="missing_session_id")
    return {
        "code": "cancel_request_recorded",
        "live_cancel_attempted": False,
        "live_cancel_confirmed": False,
        "message": "Cancel request was recorded.",
    }


def _live_cancel_failure_code(status_code: int | None, body: object) -> str:
    body_code = ""
    body_status = ""
    if isinstance(body, dict):
        body_code = str(body.get("code") or "").strip()
        body_status = str(body.get("status") or "").strip()
    if body_code == "not_in_flight" or status_code == 404:
        return "not_in_flight"
    if body_code == "stop_failed" or status_code == 409:
        return "stop_failed"
    if body_status:
        return body_status
    if status_code is None:
        return body_code or "live_cancel_failed"
    if status_code >= 500:
        return body_code or "internal_error"
    return body_code or "live_cancel_not_confirmed"


def _live_cancel_was_confirmed(status_code: int | None, body: object) -> bool:
    if status_code is None or status_code < 200 or status_code >= 300:
        return False
    if isinstance(body, dict) and body.get("ok") is False:
        return False
    if not isinstance(body, dict):
        return False
    return str(body.get("status") or "").strip() in {"cancel_requested", "stale_released"}


async def _request_live_run_cancel(session_id: str, run_id: str) -> dict:
    from vibe import internal_client

    return await internal_client.cancel_dispatch(session_id, run_id=run_id)


def _cancel_live_agent_run(store: TaskExecutionStore, run: dict) -> dict:
    session_id = _run_session_id(run)
    run_id = str(run.get("id") or "").strip()
    from vibe import internal_client

    try:
        controller_result = asyncio.run(_request_live_run_cancel(session_id, run_id))
    except internal_client.InternalServerUnavailable as exc:
        return _record_live_cancel_fallback(
            store,
            run_id,
            reason_code="internal_unavailable",
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        return _record_live_cancel_fallback(
            store,
            run_id,
            reason_code="live_cancel_failed",
            detail=str(exc),
        )

    status_code = controller_result.get("status_code")
    try:
        normalized_status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        normalized_status_code = None
    body = controller_result.get("body") or {}
    if (
        normalized_status_code is not None
        and 200 <= normalized_status_code < 300
        and isinstance(body, dict)
        and str(body.get("status") or "").strip() == "run_detached"
    ):
        saved = store.get_run(run_id)
        return {
            "code": "run_canceled_without_live_stop",
            "live_cancel_attempted": False,
            "live_cancel_confirmed": False,
            "run_terminalized": bool(
                saved and normalize_run_status(saved.get("status")) == "canceled"
            ),
            "controller_status_code": normalized_status_code,
            "controller_response": body,
            "message": "Run was canceled without stopping the shared Session turn.",
        }
    if (
        normalized_status_code is not None
        and 200 <= normalized_status_code < 300
        and isinstance(body, dict)
        and str(body.get("status") or "").strip() == "run_settled"
    ):
        return {
            "code": "run_already_settled",
            "live_cancel_attempted": False,
            "live_cancel_confirmed": False,
            "run_terminalized": False,
            "controller_status_code": normalized_status_code,
            "controller_response": body,
            "message": "Run had already settled before cancellation acquired ownership.",
        }
    if not _live_cancel_was_confirmed(normalized_status_code, body):
        return _record_live_cancel_fallback(
            store,
            run_id,
            reason_code=_live_cancel_failure_code(normalized_status_code, body),
            detail={
                "controller_status_code": normalized_status_code,
                "controller_response": body,
            },
        )

    run_terminalized = store.mark_run_canceled(run_id)
    return {
        "code": "live_cancel_confirmed",
        "live_cancel_attempted": True,
        "live_cancel_confirmed": True,
        "run_terminalized": run_terminalized,
        "controller_status_code": normalized_status_code,
        "controller_response": body,
        "message": "Live backend turn was stopped and the run was marked canceled.",
    }


def cmd_runs_cancel(args):
    store = _task_request_store()
    existing = store.get_run(args.run_id)
    if existing is None:
        _print_task_error(TaskCliError(f"run '{args.run_id}' not found", code="run_not_found", details={"run_id": args.run_id}))
        return 1
    if _should_attempt_live_run_cancel(existing):
        cancel_result = _cancel_live_agent_run(store, existing)
    else:
        canceled = store.cancel_run(args.run_id)
        if not canceled:
            _print_task_error(TaskCliError(f"run '{args.run_id}' not found", code="run_not_found", details={"run_id": args.run_id}))
            return 1
        cancel_result = _initial_cancel_result(existing)
    run = store.get_run(args.run_id)
    _print_cli_payload(
        "agent_run",
        cancel_requested=True,
        cancel_code=cancel_result["code"],
        cancel_result=cancel_result,
        run=_run_payload(run or {"id": args.run_id}),
    )
    return 0


def _format_byte_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PiB"


def cmd_data_retention(args):
    from storage import agent_events_retention
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state

    try:
        ensure_sqlite_state()
        engine = create_sqlite_engine()
        language = _configured_cli_language()
        days_override = getattr(args, "days", None)
        # The configured window is the default; --days overrides for this call.
        # A recovered/unreadable config must not silently run with substituted
        # defaults: deletion is irreversible, so the run refuses unless the
        # user supplies an explicit --days window.
        policy = agent_events_retention.RetentionPolicy(
            enabled=False,
            days=agent_events_retention.DEFAULT_RETENTION_DAYS,
            recovered=True,
        )
        try:
            config = V2Config.load()
            policy = agent_events_retention.resolve_policy(config)
        except Exception:
            # Unreadable/missing config: the controller disables the
            # automatic pass, so the status must not claim it is enabled.
            policy = agent_events_retention.RetentionPolicy(
                enabled=False,
                days=agent_events_retention.DEFAULT_RETENTION_DAYS,
                recovered=True,
            )
        retention_days = policy.days
        enabled = policy.enabled
        config_recovered = policy.recovered
        if days_override is not None:
            retention_days = int(days_override)
            try:
                agent_events_retention.validate_retention_days(retention_days)
            except ValueError:
                # Never silently clamp an explicit window: --days 0 would
                # delete everything older than one day after normalization.
                print(
                    i18n_t(
                        "data.retention.invalidDays",
                        language,
                        minimum=agent_events_retention.MIN_RETENTION_DAYS,
                        maximum=agent_events_retention.MAX_RETENTION_DAYS,
                    ),
                    file=sys.stderr,
                )
                return 1
            config_recovered = False
        should_run = bool(getattr(args, "run", False)) or bool(getattr(args, "compact", False))
        if config_recovered and should_run:
            print(
                i18n_t("data.retention.configRecovered", language),
                file=sys.stderr,
            )
            return 1

        exit_code = 0
        if should_run:
            payload = agent_events_retention.run_once(
                engine,
                retention_days=retention_days,
                force=True,
                compact=bool(getattr(args, "compact", False)),
            )
            run_status = str(payload.get("status"))
            if run_status in {"busy", "lease_lost", "cancelled"}:
                # busy: nothing deleted; lease_lost: partial deletion without
                # completion marker — automation must retry both, not record
                # success.
                exit_code = 1
            # ok_with_contested_compaction keeps exit 0: deletion completed
            # and its marker is written; only the compaction was contested.
        else:
            payload = {"mode": "plan", "enabled": enabled, **agent_events_retention.retention_status(engine, retention_days=retention_days)}
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            _print_data_retention_human(payload, language)
        return exit_code
    except Exception as exc:  # noqa: BLE001
        print(i18n_t("data.retention.error", _configured_cli_language(), error=str(exc)), file=sys.stderr)
        return 1


_COMPACTION_REASON_KEYS = {
    "checkpoint_busy": "data.retention.compactionReasonCheckpointBusy",
    "post_checkpoint_busy": "data.retention.compactionReasonCheckpointBusy",
    "insufficient_free_space": "data.retention.compactionReasonFreeSpace",
    "free_space_unknown": "data.retention.compactionReasonFreeSpace",
}


def _print_data_retention_human(payload: dict, language: str) -> None:
    from storage import agent_events_retention as _retention

    mode = str(payload.get("mode") or "run")
    if mode == "plan":
        plan = payload.get("plan") or {}
        print(
            i18n_t(
                "data.retention.plan",
                language,
                count=int(plan.get("eligible_count") or 0),
                size=_format_byte_size(int(plan.get("eligible_logical_bytes") or 0)),
                days=int(plan.get("retention_days") or _retention.DEFAULT_RETENTION_DAYS),
            )
        )
        last = payload.get("last_run") or {}
        if last:
            print(i18n_t("data.retention.lastRun", language, at=str(last.get("finished_at")), rows=int(last.get("deleted_rows") or 0)))
        else:
            print(i18n_t("data.retention.neverRun", language))
        if payload.get("enabled") is False:
            print(i18n_t("data.retention.disabled", language))
        compaction = payload.get("compaction") or {}
        print(
            i18n_t(
                "data.retention.compaction",
                language,
                size=_format_byte_size(int(compaction.get("reclaimable_bytes") or 0)),
            )
        )
        return
    status = str(payload.get("status") or "unknown")
    if status == "ok":
        print(i18n_t("data.retention.ran", language, rows=int(payload.get("deleted_rows") or 0)))
        compaction = payload.get("compaction") or {}
        compaction_status = str(compaction.get("status") or "not_attempted")
        if compaction_status == "vacuumed":
            print(i18n_t("data.retention.compacted", language, size=_format_byte_size(int(compaction.get("reclaimed_bytes") or 0))))
        elif compaction_status == "deferred":
            print(
                i18n_t(
                    "data.retention.compactionDeferred",
                    language,
                    reason=i18n_t(_COMPACTION_REASON_KEYS.get(str(compaction.get("reason")), "data.retention.compactionReasonOther"), language),
                )
            )
        elif compaction_status == "skipped":
            print(i18n_t("data.retention.compactionSkipped", language))
    elif status == "not_due":
        print(i18n_t("data.retention.notDue", language))
    elif status == "busy":
        print(i18n_t("data.retention.busy", language))
    elif status == "lease_lost":
        print(i18n_t("data.retention.leaseLost", language))
    elif status == "cancelled":
        print(i18n_t("data.retention.cancelled", language))
    elif status == "ok_with_contested_compaction":
        print(i18n_t("data.retention.ran", language, rows=int(payload.get("deleted_rows") or 0)))
        print(i18n_t("data.retention.compactionDeferred", language, reason=i18n_t("data.retention.compactionReasonOther", language)))
    else:
        print(f"retention status: {status}")


def cmd_data_query(args):
    try:
        sql = getattr(args, "sql", None)
        sql_file = getattr(args, "sql_file", None)
        if sql_file:
            sql = sys.stdin.read() if sql_file == "-" else Path(sql_file).read_text(encoding="utf-8")
        page_request = _page_request_from_args(args, help_command="vibe data query --help")
        result = run_read_only_query(sql or "", page_request=page_request)
        command = ["vibe", "data", "query"]
        if getattr(args, "sql", None):
            _add_optional_arg(command, "--sql", getattr(args, "sql", None))
        elif sql_file and sql_file != "-":
            _add_optional_arg(command, "--sql-file", sql_file)
        omit_next_command = bool(sql_file == "-")
        payload = {
            "columns": result.columns,
            "rows": result.rows,
            **_paginated_fields(
                result.pagination,
                command=command,
                include_next_command=not omit_next_command,
            ),
        }
        _print_cli_payload("data_query", **payload)
        return 0
    except ReadOnlyQueryError as exc:
        _print_task_error(TaskCliError(str(exc), code=exc.code, help_command="vibe data query --help"))
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe data query --help")
        return 1


# ``vibe session`` — Agent-facing session management. ``list`` / ``get`` are
# read-only; ``update`` edits title, visibility, or scope placement. All three go through the shared
# ``core.services.sessions`` business API (same entry the UI server uses) and
# never surface archived (soft-deleted) sessions.
# Lean list row: enough to locate a session and tell whether it is busy.
_SESSION_LIST_FIELDS = (
    "id",
    "title",
    "platform",
    "project_id",
    "agent_name",
    "agent_status",
    "last_active_at",
)
# Detail (``get``) drops the lifecycle ``status`` (archived is never returned, so
# it is always "active"), the internal resume ``session_anchor`` (Agents resume by
# id), and ``agent_id`` (``agent_name`` is the Agent's unique key).
_SESSION_GET_OMIT = ("status", "session_anchor", "agent_id")


def _session_row(payload: dict, *, brief: bool) -> dict:
    if brief:
        return {key: payload.get(key) for key in _SESSION_LIST_FIELDS}
    return {key: value for key, value in payload.items() if key not in _SESSION_GET_OMIT}


def _validate_session_type(platform: str) -> None:
    from config.platform_registry import PLATFORM_REGISTRY

    if platform not in PLATFORM_REGISTRY:
        valid = ", ".join(sorted(PLATFORM_REGISTRY))
        raise TaskCliError(
            f"unknown --type '{platform}'",
            code="invalid_session_type",
            hint=f"Valid platforms: {valid} (avibe = Web/Workbench).",
            help_command="vibe session list --help",
        )


def _session_list_hint() -> str:
    return (
        "Need richer filtering (by agent, time range, message content, or joins)? "
        "Use: vibe data query. Find sessions by what was discussed: vibe data query "
        "--sql \"select s.id, s.title from agent_sessions s join messages m "
        "on m.session_id = s.id where m.content_text like '%KEYWORD%' "
        "order by s.last_active_at desc\""
    )


def _session_get_hint(session_id: str) -> str:
    return (
        f"This session's runs: vibe runs list --session-id {session_id}. "
        "Its messages or any cross-session query: vibe data query "
        "(join messages on session_id)."
    )


def _open_session_engine():
    # Bootstrap/migrate the SQLite state first so a fresh Avibe home returns a clean
    # empty list / not-found instead of a raw "no such table" error (Codex P2).
    _ensure_cli_sqlite_state()
    return create_sqlite_engine(paths.get_sqlite_state_path())


def cmd_session_list(args):
    try:
        platform = getattr(args, "type", None)
        if platform:
            _validate_session_type(platform)
        page_request = _page_request_from_args(args, help_command="vibe session list --help")
        from core.services import sessions as sessions_service

        engine = _open_session_engine()
        with engine.connect() as conn:
            result = sessions_service.list_sessions_page(
                conn,
                platform=platform,
                page=page_request.page,
                limit=page_request.limit,
            )
        command = ["vibe", "session", "list"]
        _add_optional_arg(command, "--type", platform)
        page_fields = _paginated_fields(result, command=command)
        pagination_message = page_fields.pop("message", None)
        message = _session_list_hint()
        if pagination_message:
            message = f"{pagination_message} {message}"
        _print_cli_payload(
            "agent_sessions",
            sessions=[_session_row(row, brief=True) for row in result.items],
            **page_fields,
            message=message,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session list --help")
        return 1


def cmd_session_get(args):
    from core.services import sessions as sessions_service

    try:
        session_id, session_default_notice = _resolve_caller_session_id(
            args,
            purpose="Session",
            help_command="vibe session get --help",
        )
        engine = _open_session_engine()
        with engine.connect() as conn:
            payload = sessions_service.get_active_session(conn, session_id)
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{session_id}' not found",
                code="session_not_found",
                details={"session_id": session_id},
            ),
            help_command="vibe session get --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session get --help")
        return 1
    _print_cli_payload(
        "agent_session",
        session=_session_row(payload, brief=False),
        message=_session_get_hint(session_id),
        **({"session_default_notice": session_default_notice} if session_default_notice else {}),
    )
    return 0


def cmd_session_send_now(args):
    """Promote a Session's exact FIFO head without adding work."""

    from core.services import sessions as sessions_service
    from vibe import internal_client

    session_id = str(getattr(args, "session_id", "") or "").strip()
    try:
        engine = _open_session_engine()
        with engine.connect() as conn:
            sessions_service.get_active_session(conn, session_id)
        controller_result = asyncio.run(internal_client.send_now(session_id))
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{session_id}' not found",
                code="session_not_found",
                details={"session_id": session_id},
            ),
            help_command="vibe session send-now --help",
        )
        return 1
    except internal_client.InternalServerUnavailable as exc:
        _print_task_error(
            TaskCliError(
                "the live Session controller is unavailable",
                code="internal_unavailable",
                hint="Keep the queued messages intact and retry after the Avibe service is reachable.",
                details={"session_id": session_id, "detail": str(exc)},
            ),
            help_command="vibe session send-now --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session send-now --help")
        return 1

    raw_status_code = controller_result.get("status_code")
    try:
        status_code = int(raw_status_code)
    except (TypeError, ValueError):
        status_code = 500
    body = controller_result.get("body")
    result = dict(body) if isinstance(body, dict) else {}
    if not 200 <= status_code < 300 or result.get("ok") is False:
        code = str(result.get("code") or result.get("status") or "send_now_failed")
        detail = str(result.get("detail") or result.get("message") or code)
        _print_task_error(
            TaskCliError(
                detail,
                code=code,
                hint="The active turn and durable queue were left intact; retry or let the turn finish normally.",
                details={
                    "session_id": session_id,
                    "controller_status_code": status_code,
                    "controller_response": result,
                },
            ),
            help_command="vibe session send-now --help",
        )
        return 1

    _print_cli_payload(
        "session_send_now",
        session_id=session_id,
        status=str(result.get("status") or "unknown"),
        result=result,
    )
    return 0


def _queued_agent_run_id(row: dict) -> str | None:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return None
    provenance = metadata.get("scheduled_provenance")
    if not isinstance(provenance, dict):
        return None
    platform_specific = provenance.get("platform_specific")
    if not isinstance(platform_specific, dict):
        return None
    if str(platform_specific.get("task_trigger_kind") or "").strip() != "agent_run":
        return None
    run_id = str(platform_specific.get("task_execution_id") or "").strip()
    return run_id or None


def _session_queue_row(row: dict, *, position: int) -> dict:
    return {
        "position": position,
        "id": str(row.get("id") or ""),
        "text": str(row.get("text") or ""),
        "created_at": row.get("created_at"),
        "author": row.get("author"),
        "source": row.get("source"),
        "run_id": _queued_agent_run_id(row),
    }


def cmd_session_queue_list(args):
    from core.services import sessions as sessions_service
    from storage import message_deliveries

    session_id = str(getattr(args, "session_id", "") or "").strip()
    try:
        page_request = _page_request_from_args(
            args,
            help_command="vibe session queue list --help",
        )
        engine = _open_session_engine()
        with engine.connect() as conn:
            sessions_service.get_active_session(conn, session_id)
            target = resolve_session_id_target(session_id)
            if target.session_key.platform != "avibe":
                raise TaskCliError(
                    "queue inspection requires a Web/Workbench Agent Session",
                    code="session_queue_unsupported_target",
                    details={
                        "session_id": session_id,
                        "platform": target.session_key.platform,
                    },
                )
            result = message_deliveries.list_queued_page(
                conn,
                session_id,
                page_request=page_request,
            )
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{session_id}' not found",
                code="session_not_found",
                details={"session_id": session_id},
            ),
            help_command="vibe session queue list --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session queue list --help")
        return 1

    _print_cli_payload(
        "session_queue",
        session_id=session_id,
        queued=[
            _session_queue_row(
                row,
                position=page_request.offset + index,
            )
            for index, row in enumerate(result.items, start=1)
        ],
        **_paginated_fields(
            result,
            command=["vibe", "session", "queue", "list", session_id],
        ),
    )
    return 0


def cmd_session_queue_remove(args):
    from core.services import sessions as sessions_service
    from storage import message_deliveries
    from storage.background import run_update_event_transaction

    session_id = str(getattr(args, "session_id", "") or "").strip()
    message_id = str(getattr(args, "message_id", "") or "").strip()
    try:
        engine = _open_session_engine()
        with run_update_event_transaction(engine) as conn:
            from storage.agent_session_rows import reserve_write_lock

            reserve_write_lock(conn)
            sessions_service.get_active_session(conn, session_id)
            target = resolve_session_id_target(session_id)
            if target.session_key.platform != "avibe":
                raise TaskCliError(
                    "queue removal requires a Web/Workbench Agent Session",
                    code="session_queue_unsupported_target",
                    details={
                        "session_id": session_id,
                        "platform": target.session_key.platform,
                    },
                )
            removed = message_deliveries.retire_queued_with_run(
                conn,
                session_id,
                message_id,
            )
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{session_id}' not found",
                code="session_not_found",
                details={"session_id": session_id},
            ),
            help_command="vibe session queue remove --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session queue remove --help")
        return 1

    if removed:
        _post_session_queue_updated_to_live_ui(session_id)
    _print_cli_payload(
        "session_queue_remove",
        session_id=session_id,
        message_id=message_id,
        removed=removed,
        status="removed" if removed else "not_found",
    )
    return 0


def cmd_session_update(args):
    from core.services import sessions as sessions_service

    try:
        updates = {}
        if getattr(args, "title", None) is not None:
            updates["title"] = args.title
            updates["title_source"] = "agent"
        if getattr(args, "visibility", None) is not None:
            updates["visibility"] = args.visibility
        raw_scope_id = getattr(args, "scope_id", None)
        if raw_scope_id is not None:
            cleaned_scope_id = str(raw_scope_id).strip()
            updates["scope_id"] = (
                None
                if cleaned_scope_id.lower() == "none"
                else _validate_existing_scope_id(
                    cleaned_scope_id,
                    help_command="vibe session update --help",
                ).session_scope
            )
        if not updates:
            raise TaskCliError(
                "no update field supplied",
                code="missing_session_update",
                hint="Pass --title, --visible/--hidden, --visibility, or --scope-id.",
                help_command="vibe session update --help",
            )
        session_id, session_default_notice = _resolve_caller_session_id(
            args,
            purpose="Session",
            help_command="vibe session update --help",
        )
        engine = _open_session_engine()
        with engine.begin() as conn:
            # Validate first so an archived/missing id is a clean not-found rather
            # than silently writing a title onto a soft-deleted row.
            previous_session = sessions_service.get_active_session(conn, session_id)
            payload = sessions_service.update_session(conn, session_id, **updates)
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{session_id}' not found",
                code="session_not_found",
                details={"session_id": session_id},
            ),
            help_command="vibe session update --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session update --help")
        return 1
    # The DB write is committed above; ping a running UI so the rename shows live
    # (best-effort — never affects this command's result).
    _post_session_activity_to_live_ui(
        session_id,
        previous_scope_id=previous_session.get("scope_id"),
        previous_visibility=previous_session.get("visibility"),
    )
    _print_cli_payload(
        "agent_session",
        updated=True,
        session=_session_row(payload, brief=False),
        **({"session_default_notice": session_default_notice} if session_default_notice else {}),
    )
    return 0


# ----- vault: secret management (design: docs/plans/vaults.md) -----
# The agent-facing CLI is value-free: it can find, request, approve/wait, and
# deliver existing vault material, but it never accepts plaintext secrets for create.


def _open_vault_engine():
    _ensure_cli_sqlite_state()
    return create_sqlite_engine(paths.get_sqlite_state_path())


def _vault_caller_metadata() -> dict[str, str]:
    context = caller_context_from_env()
    return context.to_metadata() if context is not None else {}


def _vault_cli_session_id(args) -> str | None:
    session_id = (getattr(args, "session_id", None) or "").strip()
    if session_id:
        return session_id
    context = caller_context_from_env()
    return _default_session_id_from_caller(context)


def _vault_cli_requester(args) -> dict:
    requester = {"source": "agent-cli", "pid": os.getpid()}
    requester.update(_vault_caller_metadata())
    session_id = _vault_cli_session_id(args)
    if session_id:
        requester["session_id"] = session_id
    skill = (getattr(args, "skill", None) or "").strip()
    if skill:
        requester["skill"] = skill
    if _vault_callback_disabled(args):
        # Opt out of auto-resume: the daemon sweep marks this request's callback "skipped".
        requester["callback_disabled"] = True
    return requester


def _vault_callback_disabled(args) -> bool:
    """Whether this request opts out of the auto-resume callback at creation time.

    Only explicit ``--no-callback``. ``--wait`` must NOT pre-disable it: a finite wait can time
    out with the request still pending, and the agent must then still be auto-resumed when it
    later resolves. The redundant callback for a wait that DOES observe fulfillment is suppressed
    at that point instead (see ``cmd_vault_request``).
    """
    return bool(getattr(args, "no_callback", False))


def _vault_cli_delivery(args, **fields) -> dict:
    delivery = {key: value for key, value in fields.items() if value is not None}
    session_id = _vault_cli_session_id(args)
    if session_id:
        delivery["session_id"] = session_id
    skill = (getattr(args, "skill", None) or "").strip()
    if skill:
        delivery["skill"] = skill
    command = getattr(args, "operation_command", None)
    if command is None:
        command = getattr(args, "command", None)
        if command == "vault":
            command = None
    if command:
        delivery["command"] = command
    egress = getattr(args, "egress", None)
    if egress:
        delivery["egress"] = egress
    return delivery


def _vault_cli_signing_context(args, *, digest: str, help_command: str) -> dict | None:
    raw = getattr(args, "signing_context_json", None)
    if raw is None:
        return None
    try:
        context = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskCliError("signing context must be valid JSON", code="invalid_signing_context", help_command=help_command) from exc
    try:
        return api._verifiable_signing_context_from_payload({"signing_context": context}, digest=digest, required=True)
    except api.VaultApiError as exc:
        raise TaskCliError(str(exc), code=exc.code, help_command=help_command) from exc


def _publish_cli_vaults_updated(
    *,
    scope: str,
    request: dict | None = None,
    grant: dict | None = None,
    secret_name: str | None = None,
) -> None:
    """Best-effort bridge for CLI/agent vault writes into browser SSE."""

    if request is None and grant is None and not secret_name:
        return
    if scope == "request" and isinstance(request, dict):
        _publish_cli_vault_request_notification(request)
    try:
        from core.inbox_events import VAULTS_UPDATED_EVENT, vaults_updated_payload
        from vibe import internal_client

        internal_client.publish_event_sync(
            VAULTS_UPDATED_EVENT,
            vaults_updated_payload(
                scope=scope,
                request_id=str(request.get("id") or "") if request else None,
                request_status=str(request.get("status") or "") if request else None,
                grant_id=str(grant.get("id") or "") if grant else None,
                grant_status=str(grant.get("status") or "") if grant else None,
                secret_name=secret_name or (str(request.get("secret_name") or "") if request else None),
            ),
            timeout=1.5,
        )
    except Exception:
        logger.debug("failed to publish CLI vault update event", exc_info=True)


def _publish_cli_vault_request_notification(request: dict) -> None:
    """Best-effort bridge for CLI-created Vault requests into IM notification delivery."""

    try:
        from vibe import internal_client

        internal_client.notify_vault_request_created_sync(request, timeout=2.0)
    except Exception:
        logger.debug("failed to publish CLI vault request notification", exc_info=True)


def _is_env_name(name: str) -> bool:
    """ASCII shell/env identifier: a letter or underscore, then letters/digits/underscores."""
    if not name or not name[0].isascii() or not (name[0].isalpha() or name[0] == "_"):
        return False
    return all(c.isascii() and (c.isalnum() or c == "_") for c in name)


def _parse_env_specs_parts(specs) -> tuple[dict[str, str], list[str]]:
    """Map ENV var name -> vault secret name from ``--env`` specs.

    Accepts ``NAME`` (inject as the same name), ``LOCAL=NAME`` (rename), and
    comma-separated ``A,B`` within one flag.
    """
    mapping: dict[str, str] = {}
    env_by_secret: dict[str, str] = {}
    normalized: list[str] = []
    for spec in specs or []:
        for part in str(spec).split(","):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                local, _, vault_name = part.partition("=")
                local, vault_name = local.strip(), vault_name.strip()
            else:
                local = vault_name = part
            if not local or not vault_name:
                raise TaskCliError(f"invalid --env spec: {part!r}", code="invalid_env_spec", help_command="vibe vault run --help")
            # The local (LHS) becomes an env var name / is interpolated into `export`
            # lines for eval — reject anything that isn't a plain identifier so it can't
            # break the shell or smuggle in extra commands.
            if not _is_env_name(local):
                raise TaskCliError(f"invalid env var name: {local!r} (use [A-Za-z_][A-Za-z0-9_]*)", code="invalid_env_name", help_command="vibe vault run --help")
            existing = mapping.get(local)
            if existing is not None and existing != vault_name:
                raise TaskCliError(
                    f"env var {local!r} maps to both {existing!r} and {vault_name!r}",
                    code="conflicting_env_alias",
                    help_command="vibe vault run --help",
                )
            existing_env = env_by_secret.get(vault_name)
            if existing_env is not None and existing_env != local:
                raise TaskCliError(
                    f"secret '{vault_name}' was selected as both {existing_env!r} and {local!r}",
                    code="conflicting_env_alias",
                    hint="Use one --env alias for each selected secret.",
                    help_command="vibe vault run --help",
                )
            if existing == vault_name:
                continue
            mapping[local] = vault_name
            env_by_secret[vault_name] = local
            normalized.append(vault_name if local == vault_name else f"{local}={vault_name}")
    return mapping, normalized


def _parse_env_specs(specs) -> dict:
    return _parse_env_specs_parts(specs)[0]


def _arg_list(args, name: str) -> list[str]:
    value = getattr(args, name, None)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _add_vault_run_selection(
    selections: dict[str, str],
    *,
    vault_name: str,
    env_name: str,
) -> None:
    if not vault_name or not env_name:
        raise TaskCliError("vault run selector produced an empty secret or env name", code="invalid_selector")
    existing_secret = selections.get(env_name)
    if existing_secret is not None:
        if existing_secret != vault_name:
            raise TaskCliError(
                f"env var {env_name!r} is selected for both {existing_secret!r} and {vault_name!r}",
                code="conflicting_env_alias",
                help_command="vibe vault run --help",
            )
        return
    existing_env = next((selected_env for selected_env, selected_secret in selections.items() if selected_secret == vault_name), None)
    if existing_env is not None and existing_env != env_name:
        raise TaskCliError(
            f"secret '{vault_name}' was selected as both {existing_env!r} and {env_name!r}",
            code="conflicting_env_alias",
            hint="Use one --env alias for each selected secret.",
            help_command="vibe vault run --help",
        )
    selections[env_name] = vault_name


def _resolve_vault_run_selectors(engine, args) -> tuple[dict[str, str], dict]:
    """Expand --env/--tag/--skill to a fixed env-name -> vault-name plan."""

    from storage import vault_service

    env_specs = list(getattr(args, "env", None) or [])
    explicit_mapping, normalized_env_specs = _parse_env_specs_parts(env_specs)
    tag_specs = _arg_list(args, "tag")
    skill_specs = _arg_list(args, "skill")
    selector_requested = bool(normalized_env_specs or tag_specs or skill_specs)
    selections: dict[str, str] = {}
    for env_name, vault_name in explicit_mapping.items():
        _add_vault_run_selection(selections, vault_name=vault_name, env_name=env_name)

    source_selector: dict = {"env": normalized_env_specs}
    if tag_specs or skill_specs:
        with engine.connect() as conn:
            expanded = vault_service.expand_value_delivery_selector(conn, tags=tag_specs, skills=skill_specs)
        source_selector["tags"] = list(expanded["source_selector"].get("tags") or [])
        for item in expanded.get("secrets") or []:
            _add_vault_run_selection(
                selections,
                vault_name=str(item.get("name") or ""),
                env_name=str(item.get("env") or ""),
            )
    else:
        source_selector["tags"] = []

    if not selections and selector_requested:
        raise TaskCliError(
            "vault run selector matched no value-deliverable secrets",
            code="no_matching_secrets",
            hint="Check the --env, --tag, or --skill selector, or ask the user to store/link the secret first.",
            help_command="vibe vault run --help",
        )
    if not selections:
        raise TaskCliError(
            "at least one --env NAME, --tag TAG, or --skill SKILL is required",
            code="missing_selector",
            help_command="vibe vault run --help",
        )
    return selections, source_selector


def _source_selector_tags(source_selector: dict | None) -> list[str]:
    if not isinstance(source_selector, dict):
        return []
    tags = source_selector.get("tags")
    if not isinstance(tags, list):
        return []
    return [str(tag) for tag in tags if isinstance(tag, str) and tag]


def _needs_protected_selector_set(protected_names: list[str], source_selector: dict | None) -> bool:
    return bool(protected_names) and (len(protected_names) > 1 or bool(_source_selector_tags(source_selector)))


def _always_ask_names(metas: dict[str, dict], names: list[str]) -> list[str]:
    selected: list[str] = []
    for name in names:
        policy = metas.get(name, {}).get("policy")
        if isinstance(policy, dict) and policy.get("always_ask"):
            selected.append(name)
    return selected


def _vault_query_arg(args, *, help_command: str) -> str | None:
    query = (getattr(args, "query", None) or "").strip()
    query_flag = (getattr(args, "query_filter", None) or "").strip()
    if query and query_flag:
        raise TaskCliError("use positional query or --q, not both", code="invalid_query", help_command=help_command)
    return query or query_flag or None


def _vault_tag_filters(args) -> list[str]:
    return _split_vault_metadata_values(getattr(args, "tag", None))


def _vault_raw_tag_args(args) -> list[str]:
    raw = getattr(args, "tag", None)
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw or []]


def _vault_page_payload(
    *,
    items: list[dict],
    args,
    help_command: str,
    command: list[str],
) -> tuple[list[dict], dict, str | None]:
    page_request = _page_request_from_args(args, help_command=help_command)
    result = page_sequence(items, page_request)
    fields = _paginated_fields(result, command=command)
    return result.items, fields["pagination"], fields.get("message")


def _vault_lookup_next_steps(*, has_more: bool, has_filters: bool) -> list[str]:
    steps: list[str] = []
    if has_more:
        steps.append("Use pagination.next_command to fetch the next page.")
    if not has_filters:
        steps.append("Use `vibe vault find --q <keyword>` or filter by --tag/--kind/--protection to narrow results.")
    steps.append("Use `vibe vault tags` to inspect available tags.")
    return steps


def _vault_capability_payload(secret: dict) -> dict:
    return {
        "name": secret["name"],
        "kind": secret.get("kind"),
        "protection": secret.get("protection"),
        "tags": secret.get("tags") or [],
        "description": secret.get("description"),
        "policy": secret.get("policy") or {},
        "access_grantable": bool(secret.get("access_grantable")),
        "per_use_sign": bool(secret.get("per_use_sign")),
    }


def cmd_vault_list(args):
    from storage import vault_service

    help_command = "vibe vault list --help"
    try:
        engine = _open_vault_engine()
        tags = _vault_tag_filters(args)
        query = _vault_query_arg(args, help_command=help_command)
        with engine.connect() as conn:
            secrets = vault_service.list_secrets(
                conn,
                tags=tags,
                query=query,
                kind=getattr(args, "kind", None),
                protection=getattr(args, "protection", None),
            )
        command = ["vibe", "vault", "list"]
        for tag in _vault_raw_tag_args(args):
            _add_optional_arg(command, "--tag", tag)
        _add_optional_arg(command, "--q", query)
        _add_optional_arg(command, "--kind", getattr(args, "kind", None))
        _add_optional_arg(command, "--protection", getattr(args, "protection", None))
        page_items, page_payload, message = _vault_page_payload(
            items=secrets,
            args=args,
            help_command=help_command,
            command=command,
        )
        payload = {
            "secrets": page_items,
            "pagination": page_payload,
            "next_steps": _vault_lookup_next_steps(
                has_more=bool(page_payload.get("has_more")),
                has_filters=bool(tags or query or getattr(args, "kind", None) or getattr(args, "protection", None)),
            ),
        }
        if message:
            payload["message"] = f"{message} To narrow results instead, use `vibe vault find --q <keyword>`."
        elif not page_items:
            payload["message"] = "No Vault secrets matched. Try `vibe vault find --q <keyword>` or ask the user to add one with `vibe vault request NAME --reason ...`."
        _print_cli_payload("vault_secrets", **payload)
        return 0
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_find(args):
    from storage import vault_service

    help_command = "vibe vault find --help"
    try:
        engine = _open_vault_engine()
        tags = _vault_tag_filters(args)
        query = _vault_query_arg(args, help_command=help_command)
        with engine.connect() as conn:
            secrets = vault_service.list_secrets(
                conn,
                tags=tags,
                query=query,
                kind=getattr(args, "kind", None),
                protection=getattr(args, "protection", None),
            )
        command = ["vibe", "vault", "find"]
        if getattr(args, "query", None):
            command.append(getattr(args, "query"))
        _add_optional_arg(command, "--q", getattr(args, "query_filter", None))
        for tag in _vault_raw_tag_args(args):
            _add_optional_arg(command, "--tag", tag)
        _add_optional_arg(command, "--kind", getattr(args, "kind", None))
        _add_optional_arg(command, "--protection", getattr(args, "protection", None))
        capabilities = [_vault_capability_payload(secret) for secret in secrets]
        page_items, page_payload, message = _vault_page_payload(
            items=capabilities,
            args=args,
            help_command=help_command,
            command=command,
        )
        payload = {
            "secrets": page_items,
            "pagination": page_payload,
            "next_steps": _vault_lookup_next_steps(
                has_more=bool(page_payload.get("has_more")),
                has_filters=bool(tags or query or getattr(args, "kind", None) or getattr(args, "protection", None)),
            ),
        }
        if message:
            payload["message"] = message
        elif not page_items:
            payload["message"] = "No Vault capabilities matched. Try a broader keyword, inspect `vibe vault tags`, or request a missing static secret with `vibe vault request NAME --reason ...`."
        _print_cli_payload("vault_find", **payload)
        return 0
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_tags(args):
    from storage import vault_service

    help_command = "vibe vault tags --help"
    try:
        engine = _open_vault_engine()
        query = _vault_query_arg(args, help_command=help_command)
        with engine.connect() as conn:
            tags = vault_service.list_secret_tags(conn, query=query, tag_type=getattr(args, "type", None))
        command = ["vibe", "vault", "tags"]
        if getattr(args, "query", None):
            command.append(getattr(args, "query"))
        _add_optional_arg(command, "--q", getattr(args, "query_filter", None))
        _add_optional_arg(command, "--type", getattr(args, "type", None))
        page_items, page_payload, message = _vault_page_payload(
            items=tags,
            args=args,
            help_command=help_command,
            command=command,
        )
        payload = {
            "tags": page_items,
            "pagination": page_payload,
            "next_steps": [
                "Use `vibe vault find --tag <tag>` to inspect secrets under a tag.",
                "Use `vibe vault edit NAME --tag <tag>` or `--skill <skill>` to update secret tags.",
            ],
        }
        if message:
            payload["message"] = message
        elif not page_items:
            payload["message"] = "No Vault tags matched. Add tags with `vibe vault edit NAME --tag <tag>` or request a new secret with tags in --spec-json."
        _print_cli_payload("vault_tags", **payload)
        return 0
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_rm(args):
    from storage import vault_service
    from vibe import api

    help_command = "vibe vault rm --help"
    try:
        engine = _open_vault_engine()
        release_scopes: list[dict[str, str]] = []
        with engine.begin() as conn:
            meta = vault_service.get_secret_meta(conn, args.name)
            if meta.get("protection") == "protected":
                raise TaskCliError(
                    f"'{args.name}' is a protected secret — delete it in the browser (Vaults), where it's "
                    f"confirmed by the signed-in user. The CLI can't delete protected secrets.",
                    code="protected_delete_forbidden",
                    help_command=help_command,
                )
            grant_rows = vault_service.active_grant_rows_for_secret(conn, args.name)
            vault_service.delete_secret(conn, args.name)
            release_scopes = vault_service.agent_release_scopes_after_rows(conn, grant_rows)
        api.release_vault_agent_scopes(release_scopes, reason="vault_rm")
        _publish_cli_vaults_updated(scope="secret", secret_name=args.name)
        _print_cli_payload("vault_secret", removed=True, name=args.name)
        return 0
    except vault_service.SecretNotFoundError:
        _print_task_error(TaskCliError(f"secret '{args.name}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def _split_vault_metadata_values(values: list[str] | str | None) -> list[str]:
    out: list[str] = []
    iterable = [values] if isinstance(values, str) else values or []
    for raw in iterable:
        for item in str(raw).split(","):
            item = item.strip()
            if item:
                out.append(item)
    return out


_VAULT_EDIT_ALLOWED_FIELDS = {"description", "tags", "policy"}
_VAULT_EDIT_SECRET_FIELDS = {
    "value",
    "sealed",
    "envelope",
    "blind_box",
    "ciphertext",
    "nonce",
    "wrap_meta",
    "private_key",
    "secret",
}


def _reject_vault_edit_secret_fields(value: object, *, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in _VAULT_EDIT_SECRET_FIELDS:
                raise TaskCliError(f"{path}.{key} is not allowed in vault metadata", code="secret_material_rejected")
            _reject_vault_edit_secret_fields(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_vault_edit_secret_fields(item, path=f"{path}[{index}]")


def _vault_edit_payload_from_args(args, *, current: dict, help_command: str) -> dict:
    metadata_json = getattr(args, "metadata_json", None)
    flag_fields = [
        getattr(args, "description", None) is not None,
        bool(getattr(args, "clear_description", False)),
        bool(getattr(args, "tag", None)),
        bool(getattr(args, "skill", None)),
        bool(getattr(args, "clear_tags", False)),
        bool(getattr(args, "allow_host", None)),
        bool(getattr(args, "clear_allowed_hosts", False)),
        getattr(args, "fetch_auth", None) is not None,
        bool(getattr(args, "clear_fetch_auth", False)),
        getattr(args, "auth_name", None) is not None,
    ]
    if metadata_json and any(flag_fields):
        raise TaskCliError("use --metadata-json or field flags, not both", code="invalid_metadata", help_command=help_command)
    if metadata_json:
        try:
            payload = json.loads(str(metadata_json))
        except ValueError as exc:
            raise TaskCliError(f"invalid metadata JSON: {exc}", code="invalid_metadata", help_command=help_command) from exc
        if not isinstance(payload, dict):
            raise TaskCliError("metadata JSON must be an object", code="invalid_metadata", help_command=help_command)
        try:
            _reject_vault_edit_secret_fields(payload)
        except TaskCliError as exc:
            exc.help_command = help_command
            raise
        extra_fields = set(payload) - _VAULT_EDIT_ALLOWED_FIELDS
        if extra_fields:
            raise TaskCliError(
                f"unsupported vault metadata fields: {', '.join(sorted(extra_fields))}",
                code="invalid_metadata",
                help_command=help_command,
            )
        return payload

    payload: dict[str, object] = {}
    if getattr(args, "description", None) is not None and getattr(args, "clear_description", False):
        raise TaskCliError("use --description or --clear-description, not both", code="invalid_metadata", help_command=help_command)
    if getattr(args, "description", None) is not None:
        payload["description"] = str(args.description)
    elif getattr(args, "clear_description", False):
        payload["description"] = None

    if getattr(args, "clear_tags", False) and (getattr(args, "tag", None) or getattr(args, "skill", None)):
        raise TaskCliError("use --clear-tags or --tag/--skill, not both", code="invalid_metadata", help_command=help_command)
    if getattr(args, "clear_tags", False):
        payload["tags"] = []
    elif getattr(args, "tag", None) or getattr(args, "skill", None):
        current_tags = [str(tag) for tag in current.get("tags") or [] if isinstance(tag, str) and tag]
        current_plain_tags = [tag for tag in current_tags if not tag.startswith("skill:")]
        current_skill_tags = [tag for tag in current_tags if tag.startswith("skill:")]
        tags = _split_vault_metadata_values(getattr(args, "tag", None)) if getattr(args, "tag", None) else current_plain_tags
        skill_tags = (
            [
                skill if skill.startswith("skill:") else f"skill:{skill}"
                for skill in _split_vault_metadata_values(getattr(args, "skill", None))
            ]
            if getattr(args, "skill", None)
            else current_skill_tags
        )
        tags.extend(skill_tags)
        payload["tags"] = tags

    policy_requested = any(
        [
            getattr(args, "allow_host", None),
            getattr(args, "clear_allowed_hosts", False),
            getattr(args, "fetch_auth", None) is not None,
            getattr(args, "clear_fetch_auth", False),
            getattr(args, "auth_name", None) is not None,
        ]
    )
    if policy_requested:
        if getattr(args, "clear_allowed_hosts", False) and getattr(args, "allow_host", None):
            raise TaskCliError("use --clear-allowed-hosts or --allow-host, not both", code="invalid_metadata", help_command=help_command)
        if getattr(args, "clear_fetch_auth", False) and getattr(args, "fetch_auth", None) is not None:
            raise TaskCliError("use --clear-fetch-auth or --fetch-auth, not both", code="invalid_metadata", help_command=help_command)
        if getattr(args, "auth_name", None) is not None and getattr(args, "fetch_auth", None) not in {"header", "query"}:
            raise TaskCliError("--auth-name requires --fetch-auth header or --fetch-auth query", code="invalid_metadata", help_command=help_command)
        current_policy = current.get("policy") if isinstance(current.get("policy"), dict) else {}
        policy: dict[str, object] = {}
        if getattr(args, "clear_allowed_hosts", False):
            policy["allowed_hosts"] = []
        elif getattr(args, "allow_host", None):
            policy["allowed_hosts"] = _split_vault_metadata_values(getattr(args, "allow_host", None))
        elif current_policy.get("allowed_hosts") is not None:
            policy["allowed_hosts"] = current_policy.get("allowed_hosts")

        if not getattr(args, "clear_fetch_auth", False):
            auth_type = getattr(args, "fetch_auth", None)
            if auth_type is None:
                current_auth = current_policy.get("auth") if isinstance(current_policy.get("auth"), dict) else None
                if current_auth:
                    policy["auth"] = current_auth
            elif auth_type == "bearer":
                policy["auth"] = {"type": "bearer"}
            else:
                policy["auth"] = {"type": auth_type, "name": str(getattr(args, "auth_name", "") or "").strip()}
        payload["policy"] = policy

    if not payload:
        raise TaskCliError("no metadata fields were provided", code="missing_metadata", help_command=help_command)
    return payload


def cmd_vault_edit(args):
    from storage import vault_service
    from vibe import api

    help_command = "vibe vault edit --help"
    try:
        engine = _open_vault_engine()
        release_scopes: list[dict[str, str]] = []
        with engine.begin() as conn:
            current = vault_service.get_secret_meta(conn, args.name)
            payload = _vault_edit_payload_from_args(args, current=current, help_command=help_command)
            secret = vault_service.update_secret_metadata(
                conn,
                args.name,
                release_scopes=release_scopes,
                **{key: payload[key] for key in ("description", "tags", "policy") if key in payload},
            )
        api.release_vault_agent_scopes(release_scopes, reason="vault_edit")
        _publish_cli_vaults_updated(scope="secret", secret_name=secret.get("name") or args.name)
        _print_cli_payload(
            "vault_secret",
            secret=secret,
            message="Vault metadata updated. Secret value, kind, protection tier, and existing grant member snapshots were not changed.",
            next_steps=[
                "Use `vibe vault list --q <keyword>` or `vibe vault find --tag <tag>` to verify the metadata.",
                "Use `vibe vault run` / `fetch` / `sign` according to the secret kind when continuing the task.",
            ],
        )
        return 0
    except vault_service.SecretNotFoundError:
        _print_task_error(TaskCliError(f"secret '{args.name}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except vault_service.VaultServiceError as exc:
        _print_task_error(TaskCliError(str(exc), code="invalid_metadata", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_access(args):
    from storage import vault_crypto, vault_service

    help_command = "vibe vault access --help"
    name = getattr(args, "name", "")
    try:
        if not vault_crypto.is_valid_secret_name(name):
            raise TaskCliError(f"invalid secret name: {name!r} (use ^[A-Za-z_][A-Za-z0-9_]*$)", code="invalid_name", help_command=help_command)
        engine = _open_vault_engine()
        with engine.begin() as conn:
            vault_service.get_secret_meta(conn, name)
            request = vault_service.create_access_request(
                conn,
                name,
                requester=_vault_cli_requester(args),
                delivery=_vault_cli_delivery(args, mode="access"),
            )
        _publish_cli_vaults_updated(scope="request", request=request)
        _print_cli_payload(
            "vault_access_request",
            request_id=request["id"],
            request=request,
            message=_vault_request_followup_message(args, request["id"], resolved_verb="approves or denies it"),
        )
        return 0
    except vault_service.SecretNotFoundError:
        _print_task_error(TaskCliError(f"secret '{name}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except vault_service.NotGrantableError as exc:
        _print_task_error(TaskCliError(str(exc), code="not_grantable", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def _expire_agent_grant_after_missing(
    engine,
    grant_id: str,
    names: list[str],
    *,
    requester: dict | None = None,
    delivery: dict | None = None,
    purpose: str = "run",
) -> dict | None:
    from storage import vault_service

    first_request = None
    try:
        with engine.begin() as conn:
            vault_service.expire_grant(conn, grant_id, reason="grant-expired-agent-cache-missing")
            delivery_payload = dict(delivery or {})
            source_selector = delivery_payload.get("source_selector")
            if isinstance(source_selector, dict):
                try:
                    first_request = vault_service.create_access_request(
                        conn,
                        source_selector=source_selector,
                        requester=requester or {"source": "cli", "pid": os.getpid()},
                        delivery=delivery_payload,
                        purpose=purpose,
                    )
                except vault_service.NotGrantableError:
                    pass
            if first_request is None:
                for name in names:
                    resolved = vault_service.resolve_secret_access(
                        conn,
                        name,
                        requester=requester or {"source": "cli", "pid": os.getpid()},
                        delivery=delivery or {},
                        purpose=purpose,
                    )
                    if first_request is None and isinstance(resolved.get("request"), dict):
                        first_request = resolved["request"]
                        break
    except Exception:
        pass
    else:
        _publish_cli_vaults_updated(scope="grant", grant={"id": grant_id, "status": "expired"})
        _publish_cli_vaults_updated(scope="request", request=first_request)
    return first_request


def _agent_missing_grant(exc: Exception) -> bool:
    text = str(exc).lower()
    return "grant is missing or expired" in text or "grant does not cover" in text


def _vault_cli_delivery_context(args, *, mode: str, **extra) -> tuple[dict, dict, str | None]:
    session_id = _vault_cli_session_id(args)
    requester = {"source": "cli", "pid": os.getpid()}
    delivery = {"mode": mode, **extra}
    if session_id:
        requester["session_id"] = session_id
        delivery["session_id"] = session_id
    return requester, delivery, session_id


def _preflight_vault_names(engine, names: list[str], *, mixed_message: str, mixed_code: str = "mixed_protection_tiers") -> dict[str, dict]:
    from storage import vault_service

    metas: dict[str, dict] = {}
    with engine.connect() as conn:
        for name in dict.fromkeys(names):
            metas[name] = vault_service.get_secret_meta(conn, name)
    tiers = {str(meta.get("protection") or "standard") for meta in metas.values()}
    if len(tiers) > 1:
        raise TaskCliError(mixed_message, code=mixed_code)
    return metas


def _preflight_vault_run_batch(engine, mapping: dict[str, str]) -> dict[str, dict]:
    from storage import vault_service

    metas: dict[str, dict] = {}
    with engine.connect() as conn:
        for name in dict.fromkeys(mapping.values()):
            metas[name] = vault_service.get_secret_meta(conn, name)
            if metas[name].get("kind") == "keypair":
                raise vault_service.KeypairNotValueDeliverableError(
                    f"{name} is a signing key; use vibe vault sign instead of value delivery"
                )
    return metas


def _preflight_vault_inject_batch(engine, names: list[str]) -> dict[str, dict]:
    return _preflight_vault_names(
        engine,
        names,
        mixed_message="mixing protected and standard secrets in one vault inject is not wired yet",
    )


class _AgentRunOutputBridge:
    """Stream protected child stdio through temporary FIFOs owned by this CLI."""

    def __init__(
        self,
        stdout,
        stderr,
        *,
        stdin=None,
        env_exclude: set[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not hasattr(os, "mkfifo"):
            raise TaskCliError("protected vault run output streaming requires Unix FIFOs", code="unsupported_platform")
        runtime_dir = paths.get_runtime_dir()
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="vault-run-", dir=str(runtime_dir)))
        self._tmpdir.chmod(0o700)
        self.stdout_path = self._tmpdir / "stdout"
        self.stderr_path = self._tmpdir / "stderr"
        self.stdin_path = self._tmpdir / "stdin"
        self.env_path = self._tmpdir / "env.sh"
        self.keep_env_path = self._tmpdir / "keep-env"
        self._keeper_fds: list[int] = []
        try:
            os.mkfifo(self.stdout_path, 0o600)
            os.mkfifo(self.stderr_path, 0o600)
            os.mkfifo(self.stdin_path, 0o600)
            os.mkfifo(self.env_path, 0o600)
            keep_env_names = sorted(name for name in (env_exclude or set()) if _is_shell_env_name(name))
            self.keep_env_path.write_text("".join(f"{name}\n" for name in keep_env_names), encoding="utf-8")
            self.keep_env_path.chmod(0o600)
            self._keeper_fds = [
                os.open(self.stdout_path, os.O_RDWR | os.O_NONBLOCK),
                os.open(self.stderr_path, os.O_RDWR | os.O_NONBLOCK),
            ]
        except OSError as exc:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            raise TaskCliError("protected vault run stdio streaming requires Unix FIFOs", code="unsupported_platform") from exc
        stdin = stdin if stdin is not None else getattr(sys.stdin, "buffer", sys.stdin)
        self._stdin_stop = threading.Event()
        self._env_stop = threading.Event()
        env = os.environ if env is None else env
        self._env_thread = threading.Thread(
            target=self._write_env_fifo,
            args=(self.env_path, _shell_env_exports(env, exclude=env_exclude).encode("utf-8"), self._env_stop),
            daemon=True,
        )
        self._env_thread.start()
        self._stdin_thread = threading.Thread(
            target=self._copy_stdin_fifo,
            args=(self.stdin_path, stdin, self._stdin_stop),
            daemon=True,
        )
        self._stdin_thread.start()
        self._threads = [
            threading.Thread(target=self._copy_fifo, args=(self.stdout_path, stdout), daemon=True),
            threading.Thread(target=self._copy_fifo, args=(self.stderr_path, stderr), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self) -> None:
        for fd in self._keeper_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._keeper_fds.clear()
        for thread in self._threads:
            thread.join(timeout=2)
        self._stdin_stop.set()
        self._env_stop.set()
        self._stdin_thread.join(timeout=2)
        self._env_thread.join(timeout=2)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _copy_fifo(path: Path, target) -> None:
        try:
            with path.open("rb", buffering=0) as source:
                while True:
                    chunk = source.read(8192)
                    if not chunk:
                        break
                    target.write(chunk)
                    target.flush()
        except OSError:
            return

    @staticmethod
    def _write_env_fifo(path: Path, script: bytes, stop_event: threading.Event) -> None:
        fd = _AgentRunOutputBridge._open_fifo_writer(path, stop_event)
        if fd is None:
            return
        try:
            _AgentRunOutputBridge._write_all(fd, script, stop_event)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _copy_stdin_fifo(path: Path, source, stop_event: threading.Event) -> None:
        fd = _AgentRunOutputBridge._open_fifo_writer(path, stop_event)
        if fd is None:
            return
        try:
            while not stop_event.is_set():
                chunk = _AgentRunOutputBridge._read_stdin_chunk(source, stop_event)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                _AgentRunOutputBridge._write_all(fd, chunk, stop_event)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @staticmethod
    def _open_fifo_writer(path: Path, stop_event: threading.Event) -> int | None:
        while not stop_event.is_set():
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno in {errno.ENXIO, errno.ENOENT}:
                    time.sleep(0.01)
                    continue
                return
        return None

    @staticmethod
    def _write_all(fd: int, data: bytes, stop_event: threading.Event) -> None:
        view = memoryview(data)
        offset = 0
        while offset < len(view) and not stop_event.is_set():
            try:
                written = os.write(fd, view[offset:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError:
                return
            if written <= 0:
                return
            offset += written

    @staticmethod
    def _read_stdin_chunk(source, stop_event: threading.Event):
        try:
            fileno = source.fileno()
        except (AttributeError, OSError, ValueError):
            try:
                return source.read(8192)
            except (OSError, ValueError):
                return b""
        while not stop_event.is_set():
            try:
                ready, _, _ = select_module.select([fileno], [], [], 0.05)
            except (OSError, ValueError):
                try:
                    return source.read(8192)
                except (OSError, ValueError):
                    return b""
            if ready:
                try:
                    return os.read(fileno, 8192)
                except OSError:
                    return b""
        return b""


def _is_shell_env_name(name: str) -> bool:
    if not name or not (name[0] == "_" or "A" <= name[0] <= "Z" or "a" <= name[0] <= "z"):
        return False
    return all(ch == "_" or "A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9" for ch in name)


def _shell_env_exports(env: Mapping[str, str], *, exclude: set[str] | None = None) -> str:
    excluded = exclude or set()
    lines: list[str] = []
    for name, value in env.items():
        if name in excluded:
            continue
        if not _is_shell_env_name(name) or "\x00" in value:
            continue
        lines.append(f"export {name}={shlex.quote(value)}\n")
    return "".join(lines)


def _agent_run_command(
    command_argv: list[str],
    *,
    cwd: str | None = None,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    stdin_path: str | None = None,
    env_path: str | None = None,
    keep_env_path: str | None = None,
) -> list[str]:
    """Preserve the invoking cwd when a resident agent executes the child.

    The current avault agent frame has no cwd field. Wrap the command in a tiny
    shell trampoline so the long-lived agent executes the requested argv from
    the CLI's working directory without shell-interpolating any user argument.
    """

    shell = shutil.which("sh") or "/bin/sh"
    env_binary = shlex.quote(shutil.which("env") or "/usr/bin/env")
    grep_binary = shlex.quote(shutil.which("grep") or "/usr/bin/grep")
    sed_binary = shlex.quote(shutil.which("sed") or "/usr/bin/sed")
    child_argv = list(command_argv)
    if child_argv:
        executable = child_argv[0]
        has_path_separator = os.sep in executable or (os.altsep is not None and os.altsep in executable)
        if not has_path_separator and (resolved := shutil.which(executable)):
            child_argv[0] = resolved
    if stdout_path and stderr_path and stdin_path and env_path and keep_env_path:
        return [
            shell,
            "-c",
            (
                'stdout_fifo=$1; stderr_fifo=$2; stdin_fifo=$3; env_file=$4; keep_env_file=$5; cwd=$6; shift 6; '
                'exec <"$stdin_fifo" >"$stdout_fifo" 2>"$stderr_fifo"; '
                f'for name in $({env_binary} | {sed_binary} "s/=.*//"); do '
                'case "$name" in ""|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*|[0123456789]*) continue;; esac; '
                f'if ! {grep_binary} -Fqx "$name" "$keep_env_file"; then unset "$name"; fi; '
                'done; '
                '. "$env_file"; cd "$cwd" || exit 125; exec "$@"'
            ),
            "avibe-vault-run",
            stdout_path,
            stderr_path,
            stdin_path,
            env_path,
            keep_env_path,
            cwd or os.getcwd(),
            *child_argv,
        ]
    return [
        shell,
        "-c",
        'cd "$1" || exit 125; shift; exec "$@"',
        "avibe-vault-run",
        cwd or os.getcwd(),
        *child_argv,
    ]


def _resolve_cli_output_path(path: str) -> str:
    output_path = Path(path).expanduser()
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    return str(output_path.resolve(strict=False))


def _preflight_cli_output_path(path: str, *, help_command: str) -> None:
    output_path = Path(path)
    if output_path.exists() and not output_path.is_file():
        raise TaskCliError(
            f"output path is not a regular file: {output_path}",
            code="output_unwritable",
            help_command=help_command,
        )
    parent = output_path.parent
    if not parent.exists():
        raise TaskCliError(f"output parent does not exist: {parent}", code="output_unwritable", help_command=help_command)
    if not parent.is_dir():
        raise TaskCliError(f"output parent is not a directory: {parent}", code="output_unwritable", help_command=help_command)
    try:
        with tempfile.NamedTemporaryFile(dir=str(parent), prefix=f".{output_path.name}.", delete=True):
            pass
    except OSError as exc:
        raise TaskCliError(f"cannot write output file: {exc}", code="output_unwritable", help_command=help_command) from exc


def _consume_one_shot_grants(grants: list[dict] | tuple[dict, ...] | None, *, reason: str) -> None:
    from vibe import api

    try:
        api.consume_one_shot_grants(grants, reason=reason)
    except Exception:
        logger.debug("failed to consume one-shot vault grants after delivery", exc_info=True)


def _unique_one_shot_grants(grants: list[dict] | tuple[dict, ...] | None) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for grant in grants or []:
        if not isinstance(grant, dict) or grant.get("one_shot") is not True:
            continue
        grant_id = str(grant.get("id") or "")
        if not grant_id or grant_id in seen:
            continue
        seen.add(grant_id)
        unique.append(grant)
    return unique


def _release_one_shot_reservations(engine, grants: list[dict] | tuple[dict, ...] | None) -> None:
    grants = _unique_one_shot_grants(grants)
    if not grants:
        return
    from storage import vault_service

    try:
        with engine.begin() as conn:
            for grant in grants:
                grant_id = str(grant["id"])
                with contextlib.suppress(
                    vault_service.GrantNotActiveError,
                    vault_service.GrantNotFoundError,
                    vault_service.InvalidGrantError,
                ):
                    vault_service.release_one_shot_reservation(conn, grant_id)
    except Exception:
        logger.debug("failed to release one-shot vault grant reservations", exc_info=True)


def _run_delivery_result(raw_result) -> tuple[int, bool]:
    if isinstance(raw_result, dict):
        exit_code = int(raw_result["exit_code"])
        return exit_code, bool(raw_result.get("delivered", True))
    exit_code = int(raw_result)
    return exit_code, True


def _mixed_grants_error(message: str) -> TaskCliError:
    return TaskCliError(message, code="mixed_grants")


def _raise_after_releasing_one_shot_reservations(engine, grants, exc):
    _release_one_shot_reservations(engine, grants)
    raise exc


def _consume_after_possible_use(grants: list[dict] | tuple[dict, ...] | None, *, reason: str) -> None:
    """Fail closed for one-shot grants after handoff to avault/resident agent."""
    _consume_one_shot_grants(_unique_one_shot_grants(grants), reason=reason)


def _finish_one_shot_after_avault_error(
    engine,
    grants: list[dict] | tuple[dict, ...] | None,
    exc: Exception,
    *,
    reason: str,
) -> None:
    from vibe import api

    if isinstance(exc, api.AvaultPreHandoffError):
        _release_one_shot_reservations(engine, grants)
    else:
        _consume_after_possible_use(grants, reason=reason)


def _resolve_vault_run_delivery(
    engine,
    mapping: dict[str, str],
    command_argv: list[str],
    *,
    args=None,
    source_selector: dict | None = None,
):
    from storage import vault_service

    requester, delivery, session_id = _vault_cli_delivery_context(args, mode="run", command=command_argv)
    if source_selector:
        delivery["source_selector"] = source_selector
    metas = _preflight_vault_run_batch(engine, mapping)
    protected_names = [
        name
        for name in dict.fromkeys(mapping.values())
        if str(metas[name].get("protection") or "standard") == "protected"
    ]
    standard_approval_error: TaskCliError | None = None
    approval_request_to_publish: dict | None = None
    if metas and not protected_names:
        with engine.begin() as conn:
            standard_names = list(dict.fromkeys(mapping.values()))
            approval_names = _always_ask_names(metas, standard_names)
            common_grant = vault_service.find_active_grant_for_secrets(
                conn,
                approval_names or standard_names,
                session_id=session_id,
                purpose="run",
                reserve_one_shot=True,
            )
            if isinstance(common_grant, dict) and common_grant.get("one_shot") is True:
                return None, [common_grant], [
                    {"name": vault_name, "env": env_name, "envelope": vault_service.get_envelope(conn, vault_name)}
                    for env_name, vault_name in mapping.items()
                ]
            if approval_names and _source_selector_tags(source_selector):
                req = vault_service.create_access_request(
                    conn,
                    None,
                    source_selector=source_selector,
                    requester=requester,
                    delivery=delivery,
                    purpose="run",
                )
                approval_request_to_publish = req
                standard_approval_error = TaskCliError(
                    "standard always_ask secrets need approval before vault run delivery",
                    code="approval_required",
                    details={"request_id": req.get("id"), "secret_names": approval_names},
                )
        if standard_approval_error is not None:
            _publish_cli_vaults_updated(scope="request", request=approval_request_to_publish)
            raise standard_approval_error
    secrets = []
    grant: dict | None = None
    one_shot_grants: list[dict] = []
    approval_error: TaskCliError | None = None
    resolved_by_name: dict[str, dict] = {}
    try:
        with engine.begin() as conn:
            if protected_names:
                needs_selector_set = _needs_protected_selector_set(protected_names, source_selector)
                selector_standard_names = [
                    name
                    for name in dict.fromkeys(mapping.values())
                    if name not in protected_names and str(metas[name].get("protection") or "standard") == "standard"
                ]
                selector_standard_approval_names = _always_ask_names(metas, selector_standard_names)
                common_standard_grant = None
                if selector_standard_approval_names:
                    common_standard_grant = vault_service.find_active_grant_for_secrets(
                        conn,
                        selector_standard_approval_names,
                        session_id=session_id,
                        purpose="run",
                        reserve_one_shot=True,
                    )
                    if isinstance(common_standard_grant, dict) and common_standard_grant.get("one_shot") is True:
                        one_shot_grants.append(common_standard_grant)
                        for standard_name in selector_standard_approval_names:
                            resolved_by_name[standard_name] = {
                                "status": "standard",
                                "secret": metas[standard_name],
                                "grant": common_standard_grant,
                                "envelope": vault_service.get_envelope(conn, standard_name),
                            }
                grant = vault_service.find_active_grant_for_secrets(
                    conn,
                    protected_names,
                    session_id=session_id,
                    purpose="run",
                    reserve_one_shot=True,
                )
                if grant is None:
                    always_ask_names = _always_ask_names(metas, protected_names) if needs_selector_set else []
                    unresolved_standard_names = [name for name in selector_standard_approval_names if name not in resolved_by_name]
                    standard_always_ask_names = (
                        _always_ask_names(metas, unresolved_standard_names) if needs_selector_set else []
                    )
                    if always_ask_names or standard_always_ask_names:
                        approval_error = TaskCliError(
                            "always_ask secrets cannot be approved as one protected selector-set grant",
                            code="always_ask_selector_set",
                            details={
                                "protected_secret_names": protected_names,
                                "always_ask_secret_names": always_ask_names,
                                "standard_always_ask_secret_names": standard_always_ask_names,
                            },
                            hint="Run always_ask secrets individually so each per-use approval can be consumed once.",
                        )
                    else:
                        request_delivery = dict(delivery)
                        if needs_selector_set:
                            request_delivery["protected_secret_names"] = protected_names
                        req = vault_service.create_access_request(
                            conn,
                            None if needs_selector_set else protected_names[0],
                            source_selector=source_selector if needs_selector_set else None,
                            requester=requester,
                            delivery=request_delivery,
                            purpose="run",
                        )
                        approval_request_to_publish = req
                        approval_error = TaskCliError(
                            "protected secrets need approval before vault run delivery",
                            code="approval_required",
                            details={"request_id": req.get("id"), "protected_secret_names": protected_names},
                        )
                elif grant.get("one_shot") is True:
                    one_shot_grants.append(grant)
            for env_name, vault_name in mapping.items():
                if approval_error is not None:
                    break
                if vault_name in protected_names:
                    secrets.append(
                        {
                            "name": vault_name,
                            "env": env_name,
                            "envelope": vault_service.get_protected_envelope(conn, vault_name),
                            "tier": "protected",
                        }
                    )
                    continue
                resolved = resolved_by_name.get(vault_name)
                if resolved is None:
                    resolved = vault_service.resolve_secret_access(
                        conn,
                        vault_name,
                        purpose="run",
                        requester=requester,
                        delivery=delivery,
                        reserve_one_shot=True,
                    )
                    resolved_by_name[vault_name] = resolved
                if resolved["status"] == "approval_required":
                    req = resolved.get("request") or {}
                    if isinstance(req, dict):
                        approval_request_to_publish = req
                    approval_error = TaskCliError(
                        f"secret '{vault_name}' needs approval before protected delivery",
                        code="approval_required",
                        details={"request_id": req.get("id")},
                    )
                    break
                if resolved["status"] == "standard":
                    current_grant = resolved.get("grant")
                    if isinstance(current_grant, dict) and current_grant.get("one_shot") is True:
                        one_shot_grants.append(current_grant)
                    item = {"name": vault_name, "env": env_name, "envelope": resolved["envelope"]}
                    if protected_names:
                        item["tier"] = "standard"
                    secrets.append(item)
                    continue
                if resolved["status"] == "agent_delivery_ready":
                    raise TaskCliError("protected vault run requires one grant covering the protected selector set", code="mixed_grants")
                raise TaskCliError(f"unsupported vault access status: {resolved['status']}", code="vault_access_error")
    except Exception as exc:
        _raise_after_releasing_one_shot_reservations(engine, one_shot_grants, exc)
    if approval_error is not None:
        _publish_cli_vaults_updated(scope="request", request=approval_request_to_publish)
        _release_one_shot_reservations(engine, one_shot_grants)
        raise approval_error
    return grant, one_shot_grants, secrets


def _resolve_single_vault_delivery(
    engine,
    name: str,
    *,
    requester: dict,
    delivery: dict,
    purpose: str = "run",
) -> tuple[dict | None, dict | None, object]:
    from storage import vault_service

    with engine.begin() as conn:
        resolved = vault_service.resolve_secret_access(
            conn,
            name,
            purpose=purpose,
            requester=requester,
            delivery=delivery,
            reserve_one_shot=True,
        )
    if resolved["status"] == "approval_required":
        req = resolved.get("request") or {}
        if isinstance(req, dict):
            _publish_cli_vaults_updated(scope="request", request=req)
        raise TaskCliError(
            f"secret '{name}' needs approval before protected delivery",
            code="approval_required",
            details={"request_id": req.get("id")},
        )
    if resolved["status"] == "standard":
        current_grant = resolved.get("grant")
        return None, current_grant if isinstance(current_grant, dict) and current_grant.get("one_shot") is True else None, resolved["envelope"]
    if resolved["status"] == "agent_delivery_ready":
        current_grant = resolved["grant"]
        return current_grant, current_grant if current_grant.get("one_shot") is True else None, resolved["envelope"]
    raise TaskCliError(f"unsupported vault access status: {resolved['status']}", code="vault_access_error")


def _resolve_vault_inject_delivery(engine, names: list[str], *, path: str, fmt: str, args=None):
    from storage import vault_service

    requester, delivery, session_id = _vault_cli_delivery_context(args, mode="inject", path=path, format=fmt)
    metas = _preflight_vault_inject_batch(engine, names)
    tiers = {str(meta.get("protection") or "standard") for meta in metas.values()}
    if metas and tiers == {"protected"}:
        with engine.begin() as conn:
            common_grant = vault_service.find_active_grant_for_secrets(
                conn,
                names,
                session_id=session_id,
                purpose="inject",
                reserve_one_shot=True,
            )
            if common_grant is not None:
                return common_grant, [common_grant] if common_grant.get("one_shot") is True else [], [
                    {"name": name, "key": name, "envelope": vault_service.get_protected_envelope(conn, name)}
                    for name in names
                ]
    if metas and tiers == {"standard"}:
        with engine.begin() as conn:
            standard_secrets = [
                {"name": name, "key": name, "envelope": vault_service.get_envelope(conn, name)}
                for name in names
            ]
            common_grant = vault_service.find_active_grant_for_secrets(
                conn,
                names,
                session_id=session_id,
                purpose="inject",
                reserve_one_shot=True,
            )
            if isinstance(common_grant, dict) and common_grant.get("one_shot") is True:
                return None, [common_grant], standard_secrets
    secrets = []
    grant: dict | None = None
    one_shot_grants: list[dict] = []
    approval_error: TaskCliError | None = None
    approval_request_to_publish: dict | None = None
    pre_delivery_error: TaskCliError | None = None
    resolved_by_name: dict[str, dict] = {}
    try:
        with engine.begin() as conn:
            for name in names:
                resolved = resolved_by_name.get(name)
                if resolved is None:
                    resolved = vault_service.resolve_secret_access(
                        conn,
                        name,
                        purpose="inject",
                        requester=requester,
                        delivery=delivery,
                        reserve_one_shot=True,
                    )
                    resolved_by_name[name] = resolved
                if resolved["status"] == "approval_required":
                    req = resolved.get("request") or {}
                    if isinstance(req, dict):
                        approval_request_to_publish = req
                    approval_error = TaskCliError(
                        f"secret '{name}' needs approval before protected delivery",
                        code="approval_required",
                        details={"request_id": req.get("id")},
                    )
                    break
                if resolved["status"] == "standard":
                    current_grant = resolved.get("grant")
                    if isinstance(current_grant, dict) and current_grant.get("one_shot") is True:
                        one_shot_grants.append(current_grant)
                    secrets.append({"name": name, "key": name, "envelope": resolved["envelope"], "protected": False})
                    continue
                if resolved["status"] == "agent_delivery_ready":
                    current_grant = resolved["grant"]
                    if grant is None:
                        grant = current_grant
                    elif grant["id"] != current_grant["id"]:
                        if current_grant.get("one_shot") is True:
                            one_shot_grants.append(current_grant)
                        pre_delivery_error = _mixed_grants_error(
                            "protected vault inject currently requires all protected secrets to share one active grant",
                        )
                        break
                    if current_grant.get("one_shot") is True:
                        one_shot_grants.append(current_grant)
                    secrets.append({"name": name, "key": name, "envelope": resolved["envelope"], "protected": True})
                    continue
                raise TaskCliError(f"unsupported vault access status: {resolved['status']}", code="vault_access_error")
    except Exception as exc:
        _raise_after_releasing_one_shot_reservations(engine, one_shot_grants, exc)
    if approval_error is not None:
        _publish_cli_vaults_updated(scope="request", request=approval_request_to_publish)
        _release_one_shot_reservations(engine, one_shot_grants)
        raise approval_error
    if pre_delivery_error is not None:
        _release_one_shot_reservations(engine, one_shot_grants)
        raise pre_delivery_error
    protected = [item for item in secrets if item["protected"]]
    standard = [item for item in secrets if not item["protected"]]
    if protected and standard:
        _release_one_shot_reservations(engine, one_shot_grants)
        raise TaskCliError(
            "mixing protected and standard secrets in one vault inject is not wired yet",
            code="mixed_protection_tiers",
        )
    selected = protected or standard
    return grant, one_shot_grants, [{key: value for key, value in item.items() if key != "protected"} for item in selected]


def cmd_vault_run(args):
    from storage import vault_service

    help_command = "vibe vault run --help"
    try:
        engine = _open_vault_engine()
        mapping, source_selector = _resolve_vault_run_selectors(engine, args)
        command_argv = list(getattr(args, "command_argv", None) or [])
        if command_argv and command_argv[0] == "--":
            command_argv = command_argv[1:]
        if not command_argv:
            raise TaskCliError(
                "a command is required after --",
                code="missing_command",
                help_command=help_command,
                example="vibe vault run --env OPENAI_API_KEY -- python sync.py",
            )
        # Preflight the command BEFORE resolving — a missing binary shouldn't decrypt the
        # secret, bump use_count, or write a 'delivered' audit for a delivery that never
        # reached a child.
        if shutil.which(command_argv[0]) is None:
            raise TaskCliError(
                f"command not found: {command_argv[0]!r}",
                code="command_not_found",
                help_command=help_command,
                example="vibe vault run --env OPENAI_API_KEY -- python sync.py",
            )
        approval_wait = _vault_approval_wait_seconds(args, help_command=help_command)
        waited_for_approval = False
        while True:
            try:
                grant, one_shot_grants, secrets = _resolve_vault_run_delivery(
                    engine,
                    mapping,
                    command_argv,
                    args=args,
                    source_selector=source_selector,
                )
                break
            except TaskCliError as exc:
                if exc.code == "approval_required" and approval_wait > 0 and not waited_for_approval:
                    _wait_for_vault_delivery_approval(
                        args,
                        exc,
                        timeout=approval_wait,
                        help_command=help_command,
                        operation="vault run",
                    )
                    waited_for_approval = True
                    continue
                raise
    except vault_service.SecretNotFoundError as exc:
        _print_task_error(TaskCliError(f"secret '{exc}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except vault_service.KeypairNotValueDeliverableError as exc:
        _print_task_error(
            TaskCliError(
                str(exc),
                code="keypair_not_value_deliverable",
                hint="Use 'vibe vault sign' for keypair secrets.",
                help_command=help_command,
            )
        )
        return 1
    except vault_service.UnsupportedProtectionError as exc:
        _print_task_error(TaskCliError(str(exc), code="protected_tier_unavailable", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1
    # Hand the envelopes + command to avault: it decrypts, spawns the child with the secret
    # env, waits, and zeroizes. The plaintext never returns here. Protected agent runs stream
    # child stdout/stderr through temporary FIFOs because the resident-agent JSON protocol only
    # returns the exit code.
    from vibe import api

    handoff_started = False
    try:
        if grant is not None:
            secret_env_names = {str(secret["env"]) for secret in secrets if secret.get("env")}
            with _AgentRunOutputBridge(
                sys.stdout.buffer,
                sys.stderr.buffer,
                env_exclude=secret_env_names,
            ) as output_bridge:
                handoff_started = True
                result = api.avault_agent_deliver_run(
                    grant_id=grant["id"],
                    secrets=secrets,
                    context={"session_id": grant.get("session_id"), "purpose": "run"},
                    command=_agent_run_command(
                        command_argv,
                        stdout_path=str(output_bridge.stdout_path),
                        stderr_path=str(output_bridge.stderr_path),
                        stdin_path=str(output_bridge.stdin_path),
                        env_path=str(output_bridge.env_path),
                        keep_env_path=str(output_bridge.keep_env_path),
                    ),
                )
            exit_code = int(result["exit_code"])
            delivered = True
        else:
            handoff_started = True
            exit_code, delivered = _run_delivery_result(api.avault_deliver_run(secrets, command_argv))
    except TaskCliError as exc:
        if handoff_started:
            _consume_after_possible_use(one_shot_grants, reason="vault-run-one-shot")
        else:
            _release_one_shot_reservations(engine, one_shot_grants)
        _print_task_error(exc)
        return 1
    except api.AvaultError as exc:
        if grant is not None and _agent_missing_grant(exc):
            _release_one_shot_reservations(
                engine,
                [one_shot_grant for one_shot_grant in one_shot_grants if one_shot_grant.get("id") != grant.get("id")],
            )
            requester, delivery, _session_id = _vault_cli_delivery_context(args, mode="run", command=command_argv)
            protected_names = [
                str(secret["name"])
                for secret in secrets
                if secret.get("tier") == "protected" and secret.get("name")
            ]
            protected_names = list(dict.fromkeys(protected_names))
            if source_selector:
                delivery["source_selector"] = source_selector
            if protected_names:
                delivery["protected_secret_names"] = protected_names
            _expire_agent_grant_after_missing(
                engine,
                grant["id"],
                protected_names or sorted(set(mapping.values())),
                requester=requester,
                delivery=delivery,
                purpose="run",
            )
            _print_task_error(TaskCliError("protected grant expired; approve the request again", code="approval_required", help_command=help_command))
            return 1
        _finish_one_shot_after_avault_error(engine, one_shot_grants, exc, reason="vault-run-one-shot")
        _print_task_error(TaskCliError(f"avault deliver failed: {exc}", code="avault_failed", help_command=help_command))
        return 1
    except Exception as exc:
        if handoff_started:
            _consume_after_possible_use(one_shot_grants, reason="vault-run-one-shot")
        else:
            _release_one_shot_reservations(engine, one_shot_grants)
        _print_task_error(exc, help_command=help_command)
        return 1
    if delivered:
        _consume_after_possible_use(one_shot_grants, reason="vault-run-one-shot")
        try:
            with engine.begin() as conn:
                vault_service.record_deliveries(
                    conn, sorted(set(mapping.values())), requester={"source": "cli", "pid": os.getpid()}, mode="run"
                )
        except Exception:
            pass
    else:
        _release_one_shot_reservations(engine, one_shot_grants)
    return exit_code


def _wait_for_provision(request_id: str, *, timeout: float, poll_interval: float = 2.0) -> dict | None:
    from storage import vault_service

    deadline = time.monotonic() + timeout
    engine = _open_vault_engine()
    while True:
        with engine.begin() as conn:
            try:
                request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
            except vault_service.RequestNotFoundError:
                raise
        if request.get("status") in {"fulfilled", "denied", "expired", "failed"}:
            return request
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return None


def _vault_access_delivery_ready(request: dict, result: dict | None) -> bool:
    if request.get("request_type") != "access":
        return True
    if not isinstance(result, dict) or result.get("type") != "grant":
        return True
    grant = result.get("grant")
    return not isinstance(grant, dict) or bool(grant.get("delivery_ready"))


def _wait_for_vault_request(
    request_id: str,
    *,
    timeout: float,
    poll_interval: float = 2.0,
    require_delivery_ready: bool = False,
) -> dict | None:
    from storage import vault_service

    deadline = time.monotonic() + timeout
    engine = _open_vault_engine()
    while True:
        with engine.begin() as conn:
            try:
                request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
            except vault_service.RequestNotFoundError:
                raise
            result = None
            if request.get("status") == "approved":
                result = api._vault_request_result(conn, request)
            if request.get("status") in {"approved", "denied", "expired", "failed", "fulfilled"}:
                if not (require_delivery_ready and not _vault_access_delivery_ready(request, result)):
                    return {"request": request, "result": result}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    return None


def _vault_approval_wait_seconds(args, *, help_command: str) -> float:
    wait_value = getattr(args, "approval_wait", None)
    no_wait = bool(getattr(args, "no_approval_wait", False))
    if no_wait and wait_value is not None:
        raise TaskCliError("use --approval-wait or --no-approval-wait, not both", code="invalid_wait", help_command=help_command)
    if no_wait:
        return 0.0
    if wait_value is None:
        return float(DEFAULT_VAULT_APPROVAL_WAIT_SECONDS)
    return float(wait_value)


def _approval_wait_callback_expected(args) -> bool:
    return not _vault_callback_disabled(args) and bool(_vault_cli_session_id(args))


def _approval_wait_timeout_hint(args, request_id: str) -> str:
    if _approval_wait_callback_expected(args):
        return (
            "Avibe will resume this Session when the user approves or denies the request. "
            "After approval, retry the original vault run/fetch command."
        )
    return f"Check the request later with: vibe vault await {request_id}"


def _print_vault_approval_wait_notice(request_id: str, *, timeout: float, operation: str) -> None:
    print(
        f"Waiting for Vault approval in the browser for {operation} "
        f"(request {request_id}, timeout {timeout:g}s)...",
        file=sys.stderr,
        flush=True,
    )


def _vault_terminal_request_error(request: dict, *, help_command: str) -> TaskCliError | None:
    request_id = str(request.get("id") or "")
    status = str(request.get("status") or "")
    if status == "denied":
        return TaskCliError(
            f"Vault request '{request_id}' was denied",
            code="request_denied",
            help_command=help_command,
            details={"request_id": request_id},
        )
    if status == "expired":
        return TaskCliError(
            f"Vault request '{request_id}' expired before approval",
            code="request_expired",
            help_command=help_command,
            details={"request_id": request_id},
        )
    if status == "failed":
        return TaskCliError(
            f"Vault request '{request_id}' failed",
            code="request_failed",
            help_command=help_command,
            details={"request_id": request_id},
        )
    return None


def _wait_for_vault_delivery_approval(args, exc: TaskCliError, *, timeout: float, help_command: str, operation: str) -> None:
    from storage import vault_service

    request_id = str((exc.details or {}).get("request_id") or "").strip()
    if not request_id:
        raise exc
    waiter_id = f"vw_{uuid4().hex[:12]}"
    deadline_at = (datetime.now(timezone.utc) + timedelta(seconds=timeout)).isoformat()
    engine = _open_vault_engine()
    with engine.begin() as conn:
        vault_service.arm_request_waiter(conn, request_id, waiter_id=waiter_id, deadline_at=deadline_at)
    _print_vault_approval_wait_notice(request_id, timeout=timeout, operation=operation)
    waited = _wait_for_vault_request(request_id, timeout=timeout, require_delivery_ready=True)
    if waited is None:
        try:
            with engine.begin() as conn:
                vault_service.timeout_request_waiter(conn, request_id, waiter_id=waiter_id)
        except Exception:
            logger.debug("failed to mark vault request waiter timed out", exc_info=True)
        raise TaskCliError(
            f"Vault approval request '{request_id}' is still waiting for the user",
            code="approval_wait_timeout",
            hint=_approval_wait_timeout_hint(args, request_id),
            help_command=help_command,
            details={
                "request_id": request_id,
                "timeout_seconds": timeout,
                "callback_expected": _approval_wait_callback_expected(args),
            },
        )
    request = waited.get("request") or {}
    terminal_error = _vault_terminal_request_error(request, help_command=help_command)
    with engine.begin() as conn:
        vault_service.complete_request_waiter(conn, request_id, waiter_id=waiter_id)
    if terminal_error is not None:
        raise terminal_error


def cmd_vault_request(args):
    from storage import vault_crypto, vault_service

    help_command = "vibe vault request --help"
    try:
        name = args.name
        if not vault_crypto.is_valid_secret_name(name):
            raise TaskCliError(f"invalid secret name: {name!r} (use ^[A-Za-z_][A-Za-z0-9_]*$)", code="invalid_name", help_command=help_command)
        spec = _load_vault_request_spec(args, help_command=help_command)
        engine = _open_vault_engine()
        with engine.begin() as conn:
            req = vault_service.create_provision_request(
                conn,
                name,
                reason=getattr(args, "reason", None),
                spec=spec,
                # Carry the caller session (AVIBE_SESSION_ID) so the provision card can be
                # scoped to the originating chat, like access/sign requests.
                requester=_vault_cli_requester(args),
            )
        _publish_cli_vaults_updated(scope="request", request=req, secret_name=name)
        if req.get("status") == "fulfilled":
            # Secret already existed — no point waiting.
            _print_cli_payload(
                "vault_request",
                request_id=req["id"],
                secret_name=name,
                status="fulfilled",
                request=req,
                message=f"'{name}' is already in the vault — use it via: vibe vault run --env {name} -- <command>",
            )
            return 0
        wait_seconds = getattr(args, "wait", None)
        if wait_seconds:
            waited = _wait_for_provision(req["id"], timeout=float(wait_seconds))
            if waited:
                # The wait delivered a terminal outcome synchronously, so suppress the
                # now-redundant async auto-resume callback for this request (best-effort — a
                # race with the ~2s sweep risks at most one benign duplicate resume). A wait
                # that TIMES OUT skips this and leaves the callback armed, so a later resolution
                # still wakes the agent.
                try:
                    with _open_vault_engine().begin() as conn:
                        vault_service.mark_request_callback(conn, str(req["id"]), status="skipped")
                except Exception:
                    pass
                if waited.get("status") == "denied":
                    _print_task_error(
                        TaskCliError(
                            f"request for '{name}' was denied",
                            code="request_denied",
                            help_command=help_command,
                            details={"request_id": req["id"]},
                        )
                    )
                    return 1
                if waited.get("status") == "expired":
                    _print_task_error(
                        TaskCliError(
                            f"request for '{name}' expired",
                            code="request_expired",
                            help_command=help_command,
                            details={"request_id": req["id"]},
                        )
                    )
                    return 1
                if waited.get("status") == "failed":
                    _print_task_error(
                        TaskCliError(
                            f"request for '{name}' failed",
                            code="request_failed",
                            help_command=help_command,
                            details={"request_id": req["id"]},
                        )
                    )
                    return 1
                _print_cli_payload(
                    "vault_request",
                    request_id=req["id"],
                    secret_name=name,
                    status="fulfilled",
                    request=waited,
                    message=f"'{name}' is now available — use it via: vibe vault run --env {name} -- <command>",
                )
                return 0
            _print_task_error(
                TaskCliError(
                    f"request for '{name}' was not fulfilled within {wait_seconds}s",
                    code="request_timeout",
                    help_command=help_command,
                    details={"request_id": req["id"]},
                )
            )
            return 1
        _print_cli_payload(
            "vault_request",
            request_id=req["id"],
            secret_name=name,
            status="pending",
            request=req,
            message=_vault_request_pending_message(
                name,
                req,
                has_spec=bool(spec),
                callback_enabled=not _vault_callback_disabled(args) and bool(_vault_cli_session_id(args)),
            ),
        )
        return 0
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except vault_service.SecretNameCaseConflictError as exc:
        _print_task_error(TaskCliError(str(exc), code="secret_name_case_conflict", help_command=help_command))
        return 1
    except vault_service.VaultServiceError as exc:
        _print_task_error(TaskCliError(str(exc), code="invalid_spec", help_command=help_command))
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_sign(args):
    from storage import vault_crypto, vault_service

    help_command = "vibe vault sign --help"
    name = getattr(args, "name", "")
    try:
        if not vault_crypto.is_valid_secret_name(name):
            raise TaskCliError(f"invalid secret name: {name!r} (use ^[A-Za-z_][A-Za-z0-9_]*$)", code="invalid_name", help_command=help_command)
        digest = api._sign_digest_from_payload(getattr(args, "digest", None))
        scheme = getattr(args, "scheme", None) or "ecdsa-secp256k1-recoverable"
        signing_context = _vault_cli_signing_context(args, digest=digest, help_command=help_command)
        engine = _open_vault_engine()
        with engine.begin() as conn:
            meta = vault_service.get_secret_meta(conn, name)
            if meta.get("kind") != "keypair":
                raise TaskCliError(f"secret '{name}' is not a signing key", code="not_signing_key", help_command=help_command)
            if (meta.get("signer_kind") or "local") != "local":
                raise TaskCliError(
                    f"secret '{name}' is not locally signable",
                    code="unsupported_signer_kind",
                    help_command=help_command,
                )
            needs_approval = vault_service.sign_needs_approval(conn, name)
            if meta.get("protection") == "protected" and signing_context is None:
                raise TaskCliError(
                    "protected signing requires --signing-context-json",
                    code="missing_signing_context",
                    help_command=help_command,
                )
            if needs_approval:
                request = vault_service.create_sign_request(
                    conn,
                    name,
                    digest=digest,
                    scheme=scheme,
                    signing_context=signing_context,
                    requester=_vault_cli_requester(args),
                    delivery=_vault_cli_delivery(args, mode="sign"),
                )
        if not needs_approval:
            result = api.vault_sign(
                {
                    "name": name,
                    "digest": digest,
                    "scheme": scheme,
                    "signing_context": signing_context,
                    "requester": _vault_cli_requester(args),
                }
            )
            _print_cli_payload(
                "vault_signature",
                name=name,
                scheme=scheme,
                digest=digest,
                signature=result.get("signature"),
            )
            return 0
        request = api._attach_signed_sign_operation_context(str(request["id"]))
        _publish_cli_vaults_updated(scope="request", request=request)
        _print_cli_payload(
            "vault_sign_request",
            request_id=request["id"],
            request=request,
            message=_vault_request_followup_message(args, request["id"], resolved_verb="approves or denies the signature"),
        )
        return 0
    except vault_service.SecretNotFoundError:
        _print_task_error(TaskCliError(f"secret '{name}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except api.VaultApiError as exc:
        _print_task_error(TaskCliError(str(exc), code=exc.code, help_command=help_command))
        return 1
    except vault_service.InvalidRequestError as exc:
        _print_task_error(TaskCliError(str(exc), code="invalid_request", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_await(args):
    from storage import vault_service

    help_command = "vibe vault await --help"
    request_id = str(getattr(args, "request_id", "") or "").strip()
    timeout = float(getattr(args, "wait", None) or 0)
    try:
        engine = _open_vault_engine()
        with engine.begin() as conn:
            request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
            result = api._vault_request_result(conn, request)
        if timeout > 0 and request.get("status") in {"pending", "signing"}:
            waited = _wait_for_vault_request(request_id, timeout=timeout)
            if waited is None:
                raise TaskCliError(
                    f"request '{request_id}' was not decided within {timeout:g}s",
                    code="request_timeout",
                    help_command=help_command,
                    details={"request_id": request_id},
                )
            request = waited["request"]
            result = waited.get("result")
        if request.get("status") == "denied":
            raise TaskCliError(
                f"request '{request_id}' was denied",
                code="request_denied",
                help_command=help_command,
                details={"request_id": request_id},
            )
        if request.get("status") == "expired":
            raise TaskCliError(
                f"request '{request_id}' expired",
                code="request_expired",
                help_command=help_command,
                details={"request_id": request_id},
            )
        if request.get("status") == "failed":
            raise TaskCliError(
                f"request '{request_id}' failed",
                code="request_failed",
                help_command=help_command,
                details={"request_id": request_id},
            )
        if request.get("status") != "approved":
            _print_cli_payload("vault_request_status", request_id=request_id, status=request.get("status"), request=request)
            return 0
        _print_cli_payload("vault_request_result", request_id=request_id, status=request.get("status"), request=request, result=result)
        return 0
    except vault_service.RequestNotFoundError:
        _print_task_error(TaskCliError(f"request '{request_id}' not found", code="request_not_found", help_command=help_command))
        return 1
    except vault_service.InvalidRequestError as exc:
        _print_task_error(TaskCliError(str(exc), code="invalid_request", help_command=help_command))
        return 1
    except api.AvaultError as exc:
        _print_task_error(TaskCliError(f"avault sign failed: {exc}", code="avault_failed", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def _vault_request_followup_message(args, request_id: str, *, resolved_verb: str) -> str:
    """Agent-facing follow-up for an access/sign request.

    By default this Session auto-resumes when the request resolves, so the agent should just end
    its turn. We deliberately do NOT suggest ``vault await`` here: with the callback armed, awaiting
    would return the synchronous result AND still leave the callback to fire a second turn. To
    block synchronously the agent must opt out at creation with ``--no-callback`` (then this points
    at ``vault await``).
    """
    if _vault_callback_disabled(args) or not _vault_cli_session_id(args):
        return f"Request recorded. Check the result yourself with: vibe vault await {request_id}"
    return (
        f"Request recorded. This Session resumes automatically once the user {resolved_verb} — "
        f"end your turn now; you'll be woken with the outcome. (To block synchronously instead, "
        f"re-issue the request with --no-callback.)"
    )


def _vault_request_pending_message(
    name: str,
    request: dict[str, object],
    *,
    has_spec: bool,
    callback_enabled: bool = True,
) -> str:
    resume = (
        " This Session resumes automatically once it is provided — you can end your turn now."
        if callback_enabled
        else f" Check back with: vibe vault await {request['id']}."
    )
    if has_spec:
        return (
            f"Recorded a request for '{name}'. The user provides it from the chat request card or the Vaults "
            f"page 'Provide secret' row, whose request-specific form preserves the requested tags, policy, and "
            f"skill links.{resume} Then use: vibe vault run --env {name} -- <command>"
        )
    return (
        f"Recorded a request for '{name}'. The user provides it from the chat request card, the Vaults page "
        f"'Provide secret' row, or by adding a secret named {name}.{resume} Then use: vibe vault run --env {name} -- <command>"
    )


def _load_vault_request_spec(args, *, help_command: str) -> dict | None:
    spec_sources = [
        value
        for value in (
            getattr(args, "spec_json", None),
            getattr(args, "spec", None),
        )
        if value
    ]
    if len(spec_sources) > 1:
        raise TaskCliError("use only one of --spec-json or --spec", code="invalid_spec", help_command=help_command)

    raw: str | None = None
    if getattr(args, "spec_json", None):
        raw = str(args.spec_json)
    elif getattr(args, "spec", None):
        spec_arg = str(args.spec)
        if spec_arg == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = Path(spec_arg).read_text(encoding="utf-8")
            except OSError as exc:
                raise TaskCliError(f"cannot read --spec: {exc}", code="spec_unreadable", help_command=help_command) from exc

    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise TaskCliError(f"invalid request spec JSON: {exc}", code="invalid_spec", help_command=help_command) from exc
    if not isinstance(parsed, dict):
        raise TaskCliError("request spec must be a JSON object", code="invalid_spec", help_command=help_command)
    return parsed


def _host_allowed(host, allowed) -> bool:
    """Exact host match, or a leading-dot entry (``.github.com``) matching subdomains.

    Hostnames are case-insensitive, so both sides are lowercased — otherwise a stored
    ``API.GITHUB.COM`` would never match the lowercase ``urlsplit().hostname`` and a valid
    host-bound secret becomes unusable.
    """
    if not host:
        return False
    host = host.lower()
    for entry in allowed or []:
        entry = str(entry).strip().lower()
        if not entry:
            continue
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


# Headers that would override the request authority. The allowlist binds a secret to the URL
# hostname, so letting any of these through (via --header OR a stored auth-header policy) could
# route the credential-bearing request to a different vhost on the same endpoint.
_FORBIDDEN_FETCH_HEADER_NAMES = frozenset({"host"})


def _reject_forbidden_header(name, *, help_command: str) -> None:
    if str(name).strip().lower() in _FORBIDDEN_FETCH_HEADER_NAMES:
        raise TaskCliError(
            f"the {str(name).strip()!r} header cannot be set in vault fetch (it overrides the request authority)",
            code="forbidden_header",
            help_command=help_command,
        )


def _parse_headers(specs) -> dict:
    headers: dict[str, str] = {}
    for spec in specs or []:
        if ":" not in spec:
            raise TaskCliError(f"invalid --header (expected 'Name: value'): {spec!r}", code="invalid_header", help_command="vibe vault fetch --help")
        name, _, value = spec.partition(":")
        name = name.strip()
        _reject_forbidden_header(name, help_command="vibe vault fetch --help")
        headers[name] = value.strip()
    return headers


def _read_request_body(args):
    data = getattr(args, "data", None)
    data_file = getattr(args, "data_file", None)
    if data is not None and data_file:
        raise TaskCliError("use at most one of --data / --data-file", code="invalid_data", help_command="vibe vault fetch --help")
    if data is not None:
        return data.encode("utf-8")
    if data_file:
        try:
            return Path(data_file).read_bytes()
        except OSError as exc:
            raise TaskCliError(f"cannot read --data-file: {exc}", code="data_file_unreadable", help_command="vibe vault fetch --help") from exc
    return None


def _validate_vault_fetch_output(output: str | None, *, help_command: str) -> None:
    if not output:
        return
    out_path = Path(output)
    if out_path.exists():
        # Require an existing regular file: a dir can't be written as a file, and a
        # FIFO / device (e.g. /dev/full) passes os.access but write_bytes can block or
        # fail AFTER the credential-bearing request already ran.
        writable = out_path.is_file() and os.access(out_path, os.W_OK)
    else:
        writable = out_path.parent.is_dir() and os.access(out_path.parent, os.W_OK)
    if not writable:
        raise TaskCliError(
            f"output path is not writable: {output}",
            code="output_unwritable",
            help_command=help_command,
        )


def _build_vault_fetch_request(
    engine,
    *,
    name: str,
    url: str,
    host: str,
    method: str,
    headers: dict,
    body,
    help_command: str,
) -> dict:
    from storage import vault_service

    # Read policy in a read connection. The host check runs BEFORE handing the envelope to
    # avault, so a disallowed target never even unwraps the secret. Callers run this both before
    # and after an approval wait so a mid-wait metadata edit cannot leave a stale allowlist or auth
    # injection policy in the egress frame.
    with engine.connect() as conn:
        policy = vault_service.get_secret_policy(conn, name)
        meta = vault_service.get_secret_meta(conn, name)
        if meta.get("kind") == "keypair":
            raise vault_service.KeypairNotValueDeliverableError(
                f"{name} is a signing key; use vault_sign instead of value delivery"
            )
        allowed = policy.get("allowed_hosts") or []
        if not allowed:
            raise TaskCliError(
                f"secret '{name}' has no allowed_hosts; it cannot be used via fetch "
                "(configure allowed hosts in the Vaults UI)",
                code="proxy_unbound",
                help_command=help_command,
            )
        if not _host_allowed(host, allowed):
            raise TaskCliError(
                f"host {host!r} is not allowed for secret '{name}'",
                code="host_not_allowed",
                help_command=help_command,
                details={"host": host, "allowed_hosts": allowed},
            )
        auth = policy.get("auth") or {"type": "bearer"}
        if auth.get("type") == "header":
            # Defensive: set-time validation blocks new Host auth-headers; this also guards
            # legacy / hand-edited policies. Reject BEFORE handing off so a bad policy never
            # even unwraps the secret.
            _reject_forbidden_header(auth.get("name", ""), help_command=help_command)

    auth_type = auth.get("type") or "bearer"
    if auth_type == "header":
        inject = {"type": "header", "name": auth.get("name", "")}
    elif auth_type == "query":
        inject = {"type": "query", "name": auth.get("name", "")}
    else:
        inject = {"type": "bearer"}
    return {
        "method": method,
        "url": url,
        "allowed_hosts": allowed,
        "headers": headers,
        "body": body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body,
        "inject": inject,
    }


def cmd_vault_fetch(args):
    from urllib.parse import urlsplit

    from storage import vault_service
    from vibe import api

    help_command = "vibe vault fetch --help"
    engine = None
    grant = None
    one_shot_grant = None
    handoff_started = False
    name = getattr(args, "auth", "")
    host = ""
    method = "GET"
    try:
        url = args.url
        method = (getattr(args, "method", None) or "GET").upper()
        headers = _parse_headers(getattr(args, "header", None))
        body = _read_request_body(args)
        if method in {"TRACE", "TRACK", "CONNECT"}:
            # These echo the request (incl. the attached Authorization / custom-auth header) back
            # in the response body, which fetch writes to stdout — leaking the secret value into
            # stdout/transcripts. Reject before decrypting or sending.
            raise TaskCliError(
                f"method {method} is not allowed for vault fetch (it can echo the credential into the response)",
                code="method_not_allowed",
                help_command=help_command,
            )
        # Preflight --output BEFORE sending: a side-effecting request (POST/PATCH) must not run
        # and then fail on a local write, or the agent will retry and duplicate the action. Check
        # the target itself (an existing dir, or an existing file we can't write), not just the
        # parent.
        output = getattr(args, "output", None)
        _validate_vault_fetch_output(output, help_command=help_command)
        host = urlsplit(url).hostname
        if not host:
            raise TaskCliError(f"invalid --url: {url!r}", code="invalid_url", help_command=help_command)
        # Never attach a credential over plaintext: a real host must be HTTPS so domain
        # binding can't be used to downgrade transport. Loopback is exempt for local dev.
        is_loopback = host in {"localhost", "127.0.0.1", "::1"}
        scheme = (urlsplit(url).scheme or "").lower()
        if scheme != "https" and not is_loopback:
            raise TaskCliError(
                f"refusing to attach a credential over plaintext {scheme or 'http'}:// to {host!r}; use https (loopback exempt)",
                code="insecure_transport",
                help_command=help_command,
            )

        engine = _open_vault_engine()
        request = _build_vault_fetch_request(
            engine,
            name=name,
            url=url,
            host=host,
            method=method,
            headers=headers,
            body=body,
            help_command=help_command,
        )
        requester, delivery, _session_id = _vault_cli_delivery_context(args, mode="fetch", host=host, method=method)
        approval_wait = _vault_approval_wait_seconds(args, help_command=help_command)
        waited_for_approval = False
        while True:
            try:
                grant, one_shot_grant, sealed = _resolve_single_vault_delivery(
                    engine,
                    name,
                    requester=requester,
                    delivery=delivery,
                    purpose="fetch",
                )
                break
            except TaskCliError as exc:
                if exc.code == "approval_required" and approval_wait > 0 and not waited_for_approval:
                    _wait_for_vault_delivery_approval(
                        args,
                        exc,
                        timeout=approval_wait,
                        help_command=help_command,
                        operation="vault fetch",
                    )
                    waited_for_approval = True
                    continue
                raise
        if waited_for_approval:
            _validate_vault_fetch_output(output, help_command=help_command)
            request = _build_vault_fetch_request(
                engine,
                name=name,
                url=url,
                host=host,
                method=method,
                headers=headers,
                body=body,
                help_command=help_command,
            )
        handoff_started = True
        if grant is not None:
            result = api.avault_agent_deliver_fetch(
                grant_id=grant["id"],
                name=name,
                sealed=sealed,
                request=request,
                context={"session_id": grant.get("session_id"), "purpose": "fetch"},
            )
        else:
            result = api.avault_deliver_fetch(name, sealed, request)
        _consume_after_possible_use([one_shot_grant] if one_shot_grant is not None else [], reason="vault-fetch-one-shot")
        status = int(result.get("status") or 0)
        resp_body = result.get("body") or ""

        try:
            with engine.begin() as conn:
                vault_service.record_proxy_use(
                    conn,
                    name,
                    requester={"source": "cli", "pid": os.getpid()},
                    delivery={"host": host, "method": method, "status": status},
                )
        except Exception:
            # The upstream request already happened (possibly a side-effecting POST/PATCH). A
            # bookkeeping failure must not make the agent see a failure and retry — duplicating
            # the upstream action. Contain it and still return the real response below.
            pass
    except vault_service.SecretNotFoundError:
        _print_task_error(TaskCliError(f"secret '{args.auth}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except vault_service.KeypairNotValueDeliverableError as exc:
        _print_task_error(TaskCliError(str(exc), code="keypair_not_value_deliverable", help_command=help_command))
        return 1
    except vault_service.UnsupportedProtectionError as exc:
        _print_task_error(TaskCliError(str(exc), code="protected_tier_unavailable", help_command=help_command))
        return 1
    except TaskCliError as exc:
        if handoff_started:
            _consume_after_possible_use([one_shot_grant] if one_shot_grant is not None else [], reason="vault-fetch-one-shot")
        else:
            _release_one_shot_reservations(engine, [one_shot_grant] if one_shot_grant is not None else [])
        _print_task_error(exc)
        return 1
    except api.AvaultError as exc:
        _finish_one_shot_after_avault_error(
            engine,
            [one_shot_grant] if one_shot_grant is not None else [],
            exc,
            reason="vault-fetch-one-shot",
        )
        if engine is not None and isinstance(grant, dict) and _agent_missing_grant(exc):
            grant_id = grant.get("id")
            if grant_id:
                _expire_agent_grant_after_missing(
                    engine,
                    grant_id,
                    [name],
                    requester=requester,
                    delivery=delivery,
                    purpose="fetch",
                )
                _print_task_error(TaskCliError("protected grant expired; approve the request again", code="approval_required", help_command=help_command))
                return 1
        _print_task_error(TaskCliError(f"request failed: {exc}", code="request_failed", help_command=help_command))
        return 1
    except Exception as exc:
        if handoff_started:
            _consume_after_possible_use([one_shot_grant] if one_shot_grant is not None else [], reason="vault-fetch-one-shot")
        else:
            _release_one_shot_reservations(engine, [one_shot_grant] if one_shot_grant is not None else [])
        _print_task_error(exc, help_command=help_command)
        return 1

    # The response body is the upstream API's response (not a secret) — pass it through. avault
    # returns it as UTF-8 text (binary responses are rejected upstream by avault).
    output = getattr(args, "output", None)
    body_bytes = resp_body.encode("utf-8")
    if output:
        try:
            Path(output).write_bytes(body_bytes)
        except OSError as exc:
            # The secret-bearing request already completed; a bad --output path should still
            # yield a structured error (missing parent / permission denied), not a traceback.
            _print_task_error(TaskCliError(f"cannot write output file: {exc}", code="output_unwritable", help_command=help_command))
            return 1
    else:
        sys.stdout.buffer.write(body_bytes)
        sys.stdout.flush()
    return 0 if 200 <= status <= 299 else 1


def cmd_vault_export(args):
    # Deprecated. avault (the custody core) deliberately has no plaintext-to-stdout sink —
    # emitting `export NAME=...` for `eval` would hand the decrypted value back to the shell
    # (and anything capturing stdout). Use `vibe vault run`, which injects secrets straight
    # into a child process's environment — never your shell, never disk.
    help_command = "vibe vault run --help"
    _print_task_error(
        TaskCliError(
            "vibe vault export is no longer supported. Use "
            "'vibe vault run --env NAME -- <command>' to inject secrets directly into a "
            "process (off your shell and off disk).",
            code="export_deprecated",
            help_command=help_command,
        )
    )
    return 1


def cmd_vault_inject(args):
    # Advanced / not recommended (prefer 'run'): render secrets into a 0600 file for
    # tools that read config files. The value lands on disk. avault renders + writes the
    # file (it holds the plaintext); nothing lands in this process. Help-only.
    from storage import vault_service
    from vibe import api

    help_command = "vibe vault inject --help"
    engine = None
    grant = None
    one_shot_grants: list[dict] = []
    keys: list[str] = []
    handoff_started = False
    try:
        keys = [k.strip() for k in (getattr(args, "keys", None) or "").split(",") if k.strip()]
        keys = list(dict.fromkeys(keys))  # dedupe, preserve order: A,A is one entry + one audit
        if not keys:
            raise TaskCliError("--keys A,B is required", code="missing_keys", help_command=help_command)
        out = getattr(args, "out", None)
        if not out:
            raise TaskCliError("--out FILE is required", code="missing_out", help_command=help_command)
        fmt = (getattr(args, "format", None) or "dotenv").lower()
        if fmt in ("yaml", "toml"):
            # avault renders the file (it holds the plaintext); only dotenv/json are wired in P1.1.
            raise TaskCliError(
                f"--format {fmt} is not yet supported via avault (use dotenv or json)",
                code="format_unavailable",
                help_command=help_command,
            )
        if fmt not in ("dotenv", "json"):
            raise TaskCliError(f"unknown --format: {fmt!r} (dotenv|json)", code="invalid_format", help_command=help_command)
        engine = _open_vault_engine()
        resolved_out = _resolve_cli_output_path(str(out))
        _preflight_cli_output_path(resolved_out, help_command=help_command)
        grant, one_shot_grants, secrets = _resolve_vault_inject_delivery(engine, keys, path=resolved_out, fmt=fmt, args=args)
        # avault writes the 0600 file atomically; if the path is unwritable it raises and no
        # delivery is recorded.
        handoff_started = True
        if grant is not None:
            api.avault_agent_deliver_inject(
                grant_id=grant["id"],
                path=resolved_out,
                fmt=fmt,
                secrets=secrets,
            )
        else:
            api.avault_deliver_inject(resolved_out, fmt, secrets)
        _consume_after_possible_use(one_shot_grants, reason="vault-inject-one-shot")
        # The file is on disk → delivered. A bookkeeping failure must not report a failed command
        # (callers would retry though the secrets are already written), so record best-effort.
        try:
            with engine.begin() as conn:
                vault_service.record_deliveries(conn, keys, requester={"source": "cli", "pid": os.getpid()}, mode=f"inject:{fmt}")
        except Exception:
            pass
        _print_cli_payload("vault_inject", written=True, path=resolved_out, format=fmt, keys=keys)
        return 0
    except vault_service.SecretNotFoundError as exc:
        _print_task_error(TaskCliError(f"secret '{exc}' not found", code="secret_not_found", help_command=help_command))
        return 1
    except vault_service.KeypairNotValueDeliverableError as exc:
        _print_task_error(TaskCliError(str(exc), code="keypair_not_value_deliverable", help_command=help_command))
        return 1
    except vault_service.UnsupportedProtectionError as exc:
        _print_task_error(TaskCliError(str(exc), code="protected_tier_unavailable", help_command=help_command))
        return 1
    except TaskCliError as exc:
        if handoff_started:
            _consume_after_possible_use(one_shot_grants, reason="vault-inject-one-shot")
        else:
            _release_one_shot_reservations(engine, one_shot_grants)
        _print_task_error(exc)
        return 1
    except api.AvaultError as exc:
        _finish_one_shot_after_avault_error(engine, one_shot_grants, exc, reason="vault-inject-one-shot")
        if engine is not None and isinstance(grant, dict) and _agent_missing_grant(exc):
            requester, delivery, _session_id = _vault_cli_delivery_context(
                args,
                mode="inject",
                path=resolved_out,
                format=fmt,
            )
            _expire_agent_grant_after_missing(
                engine,
                grant["id"],
                keys,
                requester=requester,
                delivery=delivery,
                purpose="inject",
            )
            _print_task_error(TaskCliError("protected grant expired; approve the request again", code="approval_required", help_command=help_command))
            return 1
        _print_task_error(TaskCliError(f"avault inject failed: {exc}", code="avault_failed", help_command=help_command))
        return 1
    except Exception as exc:
        if handoff_started:
            _consume_after_possible_use(one_shot_grants, reason="vault-inject-one-shot")
        else:
            _release_one_shot_reservations(engine, one_shot_grants)
        _print_task_error(exc, help_command=help_command)
        return 1


def _read_passphrase_stdin(help_command: str) -> str:
    data = sys.stdin.read()
    phrase = data.split("\n", 1)[0].strip() if data else ""
    if not phrase:
        raise TaskCliError("a passphrase is required on stdin", code="missing_passphrase", help_command=help_command)
    return phrase


def cmd_vault_key_export(args):
    from storage import vault_service
    from vibe import api

    help_command = "vibe vault key export --help"
    try:
        passphrase = _read_passphrase_stdin(help_command)
        blob = api.avault_key_export(passphrase)
        out = getattr(args, "out", None)
        if out:
            # 0600 from the moment the file exists (the blob holds the passphrase-wrapped
            # key) — write_atomic leaves no window where it's world-readable, whatever the
            # umask and whatever mode ``out`` already had.
            write_atomic(Path(out), json.dumps(blob, indent=2) + "\n")
            _print_cli_payload("vault_key_export", written=True, path=str(out))
        else:
            print(json.dumps(blob, indent=2))
            try:
                # print() may only buffer; flush so a piped consumer actually received the blob
                # before we audit it as exported.
                sys.stdout.flush()
            except BrokenPipeError:
                return 1  # pipe closed early → blob not delivered → don't audit
        # Exporting the machine key is the most sensitive vault op (it can decrypt every
        # standard-tier secret once the passphrase is known), so record a value-free audit row
        # for the activity panel. Best-effort: an audit-write hiccup must not fail a delivered
        # export.
        try:
            engine = _open_vault_engine()
            with engine.begin() as conn:
                vault_service.audit(
                    conn,
                    "key_exported",
                    requester={"source": "cli", "pid": os.getpid()},
                    delivery={"out": str(out) if out else "stdout"},
                )
        except Exception:
            pass
        return 0
    except api.AvaultError as exc:
        _print_task_error(TaskCliError(f"avault key export failed: {exc}", code="vault_key_export_failed", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_vault_key_import(args):
    from storage import vault_service
    from vibe import api

    help_command = "vibe vault key import --help"
    try:
        passphrase = _read_passphrase_stdin(help_command)
        try:
            blob = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TaskCliError(f"cannot read export file: {exc}", code="export_file_unreadable", help_command=help_command) from exc
        api.avault_key_import(blob, passphrase, force=bool(getattr(args, "force", False)))
        # Replacing the machine key changes vault decryptability for every standard-tier secret;
        # record it for the activity panel, symmetric with key export. Best-effort.
        try:
            engine = _open_vault_engine()
            with engine.begin() as conn:
                vault_service.audit(
                    conn, "key_imported", requester={"source": "cli", "pid": os.getpid()}, delivery={"file": str(args.file)}
                )
        except Exception:
            pass
        _print_cli_payload("vault_key_import", imported=True)
        return 0
    except api.AvaultError as exc:
        _print_task_error(TaskCliError(f"avault key import failed: {exc}", code="vault_key_import_failed", help_command=help_command))
        return 1
    except TaskCliError as exc:
        _print_task_error(exc)
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command=help_command)
        return 1


def cmd_watch_add(args):
    reserved_session_id: Optional[str] = None
    try:
        caller_context = caller_context_from_env()
        caller_user_context = caller_resource_user_context(caller_context)
        session_default_notice = _apply_caller_session_default(
            args,
            caller_context,
            purpose="Watch target Session",
        )
        session_policy = _validate_definition_session_policy(
            args,
            schedule_type="watch",
            help_command="vibe watch add --help",
            allow_caller_session_default=caller_context is not None,
        )
        scope_key = _resolve_definition_scope_key(args, caller_context=caller_context, help_command="vibe watch add --help")
        command, shell_command = _resolve_watch_command(args, help_command="vibe watch add --help")
        session_id, session_key = _resolve_session_target_args(
            args,
            required=session_policy == "existing",
            help_command="vibe watch add --help",
        )
        agent_resolution = _resolve_agent_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key or scope_key or "",
            help_command="vibe watch add --help",
        )
        agent = agent_resolution.agent
        agent_name = agent.name if agent else None
        expected_enabled_agent_id, expected_reference_agent_id = _agent_write_guard_ids(
            agent_resolution
        )
        cwd = _resolve_watch_cwd(args.cwd, help_command="vibe watch add --help", default_to_invocation=True)
        session_workdir = (
            _resolve_definition_session_cwd(
                explicit_cwd=getattr(args, "cwd", None),
                existing_cwd=None,
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args),
                help_command="vibe watch add --help",
            )
            if session_policy != "existing"
            else None
        )
        if session_policy == "create_once":
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                agent_id=agent.id if agent else None,
                deliver_key=scope_key or "",
                workdir=session_workdir,
                help_command="vibe watch add --help",
                require_enabled_agent=expected_enabled_agent_id is not None,
                expected_reference_agent_id=expected_reference_agent_id,
            )
            reserved_session_id = session_id
        session_target, delivery_target = _validate_definition_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=getattr(args, "post_to", None),
            deliver_key=getattr(args, "deliver_key", None),
            scope_key=scope_key,
            help_command="vibe watch add --help",
        )

        mode = "forever" if args.forever else "once"
        _validate_watch_timing(
            timeout_seconds=float(args.timeout),
            retry_delay_seconds=float(args.retry_delay),
            lifetime_timeout_seconds=float(args.lifetime_timeout),
            help_command="vibe watch add --help",
        )
        prefix = _normalize_task_name(getattr(args, "prefix", None))
        message = _resolve_optional_message_input(
            args,
            help_command="vibe watch add --help",
            example_command="vibe watch add --session-id sesk8m4q2p7x --message 'Continue when the waiter finishes.'",
            legacy_prefix=prefix,
        )

        retry_exit_codes = sorted(set(args.retry_exit_code or [DEFAULT_RETRY_EXIT_CODE]))
        store = _watch_store()
        watch = store.add_watch(
            name=_normalize_watch_name(getattr(args, "name", None)),
            session_key=session_key,
            session_id=session_id,
            command=command,
            shell_command=shell_command,
            prefix=prefix,
            message=message,
            cwd=cwd,
            mode=mode,
            timeout_seconds=float(args.timeout),
            lifetime_timeout_seconds=float(args.lifetime_timeout),
            retry_exit_codes=retry_exit_codes,
            retry_delay_seconds=float(args.retry_delay),
            post_to=args.post_to,
            deliver_key=args.deliver_key,
            agent_name=agent_name,
            session_policy=session_policy,
            metadata=_definition_metadata_with_scope(caller_context, scope_id=scope_key, session_workdir=session_workdir),
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
            user_context=caller_user_context,
        )
        reserved_session_id = None
        runtime_store = _watch_runtime_store()
        watch, runtime_entry = _wait_for_watch_startup(store, runtime_store, watch.id)
        warnings = _collect_target_warnings(session_target, delivery_target)
        watch_payload = _watch_mutation_payload(watch, runtime_entry)
        payload_fields = {
            "warnings": warnings,
        }
        if session_default_notice:
            payload_fields["session_default_notice"] = session_default_notice
        _print_definition_payload(watch_payload, **payload_fields)
        return 0
    except Exception as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="watch creation failed before its Session reservation was adopted",
            )
        _print_task_error(exc, help_command="vibe watch add --help")
        return 1


def cmd_watch_list(
    *,
    include_finished: bool = False,
    brief: bool = True,
    page_request: PageRequest = PageRequest(),
):
    with _definition_read_store() as store:
        page_result = store.list_watches_page(
            page_request=page_request,
            include_successful_finished=include_finished,
            enabled_first=True,
        )
    command = ["vibe", "watch", "list"]
    if include_finished:
        command.append("--include-finished")
    _print_definition_list_payload(
        page_result,
        payload_for_item=lambda watch: _watch_projection_payload(watch, brief=brief),
        command=command,
    )
    return 0


def cmd_watch_show(watch_id: str):
    with _definition_read_store() as store:
        watch = store.get_watch(watch_id)
    if watch is None:
        _print_task_error(
            TaskCliError(
                f"watch '{watch_id}' not found",
                code="watch_not_found",
                hint="Use 'vibe watch list' to find a valid watch ID before calling show.",
                help_command="vibe watch list",
                details={"watch_id": watch_id},
            )
        )
        return 1
    watch_payload = _watch_projection_payload(watch)
    _print_definition_payload(watch_payload)
    return 0


def cmd_watch_set_enabled(watch_id: str, enabled: bool):
    store = _watch_store()
    watch = store.get_watch(watch_id)
    if watch is None:
        action = "resume" if enabled else "pause"
        _print_task_error(
            TaskCliError(
                f"watch '{watch_id}' not found",
                code="watch_not_found",
                hint=f"Use 'vibe watch list' to find a valid watch ID before calling {action}.",
                help_command="vibe watch list",
                details={"watch_id": watch_id},
            )
        )
        return 1
    try:
        updated = store.set_enabled(watch_id, enabled)
    except DefinitionWriteConflict as exc:
        _print_task_error(
            _definition_conflict_cli_error(
                exc,
                help_command="vibe watch list",
                details={"watch_id": watch_id},
            )
        )
        return 1
    runtime_entry = _watch_runtime_store().load().get("watches", {}).get(updated.id)
    watch_payload = _watch_mutation_payload(updated, runtime_entry)
    _print_definition_payload(watch_payload)
    return 0


def cmd_watch_update(args):
    reserved_session_id: Optional[str] = None
    try:
        store = _watch_store()
        watch = store.get_watch(args.watch_id)
        if watch is None:
            raise TaskCliError(
                f"watch '{args.watch_id}' not found",
                code="watch_not_found",
                hint="Use 'vibe watch list' to find a valid watch ID before calling update.",
                help_command="vibe watch list",
                details={"watch_id": args.watch_id},
            )

        if getattr(args, "reset_delivery", False) and (
            getattr(args, "post_to", None) is not None
            or getattr(args, "deliver_key", None) is not None
            or getattr(args, "scope_id", None) is not None
            or bool(getattr(args, "same_scope", False))
        ):
            raise TaskCliError(
                "use either --reset-delivery or a new delivery flag, not both",
                code="conflicting_delivery_target",
                hint="Pass --reset-delivery to clear delivery overrides, or pass --scope-id/--same-scope to replace placement.",
                help_command="vibe watch update --help",
            )
        caller_context = caller_context_from_env()
        caller_user_context = caller_resource_user_context(caller_context)
        scope_arg_present = (getattr(args, "scope_id", None) is not None) or bool(getattr(args, "same_scope", False))
        if scope_arg_present and not (
            bool(getattr(args, "create_session", False)) or bool(getattr(args, "create_session_per_run", False))
        ):
            raise TaskCliError(
                "scope placement flags only apply when creating Sessions",
                code="scope_without_session_creation",
                hint="Use --create-session or --create-session-per-run with --scope-id/--same-scope, or omit the scope placement flag.",
                help_command="vibe watch update --help",
            )
        requested_scope_key = _resolve_definition_scope_key(
            args,
            caller_context=caller_context,
            help_command="vibe watch update --help",
        )
        if getattr(args, "name", None) is not None and getattr(args, "clear_name", False):
            raise TaskCliError(
                "use either --name or --clear-name, not both",
                code="conflicting_name_update",
                hint="Pass a new name with --name, or remove the stored name with --clear-name.",
                help_command="vibe watch update --help",
            )
        if getattr(args, "clear_name", False):
            name = None
        elif getattr(args, "name", None) is not None:
            name = _normalize_watch_name(args.name, help_command="vibe watch update --help")
        else:
            name = watch.name

        session_id_update, session_key_update = _resolve_session_target_args(
            args,
            required=False,
            help_command="vibe watch update --help",
        )
        if session_id_update is not None:
            session_id = session_id_update
            session_key = ""
        elif session_key_update:
            session_id = None
            session_key = session_key_update
        else:
            session_id = watch.session_id
            session_key = watch.session_key
        if getattr(args, "reset_delivery", False):
            post_to = None
            deliver_key = None
        else:
            requested_post_to = getattr(args, "post_to", None)
            requested_deliver_key = getattr(args, "deliver_key", None)
            if requested_post_to is not None:
                post_to = requested_post_to
                deliver_key = None
            elif requested_deliver_key is not None:
                post_to = None
                deliver_key = requested_deliver_key
            else:
                post_to = watch.post_to
                deliver_key = watch.deliver_key
        metadata = dict(watch.metadata or {})
        if requested_scope_key:
            metadata["session_scope_id"] = requested_scope_key
        elif scope_arg_present:
            metadata.pop("session_scope_id", None)

        command = list(watch.command)
        shell_command = watch.shell_command
        waiter_command = getattr(args, "waiter_command", None)
        if waiter_command == ["--"]:
            waiter_command = []
        if getattr(args, "shell", None) is not None or waiter_command:
            command, shell_command = _resolve_watch_command(args, help_command="vibe watch update --help")
        prefix = (
            None
            if getattr(args, "clear_prefix", False)
            else (
                _normalize_task_name(getattr(args, "prefix", None))
                if getattr(args, "prefix", None) is not None
                else watch.prefix
            )
        )
        message_changed = any(
            getattr(args, name, None) is not None
            for name in ("message", "message_file", "prompt", "prompt_file")
        )
        if message_changed:
            message = _resolve_optional_message_input(
                args,
                help_command="vibe watch update --help",
                example_command=f"vibe watch update {args.watch_id}",
                legacy_prefix=None,
            )
        elif getattr(args, "prefix", None) is not None or getattr(args, "clear_prefix", False):
            message = prefix
        else:
            message = getattr(watch, "message", None) or watch.prefix
        # Same three durable Agent-authority states ``vibe task update`` keeps, on the
        # sibling definition command. Rejected rather than silently resolved, exactly
        # as ``--name`` / ``--clear-name`` above: the two flags mean opposite things
        # and the pair honours neither -- ``--clear-agent`` wins for ``agent_name``
        # (-> None) while the mere PRESENCE of ``--agent`` POPS the
        # follow-the-session marker, so the definition looks like "no Agent pinned and
        # not following its Session" and the resolve below writes today's scope /
        # default Agent back as a hard pin (HFR-255, HFR-256).
        if getattr(args, "agent", None) is not None and getattr(args, "clear_agent", False):
            raise TaskCliError(
                "use either --agent or --clear-agent, not both",
                code="conflicting_agent_update",
                hint=(
                    "Pin an Agent with --agent, or hand Agent authority back to the bound "
                    "Session with --clear-agent."
                ),
                help_command="vibe watch update --help",
            )
        if getattr(args, "clear_agent", False):
            agent_name = None
        elif getattr(args, "agent", None) is not None:
            agent_name = _validate_agent_name_arg(args.agent)
        else:
            agent_name = watch.agent_name

        # "Follow the bound Session's Agent" is a durable state (set by
        # ``--clear-agent``, or by a reset rebind on a ``create_once`` definition),
        # not merely a missing ``agent_name``. An explicit ``--agent`` is the user
        # pinning again, so it ends the state.
        explicit_agent_requested = getattr(args, "agent", None) is not None
        if explicit_agent_requested:
            metadata.pop(BINDING_FOLLOWS_SESSION_METADATA_KEY, None)
        elif getattr(args, "clear_agent", False):
            metadata[BINDING_FOLLOWS_SESSION_METADATA_KEY] = True
        follows_session_agent = bool(metadata.get(BINDING_FOLLOWS_SESSION_METADATA_KEY))
        cwd = (
            None
            if getattr(args, "clear_cwd", False)
            else (
                _resolve_watch_cwd(getattr(args, "cwd", None), help_command="vibe watch update --help")
                if getattr(args, "cwd", None) is not None
                else watch.cwd
            )
        )
        mode = "forever" if getattr(args, "forever", False) else ("once" if getattr(args, "once", False) else watch.mode)
        timeout_seconds = float(args.timeout) if getattr(args, "timeout", None) is not None else watch.timeout_seconds
        lifetime_timeout_seconds = (
            float(args.lifetime_timeout)
            if getattr(args, "lifetime_timeout", None) is not None
            else watch.lifetime_timeout_seconds
        )
        retry_delay_seconds = (
            float(args.retry_delay) if getattr(args, "retry_delay", None) is not None else watch.retry_delay_seconds
        )
        retry_exit_codes = (
            sorted(set(args.retry_exit_code))
            if getattr(args, "retry_exit_code", None) is not None
            else list(watch.retry_exit_codes)
        )
        _validate_watch_timing(
            timeout_seconds=timeout_seconds,
            retry_delay_seconds=retry_delay_seconds,
            lifetime_timeout_seconds=lifetime_timeout_seconds,
            help_command="vibe watch update --help",
        )
        session_policy = _definition_session_policy_for_update(
            args,
            current_policy=watch.session_policy,
            current_schedule_type="watch",
            next_schedule_type="watch",
            help_command="vibe watch update --help",
        )
        creates_future_session = session_policy == "create_per_run" or (
            session_policy == "create_once" and (bool(getattr(args, "create_session", False)) or not session_id)
        )
        session_workdir = (
            _resolve_definition_session_cwd(
                explicit_cwd=getattr(args, "cwd", None),
                existing_cwd=None
                if getattr(args, "clear_cwd", False)
                else (str(metadata.get("session_workdir") or "").strip() or None),
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args) or bool(str(metadata.get("session_scope_id") or "").strip()),
                help_command="vibe watch update --help",
            )
            if creates_future_session
            else None
        )
        scope_key = requested_scope_key or str(metadata.get("session_scope_id") or "").strip() or _legacy_scope_key_from_target(deliver_key)
        if session_policy == "create_once" and not scope_key:
            raise TaskCliError(
                "--scope-id or --same-scope is required when a stored definition creates one reusable Session",
                code="missing_delivery_target",
                hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
                help_command="vibe watch update --help",
            )
        agent_resolution = _AgentTargetResolution(None, False)
        if follows_session_agent and not explicit_agent_requested:
            # Deliberately resolves NOTHING. Re-resolving here would write today's
            # scope/default Agent back onto a definition whose Agent authority now
            # belongs to its bound Session, and the pin wins over the Session row at
            # dispatch -- so an unrelated ``--name`` edit would silently move every
            # future watch hook onto a different Agent.
            pass
        elif agent_name is None and session_policy != "existing":
            agent_resolution = _resolve_agent_target(
                agent_name=None,
                session_id=None,
                session_key=scope_key,
                help_command="vibe watch update --help",
            )
            agent = agent_resolution.agent
            agent_name = agent.name if agent else None
        elif agent_name is not None or session_id or session_key:
            agent_resolution = _resolve_agent_target(
                agent_name=agent_name,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe watch update --help",
                existing_agent_reference=not explicit_agent_requested,
            )
            agent = agent_resolution.agent
            agent_name = agent.name if agent else None
        expected_enabled_agent_id, expected_reference_agent_id = _agent_write_guard_ids(
            agent_resolution
        )
        if session_policy == "create_once" and (
            getattr(args, "create_session", False) or not session_id
        ):
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                agent_id=agent.id if agent else None,
                deliver_key=scope_key,
                workdir=session_workdir,
                help_command="vibe watch update --help",
                require_enabled_agent=expected_enabled_agent_id is not None,
                expected_reference_agent_id=expected_reference_agent_id,
            )
            reserved_session_id = session_id
            session_key = ""
        if session_workdir:
            metadata["session_workdir"] = session_workdir
        else:
            metadata.pop("session_workdir", None)
        session_target, delivery_target = _validate_definition_update_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=post_to,
            deliver_key=deliver_key,
            scope_key=scope_key,
            help_command="vibe watch update --help",
        )

        changes = {
            "name": name,
            "session_id": session_id,
            "session_key": session_key,
            "agent_name": agent_name,
            "session_policy": session_policy,
            "command": command,
            "shell_command": shell_command,
            "prefix": prefix,
            "message": message,
            "cwd": cwd,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "lifetime_timeout_seconds": lifetime_timeout_seconds,
            "retry_exit_codes": retry_exit_codes,
            "retry_delay_seconds": retry_delay_seconds,
            "post_to": post_to,
            "deliver_key": deliver_key,
            "metadata": metadata,
        }
        current = {
            "name": watch.name,
            "session_id": watch.session_id,
            "session_key": watch.session_key,
            "agent_name": watch.agent_name,
            "session_policy": watch.session_policy,
            "command": watch.command,
            "shell_command": watch.shell_command,
            "prefix": watch.prefix,
            "message": getattr(watch, "message", None) or watch.prefix,
            "cwd": watch.cwd,
            "mode": watch.mode,
            "timeout_seconds": watch.timeout_seconds,
            "lifetime_timeout_seconds": watch.lifetime_timeout_seconds,
            "retry_exit_codes": watch.retry_exit_codes,
            "retry_delay_seconds": watch.retry_delay_seconds,
            "post_to": watch.post_to,
            "deliver_key": watch.deliver_key,
            "metadata": watch.metadata,
        }
        if changes == current:
            raise TaskCliError(
                "no watch fields were changed",
                code="no_watch_changes",
                hint="Pass at least one field to update, such as --name, --shell, --timeout, --session-id, or --scope-id.",
                help_command="vibe watch update --help",
                details={"watch_id": args.watch_id},
            )

        updated = store.update_watch(
            args.watch_id,
            **changes,
            expected_enabled_agent_id=expected_enabled_agent_id,
            expected_reference_agent_id=expected_reference_agent_id,
            user_context=caller_user_context,
        )
        reserved_session_id = None
        runtime_entry = _watch_runtime_store().load().get("watches", {}).get(updated.id)
        warnings = _collect_target_warnings(session_target, delivery_target)
        watch_payload = _watch_mutation_payload(updated, runtime_entry)
        _print_definition_payload(watch_payload, warnings=warnings)
        return 0
    except DefinitionWriteConflict as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="watch update failed before its Session reservation was adopted",
            )
        _print_task_error(
            _definition_conflict_cli_error(
                exc,
                help_command="vibe watch update --help",
                details={"watch_id": getattr(args, "watch_id", exc.definition_id)},
            )
        )
        return 1
    except Exception as exc:
        if reserved_session_id:
            _release_cli_session_reservation(
                reserved_session_id,
                reason="watch update failed before its Session reservation was adopted",
            )
        _print_task_error(exc, help_command="vibe watch update --help")
        return 1


def cmd_watch_remove(watch_id: str):
    store = _watch_store()
    removed = store.remove_watch(watch_id)
    if not removed:
        _print_task_error(
            TaskCliError(
                f"watch '{watch_id}' not found",
                code="watch_not_found",
                hint="Use 'vibe watch list' to find a valid watch ID before calling remove.",
                help_command="vibe watch list",
                details={"watch_id": watch_id},
            )
        )
        return 1
    _print_cli_payload("run_definition", removed_id=watch_id)
    return 0


def _add_dependency_download_failure(
    items: list[dict],
    error: dict | None,
    *,
    label: str,
    code_prefix: str,
    repair_target: str | None,
    retry_action: str | None = None,
    failure_status: str = "fail",
) -> None:
    language = _configured_cli_language()
    error = error or {}
    kind = str(error.get("kind") or "unknown")
    url = str(error.get("url") or i18n_t("doctor.value.selectedDependencyUrl", language))
    attempts = int(error.get("attempts") or 1)
    retry_action = retry_action or (
        i18n_t("doctor.action.repairCommand", _configured_cli_language(), target=repair_target)
        if repair_target
        else i18n_t("doctor.action.askillManual", _configured_cli_language())
    )
    if kind == "http" and error.get("http_status") == 404:
        _add_doctor_item(
            items,
            failure_status,
            i18n_t("doctor.item.dependencyHttp404", language, label=label, url=url),
            i18n_t("doctor.action.dependencyHttp404", language),
            code=f"{code_prefix}_http_404",
            download_kind=kind,
        )
    elif kind == "http":
        status = error.get("http_status") or "error"
        _add_doctor_item(
            items,
            failure_status,
            i18n_t(
                "doctor.item.dependencyHttpErrorRetried"
                if attempts > 1
                else "doctor.item.dependencyHttpError",
                language,
                label=label,
                status=status,
                attempts=attempts,
                url=url,
            ),
            i18n_t("doctor.action.dependencyHttpError", language, retry=retry_action),
            code=f"{code_prefix}_http_error",
            download_kind=kind,
        )
    elif kind == "dns":
        _add_doctor_item(
            items,
            failure_status,
            i18n_t(
                "doctor.item.dependencyDnsFailedRetried"
                if attempts > 1
                else "doctor.item.dependencyDnsFailed",
                language,
                label=label,
                attempts=attempts,
                host=error.get("host") or url,
            ),
            i18n_t("doctor.action.dependencyDnsFailed", language, retry=retry_action),
            code=f"{code_prefix}_dns_failed",
            download_kind=kind,
        )
    elif kind == "tls":
        _add_doctor_item(
            items,
            failure_status,
            i18n_t("doctor.item.dependencyTlsFailed", language, label=label, url=url),
            i18n_t("doctor.action.dependencyTlsFailed", language),
            code=f"{code_prefix}_tls_failed",
            download_kind=kind,
        )
    elif kind in {"timeout", "network"}:
        _add_doctor_item(
            items,
            failure_status,
            i18n_t(
                "doctor.item.dependencyNetworkFailedRetried"
                if attempts > 1
                else "doctor.item.dependencyNetworkFailed",
                language,
                label=label,
                attempts=attempts,
                url=url,
            ),
            i18n_t("doctor.action.dependencyNetworkFailed", language, retry=retry_action),
            code=f"{code_prefix}_{kind}_failed",
            download_kind=kind,
        )
    elif kind in {"permission", "disk", "io"}:
        _add_doctor_item(
            items,
            failure_status,
            i18n_t(
                "doctor.item.dependencyStoreFailed",
                language,
                label=label,
                reason=error.get("message") or i18n_t("doctor.value.unknownError", language),
            ),
            i18n_t("doctor.action.dependencyStoreFailed", language),
            code=f"{code_prefix}_{kind}_failed",
            download_kind=kind,
        )
    else:
        _add_doctor_item(
            items,
            failure_status,
            i18n_t(
                "doctor.item.dependencyUnreachable",
                language,
                label=label,
                reason=error.get("message") or url,
            ),
            i18n_t("doctor.action.dependencyUnreachable", language),
            code=f"{code_prefix}_unreachable",
            download_kind=kind,
        )


def _managed_dependencies_doctor_items(*, deep: bool = False) -> list[dict]:
    from core.dependency_network import probe_url

    language = _configured_cli_language()
    labels = {
        "askill": "askill",
        "avault": "avault",
        "model-hub-engine": i18n_t("doctor.value.modelHubEngine", language),
        "tmux": "tmux runtime",
        "git-runtime": "Git Runtime",
        "memory-runtime": i18n_t("doctor.value.memoryRuntime", language),
        "node": "Node.js",
    }
    repair_targets = {
        "askill": "askill",
        "avault": "avault",
        "model-hub-engine": "model-hub-engine",
        "git-runtime": "git-runtime",
        "memory-runtime": "memory-runtime",
        "tmux": "tmux",
    }
    items: list[dict] = []
    try:
        dependencies = list(api.dependencies_status(offline=True).get("deps") or [])
        from core.git_runtime import git_runtime_status

        if not any(dependency.get("id") == "git-runtime" for dependency in dependencies):
            git_status = git_runtime_status()
            managed_git = git_status.get("managed") or {}
            git_ready = git_status.get("resolution") in {"vendored", "system"}
            dependencies.append(
                {
                    "id": "git-runtime",
                    "required": False,
                    "installed": git_ready,
                    "status": "ready" if git_ready else "missing",
                    "version": git_status.get("version") or managed_git.get("version"),
                    "source": git_status.get("resolution"),
                    "reason": managed_git.get("reason"),
                    "download_error": managed_git.get("download_error"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        _add_doctor_item(
            items,
            "fail",
            i18n_t("doctor.item.dependencyStatusFailed", language, reason=exc),
            i18n_t("doctor.action.dependencyStatusFailed", language),
            code="dependencies.status_failed",
        )
        return items

    for dependency in dependencies:
        dependency_id = str(dependency.get("id") or "")
        if dependency_id == "show-runtime" or dependency_id not in labels:
            continue
        label = labels[dependency_id]
        status = str(dependency.get("status") or "missing")
        ready = bool(dependency.get("installed")) and status == "ready"
        version = dependency.get("version")
        memory_details = (
            {
                "dependency_reason": dependency.get("reason"),
                "dependency_required": bool(dependency.get("required")),
            }
            if dependency_id == "memory-runtime"
            else {}
        )
        if ready:
            if dependency_id == "git-runtime" and dependency.get("source") == "system":
                _add_doctor_item(
                    items,
                    "pass",
                    i18n_t("doctor.item.gitSystemReady", language),
                    code="dependencies.git-runtime.system_ready",
                )
            else:
                _add_doctor_item(
                    items,
                    "pass",
                    i18n_t(
                        "doctor.item.dependencyReadyVersioned" if version else "doctor.item.dependencyReady",
                        language,
                        label=label,
                        version=version,
                    ),
                    code=f"dependencies.{dependency_id}.ready",
                    dependency_status=status if dependency_id == "memory-runtime" else None,
                    **memory_details,
                )
            continue

        required = bool(dependency.get("required"))
        severity = "fail" if required else "warn"
        if dependency_id == "node":
            _add_doctor_item(
                items,
                severity,
                i18n_t("doctor.item.nodeNotReady", language, label=label),
                i18n_t("doctor.action.nodeNotReady", language),
                code="dependencies.node.not_ready",
            )
            continue

        dependency_reason = str(dependency.get("reason") or "")
        if status == "unsupported" or dependency_reason.endswith("_platform_unsupported"):
            _add_doctor_item(
                items,
                severity,
                i18n_t("doctor.item.dependencyPlatformUnsupported", language, label=label),
                i18n_t("doctor.action.dependencyPlatformUnsupported", language),
                code=(
                    f"dependencies.{dependency_id}.unsupported"
                    if dependency_id == "memory-runtime"
                    else f"dependencies.{dependency_id}.platform_unsupported"
                ),
                dependency_status=status if dependency_id == "memory-runtime" else None,
                **memory_details,
            )
            continue

        repair_target: str | None = repair_targets[dependency_id]
        if dependency_id == "askill" and not api.askill_auto_install_supported():
            repair_target = None
        retry_action = (
            i18n_t("doctor.action.repairCommand", language, target=repair_target)
            if repair_target
            else i18n_t("doctor.action.askillManual", language)
        )
        probe = None
        if deep and dependency_id == "tmux":
            from core.tmux_runtime import TmuxRuntimeManager

            probe = TmuxRuntimeManager().probe_archive_reachability()
        elif deep and dependency_id == "model-hub-engine":
            from vibe.model_hub_runtime.installer import EngineRuntimeManager

            probe = EngineRuntimeManager().probe_archive_reachability()
        elif deep and dependency_id == "git-runtime":
            from core.git_runtime import GitRuntimeManager

            probe = GitRuntimeManager().probe_archive_reachability()

        probe_reason = str((probe or {}).get("reason") or "")
        if probe_reason.endswith("_archive_url_unsupported"):
            _add_doctor_item(
                items,
                severity,
                i18n_t(
                    "doctor.item.dependencyArchiveUrlUnsupported",
                    language,
                    label=label,
                    url=probe.get("url") or i18n_t("doctor.value.unknown", language),
                ),
                i18n_t("doctor.action.dependencyArchiveUrlUnsupported", language),
                code=f"dependencies.{dependency_id}.archive_url_unsupported",
            )
            continue

        _add_doctor_item(
            items,
            severity,
            (
                i18n_t(
                    (
                        "doctor.item.memoryRuntimeMissing"
                        if status == "missing"
                        else "doctor.item.memoryRuntimeError"
                    ),
                    language,
                    reason=_doctor_memory_reason(dependency_reason, language),
                )
                if dependency_id == "memory-runtime"
                else i18n_t("doctor.item.dependencyNotReady", language, label=label)
            ),
            i18n_t("doctor.action.dependencyNotReady", language, retry=retry_action),
            code=(
                f"dependencies.{dependency_id}.{status}"
                if dependency_id == "memory-runtime"
                else f"dependencies.{dependency_id}.not_ready"
            ),
            repair_target=repair_target,
            repair_risk="low",
            dependency_status=status,
            **memory_details,
        )
        if dependency_id == "memory-runtime":
            continue
        if not deep:
            continue

        if dependency_id == "askill":
            probe = probe_url("https://askill.sh", user_agent="avibe-askill-doctor")
        elif dependency_id == "avault":
            probe = probe_url(
                api.avault_manifest_url(),
                user_agent="avibe-avault-doctor",
            )
        elif dependency_id == "tmux" and probe is None:
            from core.tmux_runtime import TmuxRuntimeManager

            probe = TmuxRuntimeManager().probe_archive_reachability()
        elif dependency_id == "git-runtime" and probe is None:
            from core.git_runtime import GitRuntimeManager

            probe = GitRuntimeManager().probe_archive_reachability()

        if probe.get("ok"):
            _add_doctor_item(
                items,
                "pass",
                i18n_t("doctor.item.dependencyReachable", language, label=label, url=probe.get("url")),
                code=f"dependencies.{dependency_id}.reachable",
            )
        elif not probe.get("checked") and probe.get("reason") == "dependency_probe_unsupported":
            _add_doctor_item(
                items,
                "warn",
                i18n_t("doctor.item.dependencyProbeUnsupported", language, label=label),
                i18n_t("doctor.action.dependencyProbeUnsupported", language, retry=retry_action),
                code=f"dependencies.{dependency_id}.probe_unsupported",
            )
        elif probe.get("download_error"):
            _add_dependency_download_failure(
                items,
                probe.get("download_error"),
                label=label,
                code_prefix=f"dependencies.{dependency_id}.download",
                repair_target=repair_target,
                retry_action=retry_action,
                failure_status=severity,
            )
        elif not probe.get("checked"):
            _add_doctor_item(
                items,
                severity,
                i18n_t(
                    "doctor.item.dependencyProbeUnavailable",
                    language,
                    label=label,
                ),
                i18n_t("doctor.action.dependencyProbeUnavailable", language),
                code=f"dependencies.{dependency_id}.probe_unavailable",
                probe_reason=probe.get("reason"),
            )
    return items


def _show_runtime_install(payload: dict) -> dict:
    install = payload.get("install")
    return install if isinstance(install, dict) else {}


def _show_runtime_doctor_items(*, deep: bool = False) -> list[dict]:
    from core.show_runtime import ShowRuntimeManager

    items: list[dict] = []
    doctor_language = _configured_cli_language()
    try:
        manager = ShowRuntimeManager(offline=True if not deep else None)
        status = manager.status()
    except Exception as exc:  # noqa: BLE001
        _add_doctor_item(
            items,
            "fail",
            i18n_t("doctor.item.showRuntimeStatusFailed", doctor_language, reason=exc),
            i18n_t("doctor.action.showRuntimeStatusFailed", doctor_language),
            code="show_runtime.status_failed",
        )
        return items

    try:
        archive_cache = manager.archive_cache_status()
    except Exception:  # noqa: BLE001
        archive_cache = None
    archive_skipped_reason = str((archive_cache or {}).get("skipped_reason") or "")
    if archive_cache and archive_skipped_reason == "archive_inspection_failed":
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.archiveCacheSkipped", doctor_language),
            i18n_t("doctor.action.archiveCacheSkippedInspection", doctor_language),
            code="show_runtime.archive_cache_skipped",
            archive_cache_skip_reason=archive_skipped_reason,
        )
    elif archive_cache and archive_skipped_reason:
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.archiveCacheSkipped", doctor_language),
            i18n_t("doctor.action.archiveCacheSkipped", doctor_language),
            code="show_runtime.archive_cache_skipped",
            archive_cache_skip_reason=archive_skipped_reason,
        )
    elif archive_cache and int(archive_cache.get("candidate_count") or 0) > 0:
        _add_doctor_item(
            items,
            "warn",
            i18n_t(
                "doctor.item.archiveCacheReclaimable",
                doctor_language,
                count=int(archive_cache.get("candidate_count") or 0),
                size=_format_byte_size(int(archive_cache.get("candidate_bytes") or 0)),
            ),
            i18n_t("doctor.action.archiveCacheReclaimable", doctor_language),
            code="show_runtime.archive_cache_reclaimable",
        )
    elif archive_cache is not None:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.archiveCacheClean", doctor_language),
            code="show_runtime.archive_cache_clean",
        )

    install = _show_runtime_install(status)
    if (
        install.get("state") == "failed"
        and status.get("reason") == "runtime_install_inspection_failed"
    ):
        inspection = (
            status.get("inspection_error")
            if isinstance(status.get("inspection_error"), dict)
            else {}
        )
        _add_doctor_item(
            items,
            "fail",
            i18n_t(
                "doctor.item.showRuntimeStatusFailed",
                doctor_language,
                reason=inspection.get("message") or status.get("reason"),
            ),
            i18n_t("doctor.action.showRuntimeStatusFailed", doctor_language),
            code="show_runtime.status_failed",
            status_reason=status.get("reason"),
        )
        return items
    installed = install.get("state") == "installed"
    provider = str(status.get("provider") or "unknown")
    display_provider = _doctor_display_value(provider, "show_runtime_provider", doctor_language)
    current_platform = status.get("platform") or i18n_t("doctor.value.currentPlatform", doctor_language)
    explicit_command = status.get("explicit_command")
    if explicit_command:
        if installed:
            _add_doctor_item(
                items,
                "pass",
                i18n_t("doctor.item.explicitCommandAvailable", doctor_language, command=explicit_command),
            )
        else:
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.explicitCommandMissing", doctor_language, command=explicit_command),
                i18n_t("doctor.action.explicitCommandMissing", doctor_language),
                code="show_runtime.explicit_command_missing",
            )
        return items

    node_available = bool(status.get("node_available"))
    node_supported = status.get("node_supported") is not False

    manifest = status.get("manifest") if isinstance(status.get("manifest"), dict) else None
    archive = status.get("archive") if isinstance(status.get("archive"), dict) else None
    archive_url = str((archive or {}).get("url") or "")
    archive_scheme = urllib.parse.urlparse(archive_url).scheme
    archive_scheme_supported = not archive_url or archive_scheme in {"https", "file"}
    legacy_archive = "github.com/avibe-bot/vibe-show-runtime/releases/latest/download/" in archive_url
    provider_repairable = True

    if provider == "manifest-cache":
        if not manifest:
            provider_repairable = False
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.manifestMissing", doctor_language),
                i18n_t("doctor.action.manifestMissing", doctor_language),
                code="show_runtime.manifest_missing",
            )
        elif not archive:
            provider_repairable = False
            _add_doctor_item(
                items,
                "fail",
                i18n_t(
                    "doctor.item.manifestPlatformUnsupported",
                    doctor_language,
                    platform=current_platform,
                ),
                i18n_t("doctor.action.manifestPlatformUnsupported", doctor_language),
                code="show_runtime.platform_unsupported",
            )
        else:
            _add_doctor_item(
                items,
                "pass",
                i18n_t(
                    "doctor.item.manifestReady",
                    doctor_language,
                    name=archive.get("name"),
                    url=archive_url,
                ),
                code="show_runtime.manifest_ready",
            )
    elif provider == "archive":
        if legacy_archive:
            provider_repairable = False
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.legacyArchiveProvider", doctor_language, url=archive_url),
                i18n_t("doctor.action.legacyArchiveProvider", doctor_language),
                code="show_runtime.legacy_archive_provider",
            )
        else:
            _add_doctor_item(
                items,
                "warn",
                i18n_t("doctor.item.unpinnedArchiveProvider", doctor_language),
                i18n_t("doctor.action.unpinnedArchiveProvider", doctor_language),
                code="show_runtime.unpinned_archive_provider",
            )
    elif provider == "npm":
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.developmentProvider", doctor_language, provider=display_provider),
            i18n_t("doctor.action.developmentProvider", doctor_language),
            code="show_runtime.development_provider",
            show_runtime_provider=provider,
        )
    else:
        provider_repairable = False
        _add_doctor_item(
            items,
            "fail",
            i18n_t("doctor.item.providerUnsupported", doctor_language, provider=display_provider),
            i18n_t("doctor.action.providerUnsupported", doctor_language),
            code="show_runtime.provider_unsupported",
            show_runtime_provider=provider,
        )

    if installed:
        _add_doctor_item(
            items,
            "pass",
            i18n_t(
                "doctor.item.showRuntimeInstalled",
                doctor_language,
                platform=current_platform,
            ),
            code="show_runtime.installed",
        )
        return items

    show_runtime_repairable = provider_repairable and node_available and node_supported and archive_scheme_supported
    show_runtime_retry_action = (
        i18n_t("doctor.action.showRuntimeRepair", doctor_language)
        if show_runtime_repairable
        else i18n_t("doctor.action.showRuntimeRetry", doctor_language)
    )
    _add_doctor_item(
        items,
        "fail",
        i18n_t(
            "doctor.item.showRuntimeNotReady",
            doctor_language,
            platform=current_platform,
        ),
        (
            i18n_t("doctor.action.showRuntimeRepair", doctor_language)
            if show_runtime_repairable
            else i18n_t("doctor.action.showRuntimeRetry", doctor_language)
        ),
        code="show_runtime.not_ready",
        repair_target="show-runtime" if show_runtime_repairable else None,
        repair_risk="low",
    )

    if not archive:
        return items
    if not deep:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.archiveProbeSkipped", doctor_language),
            i18n_t("doctor.action.archiveProbeSkipped", doctor_language),
            code="show_runtime.archive_probe_skipped",
        )
        return items

    probe = manager.probe_archive_reachability()
    probe_reason = str(probe.get("reason") or "")
    if probe.get("ok"):
        target = probe.get("url") or probe.get("path") or (archive or {}).get("name")
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.archiveReachable", doctor_language, target=target),
            code="show_runtime.archive_reachable",
        )
    elif probe_reason == "runtime_archive_probe_unsupported":
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.archiveProbeUnsupported", doctor_language),
            i18n_t("doctor.action.archiveProbeUnsupported", doctor_language, retry=show_runtime_retry_action),
            code="show_runtime.archive_probe_unsupported",
        )
    elif probe.get("download_error"):
        is_manifest_failure = probe_reason.startswith("runtime_manifest_")
        _add_dependency_download_failure(
            items,
            probe.get("download_error"),
            label=i18n_t(
                "doctor.value.showRuntimeManifest" if is_manifest_failure else "doctor.value.showRuntimeArchive",
                doctor_language,
            ),
            code_prefix="show_runtime.manifest" if is_manifest_failure else "show_runtime.archive",
            repair_target="show-runtime" if show_runtime_repairable else None,
            retry_action=show_runtime_retry_action,
        )
    elif probe_reason == "runtime_archive_url_unsupported":
        _add_doctor_item(
            items,
            "fail",
            i18n_t(
                "doctor.item.archiveUrlUnsupported",
                doctor_language,
                url=probe.get("url") or (archive or {}).get("url"),
            ),
            i18n_t("doctor.action.archiveUrlUnsupported", doctor_language),
            code="show_runtime.archive_url_unsupported",
        )
    elif probe_reason.startswith("runtime_manifest_"):
        _add_doctor_item(
            items,
            "fail",
            i18n_t("doctor.item.manifestUnavailable", doctor_language),
            i18n_t("doctor.action.manifestUnavailable", doctor_language),
            code="show_runtime.manifest_unavailable",
            probe_reason=probe_reason,
        )
    elif probe_reason == "runtime_platform_unsupported":
        _add_doctor_item(
            items,
            "fail",
            i18n_t(
                "doctor.item.manifestPlatformUnsupported",
                doctor_language,
                platform=current_platform,
            ),
            i18n_t("doctor.action.showRuntimePlatformUnsupported", doctor_language),
            code="show_runtime.platform_unsupported",
        )
    else:
        _add_doctor_item(
            items,
            "fail",
            i18n_t(
                "doctor.item.archiveCheckFailed",
                doctor_language,
            ),
            i18n_t(
                "doctor.action.archiveCheckFailed",
                doctor_language,
                retry=show_runtime_retry_action,
            ),
            code="show_runtime.archive_check_failed",
            probe_reason=probe_reason,
        )
    return items


def _doctor(*, deep: bool = False):
    """Run diagnostic checks and return results in UI-compatible format.

    Returns:
        {
            "mode": "deep|fast",
            "groups": [{"name": "...", "items": [{"status": "pass|warn|fail", "message": "...", "action": "..."}]}],
            "summary": {"pass": 0, "warn": 0, "fail": 0},
            "ok": bool
        }
    """
    groups = []
    summary = {"pass": 0, "warn": 0, "fail": 0}
    language = _configured_cli_language()

    home_items = _home_migration_items()
    for item in home_items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    groups.append({"name": i18n_t("doctor.group.runtimeHome", language), "items": home_items})

    # Configuration Group
    config_items = []
    config_path = paths.get_config_path()

    if config_path.exists():
        _add_doctor_item(
            config_items,
            "pass",
            i18n_t("doctor.item.configFound", language, path=config_path),
        )
        summary["pass"] += 1
    else:
        _add_doctor_item(
            config_items,
            "fail",
            i18n_t("doctor.item.configMissing", language),
            i18n_t("doctor.action.configMissing", language),
        )
        summary["fail"] += 1

    config = None
    try:
        config = V2Config.load(config_path)
        if config.load_warnings:
            recovery_notice = api.config_recovery_notice(config)
            if recovery_notice:
                recovery_language = getattr(config, "language", language) or language
                _add_doctor_item(
                    config_items,
                    "warn",
                    i18n_t("doctor.item.configRecovery", recovery_language),
                    i18n_t("doctor.action.configRecovery", recovery_language),
                    code="config.recovery",
                )
                summary["warn"] += 1
        else:
            _add_doctor_item(
                config_items,
                "pass",
                i18n_t("doctor.item.configLoaded", language),
            )
            summary["pass"] += 1
    except Exception as exc:
        _add_doctor_item(
            config_items,
            "fail",
            i18n_t("doctor.item.configLoadFailed", language, reason=exc),
            i18n_t("doctor.action.configLoadFailed", language),
        )
        summary["fail"] += 1

    groups.append({"name": i18n_t("doctor.group.configuration", language), "items": config_items})

    remote_access_items = []
    if config is None:
        _add_doctor_item(
            remote_access_items,
            "warn",
            i18n_t("doctor.item.remoteConfigMissing", language),
        )
    elif not config.remote_access.vibe_cloud.enabled:
        _add_doctor_item(remote_access_items, "pass", i18n_t("doctor.item.remoteDisabled", language))
    else:
        from vibe import remote_access

        remote_status = remote_access.status(config)
        if not remote_status.get("running"):
            _add_doctor_item(
                remote_access_items,
                "fail",
                i18n_t("doctor.item.remoteNotRunning", language),
                i18n_t("doctor.action.remoteNotRunning", language),
            )
        else:
            _add_doctor_item(remote_access_items, "pass", i18n_t("doctor.item.remoteRunning", language))

        quality = remote_status.get("tunnel_quality")
        if not isinstance(quality, dict):
            _add_doctor_item(
                remote_access_items,
                "warn",
                i18n_t("doctor.item.tunnelQualityUnavailable", language),
                i18n_t("doctor.action.tunnelQualityUnavailable", language),
            )
        else:
            state = str(quality.get("state") or "unknown")
            grade = str(quality.get("grade") or "unknown")
            protocol = str(quality.get("protocol") or "unknown")
            display_state = _doctor_tunnel_display_value(state, "state", language)
            display_grade = _doctor_tunnel_display_value(grade, "grade", language)
            display_protocol = _doctor_tunnel_display_value(protocol, "protocol", language)
            quality_details = {
                "tunnel_state": state,
                "tunnel_grade": grade,
                "tunnel_protocol": protocol,
            }
            try:
                sampled_at = datetime.fromisoformat(str(quality.get("sampled_at") or "").replace("Z", "+00:00"))
                quality_stale = time.time() - sampled_at.timestamp() > 150
            except (TypeError, ValueError):
                quality_stale = True
            if quality_stale:
                _add_doctor_item(
                    remote_access_items,
                    "warn",
                    i18n_t("doctor.item.tunnelQualityStale", language),
                    i18n_t("doctor.action.tunnelQualityStale", language),
                )
            else:
                rtt = quality.get("rtt_ms") if isinstance(quality.get("rtt_ms"), dict) else None
                request_path = (
                    quality.get("request_path")
                    if isinstance(quality.get("request_path"), dict)
                    else None
                )
                request_latency = (
                    request_path.get("latency_ms")
                    if remote_access.tunnel_quality.request_path_has_usable_latency(request_path)
                    else None
                )
                request_path_unavailable = bool(
                    request_path
                    and request_path.get("confidence") != "low"
                    and (
                        request_path.get("status") == "unavailable"
                        or int(request_path.get("success_count") or 0) == 0
                    )
                )
                quality_status = "pass" if state == "healthy" and grade in {"good", "fair", "unknown"} else "warn"
                if request_path_unavailable or (
                    state == "degraded" and int(quality.get("ha_connections") or 0) == 0
                ):
                    quality_status = "fail"
                if request_path_unavailable:
                    _add_doctor_item(
                        remote_access_items,
                        quality_status,
                        i18n_t(
                            "doctor.item.tunnelQualityRequestsUnavailable",
                            language,
                            state=display_state,
                            grade=display_grade,
                            succeeded=int(request_path.get("success_count") or 0),
                            samples=int(request_path.get("sample_count") or 0),
                            protocol=display_protocol,
                        ),
                        i18n_t("doctor.action.tunnelQualityWarn", language)
                        if quality_status == "warn"
                        else None,
                        **quality_details,
                    )
                elif request_latency is not None:
                    slow_rate = request_path.get("slow_request_rate") or {}
                    _add_doctor_item(
                        remote_access_items,
                        quality_status,
                        i18n_t(
                            "doctor.item.tunnelQualityLatency",
                            language,
                            state=display_state,
                            grade=display_grade,
                            p95=request_latency.get("p95"),
                            p99=request_latency.get("p99"),
                            slow_rate=round(float(slow_rate.get("over_1000_ms") or 0) * 100),
                            protocol=display_protocol,
                        ),
                        i18n_t("doctor.action.tunnelQualityWarn", language)
                        if quality_status == "warn"
                        else None,
                        **quality_details,
                    )
                elif rtt is None:
                    _add_doctor_item(
                        remote_access_items,
                        quality_status,
                        i18n_t(
                            "doctor.item.tunnelQualityRttUnavailable",
                            language,
                            state=display_state,
                            protocol=display_protocol,
                        ),
                        i18n_t("doctor.action.tunnelQualityWarn", language)
                        if quality_status == "warn"
                        else None,
                        **quality_details,
                    )
                else:
                    _add_doctor_item(
                        remote_access_items,
                        quality_status,
                        i18n_t(
                            "doctor.item.tunnelQualityRtt",
                            language,
                            state=display_state,
                            grade=display_grade,
                            median=rtt.get("median"),
                            maximum=rtt.get("max"),
                        ),
                        i18n_t("doctor.action.tunnelQualityWarn", language)
                        if quality_status == "warn"
                        else None,
                        **quality_details,
                    )

    for item in remote_access_items:
        item_status = item.get("status")
        if item_status in summary:
            summary[item_status] += 1
    groups.append({"name": i18n_t("doctor.group.remoteAccess", language), "items": remote_access_items})

    # Slack Group
    slack_items = []
    if config:
        try:
            config.slack.validate()
            _add_doctor_item(slack_items, "pass", i18n_t("doctor.item.slackTokenValid", language))
            summary["pass"] += 1

            # Check if tokens are actually set
            if config.slack.bot_token:
                _add_doctor_item(slack_items, "pass", i18n_t("doctor.item.slackBotConfigured", language))
                summary["pass"] += 1
            else:
                _add_doctor_item(
                    slack_items,
                    "warn",
                    i18n_t("doctor.item.slackBotMissing", language),
                    i18n_t("doctor.action.slackBotMissing", language),
                )
                summary["warn"] += 1

            if config.slack.app_token:
                _add_doctor_item(slack_items, "pass", i18n_t("doctor.item.slackAppConfigured", language))
                summary["pass"] += 1
            else:
                _add_doctor_item(
                    slack_items,
                    "warn",
                    i18n_t("doctor.item.slackAppMissing", language),
                    i18n_t("doctor.action.slackAppMissing", language),
                )
                summary["warn"] += 1

        except Exception as exc:
            _add_doctor_item(
                slack_items,
                "fail",
                i18n_t("doctor.item.slackValidationFailed", language, reason=exc),
                i18n_t("doctor.action.slackValidationFailed", language),
            )
            summary["fail"] += 1
    else:
        _add_doctor_item(slack_items, "fail", i18n_t("doctor.item.slackConfigMissing", language))
        summary["fail"] += 1

    groups.append({"name": i18n_t("doctor.group.slack", language), "items": slack_items})

    # Agent Backends Group
    agent_items = []
    if config:
        # OpenCode
        if config.agents.opencode.enabled:
            cli_path = config.agents.opencode.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None
            if found_path:
                _add_doctor_item(
                    agent_items,
                    "pass",
                    i18n_t("doctor.item.agentCliFound", language, agent="OpenCode", path=found_path),
                )
                summary["pass"] += 1
            else:
                _add_doctor_item(
                    agent_items,
                    "warn",
                    i18n_t("doctor.item.agentCliMissing", language, agent="OpenCode", path=cli_path),
                    i18n_t("doctor.action.agentCliMissing", language, agent="OpenCode"),
                )
                summary["warn"] += 1
        else:
            _add_doctor_item(agent_items, "pass", i18n_t("doctor.item.agentDisabled", language, agent="OpenCode"))
            summary["pass"] += 1

        # Claude
        if config.agents.claude.enabled:
            cli_path = config.agents.claude.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None

            if found_path:
                _add_doctor_item(
                    agent_items,
                    "pass",
                    i18n_t("doctor.item.agentCliFound", language, agent="Claude", path=found_path),
                )
                summary["pass"] += 1
            else:
                _add_doctor_item(
                    agent_items,
                    "warn",
                    i18n_t("doctor.item.agentCliMissing", language, agent="Claude", path=cli_path),
                    i18n_t("doctor.action.agentCliMissing", language, agent="Claude"),
                )
                summary["warn"] += 1
        else:
            _add_doctor_item(agent_items, "pass", i18n_t("doctor.item.agentDisabled", language, agent="Claude"))
            summary["pass"] += 1

        # Codex
        if config.agents.codex.enabled:
            cli_path = config.agents.codex.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None
            if found_path:
                _add_doctor_item(
                    agent_items,
                    "pass",
                    i18n_t("doctor.item.agentCliFound", language, agent="Codex", path=found_path),
                )
                summary["pass"] += 1
            else:
                _add_doctor_item(
                    agent_items,
                    "warn",
                    i18n_t("doctor.item.agentCliMissing", language, agent="Codex", path=cli_path),
                    i18n_t("doctor.action.agentCliMissing", language, agent="Codex"),
                )
                summary["warn"] += 1
        else:
            _add_doctor_item(agent_items, "pass", i18n_t("doctor.item.agentDisabled", language, agent="Codex"))
            summary["pass"] += 1

        # Default Agent check
        default_agent_name = None
        store = None
        try:
            store = _agent_store()
            default_agent = store.get_default_agent()
            default_agent_name = default_agent.name if default_agent else None
        except Exception:
            default_agent_name = None
        finally:
            if store is not None:
                store.close()
        _add_doctor_item(
            agent_items,
            "pass",
            i18n_t(
                "doctor.item.defaultAgent",
                language,
                agent=default_agent_name or i18n_t("doctor.value.notConfigured", language),
            ),
        )
        summary["pass"] += 1
    else:
        _add_doctor_item(agent_items, "fail", i18n_t("doctor.item.agentConfigMissing", language))
        summary["fail"] += 1

    groups.append({"name": i18n_t("doctor.group.agentBackends", language), "items": agent_items})

    # Runtime Group
    runtime_items = []
    if config:
        cwd = config.runtime.default_cwd
        if cwd and os.path.isdir(cwd):
            _add_doctor_item(runtime_items, "pass", i18n_t("doctor.item.workingDirectory", language, path=cwd))
            summary["pass"] += 1
        else:
            _add_doctor_item(
                runtime_items,
                "warn",
                i18n_t("doctor.item.workingDirectoryMissing", language, path=cwd),
                i18n_t("doctor.action.workingDirectoryMissing", language),
            )
            summary["warn"] += 1

        _add_doctor_item(
            runtime_items,
            "pass",
            i18n_t("doctor.item.logLevel", language, level=config.runtime.log_level),
        )
        summary["pass"] += 1

    # Check log file
    log_path = paths.get_logs_dir() / "vibe_remote.log"
    if log_path.exists():
        _add_doctor_item(runtime_items, "pass", i18n_t("doctor.item.logFile", language, path=log_path))
        summary["pass"] += 1
    else:
        _add_doctor_item(runtime_items, "pass", i18n_t("doctor.item.logFilePending", language))
        summary["pass"] += 1

    for item in [
        *_service_lifecycle_items(detect_extra_processes=deep),
        *_service_install_family_items(detect_extra_processes=deep),
        *_restart_state_items(),
        *_runtime_architecture_items(),
        *_show_git_checkpoint_items(),
    ]:
        runtime_items.append(item)
        status = item.get("status")
        if status in summary:
            summary[status] += 1

    groups.append({"name": i18n_t("doctor.group.runtime", language), "items": runtime_items})

    dependency_items = [
        *_managed_dependencies_doctor_items(deep=deep),
        *_show_runtime_doctor_items(deep=deep),
    ]
    for item in dependency_items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    groups.append({"name": i18n_t("doctor.group.dependencies", language), "items": dependency_items})

    local_cli_items = _local_cli_installation_items()
    for item in local_cli_items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    groups.append({"name": i18n_t("doctor.group.localCliInstallation", language), "items": local_cli_items})

    # Calculate overall status
    ok = summary["fail"] == 0

    result = {
        "mode": "deep" if deep else "fast",
        "groups": groups,
        "summary": summary,
        "ok": ok,
    }

    _write_json(paths.get_runtime_doctor_path(), result)
    return result


def _add_doctor_item(
    items: list[dict],
    status: str,
    message: str,
    action: str | None = None,
    *,
    code: str | None = None,
    repair_target: str | None = None,
    repair_risk: str | None = None,
    **details: object,
) -> None:
    item = {"status": status, "message": message}
    if code:
        item["code"] = code
    if action:
        item["action"] = action
    if repair_target:
        item["repairable"] = True
        item["repair"] = {
            "target": repair_target,
            "command": f"vibe doctor repair {repair_target}",
            "risk": repair_risk or "medium",
        }
    item.update({key: value for key, value in details.items() if value is not None})
    items.append(item)


def _path_entries_for_executable(name: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()
    suffixes = [""]
    if sys.platform == "win32":
        suffixes = [".exe", ".cmd", ".bat", ""]

    for directory in os.get_exec_path():
        if not directory:
            continue
        for suffix in suffixes:
            candidate = (Path(directory) / f"{name}{suffix}").expanduser()
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate.absolute()
            key = str(resolved)
            if key in seen or not candidate.exists():
                continue
            seen.add(key)
            candidates.append(resolved)
    return candidates


def _uv_tool_site_packages_for_vibe(vibe_path: Path) -> list[Path]:
    tool_roots: list[Path] = []
    seen_roots: set[str] = set()

    def add_tool_root(tool_root: Path) -> None:
        try:
            resolved = tool_root.expanduser().resolve()
        except OSError:
            resolved = tool_root.expanduser().absolute()
        key = str(resolved)
        if key not in seen_roots:
            seen_roots.add(key)
            tool_roots.append(resolved)

    # Atomic upgrades keep each validated uv environment in a durable
    # generation directory and switch only the stable PATH launcher.  Resolve
    # that generation directly so doctor inspects the active candidate just as
    # it inspects a conventional ``~/.local/share/uv/tools/<package>`` root.
    generation_root = atomic_uv_install_root().expanduser().resolve()
    generation = _launcher_generation(vibe_path, generation_root)
    if generation:
        for tools_dir in (generation / "uv" / "tools", generation / "tools"):
            for package_name in UV_TOOL_PACKAGE_NAMES:
                add_tool_root(tools_dir / package_name)

    parts = vibe_path.parts
    try:
        tools_index = parts.index("tools")
    except ValueError:
        pass
    else:
        if tools_index + 1 < len(parts) and parts[tools_index + 1] in UV_TOOL_PACKAGE_NAMES:
            add_tool_root(Path(*parts[: tools_index + 2]))

    uv_bin_dir = _uv_tool_dir(bin_dir=True)
    if uv_bin_dir is not None and _path_is_relative_to(vibe_path, uv_bin_dir):
        uv_tools_dir = _uv_tool_dir(bin_dir=False)
        if uv_tools_dir is not None:
            for package_name in UV_TOOL_PACKAGE_NAMES:
                add_tool_root(uv_tools_dir / package_name)

    site_packages_dirs: list[Path] = []
    for tool_root in tool_roots:
        site_packages_dirs.extend(_site_packages_dirs_for_tool_root(tool_root))
    return site_packages_dirs


def _site_packages_dirs_for_tool_root(tool_root: Path) -> list[Path]:
    candidates: list[Path] = []
    posix_lib_dir = tool_root / "lib"
    if posix_lib_dir.exists():
        candidates.extend(sorted(posix_lib_dir.glob("python*/site-packages")))

    windows_site_packages = tool_root / "Lib" / "site-packages"
    if windows_site_packages.exists():
        candidates.append(windows_site_packages)

    return candidates


def _uv_tool_dir(*, bin_dir: bool) -> Path | None:
    uv_path = shutil.which("uv")
    if not uv_path:
        return None
    command = [uv_path, "tool", "dir"]
    if bin_dir:
        command.append("--bin")
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    if not output:
        return None
    return Path(output).expanduser()


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.expanduser().resolve())
    except (OSError, ValueError):
        try:
            path.absolute().relative_to(parent.expanduser().absolute())
        except ValueError:
            return False
    return True


def _is_uv_tool_editable(site_packages: Path) -> bool:
    editable_patterns = ("_editable*_avibe_os*.pth", "_editable*_vibe_remote*.pth")
    if any(list(site_packages.glob(pattern)) for pattern in editable_patterns):
        return True
    for dist_info_pattern in ("avibe_os-*.dist-info/direct_url.json", "vibe_remote-*.dist-info/direct_url.json"):
        for direct_url in site_packages.glob(dist_info_pattern):
            try:
                payload = json.loads(direct_url.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("dir_info", {}).get("editable") is True:
                return True
    return False


def _available_alembic_revisions(alembic_versions_dir: Path) -> set[str]:
    revisions: set[str] = set()
    if not alembic_versions_dir.exists():
        return revisions
    for migration in alembic_versions_dir.glob("*.py"):
        name = migration.name
        if name == "__init__.py":
            continue
        revision = name.split("_", 2)
        if len(revision) >= 2:
            revisions.add("_".join(revision[:2]))
    return revisions


def _current_sqlite_revision() -> str | None:
    db_path = paths.get_sqlite_state_path().expanduser()
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute("select version_num from alembic_version").fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def _local_cli_installation_items() -> list[dict]:
    items: list[dict] = []
    language = _configured_cli_language()

    vibe_paths = _path_entries_for_executable("vibe")
    preferred_vibe = (Path.home() / ".local" / "bin" / "vibe").expanduser()
    active_vibe_path: Path | None = None
    if not vibe_paths:
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.cliMissing", language),
            i18n_t("doctor.action.cliMissing", language),
        )
    else:
        first_vibe = vibe_paths[0]
        active_vibe_path = first_vibe
        try:
            preferred_resolved = preferred_vibe.resolve()
        except OSError:
            preferred_resolved = preferred_vibe
        if preferred_vibe.exists() and first_vibe != preferred_resolved:
            _add_doctor_item(
                items,
                "warn",
                i18n_t(
                    "doctor.item.cliPathPrecedence",
                    language,
                    active=first_vibe,
                    preferred=preferred_resolved,
                ),
                i18n_t("doctor.action.cliPathPrecedence", language),
            )
        else:
            _add_doctor_item(items, "pass", i18n_t("doctor.item.cliPath", language, path=first_vibe))

    site_packages_dirs = _uv_tool_site_packages_for_vibe(active_vibe_path) if active_vibe_path is not None else []
    if not site_packages_dirs:
        _add_doctor_item(
            items,
            "warn",
            i18n_t("doctor.item.cliNotUvTool", language),
            i18n_t("doctor.action.cliNotUvTool", language),
        )
        return items

    recognized_revisions: set[str] = set()
    for site_packages in site_packages_dirs:
        if _is_uv_tool_editable(site_packages):
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.cliEditable", language, path=site_packages),
                i18n_t("doctor.action.cliEditable", language),
            )
        else:
            _add_doctor_item(
                items,
                "pass",
                i18n_t("doctor.item.cliNotEditable", language, path=site_packages),
            )

        # A successful package-manager exit only proves that its metadata was
        # written.  RECORD is the wheel-level evidence that every installed
        # file is still present and unchanged; this is what catches an
        # interrupted dependency copy such as a half-written lark-oapi tree.
        if any(site_packages.glob("*.dist-info")):
            integrity = verify_site_packages(site_packages)
            if integrity.ok:
                _add_doctor_item(
                    items,
                    "pass",
                    i18n_t(
                        "doctor.item.packageIntegrityOk",
                        language,
                        count=integrity.checked_files,
                    ),
                    code="installation.package_integrity",
                )
            else:
                failure_detail = ", ".join(integrity.failures[:5]) or i18n_t(
                    "doctor.value.unknownError",
                    language,
                )
                remaining = max(0, len(integrity.failures) - 5)
                _add_doctor_item(
                    items,
                    "fail",
                    i18n_t(
                        "doctor.item.packageIntegrityFailedMore"
                        if remaining
                        else "doctor.item.packageIntegrityFailed",
                        language,
                        detail=failure_detail,
                        count=remaining,
                    ),
                    i18n_t("doctor.action.packageIntegrityFailed", language),
                    code="installation.package_integrity",
                )

        alembic_dir = site_packages / "storage" / "alembic"
        versions_dir = alembic_dir / "versions"
        if not alembic_dir.exists() or not versions_dir.exists():
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.alembicMissing", language, path=alembic_dir),
                i18n_t("doctor.action.alembicMissing", language),
            )
            continue

        revisions = _available_alembic_revisions(versions_dir)
        recognized_revisions.update(revisions)
        if revisions:
            _add_doctor_item(
                items,
                "pass",
                i18n_t("doctor.item.alembicFound", language, path=versions_dir),
            )
        else:
            _add_doctor_item(
                items,
                "fail",
                i18n_t("doctor.item.alembicRevisionsMissing", language, path=versions_dir),
                i18n_t("doctor.action.alembicRevisionsMissing", language),
            )

    sqlite_revision = _current_sqlite_revision()
    if sqlite_revision is None:
        _add_doctor_item(items, "pass", i18n_t("doctor.item.sqliteRevisionAbsent", language))
    elif sqlite_revision in recognized_revisions:
        _add_doctor_item(
            items,
            "pass",
            i18n_t("doctor.item.sqliteRevisionKnown", language, revision=sqlite_revision),
        )
    else:
        _add_doctor_item(
            items,
            "fail",
            i18n_t("doctor.item.sqliteRevisionUnknown", language, revision=sqlite_revision),
            i18n_t("doctor.action.sqliteRevisionUnknown", language),
        )

    return items


def _doctor_tunnel_display_value(value: object, value_type: str, language: str) -> str:
    return _doctor_display_value(value, f"tunnel_{value_type}", language)


def _doctor_display_value(value: object, category: str, language: str) -> str:
    """Project finite Doctor vocabulary at the human-rendering boundary."""

    raw_value = str(value or "unknown")
    key = DOCTOR_DISPLAY_PROJECTIONS.get(category, {}).get(raw_value)
    return i18n_t(key, language) if key else i18n_t("doctor.value.unknown", language)


def _doctor_memory_reason(reason: object, language: str) -> str:
    reason_code = reason.strip() if isinstance(reason, str) else ""
    key = _DOCTOR_MEMORY_REASON_I18N_KEYS.get(
        reason_code,
        _MEMORY_CLI_REASON_I18N_KEYS.get(reason_code),
    )
    if key:
        return i18n_t(key, language)
    return reason_code or i18n_t("doctor.value.unknownError", language)


def _doctor_managed_reason_key(reason: str) -> str | None:
    projections = DOCTOR_DISPLAY_PROJECTIONS
    key = projections["repair_reason"].get(reason)
    if key:
        return key
    for suffix, suffix_key in sorted(
        projections["repair_suffix"].items(),
        key=lambda entry: len(entry[0]),
        reverse=True,
    ):
        if reason == suffix or reason.endswith(f"_{suffix}"):
            return suffix_key
    return None


def _doctor_managed_failure_detail(target: str, result: dict, language: str) -> str:
    download_error = result.get("download_error") if isinstance(result.get("download_error"), dict) else None
    if download_error:
        kind = str(download_error.get("kind") or "")
        key = DOCTOR_DISPLAY_PROJECTIONS["download_kind"].get(kind)
        attempts = int(download_error.get("attempts") or 1)
        if key == "doctor.repair.dependencyDownloadHttp":
            return i18n_t(
                key,
                language,
                status=download_error.get("http_status") or i18n_t("doctor.value.unknown", language),
                attempts=attempts,
            )
        if key:
            return i18n_t(key, language, attempts=attempts)
        return i18n_t("doctor.repair.dependencyDownloadUnknown", language)

    reason = str(result.get("reason") or "")
    key = _doctor_managed_reason_key(reason)
    error = result.get("error") or i18n_t("doctor.value.unknownError", language)
    kwargs = {"target": target, "error": error}
    if reason == "askill_auto_install_unsupported":
        kwargs["tools"] = "+".join(str(tool) for tool in result.get("required_tools") or ("curl", "bash"))
    elif reason == "avault_platform_unsupported":
        kwargs["platform"] = result.get("platform") or i18n_t("doctor.value.unknown", language)
    elif reason == "avault_checksum_mismatch":
        kwargs["expected_sha256"] = result.get("expected_sha256") or i18n_t("doctor.value.unknown", language)
        kwargs["actual_sha256"] = result.get("actual_sha256") or i18n_t("doctor.value.unknown", language)
    elif reason in {"askill_install_timeout"}:
        kwargs["timeout_seconds"] = result.get("timeout_seconds") or 300
    elif reason == "askill_install_failed" or reason.endswith("_install_failed"):
        kwargs["exit_code"] = result.get("exit_code") or i18n_t("doctor.value.unknown", language)
    if key:
        return i18n_t(key, language, **kwargs)
    return i18n_t("doctor.repair.dependencyFailedDefault", language)


def _doctor_repair_result(target: str, status: str, message: str, **details) -> dict:
    payload = {"target": target, "status": status, "message": message}
    payload.update({key: value for key, value in details.items() if value is not None})
    return payload


def _write_refreshed_runtime_status() -> None:
    # Asked, not re-derived. A repair that wrote its own idea of the state word
    # would be the second place deciding one fact -- and the one that persists
    # it, so a lock holder still migrating would be recorded as `running` and
    # every later reader would inherit that answer instead of measuring.
    status = runtime.read_status()
    resolved = runtime.resolve_service_state()
    runtime.write_status(resolved.state, resolved.detail, resolved.service_pid, status.get("ui_pid"))


def _start_service_after_repair(
    target: str,
    success_key: str,
    failure_key: str,
    *,
    stopped_pids: list[int],
) -> dict:
    from vibe.memory_ui_access import generate_ui_read_secret

    # This repair stopped the old service and starts a replacement, so it is the
    # same shape ``cmd_start`` handles when it starts a service beside a
    # surviving UI: the Memory UI read proof reaches a child only over stdin and
    # is never persisted, so a bare CLI holds no secret to pass on and the new
    # controller would verify with None while the live UI keeps signing with the
    # old one. Mint a secret for the process being started and realign the UI.
    memory_ui_secret = generate_ui_read_secret()
    live_ui_pid = _live_ui_server_pid()
    language = _configured_cli_language()
    try:
        new_pid = runtime.start_service(memory_ui_secret=memory_ui_secret)
    except Exception as exc:
        _write_refreshed_runtime_status()
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t(failure_key, language, reason=exc),
            stopped_pids=stopped_pids,
        )
    ui_pid = runtime.read_status().get("ui_pid")
    if live_ui_pid is not None:
        # Remote access keeps running across the UI restart, matching cmd_start.
        try:
            runtime.stop_ui(stop_remote_access=False)
            config = _ensure_config()
            ui_pid = runtime.start_ui(
                runtime.effective_ui_bind_host(config),
                config.ui.setup_port,
                memory_ui_secret=memory_ui_secret,
            )
        except Exception:
            # The service repair itself succeeded; report it rather than failing
            # the whole repair because the UI could not be realigned.
            logger.exception(
                "Repaired the service but could not restart the Web UI pid=%s to share the Memory UI proof secret; "
                "Memory profile, search and clear stay unavailable until both processes restart together",
                live_ui_pid,
            )
    runtime.write_status("running", f"pid={new_pid}", new_pid, ui_pid)
    return _doctor_repair_result(
        target,
        "repaired",
        i18n_t(success_key, language),
        stopped_pids=stopped_pids,
        service_pid=new_pid,
    )


def _runtime_home_exists_for_repair() -> bool:
    runtime_home = paths.get_vibe_remote_dir()
    return runtime_home.is_dir()


def _repair_home_migration(*, dry_run: bool = False) -> dict:
    target = "home-migration"
    language = _configured_cli_language()
    if os.environ.get(paths.AVIBE_HOME_ENV):
        return _doctor_repair_result(
            target,
            "skipped",
            i18n_t("doctor.repair.homeExplicit", language),
        )

    avibe_home = Path.home() / paths.AVIBE_HOME_DIRNAME
    legacy_home = Path.home() / paths.LEGACY_HOME_DIRNAME
    avibe_present = avibe_home.exists() or avibe_home.is_symlink()
    legacy_present = legacy_home.exists() or legacy_home.is_symlink()

    if not avibe_present and not legacy_present:
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.homeNoRuntime", language))

    if avibe_present and legacy_present and not legacy_home.is_symlink():
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.homeConflict", language),
        )

    if not avibe_present and legacy_home.is_symlink():
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.homeSymlinkMissingCanonical", language),
        )

    if avibe_present and legacy_home.is_symlink() and _path_points_to(legacy_home, avibe_home):
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.homeHealthy", language))

    if dry_run:
        if not avibe_present and legacy_present and not legacy_home.is_symlink():
            return _doctor_repair_result(target, "planned", i18n_t("doctor.repair.homeDryMove", language))
        if avibe_present:
            return _doctor_repair_result(target, "planned", i18n_t("doctor.repair.homeDryLink", language))
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.homeNoNeed", language))

    if avibe_present:
        if legacy_home.is_symlink() or not legacy_present:
            legacy_home.unlink(missing_ok=True)
            try:
                legacy_home.symlink_to(avibe_home, target_is_directory=True)
            except OSError as exc:
                return _doctor_repair_result(
                    target,
                    "failed",
                    i18n_t("doctor.repair.homeLinkFailed", language, reason=exc),
                )
            return _doctor_repair_result(target, "repaired", i18n_t("doctor.repair.homeLinkCreated", language))
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.homeNoNeed", language))

    migrated_home = paths.migrate_default_home()
    if not _path_points_to(migrated_home, avibe_home):
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.homeMigrationIncomplete", language, path=migrated_home),
        )
    if not _path_points_to(legacy_home, avibe_home):
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.homeMigrationLinkFailed", language),
        )
    paths.ensure_data_dirs()
    return _doctor_repair_result(target, "repaired", i18n_t("doctor.repair.homeMigrated", language))


def _repair_stale_restart_state(*, dry_run: bool = False) -> dict:
    target = "stale-restart-state"
    language = _configured_cli_language()
    restart_path = runtime.get_restart_status_path()
    payload = runtime.read_json(restart_path) or {}
    if not payload:
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.restartAbsent", language))
    if not _restart_status_is_stale(payload, restart_path):
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.restartCurrent", language))
    if dry_run:
        return _doctor_repair_result(target, "planned", i18n_t("doctor.repair.restartDryRun", language))
    restart_path.unlink(missing_ok=True)
    _write_refreshed_runtime_status()
    return _doctor_repair_result(target, "repaired", i18n_t("doctor.repair.restartRepaired", language))


def _repair_duplicate_service_processes(*, dry_run: bool = False) -> dict:
    target = "duplicate-service-processes"
    language = _configured_cli_language()
    if runtime.service_instance_lock_attached_to_process():
        return _doctor_repair_result(target, "failed", i18n_t("doctor.repair.cliOnly", language))
    if not _runtime_home_exists_for_repair():
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.noRuntimeProcessState", language))

    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    extra_pids = runtime.extra_service_process_pids(owner_pid=owner_pid)
    if not extra_pids:
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.noExtraProcess", language))
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            i18n_t(
                "doctor.repair.extraProcessDryRun",
                language,
                pids=",".join(map(str, extra_pids)),
            ),
            pids=extra_pids,
        )

    stopped: list[int] = []
    failed: list[int] = []
    for pid in extra_pids:
        if runtime.stop_pid(pid, timeout=5):
            stopped.append(pid)
        else:
            failed.append(pid)

    if not owner_pid and stopped and not failed:
        return _start_service_after_repair(
            target,
            "doctor.repair.duplicateStoppedStarted",
            "doctor.repair.duplicateStoppedStartFailed",
            stopped_pids=stopped,
        )

    _write_refreshed_runtime_status()
    if failed:
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.extraProcessPartial", language),
            stopped_pids=stopped,
            failed_pids=failed,
        )
    return _doctor_repair_result(
        target,
        "repaired",
        i18n_t("doctor.repair.extraProcessStopped", language),
        stopped_pids=stopped,
    )


def _repair_stale_install_runtime(*, dry_run: bool = False) -> dict:
    target = "stale-install-runtime"
    language = _configured_cli_language()
    if runtime.service_instance_lock_attached_to_process():
        return _doctor_repair_result(target, "failed", i18n_t("doctor.repair.cliOnly", language))
    if not _runtime_home_exists_for_repair():
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.noRuntimeProcessState", language))

    current_family = _current_cli_install_family()
    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    service_pids = [pid for pid in [owner_pid] if pid]
    service_pids.extend(runtime.extra_service_process_pids(owner_pid=owner_pid))
    stale_pids: list[int] = []
    current_pids: list[int] = []
    for pid in sorted(set(service_pids)):
        family = _tool_family_from_text(runtime.get_process_command(pid))
        if current_family == PACKAGE_NAME and family == LEGACY_PACKAGE_NAME:
            stale_pids.append(pid)
        elif family == PACKAGE_NAME:
            current_pids.append(pid)
    if not stale_pids:
        return _doctor_repair_result(target, "skipped", i18n_t("doctor.repair.noLegacyProcess", language))
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            i18n_t("doctor.repair.staleInstallDryRun", language, pids=",".join(map(str, stale_pids))),
            pids=stale_pids,
        )

    stopped: list[int] = []
    failed: list[int] = []
    for pid in stale_pids:
        if runtime.stop_pid(pid, timeout=5):
            stopped.append(pid)
        else:
            failed.append(pid)

    if failed:
        _write_refreshed_runtime_status()
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.staleInstallPartial", language),
            stopped_pids=stopped,
            failed_pids=failed,
        )

    if current_pids or (owner_pid is not None and owner_pid not in stale_pids):
        _write_refreshed_runtime_status()
        return _doctor_repair_result(
            target,
            "repaired",
            i18n_t("doctor.repair.staleInstallStopped", language),
            stopped_pids=stopped,
        )

    return _start_service_after_repair(
        target,
        "doctor.repair.staleInstallStoppedStarted",
        "doctor.repair.staleInstallStoppedStartFailed",
        stopped_pids=stopped,
    )


def _repair_managed_dependency(target: str, installer, *, dry_run: bool = False) -> dict:
    language = _configured_cli_language()
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            i18n_t(DOCTOR_REPAIR_DRY_RUN_I18N_KEYS[target], language),
        )
    try:
        result = installer(force=True)
    except Exception as exc:  # noqa: BLE001
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t(
                "doctor.repair.dependencyResultFailed",
                language,
                target=target,
                detail=i18n_t("doctor.repair.dependencyException", language, target=target, error=exc),
            ),
            reason=f"{target}_repair_exception",
            error=str(exc),
        )
    if not isinstance(result, dict):
        result = {
            "ok": False,
            "reason": f"{target}_invalid_result",
            "error": i18n_t("doctor.value.unknownError", language),
        }
    if result.get("ok"):
        version = result.get("version")
        detail = (
            i18n_t("doctor.repair.dependencyVersion", language, version=version)
            if version
            else i18n_t("doctor.repair.dependencyReadyDefault", language)
        )
        return _doctor_repair_result(
            target,
            "repaired" if result.get("changed", True) else "skipped",
            i18n_t(
                "doctor.repair.dependencyReady",
                language,
                target=target,
                detail=detail,
            ),
            path=result.get("path"),
            version=version,
        )
    download_error = result.get("download_error") if isinstance(result.get("download_error"), dict) else None
    detail = _doctor_managed_failure_detail(target, result, language)
    return _doctor_repair_result(
        target,
        "failed",
        i18n_t(
            "doctor.repair.dependencyResultFailed",
            language,
            target=target,
            detail=detail,
        ),
        reason=result.get("reason"),
        download_error=download_error,
        output=result.get("output"),
    )


def _repair_askill(*, dry_run: bool = False) -> dict:
    return _repair_managed_dependency("askill", api.ensure_askill_installed, dry_run=dry_run)


def _repair_avault(*, dry_run: bool = False) -> dict:
    return _repair_managed_dependency("avault", api.ensure_avault_installed, dry_run=dry_run)


def _repair_model_hub_engine(*, dry_run: bool = False) -> dict:
    return _repair_managed_dependency(
        "model-hub-engine",
        api.ensure_model_hub_engine_installed,
        dry_run=dry_run,
    )


def _repair_tmux(*, dry_run: bool = False) -> dict:
    from core.tmux_runtime import ensure_tmux_installed

    return _repair_managed_dependency("tmux", ensure_tmux_installed, dry_run=dry_run)


def _repair_memory_runtime(*, dry_run: bool = False) -> dict:
    target = "memory-runtime"
    language = _configured_cli_language()
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            i18n_t(DOCTOR_REPAIR_DRY_RUN_I18N_KEYS[target], language),
        )

    try:
        from vibe import internal_client

        response = internal_client.memory_install_runtime_sync()
    except Exception as exc:  # noqa: BLE001
        return _doctor_repair_result(
            target,
            "failed",
            i18n_t("doctor.repair.memoryRuntimeControllerUnavailable", language, reason=exc),
            reason="memory_runtime_install_failed",
        )

    payload = response.get("body") if isinstance(response.get("body"), dict) else {}
    reason = str(payload.get("reason") or "memory_runtime_install_failed")
    download_error = (
        payload.get("download_error")
        if isinstance(payload.get("download_error"), dict)
        else None
    )
    if response.get("status_code") == 200 and payload.get("ok") is True:
        return _doctor_repair_result(
            target,
            "repaired",
            i18n_t("doctor.repair.memoryRuntimeReady", language),
        )
    return _doctor_repair_result(
        target,
        "failed",
        i18n_t(
            "doctor.repair.memoryRuntimeFailed",
            language,
            reason=_doctor_memory_reason(reason, language),
        ),
        reason=reason,
        download_error=download_error,
    )


def _repair_git_runtime(*, dry_run: bool = False) -> dict:
    from core.git_runtime import GitRuntimeManager

    return _repair_managed_dependency("git-runtime", GitRuntimeManager().ensure, dry_run=dry_run)


def _repair_show_runtime(*, dry_run: bool = False) -> dict:
    from core.show_runtime import ShowRuntimeManager

    target = "show-runtime"
    language = _configured_cli_language()
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            i18n_t(DOCTOR_REPAIR_DRY_RUN_I18N_KEYS[target], language),
        )

    result = ShowRuntimeManager().repair()
    outcome = result.get("outcome")
    verification = (
        result.get("verification")
        if isinstance(result.get("verification"), dict)
        else {}
    )
    if outcome == "healthy":
        return _doctor_repair_result(
            target,
            "skipped",
            i18n_t("doctor.repair.showRuntimeHealthy", language),
            provider=result.get("provider"),
            platform=result.get("platform"),
            install_dir=result.get("install_dir"),
        )
    if outcome == "repaired":
        return _doctor_repair_result(
            target,
            "repaired",
            i18n_t(
                (
                    "doctor.repair.showRuntimeReinstalled"
                    if result.get("was_installed")
                    else "doctor.repair.showRuntimeInstalled"
                ),
                language,
            ),
            provider=result.get("provider"),
            platform=result.get("platform"),
            install_dir=result.get("install_dir"),
        )

    reason = str(result.get("reason") or "runtime_prepare_failed")
    message_kwargs: dict[str, object]
    if result.get("explicit_command"):
        message_key = "doctor.repair.showRuntimeExplicitFailed"
        message_kwargs = {"reason": reason}
    elif reason == "runtime_legacy_archive_unavailable":
        message_key = "doctor.repair.showRuntimeLegacyUnavailable"
        message_kwargs = {}
    elif verification.get("state") == "undetermined":
        message_key = (
            "doctor.repair.showRuntimePostVerificationFailed"
            if result.get("verification_phase") == "after"
            else "doctor.repair.showRuntimeVerificationFailed"
        )
        message_kwargs = {"detail": verification.get("detail") or reason}
    elif result.get("verification_phase") == "after" and verification.get("state") == "not_startable":
        message_key = (
            "doctor.repair.showRuntimeReinstallStartFailed"
            if result.get("was_installed")
            else "doctor.repair.showRuntimeInstallStartFailed"
        )
        message_kwargs = {"reason": verification.get("reason") or reason}
    else:
        download_error = (
            result.get("download_error")
            if isinstance(result.get("download_error"), dict)
            else None
        )
        detail = str(download_error.get("message") or reason) if download_error else reason
        if download_error and download_error.get("url"):
            detail = f"{detail}: {download_error['url']}"
        message_key = "doctor.repair.showRuntimePrepareFailed"
        message_kwargs = {"detail": detail}
    download_error = (
        result.get("download_error")
        if isinstance(result.get("download_error"), dict)
        else None
    )
    return _doctor_repair_result(
        target,
        "failed",
        i18n_t(message_key, language, **message_kwargs),
        provider=result.get("provider"),
        platform=result.get("platform"),
        install_dir=result.get("install_dir"),
        installed=result.get("installed"),
        reason=reason,
        download_error=download_error,
        start_error=result.get("start_error"),
        explicit_command=result.get("explicit_command"),
        archive_url=result.get("archive_url"),
    )


def _repair_doctor_targets(targets: list[str], *, dry_run: bool = False, deep: bool = False) -> dict:
    language = _configured_cli_language()
    requested_targets = targets or list(DOCTOR_DEFAULT_REPAIR_TARGETS)
    unknown = [target for target in requested_targets if target not in DOCTOR_REPAIR_TARGETS]
    if unknown:
        return {
            "ok": False,
            "kind": "doctor_repair",
            "dry_run": dry_run,
            "results": [
                _doctor_repair_result(
                    target,
                    "failed",
                    i18n_t(
                        "doctor.repair.unknownTarget",
                        language,
                        target=target,
                        known_targets=", ".join(DOCTOR_REPAIR_TARGETS),
                    ),
                )
                for target in unknown
            ],
        }

    if dry_run:
        return {
            "ok": True,
            "kind": "doctor_repair",
            "dry_run": True,
            "results": [
                _doctor_repair_result(
                    target,
                    "planned",
                    i18n_t(DOCTOR_REPAIR_DRY_RUN_I18N_KEYS[target], language),
                )
                for target in requested_targets
            ],
        }

    handlers = {
        "home-migration": _repair_home_migration,
        "stale-install-runtime": _repair_stale_install_runtime,
        "duplicate-service-processes": _repair_duplicate_service_processes,
        "stale-restart-state": _repair_stale_restart_state,
        "askill": _repair_askill,
        "avault": _repair_avault,
        "model-hub-engine": _repair_model_hub_engine,
        "git-runtime": _repair_git_runtime,
        "memory-runtime": _repair_memory_runtime,
        "show-runtime": _repair_show_runtime,
        "tmux": _repair_tmux,
    }
    results = [handlers[target](dry_run=dry_run) for target in requested_targets]
    payload = {
        "ok": not any(result["status"] == "failed" for result in results),
        "kind": "doctor_repair",
        "dry_run": dry_run,
        "results": results,
    }
    if not dry_run and any(result["status"] != "skipped" for result in results):
        refresh_deep = deep or bool(set(targets) & DOCTOR_DEPENDENCY_REPAIR_TARGETS)
        payload["doctor"] = _doctor(deep=refresh_deep)
    return payload


def _confirm_doctor_repair(targets: list[str]) -> bool:
    if not sys.stdin.isatty():
        return False
    target_text = ", ".join(targets or DOCTOR_DEFAULT_REPAIR_TARGETS)
    answer = input(
        i18n_t(
            "doctor.repairConfirm",
            _configured_cli_language(),
            targets=target_text,
        )
    )
    return answer.strip().lower() == "yes"


def _live_ui_server_pid() -> int | None:
    """Return the recorded UI pid while it is still a live Avibe UI server."""

    if not runtime.ui_pid_file_points_to_running_ui():
        return None
    try:
        return int(paths.get_runtime_ui_pid_path().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def cmd_start():
    _guard_cli_default_state_migration()
    paths.ensure_data_dirs()
    config = _ensure_config()

    has_configured_platform_credentials = getattr(config, "has_configured_platform_credentials", None)
    if callable(has_configured_platform_credentials):
        ready = bool(has_configured_platform_credentials())
    else:
        ready = bool(getattr(getattr(config, "slack", None), "bot_token", ""))

    if not ready:
        _write_status("setup", "missing platform credentials")
    else:
        _write_status("starting")

    from vibe.memory_ui_access import generate_ui_read_secret

    # The Memory UI read proof is a per-launch secret: it reaches a child only
    # over stdin and is deliberately never persisted, so this launcher can only
    # align a pair it starts itself. It cannot read the copy a surviving process
    # already holds. Minting a fresh secret while reusing one live process is
    # what left Memory profile/search/clear answering memory_access_denied after
    # a partial restart, so track which side actually started here.
    memory_ui_secret = generate_ui_read_secret()
    live_service_pid = runtime.resolve_service_owner_pid(include_starting=True)
    live_ui_pid = _live_ui_server_pid()
    service_pid = runtime.start_service(
        wait_for_ready=False,
        memory_ui_secret=memory_ui_secret,
    )
    service_reused = live_service_pid is not None and service_pid == live_service_pid
    if service_reused:
        # The reused service still verifies proofs with the secret it was started
        # with. Signing with a different one would only produce requests it
        # rejects, so leave the surviving pair's own secret authoritative.
        ui_memory_secret = None
    else:
        ui_memory_secret = memory_ui_secret
        if live_ui_pid is not None:
            # A surviving UI signs with the previous secret, which the service
            # started just now cannot verify. Restart it so the pair shares one
            # secret; remote access keeps running across the UI restart.
            runtime.stop_ui(stop_remote_access=False)
            live_ui_pid = None
    bind_host = runtime.effective_ui_bind_host(config)
    ui_pid = runtime.start_ui(
        bind_host,
        config.ui.setup_port,
        memory_ui_secret=ui_memory_secret,
    )
    if service_reused and ui_pid != live_ui_pid:
        logger.warning(
            "Started UI pid=%s against reused service pid=%s without a shared Memory UI proof secret; "
            "Memory profile, search and clear stay unavailable until both processes restart together",
            ui_pid,
            service_pid,
        )
        if bool(getattr(getattr(config, "memory", None), "enabled", False)):
            language = normalize_language(getattr(config, "language", None))
            print(i18n_t("memory.cli.partialRestartWarning", language))
            print("")
    # The WAIT below is asked unconditionally. The predicate that used to guard
    # it is the lock, which is taken before the database is migrated -- so it is
    # already true of a process that has not finished starting and may never, and
    # guarding with it skipped the wait in exactly the case the wait exists for.
    # Nothing is paid for asking: a service that is up answers on the first probe.
    #
    # The provisional "starting" WRITE is guarded, and the difference is the
    # point: `write_status` carries `started_at` forward only across consecutive
    # `running` writes, so announcing a transition for a service this command did
    # not start resets its recorded uptime to now and briefly shows a starting
    # service to every status consumer. `vibe start` against a live instance is
    # idempotent and must stay observably so.
    if not service_reused:
        runtime.write_status("starting", "waiting for service process", service_pid, ui_pid)
    # The wait resolves the authoritative service.lock holder rather than waiting
    # on the raw pid start_service handed back: under a delegated user scope that
    # pid can be a launcher that never takes the lock, so wait_for_service_ready
    # adopts and returns the real owner instead of stalling the full timeout.
    resolved_pid = runtime.wait_for_service_ready(
        service_pid,
        timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS,
    )
    service_ready = resolved_pid is not None
    if resolved_pid is not None:
        service_pid = resolved_pid
    if service_ready:
        runtime.write_status("running", "pid={}".format(service_pid), service_pid, ui_pid)
    elif runtime.pid_alive(service_pid):
        runtime.write_status("starting", "service process is still starting", service_pid, ui_pid)
    else:
        runtime.write_status("error", "service process exited before startup completed", service_pid, ui_pid)
        raise RuntimeError(f"Vibe service process pid={service_pid} exited before acquiring the service lock")

    ui_url = "http://{}:{}".format(config.ui.setup_host, config.ui.setup_port)

    # Always print Web UI access instructions.
    print("Web UI:")
    print(f"  {ui_url}")
    print("")
    print("Want to open this Web UI from another device or a remote server?")
    print("  Run: vibe remote")
    print("  Avibe will guide you through creating a private avibe.bot URL.")
    print("")

    # If running over SSH, avoid trying to open a browser on the server.
    if config.ui.open_browser and not _in_ssh_session():
        opened = _open_browser(ui_url)
        if not opened:
            print(f"(Tip) Could not auto-open a browser. Open this URL manually: {ui_url}")
            print("")

    return 0


def cmd_vibe():
    """Compatibility default: bare `vibe` starts services and opens the Web UI."""
    return cmd_start()


def _stop_opencode_server():
    """Terminate the OpenCode server if running."""
    pid_file = paths.get_logs_dir() / "opencode_server.json"
    if not pid_file.exists():
        return False

    try:
        info = json.loads(pid_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Failed to parse OpenCode PID file: %s", e)
        return False

    pid = info.get("pid") if isinstance(info, dict) else None
    if not isinstance(pid, int) or not _pid_alive(pid):
        pid_file.unlink(missing_ok=True)
        return False

    # Verify it's actually an opencode serve process
    cmd = runtime.get_process_command(pid)
    if not cmd:
        logger.debug("Failed to verify OpenCode process (pid=%s): command not available", pid)
        return False
    if "opencode" not in cmd or "serve" not in cmd:
        return False

    if runtime.stop_pid(pid, timeout=5):
        pid_file.unlink(missing_ok=True)
        return True
    logger.warning("Failed to stop OpenCode server (pid=%s)", pid)
    return False


def _pid_file_points_to_live_process(pid_path: Path) -> bool:
    if pid_path == paths.get_runtime_pid_path():
        return runtime.resolve_service_owner_pid(include_starting=True) is not None or bool(
            runtime.extra_service_process_pids()
        )
    try:
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _runtime_process_was_running() -> bool:
    return runtime.service_process_running() or runtime.ui_pid_file_points_to_running_ui()


def cmd_stop():
    service_was_running = _pid_file_points_to_live_process(paths.get_runtime_pid_path())
    ui_was_running = _pid_file_points_to_live_process(paths.get_runtime_ui_pid_path())

    service_stopped = runtime.stop_service()
    ui_stopped = runtime.stop_ui()

    # Also terminate OpenCode server on full stop
    if _stop_opencode_server():
        print("OpenCode server stopped")

    if service_was_running and service_stopped is False:
        print("ERROR: Avibe service did not stop; preserving pidfile and aborting.", file=sys.stderr)
        _write_status("error", "service stop failed")
        return 2
    if ui_was_running and ui_stopped is False:
        print("ERROR: Avibe UI did not stop; preserving pidfile and aborting.", file=sys.stderr)
        _write_status("error", "ui stop failed")
        return 2

    _write_status("stopped")
    return 0


def cmd_status():
    print(_render_status())
    return 0


def _remote_access_result_status(result: dict) -> str:
    if not result.get("ok"):
        return "error"
    if result.get("running"):
        return "running"
    if result.get("paired"):
        return "paired"
    if result.get("enabled"):
        return "enabled"
    return "not paired"


def _print_remote_status(result: dict) -> None:
    print("Remote access:")
    print(f"  Status: {_remote_access_result_status(result)}")
    public_url = result.get("public_url")
    if public_url:
        print(f"  URL: {public_url}")
    if result.get("paired") is not None:
        print(f"  Paired: {'yes' if result.get('paired') else 'no'}")
    if result.get("enabled") is not None:
        print(f"  Enabled: {'yes' if result.get('enabled') else 'no'}")
    if result.get("running") is not None:
        print(f"  Tunnel: {'running' if result.get('running') else 'stopped'}")
    if result.get("binary_found") is not None:
        print(f"  cloudflared: {'found' if result.get('binary_found') else 'not found'}")
    if result.get("error"):
        print(f"  Error: {result.get('error')}")
    if result.get("detail"):
        print(f"  Detail: {result.get('detail')}")


def _read_pairing_key_from_args(args) -> str:
    pairing_key = (getattr(args, "pairing_key", None) or "").strip()
    if pairing_key:
        return pairing_key
    try:
        return getpass.getpass("Paste pairing key (input hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _print_remote_setup_intro() -> None:
    print("Avibe Cloud remote access")
    print("")
    print("This connects your local Avibe Web UI to a private avibe.bot URL.")
    print("Your agent and code still run on this machine; the remote URL only opens the local Web UI through a managed secure tunnel.")
    print("")
    print("Step 1: Get your pairing key")
    print("  1. Open https://avibe.bot")
    print("  2. Sign up or log in")
    print("  3. Create a new remote-access bot")
    print("  4. Claim your personal domain")
    print("  5. Copy the one-time pairing key")
    print("")


def _wait_for_pairing_key_ready() -> bool:
    try:
        input("Press Enter when you have copied the pairing key, or Ctrl+C to cancel.")
        return True
    except (EOFError, KeyboardInterrupt):
        print("")
        return False


def _print_remote_pair_start() -> None:
    print("")
    print("Step 2: Pair this device")


def _print_remote_pair_failure(result: dict) -> None:
    error_code = str(result.get("error") or "unknown_error")
    if error_code in {"invalid_pairing_key", "pairing_key_expired", "pairing_key_used"}:
        print("Pairing key is invalid or expired.", file=sys.stderr)
        print("Create a new pairing key at https://avibe.bot, then run:", file=sys.stderr)
        print("  vibe remote", file=sys.stderr)
        return
    if error_code in {"pairing_request_failed", "backend_http_error"}:
        print("Could not reach Avibe Cloud.", file=sys.stderr)
        print("Check your network connection, then run:", file=sys.stderr)
        print("  vibe remote", file=sys.stderr)
        if result.get("detail"):
            print(f"Detail: {result['detail']}", file=sys.stderr)
        return
    if error_code == "invalid_pairing_response":
        print("Avibe Cloud returned incomplete pairing data.", file=sys.stderr)
        print("Create a fresh pairing key and run:", file=sys.stderr)
        print("  vibe remote", file=sys.stderr)
        return
    print(f"Remote access setup failed: {error_code}", file=sys.stderr)
    if result.get("detail"):
        print(f"Detail: {result['detail']}", file=sys.stderr)
    print("Run 'vibe remote' to try again.", file=sys.stderr)


def _print_remote_start_failure(start_result: dict) -> None:
    error_code = str(start_result.get("error") or "unknown_error")
    print("Remote access is paired, but the tunnel did not start.", file=sys.stderr)
    if error_code == "cloudflared_install_failed":
        print("Avibe could not install cloudflared automatically.", file=sys.stderr)
    elif error_code == "cloudflared_spawn_failed":
        print("Avibe could not launch cloudflared.", file=sys.stderr)
    elif error_code == "cloudflared_exited":
        print("cloudflared exited immediately after launch.", file=sys.stderr)
    elif error_code == "remote_access_disabled":
        print("Remote access is disabled in the saved config.", file=sys.stderr)
    else:
        print(f"Reason: {error_code}", file=sys.stderr)
    if start_result.get("detail"):
        print(f"Detail: {start_result['detail']}", file=sys.stderr)
    print("After fixing the issue, run:", file=sys.stderr)
    print("  vibe remote start", file=sys.stderr)


def _print_remote_pair_success(result: dict, start_result: dict) -> None:
    print("")
    if not start_result.get("ok"):
        print("Step 3: Pairing saved")
        _print_remote_start_failure(start_result)
        return
    print("Step 3: Remote access is ready")
    public_url = result.get("public_url")
    if public_url:
        print("Open:")
        print(f"  {public_url}")
        print("")
        print("This URL opens the Web UI for this local Avibe instance.")
        print("When you open it, sign in with the same avibe.bot account to continue.")
    print("Tunnel: running" if result.get("running") else "Tunnel: ready")
    print("")
    print("Useful commands:")
    print("  vibe remote status   Check the remote URL and tunnel status")
    print("  vibe remote start    Start the tunnel again after a reboot or stop")
    print("  vibe remote stop     Stop remote access without deleting the pairing")


def _print_remote_already_configured(result: dict) -> None:
    print("Remote access is already configured.")
    public_url = result.get("public_url")
    if public_url:
        print("")
        print("Open:")
        print(f"  {public_url}")
        print("")
        print("When you open this URL, sign in with the same avibe.bot account to access this local Web UI.")
    print("")
    print(f"Tunnel: {'running' if result.get('running') else 'stopped'}")
    print("")
    print("Useful commands:")
    print("  vibe remote status   Show the remote URL and tunnel status")
    print("  vibe remote start    Start the tunnel again after a reboot or stop")
    print("  vibe remote stop     Temporarily disable remote access")
    print("")
    print("Need to switch account or domain?")
    print("  Run: vibe remote pair")


def _run_remote_pair(args, *, guided: bool) -> int:
    from vibe import remote_access

    if guided:
        current = remote_access.status()
        if current.get("paired"):
            _print_remote_already_configured(current)
            return 0
        _print_remote_setup_intro()
        if not _wait_for_pairing_key_ready():
            print("Remote access setup cancelled.")
            return 1
        _print_remote_pair_start()

    pairing_key = _read_pairing_key_from_args(args)
    if not pairing_key:
        payload = {"ok": False, "error": "missing_pairing_key", "hint": "Run 'vibe remote' to restart setup."}
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            print("Pairing failed: missing pairing key.", file=sys.stderr)
            print("Run 'vibe remote' to restart setup.", file=sys.stderr)
        return 1

    if not getattr(args, "json", False):
        print("Pairing this device with Avibe Cloud remote access...", flush=True)
    result = remote_access.pair(
        pairing_key,
        getattr(args, "backend_url", "https://avibe.bot"),
        getattr(args, "device_name", "avibe"),
    )
    start_result = result.get("start") if isinstance(result.get("start"), dict) else {}
    command_ok = bool(result.get("ok") and start_result.get("ok"))
    if getattr(args, "json", False):
        payload = {**result, "ok": command_ok}
        if result.get("ok") and not command_ok:
            payload.setdefault("pairing", {"ok": True})
            payload.setdefault("error", str(start_result.get("error") or "remote_start_failed"))
        _print_json(payload)
        return 0 if command_ok else 1

    if not result.get("ok"):
        _print_remote_pair_failure(result)
        return 1

    _print_remote_pair_success(result, start_result)
    return 0 if command_ok else 1


def cmd_remote_pair(args):
    return _run_remote_pair(args, guided=False)


def cmd_remote_setup(args):
    return _run_remote_pair(args, guided=True)


def cmd_remote_status(args):
    from vibe import remote_access

    result = remote_access.status()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        _print_remote_status(result)
    return 0 if result.get("ok") else 1


def cmd_remote_start(args):
    from vibe import remote_access

    result = remote_access.start()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        if result.get("ok"):
            if result.get("started"):
                print("Remote access tunnel started.")
            elif result.get("running"):
                print("Remote access tunnel is already running.")
            else:
                print("Remote access tunnel is ready.")
            if result.get("public_url"):
                print(f"Remote URL: {result['public_url']}")
        else:
            print(f"Remote access failed to start: {result.get('error') or 'unknown_error'}", file=sys.stderr)
            if result.get("detail"):
                print(str(result["detail"]), file=sys.stderr)
    return 0 if result.get("ok") else 1


def cmd_remote_stop(args):
    from vibe import remote_access

    result = remote_access.stop()
    if getattr(args, "json", False):
        _print_json(result)
    else:
        if result.get("ok"):
            print("Remote access tunnel stopped." if result.get("stopped") else "Remote access tunnel is already stopped.")
        else:
            print(f"Remote access failed to stop: {result.get('error') or 'unknown_error'}", file=sys.stderr)
            if result.get("detail"):
                print(str(result["detail"]), file=sys.stderr)
    return 0 if result.get("ok") else 1


def _show_page_result(
    page,
    *,
    message: str,
    previous_payload: dict | None = None,
    extra: dict | None = None,
    include_annotation_guidance: bool = False,
) -> dict:
    from core.show_pages import show_page_payload

    payload = {
        "ok": True,
        **show_page_payload(page),
        "message": message,
    }
    if previous_payload:
        payload.update(previous_payload)
    if extra:
        payload.update(extra)
    payload["next_actions"] = _show_page_next_actions(
        payload,
        include_annotation_guidance=include_annotation_guidance,
    )
    return payload


def _show_page_next_actions(payload: dict, *, include_annotation_guidance: bool = False) -> list[str]:
    session_id = payload.get("session_id") or "<session-id>"
    visibility = payload.get("visibility")
    actions = [
        f"Use this local workspace internally: {payload.get('path')}",
        "Do not send implementation details such as local paths to the user unless they ask for them.",
    ]
    active_url = payload.get("active_url")
    if active_url:
        actions.append(f"Send this URL to the user: {active_url}")
    elif visibility == "offline":
        actions.append(f"Bring the page online again with: vibe show update --session-id {session_id} --visibility private")
    elif not payload.get("url_guidance"):
        actions.append("No active URL is available right now.")
    actions.append("Treat the Show Page as the primary collaboration surface; put meaningful updates there first.")
    actions.append("Use visual thinking: diagrams, timelines, maps, comparisons, dashboards, or small prototypes when they help.")
    actions.append("To update the page later, edit src/App.tsx or api/*.ts; the private page hot-reloads when open.")
    if include_annotation_guidance:
        actions.append("Annotations: users can mark up this page; see vibe show marks / reply / annotate.")
    actions.append("For more options, run: vibe show --help")
    return actions


def _print_show_page_result(payload: dict) -> None:
    print("Show Page:")
    print(f"  Path: {payload.get('path')}")
    print(f"  URL: {payload.get('active_url') or 'none'}")
    print(f"  Visibility: {payload.get('visibility')}")
    if payload.get("previous_active_url"):
        print(f"  Previous URL: {payload.get('previous_active_url')} (inactive)")
    elif payload.get("previous_public_url"):
        print(f"  Previous URL: {payload.get('previous_public_url')} (inactive)")
    elif payload.get("previous_private_url"):
        print(f"  Previous URL: {payload.get('previous_private_url')} (inactive)")
    if payload.get("message"):
        print(f"  Status: {payload.get('message')}")
    if payload.get("url_guidance"):
        print(f"  URL guidance: {payload.get('url_guidance')}")
    next_actions = payload.get("next_actions") or []
    if next_actions:
        print("")
        print("Use it:")
        for action in next_actions:
            print(f"  - {action}")


def _print_show_page_status_missing(session_id: str) -> None:
    print("Show Page: not created")
    print("  Path: none")
    print("  URL: none")
    print("  Visibility: none")
    print("")
    print("Use it:")
    print(f"  - Create the workspace with: vibe show path --session-id {session_id}")
    print("  - Then edit src/App.tsx in the returned directory.")
    print("  - For more options, run: vibe show --help")


def _print_show_page_list(payload: dict) -> None:
    pages = payload.get("pages") or []
    print("Show Pages:")
    print(f"  Count: {payload.get('count', 0)}")
    visibility = payload.get("visibility")
    if visibility:
        print(f"  Filter: visibility={visibility}")
    if payload.get("url_guidance"):
        print(f"  URL guidance: {payload.get('url_guidance')}")
    if not pages:
        print("")
        print("No Show Pages found.")
        print("Create one with: vibe show path --session-id <session-id>")
        return
    print("")
    for page in pages:
        print(f"- {page.get('session_id')}")
        print(f"  Path: {page.get('path')}")
        print(f"  URL: {page.get('active_url') or 'none'}")
        print(f"  Visibility: {page.get('visibility')}")
        print(f"  Updated: {page.get('updated_at')}")
    if payload.get("message"):
        print("")
        print(payload["message"])
    print("")
    print("Use it:")
    print("  - Open a page: vibe show status --session-id <session-id>")
    print("  - Edit files under the listed Path.")
    print("  - For more options, run: vibe show --help")


def _print_show_page_error(exc: Exception) -> None:
    code = getattr(exc, "code", "show_page_failed")
    payload = {
        "ok": False,
        "code": code,
        "error": str(exc),
        "help_command": getattr(exc, "help_command", None) or "vibe show --help",
    }
    hint = getattr(exc, "hint", None)
    if hint:
        payload["hint"] = hint
    details = getattr(exc, "details", None)
    if details:
        payload["details"] = details
    print(json.dumps(payload, indent=2), file=sys.stderr)


def _load_show_page_store():
    from core.show_pages import ShowPageStore

    return ShowPageStore()


def cmd_show_list(args):
    from core.show_pages import avibe_cloud_connect_guidance, show_page_payload

    store = _load_show_page_store()
    try:
        page_request = _page_request_from_args(args, help_command="vibe show list --help")
        updated_after = _parse_cli_time_filter(
            getattr(args, "updated_after", None),
            field_name="--updated-after",
            help_command="vibe show list --help",
        )
        updated_before = _parse_cli_time_filter(
            getattr(args, "updated_before", None),
            field_name="--updated-before",
            help_command="vibe show list --help",
        )
        result = store.list_page(
            visibility=getattr(args, "visibility", None),
            session_id=getattr(args, "session_id", None),
            updated_after=updated_after,
            updated_before=updated_before,
            query=getattr(args, "query", None),
            page_request=page_request,
        )
        command = ["vibe", "show", "list"]
        _add_optional_arg(command, "--visibility", getattr(args, "visibility", None))
        _add_optional_arg(command, "--session-id", getattr(args, "session_id", None))
        _add_optional_arg(command, "--updated-after", updated_after)
        _add_optional_arg(command, "--updated-before", updated_before)
        _add_optional_arg(command, "--q", getattr(args, "query", None))
        if getattr(args, "json", False):
            command.append("--json")
        payload = {
            "ok": True,
            "count": len(result.items),
            "visibility": getattr(args, "visibility", None),
            "pages": [show_page_payload(page) for page in result.items],
            **_paginated_fields(result, command=command),
            "url_guidance": avibe_cloud_connect_guidance(),
        }
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            _print_show_page_list(payload)
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        store.close()


def cmd_show_path(args):
    from core.show_pages import ensure_show_page_dir
    from core.show_runtime import ShowRuntimeContext

    store = _load_show_page_store()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show path --help")
        page = store.ensure(session_id)
        page_dir = ensure_show_page_dir(session_id)
        _prewarm_show_page_session_best_effort(
            session_id,
            context=ShowRuntimeContext.PRIVATE.value,
        )
        payload = _show_page_result(
            page,
            message=f"Show Page workspace is ready at {page_dir}.",
            extra={"session_default_notice": session_default_notice} if session_default_notice else None,
            include_annotation_guidance=True,
        )
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            _print_show_page_result(payload)
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        store.close()


def _prewarm_show_page_session_best_effort(
    session_id: str,
    *,
    context: str,
) -> None:
    if _request_show_page_prewarm_best_effort(session_id, context=context) is None:
        logger.debug("Show Page session prewarm skipped for %s", session_id)


def cmd_show_status(args):
    store = _load_show_page_store()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show status --help")
        page = store.get(session_id)
        if page is None:
            payload = {
                "ok": False,
                "code": "show_page_not_found",
                "session_id": session_id,
                "message": "No Show Page exists for this session.",
                "next_actions": [f"Run `vibe show path --session-id {session_id}` to create the workspace."],
            }
            if session_default_notice:
                payload["session_default_notice"] = session_default_notice
            if getattr(args, "json", False):
                _print_json(payload)
            else:
                print("No Show Page exists for this session.")
                print(f"Run: vibe show path --session-id {session_id}")
            return 1
        payload = _show_page_result(
            page,
            message=f"Show Page is {page.visibility}.",
            extra={"session_default_notice": session_default_notice} if session_default_notice else None,
        )
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            _print_show_page_result(payload)
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        store.close()


def cmd_show_update(args):
    from core.show_pages import public_url, show_page_payload
    from core.show_runtime import ShowRuntimeContext

    store = _load_show_page_store()
    try:
        extra: dict = {}
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show update --help")
        if session_default_notice:
            extra["session_default_notice"] = session_default_notice

        if getattr(args, "rotate_share", False):
            updated, previous_share_id = store.rotate_share(session_id)
            extra = {
                **extra,
                "previous_public_url": public_url(previous_share_id),
                "previous_share_id": previous_share_id,
                "message_detail": "Previous public share URL was revoked.",
            }
            message = "Public share link rotated."
        elif getattr(args, "share_id", None) is not None:
            # ``is not None`` so an explicit empty --share-id reaches
            # validate_share_id (a clear missing_share_id) instead of falling
            # through to a confusing visibility error.
            updated, previous_share_id = store.set_share_id(session_id, args.share_id)
            extra = {
                **extra,
                "previous_share_id": previous_share_id,
            }
            if previous_share_id and previous_share_id != updated.share_id:
                extra["previous_public_url"] = public_url(previous_share_id)
                extra["message_detail"] = "Previous public share URL was revoked."
            message = "Custom public link set."
        else:
            # Read the prior state for the transition message WITHOUT creating a
            # page first: update_visibility owns the archived guard + ensure, so
            # an archived/terminal session is rejected before any row (and its
            # /show/ route) is materialized. rotate_share / set_share_id guard
            # themselves the same way, so neither needs a pre-ensure either.
            existing = store.get(session_id)
            previous = show_page_payload(existing) if existing else None
            previous_visibility = existing.visibility if existing else "private"
            updated = store.update_visibility(session_id, args.visibility)
            message = f"Show Page is now {updated.visibility}."
            if previous_visibility == "private" and updated.visibility == "public":
                extra["previous_private_url"] = previous["private_url"] if previous else None
            elif previous_visibility == "public" and updated.visibility == "private":
                extra["previous_public_url"] = previous["public_url"] if previous else None
            elif updated.visibility == "offline":
                extra["previous_active_url"] = previous["active_url"] if previous else None
                message = "Show Page has been taken offline. Local files were not deleted."

        if updated.visibility != "offline":
            context = ShowRuntimeContext.SHARED if updated.visibility == "public" else ShowRuntimeContext.PRIVATE
            _prewarm_show_page_session_best_effort(
                updated.session_id,
                context=context.value,
            )
        payload = _show_page_result(updated, message=message, extra=extra)
        if getattr(args, "json", False):
            _print_json(payload)
        else:
            _print_show_page_result(payload)
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        store.close()


def _read_cli_text_argument(
    *,
    value: str | None,
    file_path: str | None,
    field_name: str,
    help_command: str = "vibe show mark --help",
) -> str:
    if file_path:
        source = sys.stdin.read() if file_path == "-" else Path(file_path).read_text(encoding="utf-8")
        text = source.strip()
    else:
        text = (value or "").strip()
    if not text:
        raise TaskCliError(
            f"{field_name} is required",
            code="invalid_arguments",
            help_command=help_command,
        )
    return text


def _ui_show_events_host(config: V2Config) -> str:
    host = (getattr(config.ui, "setup_host", "") or "").strip() or "127.0.0.1"
    if host in {"0.0.0.0", "*"}:
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _local_show_events_targets(session_id: str) -> list[_LocalShowEventsTarget]:
    from urllib.parse import quote

    try:
        config = V2Config.load()
    except Exception:
        return []
    status = runtime.read_status()
    port = getattr(config.ui, "setup_port", None)
    if not status.get("ui_pid") or not port:
        return []
    path = f"/api/show/sessions/{quote(session_id, safe='')}/events"
    configured_host = _ui_show_events_host(config)
    configured_url = f"http://{configured_host}:{int(port)}{path}"
    if configured_host in {"127.0.0.1", "localhost", "[::1]"}:
        return [_LocalShowEventsTarget(configured_url)]

    loopback_url = f"http://127.0.0.1:{int(port)}{path}"
    try:
        ui_pid = int(status["ui_pid"])
    except (TypeError, ValueError):
        ui_pid = None
    return [
        _LocalShowEventsTarget(loopback_url, verify_ui_pid=ui_pid),
        _LocalShowEventsTarget(configured_url),
    ]


def _local_show_events_url(session_id: str) -> str | None:
    from urllib.parse import quote

    try:
        config = V2Config.load()
    except Exception:
        return None
    status = runtime.read_status()
    port = getattr(config.ui, "setup_port", None)
    if not status.get("ui_pid") or not port:
        return None
    return f"http://{_ui_show_events_host(config)}:{int(port)}/api/show/sessions/{quote(session_id, safe='')}/events"


def _local_show_prewarm_targets(session_id: str) -> list[_LocalShowEventsTarget]:
    return [
        _LocalShowEventsTarget(
            f"{target.url.rsplit('/', 1)[0]}/prewarm",
            verify_ui_pid=target.verify_ui_pid,
        )
        for target in _local_show_events_targets(session_id)
    ]


def _show_prewarm_target_matches_ui_pid(url: str, expected_ui_pid: int | None) -> bool:
    from urllib.parse import urlsplit, urlunsplit

    if expected_ui_pid is None:
        return False
    parts = urlsplit(url)
    status_url = urlunsplit((parts.scheme, parts.netloc, "/status", "", ""))
    request = urllib.request.Request(status_url, method="GET", headers={"X-Vibe-Show-Client": "cli"})
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        logger.debug("Failed to verify Show Page prewarm loopback target at %s", status_url, exc_info=True)
        return False
    try:
        actual_ui_pid = int(payload.get("ui_pid"))
    except (TypeError, ValueError):
        return False
    return actual_ui_pid == expected_ui_pid


def _request_show_page_prewarm_best_effort(
    session_id: str,
    *,
    context: str,
) -> dict | None:
    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token

    targets = _local_show_prewarm_targets(session_id)
    if not targets:
        return None
    payload = {"context": context}
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Vibe-Show-Client": "cli",
        SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token(),
    }
    for target in targets:
        if target.verify_ui_pid is not None and not _show_prewarm_target_matches_ui_pid(target.url, target.verify_ui_pid):
            logger.debug("Skipping unverified Show Page prewarm loopback target at %s", target.url)
            continue
        url = target.url
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
                return data if isinstance(data, dict) else None
        except Exception:
            logger.debug("Failed to request Show Page prewarm from live UI at %s", url, exc_info=True)
    return None


def _post_show_event_to_live_ui(session_id: str, payload: dict) -> dict | None:
    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token

    url = _local_show_events_url(session_id)
    if not url:
        return None
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except TimeoutError:
        return _resolve_show_event_after_ambiguous_live_timeout(session_id, payload)
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return _resolve_show_event_after_ambiguous_live_timeout(session_id, payload)
        return None
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("dispatch_pending") is True:
        return _resolve_show_event_after_ambiguous_live_timeout(session_id, payload)
    return parsed.get("event") if parsed.get("ok") is True else None


def _resolve_show_event_after_ambiguous_live_timeout(
    session_id: str,
    payload: dict,
    *,
    wait_seconds: float = 15.0,
) -> dict | None:
    """Wait for acceptance, then let the caller replay the same reservation."""
    from core.show_session_events import ShowSessionEventStore
    from storage.delivery_states import ADMITTED_DELIVERY_STATES
    event_id = payload.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    deadline = time.monotonic() + wait_seconds
    store = ShowSessionEventStore()
    try:
        while True:
            event = store.get_event(session_id, event_id)
            if event is None:
                return None
            delivery = event.get("delivery")
            if isinstance(delivery, dict):
                state = delivery.get("state")
                if state in ADMITTED_DELIVERY_STATES:
                    return event
                if state == "retired":
                    return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
    finally:
        store.close()


def _post_show_mark_to_live_ui(session_id: str, payload: dict) -> dict | None:
    return _post_show_event_to_live_ui(session_id, payload)


def _post_session_activity_to_live_ui(
    session_id: str,
    *,
    previous_scope_id: Optional[str],
    previous_visibility: Optional[str],
) -> None:
    """Best-effort: ping a running UI so it broadcasts a ``session.activity`` update
    for this session (e.g. after ``vibe session update`` renames it). The CLI writes
    the DB in a separate process from the in-proc SSE broker, so without this the
    rename only shows after a page refresh. Silently no-ops when the UI isn't running
    or is unreachable — it must never affect the CLI command's own result."""
    _post_session_cli_event_to_live_ui(
        session_id,
        {
            "event": "session_updated",
            "previous_scope_id": previous_scope_id,
            "previous_visibility": previous_visibility,
        },
    )


def _post_session_queue_updated_to_live_ui(session_id: str) -> None:
    """Best-effort queue refresh for Web surfaces after an out-of-process CLI write."""

    _post_session_cli_event_to_live_ui(
        session_id,
        {"event": "queue_updated"},
    )


def _post_session_cli_event_to_live_ui(session_id: str, payload: dict) -> None:
    from urllib.parse import quote

    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token

    try:
        config = V2Config.load()
    except Exception:
        return
    status = runtime.read_status()
    port = getattr(config.ui, "setup_port", None)
    if not status.get("ui_pid") or not port:
        return
    url = f"http://{_ui_show_events_host(config)}:{int(port)}/api/sessions/{quote(session_id, safe='')}/cli-activity"
    body = json.dumps(payload).encode("utf-8")
    http_request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Vibe-Show-Client": "cli",
            SHOW_CLI_EVENT_TOKEN_HEADER: show_cli_event_token(),
        },
    )
    try:
        with urllib.request.urlopen(http_request, timeout=3):
            pass
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
        pass


def _with_show_event_dispatch(payload: dict) -> dict:
    if isinstance(payload.get("annotation"), dict):
        return {**payload, "annotation": {**payload["annotation"], "dispatch": True}}
    if isinstance(payload.get("payload"), dict):
        return {**payload, "payload": {**payload["payload"], "dispatch": True}}
    event_fields = {"type", "id", "session_id", "sessionId", "created_at", "createdAt", "anchor", "message"}
    event_payload = {key: value for key, value in payload.items() if key not in event_fields}
    return {**payload, "payload": {**event_payload, "dispatch": True}}


def _read_event_json_argument(value: str | None, file_path: str | None) -> dict:
    if value is None and file_path is None:
        return {}
    if value is not None and file_path is not None:
        raise TaskCliError(
            "use either --event-json or --event-json-file, not both",
            code="conflicting_event_json_inputs",
            help_command="vibe show event --help",
        )
    if file_path is not None:
        raw = _read_cli_text_argument(
            value=None,
            file_path=file_path,
            field_name="--event-json-file",
            help_command="vibe show event --help",
        )
    else:
        raw = value or ""
        if raw.startswith("@"):
            raw = _read_cli_text_argument(
                value=None,
                file_path=raw[1:],
                field_name="--event-json",
                help_command="vibe show event --help",
            )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskCliError(
            f"invalid event JSON: {exc}",
            code="invalid_event_json",
            help_command="vibe show event --help",
        ) from exc
    if not isinstance(payload, dict):
        raise TaskCliError(
            "event JSON must be an object",
            code="invalid_event_json",
            help_command="vibe show event --help",
        )
    return payload


def _show_mark_target(args) -> str:
    positional = (getattr(args, "target", None) or "").strip()
    option = (getattr(args, "target_option", None) or "").strip()
    if positional and option and positional != option:
        raise TaskCliError(
            "positional target and --target must match when both are provided",
            code="conflicting_mark_targets",
            help_command="vibe show mark --help",
        )
    return _read_cli_text_argument(
        value=positional or option,
        file_path=None,
        field_name="target",
        help_command="vibe show mark --help",
    )


def _record_show_mark_event(session_id: str, payload: dict, event_store) -> dict:
    event = _post_show_mark_to_live_ui(session_id, payload)
    return event if event is not None else event_store.append(session_id, payload)


def _reply_target_from_annotation(annotation: dict) -> str:
    anchor = annotation.get("anchor") if isinstance(annotation.get("anchor"), dict) else {}
    payload = annotation.get("payload") if isinstance(annotation.get("payload"), dict) else {}
    scope = str(payload.get("scope") or "default").strip() or "default"
    for key in ("target", "selector"):
        value = str(anchor.get(key) or "").strip()
        if value:
            return value
    mark = str(anchor.get("mark") or "").strip()
    if mark:
        return f"mark-{scope}-{mark}"
    anchor_id = str(anchor.get("id") or "").strip()
    if anchor_id and anchor.get("kind") == "mark":
        return f"mark-{scope}-{anchor_id}"
    return f"annotation:{annotation['id']}"


def _show_mark_body_head(body: object, *, limit: int = 80) -> str:
    text = " ".join(str(body or "").split())
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _show_mark_listing(mark: dict) -> dict:
    return {
        "id": mark.get("id"),
        "kind": mark.get("kind"),
        "target": mark.get("target"),
        "body_head": _show_mark_body_head(mark.get("body")),
        "read_state": mark.get("read_state") or "unread",
    }


def cmd_show_mark(args):
    from core.show_pages import ShowPageStore
    from core.show_session_events import ShowSessionEventStore, stable_assistant_mark_id

    page_store = ShowPageStore()
    event_store = ShowSessionEventStore()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show mark --help")
        page = page_store.ensure(session_id)
        target = _show_mark_target(args)
        body = _read_cli_text_argument(
            value=args.body,
            file_path=args.body_file,
            field_name="--message",
            help_command="vibe show mark --help",
        )
        scope = args.scope or "default"
        mark_id = stable_assistant_mark_id(scope=scope, target=target)
        replaced = any(
            mark.get("kind") == "note" and mark.get("scope") == scope and mark.get("target") == target
            for mark in event_store.active_marks(session_id)
        )
        payload = {
            "type": "assistant.mark.created",
            "mark": {
                "id": mark_id,
                "scope": scope,
                "target": target,
                "body": body,
            },
        }
        # Either flag alone is a valid invocation -- the parser advertises them
        # independently -- and `--anchor-text` is the one the chat transcript reads
        # to tell the user where a mark landed, so nesting it under the selector
        # dropped exactly the copy that locates the mark for a human.
        anchor = {
            key: value
            for key, value in (("selector", args.anchor_selector), ("text", args.anchor_text))
            if value
        }
        if anchor:
            payload["anchor"] = anchor
        event = _record_show_mark_event(session_id, payload, event_store)
        result = _show_page_result(
            page,
            message="Assistant mark replaced." if replaced else "Assistant mark recorded.",
            extra={
                **({"session_default_notice": session_default_notice} if session_default_notice else {}),
                "event": event,
                "event_id": event["id"],
                "mark_id": mark_id,
                "message_id": event.get("message_id"),
                "replaced": replaced,
            },
        )
        if getattr(args, "json", False):
            _print_json(result)
        else:
            _print_show_page_result(result)
            print("")
            print("Mark:")
            print(f"  Event: {event['id']}")
            print(f"  Message: {event.get('message_id') or 'none'}")
            print(f"  Target: {target}")
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        page_store.close()
        event_store.close()


def cmd_show_reply(args):
    from core.show_pages import ShowPageStore
    from core.show_session_events import ShowSessionEventStore, stable_assistant_mark_id

    page_store = ShowPageStore()
    event_store = ShowSessionEventStore()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show reply --help")
        annotation = event_store.get_annotation_event(session_id, args.show_event_id)
        if annotation is None:
            recent_ids = event_store.recent_annotation_event_ids(session_id)
            recent_text = ", ".join(recent_ids) if recent_ids else "none"
            raise TaskCliError(
                f"Annotation event {args.show_event_id!r} was not found in this session. "
                f"Recent annotation event ids: {recent_text}.",
                code="show_annotation_not_found",
                hint="Use an event id dispatched for this Agent Session.",
                help_command="vibe show reply --help",
                details={"recent_annotation_event_ids": recent_ids},
            )

        page = page_store.ensure(session_id)
        body = _read_cli_text_argument(
            value=args.message,
            file_path=args.message_file,
            field_name="--message",
            help_command="vibe show reply --help",
        )
        annotation_payload = annotation.get("payload") or {}
        scope = str(annotation_payload.get("scope") or "default").strip() or "default"
        target = _reply_target_from_annotation(annotation)
        mark_id = stable_assistant_mark_id(scope=scope, reply_to=annotation["id"])
        replaced = any(mark.get("replyTo") == annotation["id"] for mark in event_store.active_marks(session_id))
        payload = {
            "type": "assistant.mark.created",
            "mark": {
                "id": mark_id,
                "scope": scope,
                "target": target,
                "body": body,
                "replyTo": annotation["id"],
            },
            "anchor": dict(annotation.get("anchor") or {}),
        }
        event = _record_show_mark_event(session_id, payload, event_store)
        replacement_notice = None
        if replaced:
            replacement_notice = "This annotation already had a reply; the active reply was replaced. Run: vibe show marks"
        result = _show_page_result(
            page,
            message="Show Page reply replaced." if replaced else "Show Page reply recorded.",
            extra={
                **({"session_default_notice": session_default_notice} if session_default_notice else {}),
                "event": event,
                "event_id": event["id"],
                "mark_id": mark_id,
                "reply_to": annotation["id"],
                "replaced": replaced,
                **({"replacement_notice": replacement_notice} if replacement_notice else {}),
            },
        )
        if getattr(args, "json", False):
            _print_json(result)
        else:
            _print_show_page_result(result)
            print("")
            print("Reply:")
            print(f"  Event: {event['id']}")
            print(f"  Mark: {mark_id}")
            print(f"  Reply to: {annotation['id']}")
            print(f"  Target: {target}")
            if replacement_notice:
                print(f"  Note: {replacement_notice}")
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        page_store.close()
        event_store.close()


def cmd_show_marks(args):
    from core.show_session_events import ShowSessionEventStore

    event_store = ShowSessionEventStore()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show marks --help")
        page_request = _page_request_from_args(args, help_command="vibe show marks --help")
        marks = [_show_mark_listing(mark) for mark in event_store.active_marks(session_id)]
        page = page_sequence(marks, page_request)
        command = ["vibe", "show", "marks", "--session-id", session_id]
        if getattr(args, "json", False):
            command.append("--json")
        result = {
            "ok": True,
            "session_id": session_id,
            "count": len(page.items),
            "marks": page.items,
            **_paginated_fields(page, command=command),
            **({"session_default_notice": session_default_notice} if session_default_notice else {}),
        }
        if getattr(args, "json", False):
            _print_json(result)
        else:
            print(f"Assistant marks ({len(page.items)} shown):")
            if not page.items:
                print("  none")
            for mark in page.items:
                print(f"- {mark['id']}  {mark['kind']}  {mark['read_state']}")
                print(f"  Target: {mark['target']}")
                print(f"  Body: {mark['body_head']}")
            if result.get("message"):
                print("")
                print(result["message"])
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        event_store.close()


def cmd_show_unmark(args):
    from core.show_session_events import ShowSessionEventStore

    event_store = ShowSessionEventStore()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show unmark --help")
        active_marks = event_store.active_marks(session_id)
        results = []
        succeeded = 0
        for identifier in args.identifiers:
            matches = [
                mark for mark in active_marks if mark.get("id") == identifier or mark.get("target") == identifier
            ]
            if not matches:
                results.append({"input": identifier, "ok": False, "error": "active mark not found"})
                continue

            resolved_ids = []
            event_ids = []
            errors = []
            for mark in matches:
                mark_payload = {
                    key: mark[key]
                    for key in ("id", "scope", "target", "body", "createdAt", "updatedAt", "replyTo")
                    if mark.get(key) is not None
                }
                try:
                    event = _record_show_mark_event(
                        session_id,
                        {"type": "assistant.mark.resolved", "mark": mark_payload, "anchor": mark.get("anchor") or {}},
                        event_store,
                    )
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                resolved_ids.append(mark["id"])
                event_ids.append(event["id"])
                active_marks = [candidate for candidate in active_marks if candidate.get("id") != mark["id"]]

            item_ok = bool(resolved_ids)
            if item_ok:
                succeeded += 1
            results.append(
                {
                    "input": identifier,
                    "ok": item_ok,
                    "mark_ids": resolved_ids,
                    "event_ids": event_ids,
                    **({"errors": errors} if errors else {}),
                }
            )

        result = {
            "ok": succeeded > 0,
            "session_id": session_id,
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "results": results,
            **({"session_default_notice": session_default_notice} if session_default_notice else {}),
        }
        if getattr(args, "json", False):
            _print_json(result)
        else:
            for item in results:
                if item["ok"]:
                    print(f"Resolved {item['input']}: {', '.join(item['mark_ids'])}")
                else:
                    detail = "; ".join(item.get("errors") or []) or item.get("error") or "failed"
                    print(f"Failed {item['input']}: {detail}", file=sys.stderr)
        return 0 if succeeded > 0 else 1
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        event_store.close()


def cmd_show_event(args):
    from core.show_pages import ShowPageStore

    page_store = ShowPageStore()
    event_id_for_retry = None
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show event --help")
        page = page_store.ensure(session_id)
        payload = _read_event_json_argument(args.event_json, args.event_json_file)
        if args.type:
            payload = {**payload, "type": args.type}
        if args.dispatch:
            payload = _with_show_event_dispatch(payload)
        event_id = payload.get("id")
        payload["id"] = (
            event_id.strip()
            if isinstance(event_id, str) and event_id.strip()
            else f"show_evt_{uuid4().hex[:16]}"
        )
        event_id_for_retry = payload["id"]
        event = _post_show_event_to_live_ui(session_id, payload)
        if event is None:
            # The local bridge handles both shapes: non-dispatch events are
            # immediately visible, while any normalized dispatch:true event
            # reserves and synchronously settles through the unified entry.
            from vibe.ui_server import record_local_show_event

            event = record_local_show_event(session_id, payload)
        result = _show_page_result(
            page,
            message="Show event recorded.",
            extra={
                **({"session_default_notice": session_default_notice} if session_default_notice else {}),
                "event": event,
                "event_id": event["id"],
                "message_id": event.get("message_id"),
            },
        )
        if getattr(args, "json", False):
            _print_json(result)
        else:
            _print_show_page_result(result)
            print("")
            print("Event:")
            print(f"  Event: {event['id']}")
            print(f"  Type: {event['type']}")
            print(f"  Message: {event.get('message_id') or 'none'}")
        return 0
    except Exception as exc:
        if event_id_for_retry:
            details = getattr(exc, "details", None)
            retry_details = dict(details) if isinstance(details, dict) else {}
            retry_details["event_id"] = event_id_for_retry
            exc.details = retry_details
        _print_show_page_error(exc)
        return 1
    finally:
        page_store.close()


def cmd_show_annotate(args):
    from core.show_pages import ShowPageStore
    from core.show_session_events import ShowSessionEventStore

    page_store = ShowPageStore()
    event_store = None
    try:
        session_id, session_default_notice = _resolve_show_session_id(
            args,
            help_command="vibe show annotate --help",
        )
        if not (args.annotation_on or args.annotation_off or args.mode):
            raise TaskCliError(
                "pass --on, --off, or --mode",
                code="invalid_arguments",
                help_command="vibe show annotate --help",
            )
        if args.annotation_off and args.mode:
            raise TaskCliError(
                "--off cannot be combined with --mode",
                code="invalid_arguments",
                help_command="vibe show annotate --help",
            )

        page, _created = page_store.ensure_active(session_id)
        if args.annotation_on:
            control = {"action": "enable", **({"mode": args.mode} if args.mode else {})}
        elif args.annotation_off:
            control = {"action": "disable"}
        else:
            control = {"action": "set-mode", "mode": args.mode}
        payload = {"type": "system.annotation.control", "payload": control}
        event = _post_show_event_to_live_ui(session_id, payload)
        if event is None:
            event_store = ShowSessionEventStore()
            event = event_store.append(session_id, payload)

        result = _show_page_result(
            page,
            message="Annotation control recorded.",
            extra={
                **({"session_default_notice": session_default_notice} if session_default_notice else {}),
                "event": event,
                "event_id": event["id"],
                "message_id": event.get("message_id"),
            },
        )
        if getattr(args, "json", False):
            _print_json(result)
        else:
            _print_show_page_result(result)
            print("")
            print("Annotation:")
            print(f"  Event: {event['id']}")
            print(f"  Action: {event['payload']['action']}")
            if event["payload"].get("mode"):
                print(f"  Mode: {event['payload']['mode']}")
        return 0
    except Exception as exc:
        _print_show_page_error(exc)
        return 1
    finally:
        page_store.close()
        if event_store is not None:
            event_store.close()


def cmd_show(args):
    if args.show_command is None:
        args.show_help_parser.print_help()
        return 0
    if args.show_command == "list":
        return cmd_show_list(args)
    if args.show_command == "path":
        return cmd_show_path(args)
    if args.show_command == "status":
        return cmd_show_status(args)
    if args.show_command == "update":
        return cmd_show_update(args)
    if args.show_command == "mark":
        return cmd_show_mark(args)
    if args.show_command == "reply":
        return cmd_show_reply(args)
    if args.show_command == "marks":
        return cmd_show_marks(args)
    if args.show_command == "unmark":
        return cmd_show_unmark(args)
    if args.show_command == "event":
        return cmd_show_event(args)
    if args.show_command == "annotate":
        return cmd_show_annotate(args)
    raise TaskCliError(
        "show command is required",
        code="invalid_arguments",
        help_command="vibe show --help",
    )


def _doctor_repair_requested(args) -> bool:
    return bool(
        getattr(args, "fix", False)
        or getattr(args, "doctor_action", None) == "repair"
    )


def _print_doctor_repair_result(result: dict) -> None:
    language = _configured_cli_language()
    title = i18n_t("doctor.repairTitle", language)
    if result.get("dry_run"):
        title += i18n_t("doctor.dryRunSuffix", language)
    print(f"\n  {title}")
    print("  " + "=" * 40)
    for item in result.get("results", []):
        status = item.get("status")
        if status in {"repaired", "planned"}:
            icon = "\033[32m✓\033[0m"
        elif status == "skipped":
            icon = "\033[33m!\033[0m"
        else:
            icon = "\033[31m✗\033[0m"
        print(f"  {icon} {item.get('target')}: {item.get('message')}")
    print()


def cmd_doctor(args=None):
    if args is not None and _doctor_repair_requested(args):
        targets = list(getattr(args, "doctor_repair_targets", []) or [])
        dry_run = bool(getattr(args, "dry_run", False))
        if not dry_run and not getattr(args, "yes", False) and not _confirm_doctor_repair(targets):
            print(i18n_t("doctor.repairNotRun", _configured_cli_language()), file=sys.stderr)
            return 2
        result = _repair_doctor_targets(
            targets,
            dry_run=dry_run,
            deep=bool(getattr(args, "doctor_deep", False)),
        )
        _print_doctor_repair_result(result)
        return 0 if result.get("ok") else 1

    deep = bool(getattr(args, "doctor_deep", False)) if args is not None else False
    result = _doctor(deep=deep)

    # Terminal-friendly output
    language = _configured_cli_language()
    print(f"\n  {i18n_t('doctor.title', language)}")
    print("  " + "=" * 40)

    for group in result.get("groups", []):
        print(f"\n  {group['name']}")
        print("  " + "-" * 30)
        for item in group.get("items", []):
            status = item["status"]
            if status == "pass":
                icon = "\033[32m✓\033[0m"  # Green checkmark
            elif status == "warn":
                icon = "\033[33m!\033[0m"  # Yellow warning
            else:
                icon = "\033[31m✗\033[0m"  # Red X

            print(f"  {icon} {item['message']}")
            if item.get("action"):
                print(f"      → {item['action']}")

    summary = result.get("summary", {})
    print("\n  " + "-" * 30)
    print(
        f"  \033[32m{i18n_t('doctor.summary.passed', language, count=summary.get('pass', 0))}\033[0m  "
        f"\033[33m{i18n_t('doctor.summary.warnings', language, count=summary.get('warn', 0))}\033[0m  "
        f"\033[31m{i18n_t('doctor.summary.failed', language, count=summary.get('fail', 0))}\033[0m"
    )
    print()

    return 0 if result["ok"] else 1


def cmd_screenshot(args):
    try:
        result = capture_screenshot(getattr(args, "output", None))
    except ScreenshotError as exc:
        payload = {
            "ok": False,
            "code": "screenshot_failed",
            "error": str(exc),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2), file=sys.stderr)
        else:
            print(f"Screenshot failed: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(result.path),
                    "backend": result.backend,
                },
                indent=2,
            )
        )
    else:
        print(str(result.path))
    return 0


def cmd_version():
    """Show current version."""
    print(f"avibe-os {__version__}")
    return 0


def get_latest_version() -> dict:
    """Fetch latest version info from PyPI.

    Returns:
        {"current": str, "latest": str, "has_update": bool, "error": str|None}
    """
    return get_latest_version_info(__version__)


def cmd_check_update():
    """Check for available updates."""
    print(f"Current version: {__version__}")
    print("Checking for updates...")

    info = get_latest_version()

    if info["error"]:
        print(f"\033[33mFailed to check for updates: {info['error']}\033[0m")
        return 1

    if info["has_update"]:
        print(f"\033[32mNew version available: {info['latest']}\033[0m")
        print(f"\nRun '\033[1mvibe upgrade\033[0m' to update.")
    else:
        print("\033[32mYou are using the latest version.\033[0m")

    return 0


def cmd_upgrade():
    """Upgrade avibe-os to the latest version."""
    print(f"Current version: {__version__}")
    print("Checking for updates...")

    info = get_latest_version()

    if info["error"]:
        print(f"\033[33mFailed to check for updates: {info['error']}\033[0m")
    elif not info["has_update"]:
        print("\033[32mYou are already using the latest version.\033[0m")
        return 0
    else:
        print(f"New version available: {info['latest']}")

    current_vibe_path = cache_running_vibe_path()
    try:
        plan = build_upgrade_plan(
            vibe_path=current_vibe_path,
            memory_enabled=configured_memory_enabled(),
            target_version=info.get("latest"),
        )
    except MemoryRequirementUnreadableError:
        print(f"\033[31m{i18n_t('update.memoryRequirementUnreadable')}\033[0m")
        return 1
    except ValueError as exc:
        print(f"\033[31mUpgrade failed: {exc}\033[0m")
        return 1
    if info["error"]:
        print("Attempting upgrade anyway...")
    print("\nUpgrading...")
    if plan.preflight_error:
        print(f"\033[31mUpgrade cannot be activated safely: {plan.preflight_error}\033[0m")
        return 1
    print(f"Using {plan.method}: {' '.join(plan.command)}")
    runtime_was_running = _runtime_process_was_running()

    # Use a stable directory as cwd to avoid issues when running from a
    # directory that uv may delete during upgrade (e.g. inside the uv tool venv).
    safe_cwd = get_safe_cwd()
    restart = None
    restart_error = None
    deferred_activation = False
    restart_python = None

    try:
        with atomic_upgrade_lock():
            if restart_is_pending():
                print("\033[31mUpgrade already has a restart in progress; wait for it to finish.\033[0m")
                return 1
            if plan.activation is not None and activation_block_reason(plan.activation) == "superseded":
                print("\033[31mUpgrade was superseded by another activation; retry the upgrade.\033[0m")
                return 1
            result = execute_upgrade_plan(
                plan,
                run=subprocess.run,
                capture_output=True,
                text=True,
                cwd=safe_cwd,
                timeout=UPGRADE_INSTALL_TIMEOUT_SECONDS,
            )
            if result.returncode == 0 and plan.activation is not None:
                try:
                    if os.name == "nt" and launcher_is_current_process(plan.activation.launcher):
                        candidate_result = verify_upgrade_candidate(plan.activation)
                        if not candidate_result.ok:
                            raise RuntimeError(candidate_result.detail)
                        defer_upgrade_activation(
                            plan.activation,
                            parent_pid=os.getpid(),
                            restart_required=runtime_was_running,
                            prepare_show_runtime=not should_skip_show_runtime_prepare(),
                        )
                        deferred_activation = True
                    else:
                        restart_python = _candidate_python(plan.activation.candidate_launcher)
                        activate_upgrade_candidate(plan.activation)
                except Exception as exc:  # noqa: BLE001
                    discard_atomic_uv_install_generation(plan.activation.candidate_launcher)
                    print(f"\033[31mUpgrade candidate failed integrity verification: {exc}\033[0m")
                    return 1
            elif result.returncode != 0 and plan.activation is not None:
                discard_atomic_uv_install_generation(plan.activation.candidate_launcher)
            if result.returncode == 0 and plan.activation is None and plan.method == "pip":
                integrity = verify_python_environment(sys.executable)
                if not integrity.ok:
                    print(f"\033[31mUpgrade installed an incomplete Python environment: {integrity.detail}\033[0m")
                    return 1
            if result.returncode == 0 and runtime_was_running and not deferred_activation:
                try:
                    restart = schedule_restart(
                        delay_seconds=0.0,
                        vibe_path=current_vibe_path,
                        trigger="upgrade",
                        prepare_show_runtime=not should_skip_show_runtime_prepare(),
                        **({"python_executable": str(restart_python)} if restart_python else {}),
                    )
                except Exception as exc:
                    restart_error = exc
        if result.returncode == 0:
            print("\033[32mUpgrade successful!\033[0m")
            if deferred_activation:
                print("Upgrade validated; launcher activation will complete after this command exits.")
                if runtime_was_running:
                    print("Restart will be scheduled by the activation helper.")
                return 0
            if restart_error is not None:
                print("\033[33mUpgrade installed, but restart scheduling failed.\033[0m")
                print(f"Restart error: {restart_error}")
                print("Run `vibe restart` to use the new version.")
                return 2
            if restart is not None:
                print("Restart scheduled to use the new version.")
                print(f"Job ID: {restart['job_id']}")
                print("Run `vibe status` to inspect the restart result.")
            else:
                _prepare_show_runtime_after_install(current_vibe_path)
                print("Avibe was not running; the new version will be used next time you start it.")
            return 0
        else:
            print(f"\033[31mUpgrade failed:\033[0m\n{result.stderr}")
            return 1
    except Exception as e:
        if plan.activation is not None:
            discard_atomic_uv_install_generation(plan.activation.candidate_launcher)
        print(f"\033[31mUpgrade failed: {e}\033[0m")
        return 1


def _show_runtime_manager_from_args(args):
    from core.show_runtime import ShowRuntimeManager

    offline = True if getattr(args, "offline", False) else None
    return ShowRuntimeManager(
        runtime_source=getattr(args, "source", None),
        manifest_path=getattr(args, "manifest", None),
        manifest_url=getattr(args, "manifest_url", None),
        offline=offline,
        force_install=bool(getattr(args, "force", False)),
    )


def _git_runtime_status() -> dict:
    try:
        from core.git_runtime import git_runtime_status

        return git_runtime_status()
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "git",
            "resolution": "none",
            "path": None,
            "version": None,
            "reason": str(exc),
        }


def _git_prepare_satisfies_strict(result: dict) -> bool:
    if result.get("ok"):
        return True
    return result.get("reason") in {
        "git_platform_unsupported",
        "git_runtime_unpublished",
    }


def _print_runtime_status(payload: dict) -> None:
    print("Show Runtime:")
    print(f"  Provider: {payload.get('provider')}")
    print(f"  Platform: {payload.get('platform')}")
    print(f"  Node: {'available' if payload.get('node_available') else 'missing'}")
    manifest = payload.get("manifest") or {}
    if manifest:
        print(f"  Manifest runtime: {manifest.get('runtime_version')}")
        print(f"  Manifest sha256: {manifest.get('sha256')}")
        print(f"  Manifest source: {manifest.get('source')}")
    archive = payload.get("archive") or {}
    if archive:
        print(f"  Archive: {archive.get('name')}")
        print(f"  Archive sha256: {archive.get('sha256')}")
    install = _show_runtime_install(payload)
    print(f"  Installed: {'yes' if install.get('state') == 'installed' else 'no'}")
    if install.get("install_dir"):
        print(f"  Install dir: {install.get('install_dir')}")
    if payload.get("reason"):
        print(f"  Reason: {payload.get('reason')}")
    git = payload.get("git") or {}
    print("Git Runtime:")
    print(f"  Resolution: {git.get('resolution') or 'none'}")
    print(f"  Version: {git.get('version') or 'unknown'}")
    if git.get("path"):
        print(f"  Path: {git['path']}")
    agent_git = git.get("agent") or {}
    print(f"  Agent PATH resolution: {agent_git.get('resolution') or 'none'}")
    if agent_git.get("path"):
        print(f"  Agent PATH: {agent_git['path']}")
    managed_git = git.get("managed") or {}
    if managed_git.get("reason"):
        print(f"  Managed runtime: {managed_git['reason']}")


def cmd_runtime(args) -> int:
    manager = _show_runtime_manager_from_args(args)
    command = getattr(args, "runtime_command", None)
    if command == "status":
        payload = manager.status()
        payload["git"] = _git_runtime_status()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            _print_runtime_status(payload)
        return 0
    if command == "prepare":
        offline = True if getattr(args, "offline", False) else None
        payload = manager.prepare(force=getattr(args, "force", False), offline=offline)
        force = bool(getattr(args, "force", False))
        askill = _ensure_askill_during_prepare(offline=bool(offline), force=force)
        tmux = _ensure_tmux_during_prepare(offline=bool(offline), force=force)
        git = _ensure_git_during_prepare(offline=offline, force=force)
        avault = _ensure_avault_during_prepare(offline=bool(offline), force=force)
        model_hub_engine = _ensure_model_hub_engine_during_prepare(
            offline=bool(offline),
            force=force,
        )
        payload["askill"] = askill
        payload["avault"] = avault
        payload["model_hub_engine"] = model_hub_engine
        payload["tmux"] = tmux
        payload["git"] = git
        install = payload.get("install") if isinstance(payload.get("install"), dict) else {}
        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        runtime_prepared = bool(payload.get("ok"))
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            language = _configured_cli_language()
            if runtime_prepared:
                print(i18n_t("runtime.prepare.prepared", language))
                status = payload.get("status") or {}
                status_install = _show_runtime_install(status)
                if status_install.get("install_dir"):
                    print(f"Install dir: {status_install['install_dir']}")
            elif policy.get("state") == "skipped":
                print(
                    i18n_t(
                        "runtime.prepare.skipped",
                        language,
                        reason=policy.get("reason") or "unknown",
                    ),
                    file=sys.stderr,
                )
            else:
                reason = payload.get("reason") or install.get("reason") or "unknown"
                print(
                    i18n_t(
                        (
                            "runtime.prepare.unsupportedSource"
                            if reason == "runtime_source_unsupported"
                            else "runtime.prepare.failed"
                        ),
                        language,
                        reason=reason,
                    ),
                    file=sys.stderr,
                )
            if askill.get("skipped"):
                print(f"askill: skipped ({askill.get('reason') or 'skipped'}).")
            elif askill.get("ok"):
                print("askill installed." if askill.get("changed") else "askill ready.")
            else:
                print(f"askill not ready: {askill.get('message') or 'install failed'}", file=sys.stderr)
            if avault.get("skipped"):
                print(f"avault: skipped ({avault.get('reason') or 'skipped'}).")
            elif avault.get("ok"):
                print("avault installed." if avault.get("changed") else "avault ready.")
            else:
                print(f"avault not ready: {avault.get('message') or 'install failed'}", file=sys.stderr)
            if model_hub_engine.get("skipped"):
                print(
                    i18n_t(
                        "runtime.prepare.modelHubEngineSkipped",
                        language,
                        reason=model_hub_engine.get("reason") or "skipped",
                    )
                )
            elif model_hub_engine.get("ok"):
                print(
                    i18n_t(
                        (
                            "runtime.prepare.modelHubEngineInstalled"
                            if model_hub_engine.get("changed")
                            else "runtime.prepare.modelHubEngineReady"
                        ),
                        language,
                    )
                )
            else:
                print(
                    i18n_t(
                        "runtime.prepare.modelHubEngineNotReady",
                        language,
                        reason=(
                            model_hub_engine.get("message")
                            or model_hub_engine.get("reason")
                            or "install failed"
                        ),
                    ),
                    file=sys.stderr,
                )
            if tmux.get("skipped") or tmux.get("status") == "skipped":
                print(f"tmux: skipped ({tmux.get('reason') or 'skipped'}).")
            elif tmux.get("ok"):
                print("tmux installed." if tmux.get("changed") else "tmux ready.")
            else:
                print(f"tmux not ready: {tmux.get('message') or tmux.get('reason') or 'install failed'}", file=sys.stderr)
            if git.get("skipped"):
                print(f"git runtime: skipped ({git.get('reason') or 'skipped'}).")
            elif git.get("ok"):
                print("git runtime installed." if git.get("changed") else "git runtime ready.")
            else:
                print(
                    f"git runtime not ready: {git.get('message') or git.get('reason') or 'install failed'}",
                    file=sys.stderr,
                )
        strict_ok = runtime_prepared and _git_prepare_satisfies_strict(git)
        return 1 if getattr(args, "strict", False) and not strict_ok else 0
    if command == "clean":
        dry_run = bool(getattr(args, "dry_run", False))
        keep_previous = getattr(args, "keep_previous", 1)
        payload = manager.clean(
            keep_previous=keep_previous,
            dry_run=dry_run,
        )
        managed_runtimes = _clean_managed_runtime_consumers(
            keep_previous=keep_previous,
            dry_run=dry_run,
        )
        payload.update(managed_runtimes)
        show_verdict = _runtime_clean_verdict(payload, dry_run=dry_run)
        managed_verdicts = {
            runtime_id: _runtime_clean_verdict(result, dry_run=dry_run)
            for runtime_id, result in managed_runtimes.items()
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            language = _configured_cli_language()
            archives_value = payload.get("archives")
            archives = archives_value if isinstance(archives_value, Mapping) else {}
            skipped_reason = str(archives.get("skipped_reason") or "")
            outcome = str(archives.get("outcome") or "")
            # Consumer results are reported independently of the Show archive
            # outcome: a skipped archive pass must not hide what the rest of
            # the cleanup actually reclaimed (or would reclaim).
            prefix_key = "runtime.clean.wouldRemove" if dry_run else "runtime.clean.removed"
            removed = payload.get("removed") or []
            print(i18n_t(f"{prefix_key}Items", language, count=len(removed)))
            if show_verdict.failed:
                _print_runtime_clean_failure(
                    consumer="Show Runtime",
                    reason=show_verdict.reason,
                    dry_run=dry_run,
                    language=language,
                )
            is_partial_run = outcome == "partial" and not dry_run
            if is_partial_run:
                print(
                    i18n_t(
                        "runtime.clean.removedArchives",
                        language,
                        count=int(archives.get("removed_count") or 0),
                        size=_format_byte_size(int(archives.get("removed_bytes") or 0)),
                    )
                )
                print(
                    i18n_t("runtime.clean.partiallyRemoved", language, failed=int(archives.get("failed_count") or 0)),
                    file=sys.stderr,
                )
            elif skipped_reason:
                # A skipped/failed archive pass is not a completed zero-removal
                # cleanup; say so instead of printing placeholder counts, with
                # remediation that matches the actual reason.
                if skipped_reason == "archive_removal_failed":
                    print(
                        i18n_t(
                            "runtime.clean.removalFailed",
                            language,
                            failed=int(archives.get("failed_count") or 0),
                        ),
                        file=sys.stderr,
                    )
                else:
                    skip_key = (
                        "runtime.clean.skippedInspection"
                        if skipped_reason == "archive_inspection_failed"
                        else "runtime.clean.skipped"
                    )
                    print(i18n_t(skip_key, language, reason=skipped_reason), file=sys.stderr)
            elif not show_verdict.archives_failed and (archives or not show_verdict.failed):
                archive_count = int(archives.get("candidate_count") or 0) if dry_run else int(archives.get("removed_count") or 0)
                archive_bytes = int(archives.get("candidate_bytes") or 0) if dry_run else int(archives.get("removed_bytes") or 0)
                print(
                    i18n_t(
                        f"{prefix_key}Archives",
                        language,
                        count=archive_count,
                        size=_format_byte_size(archive_bytes),
                    )
                )
            for runtime_id, result in managed_runtimes.items():
                _print_managed_runtime_clean_result(
                    runtime_id=runtime_id,
                    result=result,
                    dry_run=dry_run,
                    language=language,
                    verdict=managed_verdicts[runtime_id],
                )
        failed = show_verdict.failed or any(verdict.failed for verdict in managed_verdicts.values())
        return 1 if failed else 0
    raise TaskCliError("runtime command is required", code="invalid_arguments", help_command="vibe runtime --help")


def _prepare_show_runtime_after_install(vibe_path: str | None) -> None:
    if should_skip_show_runtime_prepare():
        print("\033[33mSkipping Show Runtime preparation because VIBE_INSTALL_SKIP_SHOW_RUNTIME is set.\033[0m")
        return
    executable = vibe_path or shutil.which("vibe")
    if not executable:
        print("\033[33mShow Runtime was not prepared because the vibe executable was not found.\033[0m")
        return
    safe_cwd = get_safe_cwd()
    try:
        result = subprocess.run(
            [executable, "runtime", "prepare", "--strict"],
            capture_output=True,
            text=True,
            cwd=safe_cwd,
            # 600s (not 300s): prepare now refreshes both the Show Runtime AND
            # askill, so budget for two installers nested in this one call.
            timeout=600,
            check=False,
        )
    except Exception as exc:
        print(f"\033[33mShow Runtime preparation skipped: {exc}\033[0m")
        return
    if result.returncode == 0:
        print("Show Runtime prepared.")
        return
    detail = (result.stderr or result.stdout).strip()
    print("\033[33mShow Runtime preparation failed; Avibe upgrade is still installed.\033[0m")
    if detail:
        print(detail)


def _ensure_askill_during_prepare(offline: bool = False, force: bool = False) -> dict:
    """Ensure askill (a required local dependency) alongside the Show Runtime.

    Folded into ``vibe runtime prepare`` so askill auto-installs at exactly the
    same lifecycle points as the Show Page runtime (post install / upgrade),
    with a ``VIBE_INSTALL_SKIP_ASKILL`` escape hatch mirroring the Show Runtime
    one. Skipped under ``--offline`` (the askill installer needs the network).
    Refreshes askill to latest so prepare stays the chokepoint that keeps
    required local deps current on upgrade, but asks whether that refresh would
    change anything before running the installer: askill.sh re-downloads the CLI
    on every run, so an unconditional refresh charged every prepare ~30s to
    install the version already on disk. An askill hiccup never fails the
    prepare; the Dependencies page offers a manual retry.

    ``force`` is prepare's ``--force``, and it means repair, not currency: a
    corrupted binary can still report the current version, so an explicit
    ``vibe runtime prepare --force`` must reinstall rather than ask. Currency is
    the default; repair stays available on request, exactly as it is for the
    Show Runtime, tmux, and git phases.

    Only an explicit ``up_to_date`` verdict may report ready. Any other verdict
    that installed nothing means currency was not established, not that it holds,
    so prepare installs instead of claiming a fact it never checked.
    """
    if offline:
        return {"ok": True, "skipped": True, "reason": "offline"}
    if os.environ.get("VIBE_INSTALL_SKIP_ASKILL", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_ASKILL"}
    try:
        if force:
            return api.ensure_askill_installed(force=True)
        result = api.refresh_askill_if_stale()
        if not (result.get("ok") and result.get("action") is None):
            return result
        if result.get("reason") != "up_to_date":
            # The owner skipped without establishing currency — today that is
            # ``latest_unavailable``, when the upstream version probe failed.
            # Prepare is the chokepoint that must *make* the dependency current,
            # so with no evidence either way it does what it did before this fast
            # path existed and installs. The probe and the askill.sh installer
            # are independent paths: a rate-limited or blipped version lookup
            # says nothing about whether the install would succeed, and reporting
            # ready off the back of it would claim currency we never checked.
            # Only ``up_to_date`` may report ready, so a skip reason added later
            # takes this branch rather than inheriting a false pass.
            refreshed = api.ensure_askill_installed(force=True)
            refreshed["action"] = "refresh_currency_unknown"
            return refreshed
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    # Already current, and the owner said so: report ready rather than skipped so
    # prepare's dependency lines read the same way as the pinned providers when
    # nothing needed doing.
    status = result.get("status") or {}
    return {
        "ok": True,
        "installed": True,
        "changed": False,
        "path": status.get("path"),
        "version": status.get("version"),
    }


def _ensure_tmux_during_prepare(offline: bool = False, force: bool = False) -> dict:
    """Ensure optional tmux alongside managed runtimes.

    tmux powers persistent Web Terminal sessions, but absence must never block
    prepare or upgrades: the terminal backend will fall back to ephemeral PTY.
    """
    if offline:
        return {"ok": True, "skipped": True, "reason": "offline"}
    if os.environ.get("VIBE_UI_ENABLE_TERMINAL", "").strip().lower() in _FALSY_ENV_VALUES:
        return {"ok": True, "status": "skipped", "reason": "terminal_disabled"}
    if os.environ.get("VIBE_INSTALL_SKIP_TMUX", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_TMUX"}
    try:
        from core.tmux_runtime import ensure_tmux_installed

        return ensure_tmux_installed(force=force)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def _ensure_git_during_prepare(offline: bool | None = None, force: bool = False) -> dict:
    """Prepare verified vendored Git without making it a service-start requirement."""

    try:
        from core.git_runtime import GitRuntimeManager

        return GitRuntimeManager(offline=offline).ensure(force=force)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def _format_byte_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PiB"


def _managed_runtime_cleaners() -> tuple[tuple[str, Callable[..., dict[str, Any]]], ...]:
    """Return the shared-runtime cleanup passes in stable output order."""

    from core.tmux_runtime import get_tmux_runtime_manager
    from vibe.model_hub_runtime.installer import EngineRuntimeManager

    def clean_memory(*, keep_previous: int, dry_run: bool) -> dict[str, Any]:
        try:
            from avibe_memory.artifact import get_memory_artifact_manager
        except ModuleNotFoundError as exc:
            if exc.name not in {"avibe_memory", "avibe_memory.artifact"}:
                raise
            return {
                "ok": True,
                "removed": [],
                "skipped": True,
                "reason": "memory_implementation_unavailable",
            }

        return get_memory_artifact_manager().clean(
            keep_previous=keep_previous,
            dry_run=dry_run,
        )

    def clean_model_hub(*, keep_previous: int, dry_run: bool) -> dict[str, Any]:
        return EngineRuntimeManager().clean(
            keep_previous=keep_previous,
            dry_run=dry_run,
        )

    def clean_tmux(*, keep_previous: int, dry_run: bool) -> dict[str, Any]:
        return get_tmux_runtime_manager().clean(
            keep_previous=keep_previous,
            dry_run=dry_run,
        )

    return (
        ("git", _clean_git_runtime),
        ("memory-runtime", clean_memory),
        ("model_hub_engine", clean_model_hub),
        ("tmux", clean_tmux),
    )


def _clean_managed_runtime_consumers(*, keep_previous: int, dry_run: bool = False) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for runtime_id, cleaner in _managed_runtime_cleaners():
        try:
            result = cleaner(keep_previous=keep_previous, dry_run=dry_run)
            if not isinstance(result, Mapping):
                raise TypeError("runtime cleanup returned a non-mapping result")
            results[runtime_id] = dict(result)
        except Exception as exc:  # noqa: BLE001
            results[runtime_id] = {
                "ok": False,
                "removed": [],
                "reason": f"{runtime_id}_clean_failed",
                "message": str(exc),
            }
    return results


@dataclass(frozen=True)
class _RuntimeCleanVerdict:
    reason: str | None
    archives_failed: bool

    @property
    def failed(self) -> bool:
        return self.reason is not None


def _runtime_clean_verdict(result: Mapping[str, Any], *, dry_run: bool) -> _RuntimeCleanVerdict:
    archives_value = result.get("archives")
    archives = archives_value if isinstance(archives_value, Mapping) else {}
    archive_reason = archives.get("skipped_reason")
    failed_count = archives.get("failed_count")
    archives_failed = bool(archive_reason) or (
        not dry_run
        and (
            archives.get("outcome") == "partial"
            or (isinstance(failed_count, (int, float)) and failed_count > 0)
        )
    )
    nested_reason = archive_reason or ("archive_removal_failed" if archives_failed else None)
    ok = result.get("ok")
    reason = result.get("reason")
    top_level_failed = ok is False or (ok is not True and bool(reason))
    if not top_level_failed and not archives_failed:
        return _RuntimeCleanVerdict(reason=None, archives_failed=False)
    return _RuntimeCleanVerdict(
        reason=str(reason or nested_reason or "unknown"),
        archives_failed=archives_failed,
    )


def _managed_runtime_label(runtime_id: str) -> str:
    labels = {
        "git": "Git Runtime",
        "memory-runtime": "Memory Runtime",
        "model_hub_engine": "Model Hub Runtime",
        "tmux": "tmux Runtime",
    }
    return labels.get(runtime_id, runtime_id.replace("-", " ").replace("_", " ").title())


def _print_runtime_clean_failure(
    *,
    consumer: str,
    reason: str | None,
    dry_run: bool,
    language: str,
) -> None:
    key = "runtime.clean.consumerPreviewFailed" if dry_run else "runtime.clean.consumerFailed"
    print(
        i18n_t(
            key,
            language,
            consumer=consumer,
            reason=reason or "unknown",
        ),
        file=sys.stderr,
    )


def _print_managed_runtime_clean_result(
    *,
    runtime_id: str,
    result: Mapping[str, Any],
    dry_run: bool,
    language: str,
    verdict: _RuntimeCleanVerdict,
) -> None:
    consumer = _managed_runtime_label(runtime_id)
    prefix_key = "runtime.clean.consumerWouldRemove" if dry_run else "runtime.clean.consumerRemoved"
    removed = result.get("removed")
    removed_count = len(removed) if isinstance(removed, list) else 0
    print(i18n_t(f"{prefix_key}Items", language, consumer=consumer, count=removed_count))

    if verdict.failed:
        _print_runtime_clean_failure(
            consumer=consumer,
            reason=verdict.reason,
            dry_run=dry_run,
            language=language,
        )

    archives_value = result.get("archives")
    if not isinstance(archives_value, Mapping) or not archives_value:
        return
    archives = archives_value
    skipped_reason = str(archives.get("skipped_reason") or "")
    outcome = str(archives.get("outcome") or "")
    if outcome == "partial" and not dry_run:
        print(
            i18n_t(
                "runtime.clean.consumerRemovedArchives",
                language,
                consumer=consumer,
                count=int(archives.get("removed_count") or 0),
                size=_format_byte_size(int(archives.get("removed_bytes") or 0)),
            )
        )
        print(
            i18n_t(
                "runtime.clean.consumerArchivesPartial",
                language,
                consumer=consumer,
                reason=skipped_reason or "archive_removal_failed",
                failed=int(archives.get("failed_count") or 0),
            ),
            file=sys.stderr,
        )
        return
    if skipped_reason:
        print(
            i18n_t(
                "runtime.clean.consumerArchivesSkipped",
                language,
                consumer=consumer,
                reason=skipped_reason,
            ),
            file=sys.stderr,
        )
        return
    if verdict.archives_failed:
        return
    count_key = "candidate_count" if dry_run else "removed_count"
    bytes_key = "candidate_bytes" if dry_run else "removed_bytes"
    print(
        i18n_t(
            f"{prefix_key}Archives",
            language,
            consumer=consumer,
            count=int(archives.get(count_key) or 0),
            size=_format_byte_size(int(archives.get(bytes_key) or 0)),
        )
    )


def _clean_git_runtime(*, keep_previous: int, dry_run: bool = False) -> dict:
    try:
        from core.git_runtime import get_git_runtime_manager

        return get_git_runtime_manager().clean(keep_previous=keep_previous, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "removed": [],
            "reason": "git_clean_failed",
            "message": str(exc),
        }


def _ensure_avault_during_prepare(offline: bool = False, force: bool = False) -> dict:
    """Ensure avault (the Vault custody core) alongside other local deps.

    Raises avault to the managed pin on upgrade, but only downloads when the pin
    is not already satisfied: the reinstall it used to force on every prepare
    took ~20s to put back the release that was already installed. ``force`` is
    prepare's ``--force`` repair request and still reinstalls the managed
    release, since a corrupted binary can report the pinned version.
    """
    if offline:
        return {"ok": True, "skipped": True, "reason": "offline"}
    if os.environ.get("VIBE_INSTALL_SKIP_AVAULT", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_AVAULT"}
    try:
        if force:
            return api.ensure_avault_installed(force=True)
        return api.refresh_avault_if_stale()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def _ensure_model_hub_engine_during_prepare(
    offline: bool = False,
    force: bool = False,
) -> dict:
    """Converge CPA to the Avibe pin without making upgrade success depend on it."""

    try:
        return api.ensure_model_hub_engine_installed(
            force=force,
            offline=True if offline else None,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


def cmd_restart():
    """Restart all services (stop + start)."""
    return _cmd_restart_with_delay(0.0)


def _format_restart_delay(delay_seconds: float) -> str:
    if delay_seconds == int(delay_seconds):
        whole_seconds = int(delay_seconds)
        if whole_seconds % 60 == 0:
            minutes = whole_seconds // 60
            if minutes == 1:
                return "1 minute"
            return f"{minutes} minutes"
        if whole_seconds == 1:
            return "1 second"
        return f"{whole_seconds} seconds"
    return f"{delay_seconds:g} seconds"


def _schedule_delayed_restart(delay_seconds: float) -> int:
    current_vibe_path = cache_running_vibe_path()
    result = schedule_restart(delay_seconds=delay_seconds, vibe_path=current_vibe_path, trigger="cli")
    print(f"Restart scheduled in {_format_restart_delay(delay_seconds)}.")
    print(f"Job ID: {result['job_id']}")
    print("This command exits immediately; the restart supervisor will run in the background.")
    return 0


def _cmd_restart_with_delay(delay_seconds: float) -> int:
    if delay_seconds > 0:
        return _schedule_delayed_restart(delay_seconds)

    result = schedule_restart(delay_seconds=0.0, vibe_path=cache_running_vibe_path(), trigger="cli")
    print("Restart scheduled.")
    print(f"Job ID: {result['job_id']}")
    print("Run `vibe status` to inspect the restart result.")
    return 0


def build_parser():
    parser = VibeArgumentParser(prog="vibe")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("stop", help="Stop all services")
    subparsers.add_parser("start", help="Start services if needed without stopping running processes")
    restart_parser = subparsers.add_parser("restart", help="Restart all services")
    restart_parser.add_argument(
        "--delay-seconds",
        type=_non_negative_float,
        default=0,
        help="Schedule the restart to run asynchronously after N seconds, then exit immediately.",
    )
    # `__restart-supervisor` is deliberately absent here. It is never typed: this
    # program spawns it, and `vibe/restart_supervisor.py` owns both the argv it
    # builds and the parser that reads it back. Restating those flags here made
    # this parser a second, silently authoritative owner -- and the one that runs
    # first. See `_dispatch_restart_supervisor`.
    subparsers.add_parser("status", help="Show service status")
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run diagnostics and optional safe repairs",
        description="Run Avibe diagnostics. Use the explicit repair action to apply common runtime fixes.",
    )
    doctor_parser.add_argument(
        "doctor_action",
        nargs="?",
        choices=("repair",),
        help="Run common repair playbooks instead of only reporting diagnostics.",
    )
    doctor_parser.add_argument(
        "doctor_repair_targets",
        nargs="*",
        choices=DOCTOR_REPAIR_TARGETS,
        help="Repair target(s). Defaults to all safe first-phase repair targets.",
    )
    doctor_depth_group = doctor_parser.add_mutually_exclusive_group()
    doctor_depth_group.add_argument(
        "--fast",
        dest="doctor_deep",
        action="store_false",
        default=False,
        help="Skip deep service process scans for a faster status-oriented diagnostic run.",
    )
    doctor_depth_group.add_argument(
        "--deep",
        dest="doctor_deep",
        action="store_true",
        help="Run full diagnostics, including duplicate service process scans.",
    )
    doctor_parser.add_argument("--fix", action="store_true", help="Alias for 'vibe doctor repair'.")
    doctor_parser.add_argument("--dry-run", action="store_true", help="Show repair actions without changing state.")
    doctor_parser.add_argument("-y", "--yes", action="store_true", help="Confirm repair actions non-interactively.")
    subparsers.add_parser("version", help="Show version")
    subparsers.add_parser("check-update", help="Check for updates")
    subparsers.add_parser("upgrade", help="Upgrade to latest version")
    memory_help_language = _memory_cli_language()
    memory_parser = subparsers.add_parser(
        "memory",
        help=i18n_t("memory.cli.help.command", memory_help_language),
    )
    memory_subparsers = memory_parser.add_subparsers(
        dest="memory_command",
        metavar="{status,profile,list,search,remember}",
    )
    memory_subparsers.required = True
    skill_help_language = _configured_cli_language()
    skill_parser = subparsers.add_parser(
        "skill",
        help=i18n_t("skill.cli.help.command", skill_help_language),
    )
    skill_subparsers = skill_parser.add_subparsers(
        dest="skill_command",
        metavar="{list,load}",
    )
    skill_subparsers.required = True
    skill_list_parser = skill_subparsers.add_parser(
        "list",
        help=i18n_t("skill.cli.help.list", skill_help_language),
    )
    skill_list_parser.add_argument(
        "--page",
        type=int,
        default=1,
        help=i18n_t("skill.cli.help.page", skill_help_language),
    )
    skill_load_parser = skill_subparsers.add_parser(
        "load",
        help=i18n_t("skill.cli.help.load", skill_help_language),
    )
    skill_load_parser.add_argument(
        "name",
        help=i18n_t("skill.cli.help.name", skill_help_language),
    )
    debug_help_language = _configured_cli_language()
    debug_parser = subparsers.add_parser(
        "debug",
        help=i18n_t("debug.cli.help.command", debug_help_language),
    )
    debug_subparsers = debug_parser.add_subparsers(dest="debug_command", metavar="{prompt}")
    debug_subparsers.required = True
    debug_prompt_parser = debug_subparsers.add_parser(
        "prompt",
        help=i18n_t("debug.cli.help.prompt", debug_help_language),
    )
    debug_prompt_subparsers = debug_prompt_parser.add_subparsers(
        dest="prompt_debug_command",
        metavar="{export}",
    )
    debug_prompt_subparsers.required = True
    debug_prompt_export_parser = debug_prompt_subparsers.add_parser(
        "export",
        help=i18n_t("debug.cli.help.promptExport", debug_help_language),
    )
    debug_prompt_export_parser.add_argument(
        "--format",
        choices=("json",),
        default="json",
        help=i18n_t("debug.cli.help.promptFormat", debug_help_language),
    )
    memory_status_parser = memory_subparsers.add_parser(
        "status",
        help=i18n_t("memory.cli.help.status", memory_help_language),
    )
    memory_status_parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_t("memory.cli.help.json", memory_help_language),
    )
    memory_profile_parser = memory_subparsers.add_parser(
        "profile",
        help=i18n_t("memory.cli.help.profile", memory_help_language),
    )
    memory_profile_parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_t("memory.cli.help.json", memory_help_language),
    )
    memory_list_parser = memory_subparsers.add_parser(
        "list",
        help=i18n_t("memory.cli.help.list", memory_help_language),
    )
    memory_list_parser.add_argument(
        "--project",
        default=None,
        help=i18n_t("memory.cli.help.project", memory_help_language),
    )
    memory_list_parser.add_argument(
        "--page",
        type=int,
        default=1,
        help=i18n_t("memory.cli.help.page", memory_help_language),
    )
    memory_list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help=i18n_t("memory.cli.help.pageLimit", memory_help_language),
    )
    memory_list_parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_t("memory.cli.help.json", memory_help_language),
    )
    memory_search_parser = memory_subparsers.add_parser(
        "search",
        help=i18n_t("memory.cli.help.search", memory_help_language),
    )
    memory_search_parser.add_argument(
        "query",
        help=i18n_t("memory.cli.help.query", memory_help_language),
    )
    memory_search_parser.add_argument(
        "--limit",
        type=int,
        default=8,
        help=i18n_t("memory.cli.help.limit", memory_help_language),
    )
    memory_search_parser.add_argument(
        "--mode",
        choices=("hybrid", "keyword", "vector", "agentic"),
        default="hybrid",
        help=i18n_t("memory.cli.help.mode", memory_help_language),
    )
    memory_search_parser.add_argument(
        "--project",
        default=None,
        help=i18n_t("memory.cli.help.project", memory_help_language),
    )
    memory_search_parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_t("memory.cli.help.json", memory_help_language),
    )
    memory_remember_parser = memory_subparsers.add_parser(
        "remember",
        help=i18n_t("memory.cli.help.remember", memory_help_language),
    )
    memory_remember_parser.add_argument(
        "text",
        help=i18n_t("memory.cli.help.text", memory_help_language),
    )
    memory_remember_parser.add_argument(
        "--project",
        default=None,
        help=i18n_t("memory.cli.help.project", memory_help_language),
    )
    memory_remember_parser.add_argument(
        "--json",
        action="store_true",
        help=i18n_t("memory.cli.help.json", memory_help_language),
    )
    runtime_parser = subparsers.add_parser(
        "runtime",
        help="Inspect and prepare managed runtimes",
        description="Inspect, prepare, and clean the managed runtimes used by Avibe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe runtime --help",
    )
    runtime_subparsers = runtime_parser.add_subparsers(dest="runtime_command", metavar="{status,prepare,clean}")
    runtime_subparsers.required = True

    def add_runtime_provider_args(runtime_command_parser):
        runtime_command_parser.add_argument(
            "--source",
            choices=("manifest-cache", "manifest", "archive", "prebuilt", "npm"),
            help="Runtime provider override. Defaults to the packaged manifest cache.",
        )
        manifest_group = runtime_command_parser.add_mutually_exclusive_group()
        manifest_group.add_argument("--manifest", help="Read a development manifest from a local path.")
        manifest_group.add_argument("--manifest-url", help="Read a development manifest from a URL.")

    runtime_status_parser = runtime_subparsers.add_parser("status", help="Show managed runtime status")
    add_runtime_provider_args(runtime_status_parser)
    runtime_status_parser.add_argument("--offline", action="store_true", help="Do not fetch a remote manifest.")
    runtime_status_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    runtime_prepare_parser = runtime_subparsers.add_parser(
        "prepare",
        help="Download, verify, and install the current platform runtime",
    )
    add_runtime_provider_args(runtime_prepare_parser)
    runtime_prepare_parser.add_argument("--force", action="store_true", help="Reinstall even when the cached runtime matches.")
    runtime_prepare_parser.add_argument("--offline", action="store_true", help="Use only the verified local cache.")
    runtime_prepare_parser.add_argument("--strict", action="store_true", help="Return a non-zero exit code when preparation fails.")
    runtime_prepare_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    runtime_clean_language = _configured_cli_language()
    runtime_clean_help = i18n_t("runtime.clean.commandHelp", runtime_clean_language)
    runtime_clean_parser = runtime_subparsers.add_parser(
        "clean",
        help=runtime_clean_help,
        description=runtime_clean_help,
    )
    runtime_clean_parser.add_argument("--keep-previous", type=int, default=1, help="Number of previous runtime versions to keep.")
    runtime_clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=i18n_t("runtime.clean.dryRunHelp", runtime_clean_language),
    )
    runtime_clean_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")
    remote_parser = subparsers.add_parser(
        "remote",
        help="Manage Avibe Cloud remote access",
        description="Start a guided Avibe Cloud remote-access setup, or manage the remote-access tunnel.",
        epilog=_remote_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe remote --help",
        error_hint="Run 'vibe remote' for guided setup, or use one of the remote subcommands below.",
    )
    remote_subparsers = remote_parser.add_subparsers(
        dest="remote_command",
        metavar="[command]",
    )

    remote_pair_parser = remote_subparsers.add_parser(
        "pair",
        help="Pair directly when you already have a pairing key",
        description="Redeem an Avibe Cloud pairing key, save remote-access config, and start the managed tunnel.",
        epilog=_remote_pair_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe remote pair --help",
        error_hint="Pass a pairing key or omit it to be prompted securely.",
    )
    remote_pair_parser.add_argument(
        "pairing_key",
        nargs="?",
        help="One-time pairing key from the Avibe Cloud console. Omit to enter it securely.",
    )
    remote_pair_parser.add_argument(
        "--backend-url",
        default="https://avibe.bot",
        help="Avibe Cloud backend URL. Default: https://avibe.bot",
    )
    remote_pair_parser.add_argument(
        "--device-name",
        default="avibe",
        help="Human-friendly name for this local device. Default: avibe",
    )
    remote_pair_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw machine-readable pairing result.",
    )

    remote_status_parser = remote_subparsers.add_parser(
        "status",
        help="Show remote-access status",
        description="Show pairing, tunnel, and cloudflared status for Avibe Cloud remote access.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe remote status --help",
    )
    remote_status_parser.add_argument("--json", action="store_true", help="Print the raw machine-readable status.")

    remote_start_parser = remote_subparsers.add_parser(
        "start",
        help="Start the remote-access tunnel",
        description="Start the managed cloudflared tunnel for the saved Avibe Cloud pairing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe remote start --help",
    )
    remote_start_parser.add_argument("--json", action="store_true", help="Print the raw machine-readable result.")

    remote_stop_parser = remote_subparsers.add_parser(
        "stop",
        help="Stop the remote-access tunnel",
        description="Stop the managed cloudflared tunnel without deleting the saved pairing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe remote stop --help",
    )
    remote_stop_parser.add_argument("--json", action="store_true", help="Print the raw machine-readable result.")

    screenshot_parser = subparsers.add_parser(
        "screenshot",
        help="Capture a local desktop screenshot",
        description=(
            "Capture the local desktop as a PNG file. This is a CLI primitive; "
            "it does not add IM commands, bot buttons, or agent prompt injection."
        ),
    )
    screenshot_parser.add_argument(
        "-o",
        "--output",
        help="PNG output path. Defaults to ~/.avibe/screenshots/screenshot_<timestamp>.png.",
    )
    screenshot_parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable result with the output path and capture backend.",
    )

    agent_parser = subparsers.add_parser(
        "agent",
        help="Manage Avibe Agents",
        description="Create, inspect, import, update, and run Avibe-owned Agent definitions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe agent --help",
    )
    agent_subparsers = agent_parser.add_subparsers(
        dest="agent_command",
        metavar="{list,show,models,create,update,enable,disable,remove,import,run}",
    )
    agent_subparsers.required = True

    agent_list_parser = agent_subparsers.add_parser("list", help="List Avibe Agents")
    agent_list_parser.add_argument("--brief", action="store_true", help=argparse.SUPPRESS)
    agent_list_parser.add_argument("--backend", choices=("codex", "claude", "opencode"), help="Filter by backend")
    agent_list_parser.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include disabled Agents while keeping pagination",
    )
    agent_list_parser.add_argument("--disabled", action="store_true", help="Show only disabled Agents")
    _add_pagination_args(agent_list_parser, help_command="vibe agent list --help")
    _add_json_noop(agent_list_parser)

    agent_show_parser = agent_subparsers.add_parser("show", help="Show one Avibe Agent")
    agent_show_parser.add_argument("name", help="Agent name")
    _add_json_noop(agent_show_parser)

    agent_default_parser = agent_subparsers.add_parser("default", help="Set the default Avibe Agent")
    agent_default_parser.add_argument("name", help="Agent name")
    _add_json_noop(agent_default_parser)

    agent_models_parser = agent_subparsers.add_parser(
        "models",
        help="List available models and reasoning efforts for an Agent or backend",
        description=(
            "List the models and reasoning-effort levels available to an Agent (by name) "
            "or to a backend directly. Reasoning efforts are nested per model. For OpenCode "
            "this includes custom providers and user-added models; use --provider to filter."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe agent models --help",
    )
    agent_models_parser.add_argument(
        "name", nargs="?", help="Agent name. Omit and pass --backend to query a backend directly."
    )
    agent_models_parser.add_argument(
        "--backend", choices=("codex", "claude", "opencode"), help="Query a backend directly instead of an Agent."
    )
    agent_models_parser.add_argument(
        "--provider", help="Filter to one OpenCode provider id (OpenCode backend only)."
    )
    agent_models_parser.add_argument("--model", help="Only show reasoning efforts for this model id.")
    _add_pagination_args(agent_models_parser, help_command="vibe agent models --help")
    _add_json_noop(agent_models_parser)

    agent_create_parser = agent_subparsers.add_parser("create", help="Create an Avibe Agent")
    agent_create_parser.add_argument("name", help="Globally unique Agent name")
    agent_create_parser.add_argument("--backend", required=True, choices=("codex", "claude", "opencode"))
    agent_create_parser.add_argument("--description")
    agent_create_parser.add_argument("--model")
    agent_create_parser.add_argument("--reasoning-effort")
    agent_create_parser.add_argument("--effort", dest="reasoning_effort", help=argparse.SUPPRESS)
    system_prompt_group = agent_create_parser.add_mutually_exclusive_group()
    system_prompt_group.add_argument("--system-prompt")
    system_prompt_group.add_argument("--system-prompt-file")
    agent_create_parser.add_argument("--metadata", help="JSON object stored with the Agent")
    agent_create_parser.add_argument("--disabled", action="store_true", help="Create the Agent disabled")
    _add_json_noop(agent_create_parser)

    agent_update_parser = agent_subparsers.add_parser("update", help="Update editable Avibe Agent fields")
    agent_update_parser.add_argument("name", help="Agent name. Name and backend are immutable.")
    agent_update_parser.add_argument("--description")
    agent_update_parser.add_argument("--clear-description", action="store_true")
    agent_update_parser.add_argument("--model")
    agent_update_parser.add_argument("--clear-model", action="store_true")
    agent_update_parser.add_argument("--reasoning-effort")
    agent_update_parser.add_argument("--effort", dest="reasoning_effort", help=argparse.SUPPRESS)
    agent_update_parser.add_argument("--clear-reasoning-effort", action="store_true")
    update_prompt_group = agent_update_parser.add_mutually_exclusive_group()
    update_prompt_group.add_argument("--system-prompt")
    update_prompt_group.add_argument("--system-prompt-file")
    update_prompt_group.add_argument("--clear-system-prompt", action="store_true")
    agent_update_parser.add_argument("--metadata", help="Replace metadata with a JSON object")
    enabled_group = agent_update_parser.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", action="store_true", help="Enable this Agent")
    enabled_group.add_argument("--disable", action="store_true", help="Disable this Agent")
    _add_json_noop(agent_update_parser)

    agent_enable_parser = agent_subparsers.add_parser("enable", help="Enable an Avibe Agent")
    agent_enable_parser.add_argument("name", help="Agent name")
    _add_json_noop(agent_enable_parser)

    agent_disable_parser = agent_subparsers.add_parser("disable", help="Disable an Avibe Agent")
    agent_disable_parser.add_argument("name", help="Agent name")
    _add_json_noop(agent_disable_parser)

    agent_remove_parser = agent_subparsers.add_parser("remove", help="Remove an Avibe Agent")
    agent_remove_parser.add_argument("name", help="Agent name")
    _add_json_noop(agent_remove_parser)

    agent_import_parser = agent_subparsers.add_parser("import", help="Import global or file-based Agents")
    import_source_group = agent_import_parser.add_mutually_exclusive_group(required=True)
    import_source_group.add_argument("--file", help="Import one markdown Agent file")
    import_source_group.add_argument("--from", dest="from_source", choices=("claude", "codex", "opencode"))
    agent_import_parser.add_argument("--backend", choices=("codex", "claude", "opencode"), help="Backend for --file imports")
    agent_import_parser.add_argument("--name", help="Import one named global Agent from --from source")
    agent_import_parser.add_argument("--all", action="store_true", help="Import all global Agents from --from source")
    _add_json_noop(agent_import_parser)

    agent_run_parser = agent_subparsers.add_parser(
        "run",
        help="Run an Avibe Agent",
        description="Run an Avibe Agent turn. Runs are async by default; use --sync to wait for the result.",
        epilog=_agent_run_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe agent run --help",
    )
    agent_run_parser.add_argument("--agent", help="Avibe Agent name")
    agent_run_parser.add_argument("--session-id", help="Existing Agent Session ID to continue")
    agent_run_delivery_group = agent_run_parser.add_mutually_exclusive_group()
    agent_run_delivery_group.add_argument(
        "--send-now",
        action="store_true",
        help="Explicitly deliver this Run as P1 to an existing Session (the default behavior)",
    )
    agent_run_delivery_group.add_argument(
        "--queue",
        action="store_true",
        help="Queue this Run behind the active Turn instead of steering it",
    )
    agent_run_parser.add_argument("--fork-session", help="Existing Agent Session ID to fork into a new Session")
    agent_run_parser.add_argument("--fork-self", action="store_true", help="Fork this current Agent Session")
    agent_run_parser.add_argument("--create-session", action="store_true", help="Create a new Avibe Session ID before running")
    agent_run_parser.add_argument("--create-session-per-run", action="store_true", help=argparse.SUPPRESS)
    agent_run_parser.add_argument("--same-scope", action="store_true", help="Place a new or forked Session in the caller/source Session scope")
    agent_run_parser.add_argument("--scope-id", help="Existing scopes.id that should own the new or forked Session")
    agent_visibility_group = agent_run_parser.add_mutually_exclusive_group()
    agent_visibility_group.add_argument(
        "--visibility",
        choices=("foreground", "background"),
        help="Visibility for the new or forked Session (default: background)",
    )
    agent_visibility_group.add_argument(
        "--visible",
        dest="visibility",
        action="store_const",
        const="foreground",
        help="Make the new or forked Session user-facing from the start",
    )
    agent_run_parser.add_argument("--deliver-key", help=argparse.SUPPRESS)
    agent_run_parser.add_argument("--model", help="Model override for the new forked Session")
    agent_run_parser.add_argument("--reasoning-effort", help="Reasoning effort override for the new forked Session")
    agent_run_parser.add_argument(
        "--cwd",
        help=(
            "Working directory for the NEW session. Caller-delegated sessions default to the invocation "
            "directory; explicit scoped sessions use the scope workdir; standalone sessions use their Show workspace. Invalid with --session-id "
            "(an existing session keeps its own working directory)."
        ),
    )
    agent_run_parser.add_argument("--post-to", choices=("thread", "channel"), help=argparse.SUPPRESS)
    agent_run_parser.add_argument("--callback-session-id", help="Caller Session ID to receive the completed async run result")
    agent_run_parser.add_argument(
        "--no-callback",
        action="store_true",
        help="For async runs, intentionally skip automatic callback delivery and inspect the run later.",
    )
    agent_wait_group = agent_run_parser.add_mutually_exclusive_group()
    agent_wait_group.add_argument(
        "--async",
        dest="async_run",
        action="store_true",
        help="Queue the run and return immediately (default; kept for compatibility)",
    )
    agent_wait_group.add_argument("--sync", dest="sync_run", action="store_true", help="Wait for the run result before exiting")
    agent_run_parser.add_argument("--wait-timeout", type=float, help="Maximum seconds the CLI waits for a synchronous run result")
    agent_message_group = agent_run_parser.add_mutually_exclusive_group(required=True)
    agent_message_group.add_argument("--message")
    agent_message_group.add_argument("--message-file")
    agent_message_group.add_argument("--prompt", help=argparse.SUPPRESS)
    agent_message_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    _add_json_noop(agent_run_parser)

    runs_parser = subparsers.add_parser(
        "runs",
        help="Inspect and manage Agent run records",
        description="List, inspect, and request cancellation for Agent run records.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe runs --help",
    )
    runs_subparsers = runs_parser.add_subparsers(dest="runs_command", metavar="{list,show,cancel}")
    runs_subparsers.required = True
    runs_list_parser = runs_subparsers.add_parser("list", help="List Agent runs")
    runs_list_parser.add_argument("--status", help="Filter by run status")
    runs_list_parser.add_argument("--type", help="Filter by run type")
    runs_list_parser.add_argument("--agent", help="Filter by Avibe Agent name")
    runs_list_parser.add_argument("--backend", choices=("codex", "claude", "opencode"), help="Filter by backend")
    runs_list_parser.add_argument("--session-id", help="Filter by Agent Session ID")
    runs_list_parser.add_argument("--current-session", action="store_true", help="Filter to this current Agent Session")
    runs_list_parser.add_argument("--definition-id", help="Filter by task or watch definition ID")
    runs_list_parser.add_argument("--created-after", help="Filter by created_at >= timestamp, or relative value such as 6h or 7d")
    runs_list_parser.add_argument("--created-before", help="Filter by created_at <= timestamp, or relative value such as 6h or 7d")
    runs_list_parser.add_argument("--q", dest="query", help="Search common run text fields")
    runs_list_parser.add_argument("--brief", action="store_true", help=argparse.SUPPRESS)
    _add_pagination_args(runs_list_parser, help_command="vibe runs list --help")
    _add_json_noop(runs_list_parser)
    runs_show_parser = runs_subparsers.add_parser("show", help="Show one Agent run")
    runs_show_parser.add_argument("run_id", nargs="?")
    _add_json_noop(runs_show_parser)
    runs_cancel_parser = runs_subparsers.add_parser("cancel", help="Request best-effort cancellation for one run")
    runs_cancel_parser.add_argument("run_id")
    _add_json_noop(runs_cancel_parser)

    harness_help_language = _configured_cli_language()
    harness_parser = subparsers.add_parser(
        "harness",
        help=i18n_t("harness.cli.help.command", harness_help_language),
        description=i18n_t("harness.cli.help.description", harness_help_language),
        error_help_command="vibe harness --help",
    )
    harness_subparsers = harness_parser.add_subparsers(
        dest="harness_command",
        metavar="{status}",
    )
    harness_subparsers.required = True
    harness_status_parser = harness_subparsers.add_parser(
        "status",
        help=i18n_t("harness.cli.help.status", harness_help_language),
    )
    _add_json_noop(harness_status_parser)

    session_parser = subparsers.add_parser(
        "session",
        help="Inspect, control, and update Agent sessions",
        description=(
            "Manage Avibe Agent sessions. 'list' and 'get' are read-only views; "
            "'send-now' promotes the exact queued head, steering it when active; "
            "'update' changes title, visibility, or scope. Archived sessions are "
            "soft-deleted and never surfaced."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session --help",
        error_hint="Run one of the session subcommands below. Start with: vibe session list",
    )
    session_subparsers = session_parser.add_subparsers(
        dest="session_command",
        metavar="{list,get,queue,send-now,update}",
    )
    session_subparsers.required = True
    session_list_parser = session_subparsers.add_parser(
        "list",
        help="List active sessions, most-recently-active first",
        description=(
            f"List active (non-archived) Agent sessions, {DEFAULT_PAGE_LIMIT} per page, "
            "newest activity first."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session list --help",
    )
    session_list_parser.add_argument(
        "--type",
        help="Filter by platform: avibe (Web/Workbench), slack, discord, telegram, lark, wechat.",
    )
    _add_pagination_args(session_list_parser, help_command="vibe session list --help")
    _add_json_noop(session_list_parser)
    session_get_parser = session_subparsers.add_parser(
        "get",
        help="Show one session's full detail by ID",
        description="Show full detail for one active session. An archived or missing ID is reported as not found.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session get --help",
    )
    session_get_parser.add_argument("session_id", nargs="?", help="Agent Session ID")
    _add_json_noop(session_get_parser)
    session_send_now_parser = session_subparsers.add_parser(
        "send-now",
        help="Promote the exact existing FIFO queue head",
        description=(
            "Promote the exact current P3 queue head without adding a message. "
            "If a native Turn is active, Avibe steers that head into it; if the "
            "Session is idle, Avibe starts that head as a new Turn. A stale head "
            "is refused rather than replaced by the next queued item."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session send-now --help",
    )
    session_send_now_parser.add_argument("session_id", help="Target Agent Session ID")
    _add_json_noop(session_send_now_parser)
    session_queue_parser = session_subparsers.add_parser(
        "queue",
        help="Inspect or remove queued Session messages",
        description=(
            "Inspect a Session's durable FIFO input queue or remove one queued "
            "message by its stable ID. Queue removal never targets transcript "
            "messages or reorders the remaining queue."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session queue --help",
    )
    session_queue_subparsers = session_queue_parser.add_subparsers(
        dest="session_queue_command",
        metavar="{list,remove}",
    )
    session_queue_subparsers.required = True
    session_queue_list_parser = session_queue_subparsers.add_parser(
        "list",
        help="List a Session's queued messages in FIFO order",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session queue list --help",
    )
    session_queue_list_parser.add_argument("session_id", help="Target Agent Session ID")
    _add_pagination_args(
        session_queue_list_parser,
        help_command="vibe session queue list --help",
    )
    _add_json_noop(session_queue_list_parser)
    session_queue_remove_parser = session_queue_subparsers.add_parser(
        "remove",
        help="Remove one queued message by stable ID",
        description=(
            "Remove one message only when it is still queued in the named "
            "Session. A stale or cross-Session ID returns removed=false."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session queue remove --help",
    )
    session_queue_remove_parser.add_argument("session_id", help="Target Agent Session ID")
    session_queue_remove_parser.add_argument("message_id", help="Queued message ID")
    _add_json_noop(session_queue_remove_parser)
    session_update_parser = session_subparsers.add_parser(
        "update",
        help="Update a session's title, visibility, or scope",
        description="Update one active Session. Moving scope never changes its stored working directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session update --help",
    )
    session_update_parser.add_argument("session_id", nargs="?", help="Agent Session ID")
    session_update_parser.add_argument(
        "--title", help="New title. Pass an empty string to clear it (reverts to id-based display)."
    )
    session_visibility_group = session_update_parser.add_mutually_exclusive_group()
    session_visibility_group.add_argument(
        "--visibility",
        choices=("foreground", "background"),
        help="Show the Session in normal chat lists or keep it background-only.",
    )
    session_visibility_group.add_argument(
        "--visible",
        dest="visibility",
        action="store_const",
        const="foreground",
        help="Promote the Session into normal chat lists.",
    )
    session_visibility_group.add_argument(
        "--hidden",
        dest="visibility",
        action="store_const",
        const="background",
        help="Hide the Session from normal chat lists and suppress outward delivery.",
    )
    session_update_parser.add_argument(
        "--scope-id",
        help="Move to an existing scopes.id, or pass 'none' to make the Session standalone.",
    )
    _add_json_noop(session_update_parser)

    vault_parser = subparsers.add_parser(
        "vault",
        help="Store and deliver secrets to agents without exposing values",
        description=(
            "Manage Vault secrets. Values are encrypted at rest and never printed to stdout: "
            "agents refer to them by name, tag, or skill tag. 'vibe vault run' injects static "
            "secrets into a child process environment, so avoid commands that print env vars or "
            "secret-bearing debug output."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault --help",
        error_hint="Run one of the vault subcommands below. Start with: vibe vault list",
    )
    vault_subparsers = vault_parser.add_subparsers(
        dest="vault_command",
        metavar="{list,find,tags,edit,rm,run,fetch,access,sign,await,request,export,inject,key}",
    )
    vault_subparsers.required = True

    vault_list_parser = vault_subparsers.add_parser(
        "list",
        help="List secrets (names + masked metadata; never values)",
        description="List secret names with masked metadata, 20 per page by default. Values are never shown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault list --help",
    )
    vault_list_parser.add_argument("--tag", action="append", metavar="TAG[,TAG2]", help="Only list secrets with all of these tags. Repeatable; comma-separated values allowed.")
    vault_list_parser.add_argument("--q", dest="query_filter", help="Search value-free metadata such as name, description, tags, allowed hosts, or public signing address")
    vault_list_parser.add_argument("--kind", choices=["static", "keypair"], help="Only show this secret kind")
    vault_list_parser.add_argument("--protection", choices=["standard", "protected"], help="Only show this protection tier")
    _add_pagination_args(vault_list_parser, help_command="vibe vault list --help")
    _add_json_noop(vault_list_parser)

    vault_find_parser = vault_subparsers.add_parser(
        "find",
        help="Find requestable secrets and signing keys",
        description="Search value-free Vault capabilities for agents: name, kind, protection tier, tags, fetch policy, access grantability, and per-use signing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault find --help",
    )
    vault_find_parser.add_argument("query", nargs="?", help="Keyword to search across value-free metadata")
    vault_find_parser.add_argument("--q", dest="query_filter", help="Keyword search; use instead of positional query")
    vault_find_parser.add_argument("--tag", action="append", metavar="TAG[,TAG2]", help="Only show secrets with all of these tags. Repeatable; comma-separated values allowed.")
    vault_find_parser.add_argument("--kind", choices=["static", "keypair"], help="Only show this secret kind")
    vault_find_parser.add_argument("--protection", choices=["standard", "protected"], help="Only show this protection tier")
    _add_pagination_args(vault_find_parser, help_command="vibe vault find --help")
    _add_json_noop(vault_find_parser)

    vault_tags_parser = vault_subparsers.add_parser(
        "tags",
        help="List available Vault tags",
        description="List normal tags and skill tags with secret counts, 20 per page by default.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault tags --help",
    )
    vault_tags_parser.add_argument("query", nargs="?", help="Keyword to search tag names")
    vault_tags_parser.add_argument("--q", dest="query_filter", help="Keyword search; use instead of positional query")
    vault_tags_parser.add_argument("--type", choices=["tag", "skill"], help="Only show normal tags or skill tags")
    _add_pagination_args(vault_tags_parser, help_command="vibe vault tags --help")
    _add_json_noop(vault_tags_parser)

    vault_rm_parser = vault_subparsers.add_parser(
        "rm",
        help="Delete a secret",
        description="Delete a secret by name.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault rm --help",
    )
    vault_rm_parser.add_argument("name", help="Secret name to delete")
    _add_json_noop(vault_rm_parser)

    vault_edit_parser = vault_subparsers.add_parser(
        "edit",
        help="Edit value-free secret metadata",
        description=(
            "Edit Vault metadata only: description, tags/skill tags, and brokered-fetch policy. "
            "This command never accepts or changes secret values, key material, kind, protection tier, or name."
        ),
        epilog=(
            "Examples:\n"
            "  vibe vault edit OPENAI_API_KEY --description 'OpenAI production key' --tag prod --skill support\n"
            "  vibe vault edit GITHUB_TOKEN --allow-host api.github.com --fetch-auth bearer\n"
            "  vibe vault edit GITHUB_TOKEN --metadata-json '{\"tags\":[\"prod\"],\"policy\":{\"allowed_hosts\":[\"api.github.com\"]}}'"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault edit --help",
    )
    vault_edit_parser.add_argument("name", help="Secret name to edit")
    vault_edit_parser.add_argument("--description", help="Replace the description")
    vault_edit_parser.add_argument("--clear-description", action="store_true", help="Clear the description")
    vault_edit_parser.add_argument("--tag", action="append", metavar="TAG[,TAG2]", help="Replace normal tags. Repeatable; comma-separated values allowed.")
    vault_edit_parser.add_argument("--skill", action="append", metavar="SKILL[,SKILL2]", help="Replace skill tags using skill:<name>. Repeatable.")
    vault_edit_parser.add_argument("--clear-tags", action="store_true", help="Clear all normal and skill tags")
    vault_edit_parser.add_argument("--allow-host", action="append", metavar="HOST[,HOST2]", help="Replace allowed fetch hosts. Repeatable; comma-separated values allowed.")
    vault_edit_parser.add_argument("--clear-allowed-hosts", action="store_true", help="Clear allowed fetch hosts")
    vault_edit_parser.add_argument("--fetch-auth", choices=["bearer", "header", "query"], help="Set the fetch credential injection mode")
    vault_edit_parser.add_argument("--auth-name", help="Header or query parameter name for --fetch-auth header/query")
    vault_edit_parser.add_argument("--clear-fetch-auth", action="store_true", help="Clear explicit fetch auth policy")
    vault_edit_parser.add_argument("--metadata-json", help="Inline JSON object with description, tags, and/or policy")
    _add_json_noop(vault_edit_parser)

    vault_run_parser = vault_subparsers.add_parser(
        "run",
        help="Run a command with secrets injected into its environment",
        description=(
            "Resolve static secrets and exec a command with them in its environment only. Avibe "
            "does not print values itself, but the command's own stdout/stderr passes through; "
            "avoid commands that print env vars, config, or secret-bearing errors."
        ),
        epilog="Example: vibe vault run --env OPENAI_API_KEY --tag deploy --skill github-release -- python sync.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault run --help",
    )
    vault_run_parser.add_argument(
        "--env",
        action="append",
        metavar="NAME[,N2]|LOCAL=NAME",
        help="Inject secret NAME as env var NAME (LOCAL=NAME to rename; comma-separates several). Repeatable.",
    )
    vault_run_parser.add_argument("--tag", action="append", metavar="TAG", help="Inject all value-deliverable secrets with this tag. Repeatable.")
    vault_run_parser.add_argument("--skill", action="append", metavar="SKILL", help="Sugar for --tag skill:SKILL. Repeatable.")
    _add_vault_approval_wait_args(vault_run_parser)
    vault_run_parser.add_argument("command_argv", nargs=argparse.REMAINDER, help="-- followed by the command to run")
    _add_json_noop(vault_run_parser)

    vault_fetch_parser = vault_subparsers.add_parser(
        "fetch",
        help="Make an authenticated HTTP request without exposing the credential",
        description=(
            "Forward an HTTP request with a vault secret attached at egress (Authorization: Bearer "
            "by default). The agent never sees the credential — only the response body, which is "
            "written to stdout (or --output). The secret must declare --allow-host (domain binding): "
            "a request to any other host is refused before the secret is even decrypted."
        ),
        epilog="Example: vibe vault fetch --auth GITHUB_PAT --method POST --url https://api.github.com/repos/o/r/issues --data-file body.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault fetch --help",
    )
    vault_fetch_parser.add_argument("--auth", required=True, metavar="NAME", help="Secret to attach as the request credential")
    vault_fetch_parser.add_argument("--url", required=True, help="Target URL (host must be in the secret's allowed_hosts)")
    vault_fetch_parser.add_argument("--method", default="GET", help="HTTP method (default GET)")
    vault_fetch_parser.add_argument("--header", action="append", metavar="'Name: value'", help="Extra request header (repeatable)")
    vault_fetch_parser.add_argument("--data", help="Request body (string)")
    vault_fetch_parser.add_argument("--data-file", help="Request body read from this file")
    vault_fetch_parser.add_argument("--output", help="Write the response body to this file instead of stdout")
    _add_vault_approval_wait_args(vault_fetch_parser)
    _add_json_noop(vault_fetch_parser)

    vault_access_parser = vault_subparsers.add_parser(
        "access",
        help="Request approval to use a static secret",
        description=(
            "Create a pending access request for a static secret in the current Agent Session. "
            "For protected secrets, the browser releases only an avault-bound DEK blind box; "
            "then run/fetch/inject deliver the value inside avault."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault access --help",
    )
    vault_access_parser.add_argument("name", help="Static secret name to request")
    vault_access_parser.add_argument("--session-id", help="Agent Session ID. Defaults from AVIBE_SESSION_ID inside an Agent shell.")
    vault_access_parser.add_argument("--skill", help="Skill requesting the secret")
    vault_access_parser.add_argument("--command", dest="operation_command", help="Command or operation shown to the user")
    vault_access_parser.add_argument("--egress", help="Egress description shown to the user")
    vault_access_parser.add_argument(
        "--no-callback",
        action="store_true",
        help="Don't auto-resume this Session when the request resolves (you'll re-check it yourself)",
    )
    _add_json_noop(vault_access_parser)

    vault_sign_parser = vault_subparsers.add_parser(
        "sign",
        help="Sign a digest with a keypair secret (standard signs inline; protected needs approval)",
        description=(
            "Sign a 32-byte digest with a local keypair. Standard keys sign immediately via "
            "avault and return the public signature inline. Protected keys — and standard keys "
            "marked always-ask — instead create a pending per-use request you approve in the "
            "browser; then 'vibe vault await <request-id>' returns the public signature."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault sign --help",
    )
    vault_sign_parser.add_argument("name", help="Keypair secret name")
    vault_sign_parser.add_argument("--digest", required=True, help="32-byte digest as hex")
    vault_sign_parser.add_argument(
        "--scheme",
        default="ecdsa-secp256k1-recoverable",
        choices=["ecdsa-secp256k1-recoverable", "ecdsa-secp256k1-der", "schnorr-secp256k1-bip340"],
        help="Signature scheme",
    )
    vault_sign_parser.add_argument("--session-id", help="Agent Session ID. Defaults from AVIBE_SESSION_ID inside an Agent shell.")
    vault_sign_parser.add_argument("--skill", help="Skill requesting the signature")
    vault_sign_parser.add_argument("--command", dest="operation_command", help="Operation shown to the user")
    vault_sign_parser.add_argument("--egress", help="Egress description shown to the user")
    vault_sign_parser.add_argument(
        "--signing-context-json",
        help="Verifiable signing context JSON required for protected keypairs",
    )
    vault_sign_parser.add_argument(
        "--no-callback",
        action="store_true",
        help="Don't auto-resume this Session when the signature resolves (you'll re-check it yourself)",
    )
    _add_json_noop(vault_sign_parser)

    vault_await_parser = vault_subparsers.add_parser(
        "await",
        help="Read or wait for an access/sign request result",
        description="Return the request status/result. With --wait, poll until approved, denied, expired, or timeout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault await --help",
    )
    vault_await_parser.add_argument("request_id", help="Vault request ID")
    vault_await_parser.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="Poll for a decision up to SECONDS")
    _add_json_noop(vault_await_parser)

    vault_export_parser = vault_subparsers.add_parser(
        "export",
        help="(advanced) Emit 'export NAME=...' lines for eval — prefer 'run'",
        description=(
            "Advanced/not recommended: print 'export NAME=value' lines for "
            "eval \"$(vibe vault export --env A --env B)\". The value transits the caller's shell, "
            "so this is weaker than 'run'; use only when several commands in one shell need the env."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault export --help",
    )
    vault_export_parser.add_argument("--env", action="append", metavar="NAME[,N2]|LOCAL=NAME", help="Secret(s) to export. Repeatable.")
    _add_json_noop(vault_export_parser)

    vault_inject_parser = vault_subparsers.add_parser(
        "inject",
        help="(advanced) Render secrets into a 0600 file — prefer 'run'",
        description=(
            "Advanced/not recommended: render secrets into a file for tools that read config files. "
            "The value lands on disk; prefer 'run' (env-only) where possible."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault inject --help",
    )
    vault_inject_parser.add_argument("--keys", required=True, metavar="A,B", help="Comma-separated secret names")
    vault_inject_parser.add_argument("--out", required=True, metavar="FILE", help="Output file (written 0600)")
    vault_inject_parser.add_argument("--format", default="dotenv", choices=["dotenv", "json", "yaml", "toml"], help="Output format (default dotenv)")
    _add_json_noop(vault_inject_parser)

    vault_key_parser = vault_subparsers.add_parser(
        "key",
        help="Back up / restore the vault machine key (for migration)",
        description=(
            "Export the machine key as a passphrase-wrapped blob, or import it on another "
            "machine. The machine key encrypts standard-tier secrets at rest; back it up if "
            "you move the vault somewhere the state dir doesn't travel with it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault key --help",
        error_hint="Run: vibe vault key export  |  vibe vault key import <file>",
    )
    vault_key_sub = vault_key_parser.add_subparsers(dest="vault_key_command", metavar="{export,import}")
    vault_key_sub.required = True
    vault_key_export_parser = vault_key_sub.add_parser(
        "export",
        help="Export the machine key (passphrase read from stdin)",
        description="Export the machine key wrapped under a passphrase read from stdin. Writes JSON to --out or stdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault key export --help",
    )
    vault_key_export_parser.add_argument("--out", help="Write the export blob here (defaults to stdout); created 0600")
    _add_json_noop(vault_key_export_parser)
    vault_key_import_parser = vault_key_sub.add_parser(
        "import",
        help="Restore the machine key from an export (passphrase from stdin)",
        description="Restore the machine key from an export blob. Refuses to overwrite an existing key without --force.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault key import --help",
    )
    vault_key_import_parser.add_argument("file", help="Export blob file produced by 'vibe vault key export'")
    vault_key_import_parser.add_argument("--force", action="store_true", help="Overwrite an existing machine key")
    _add_json_noop(vault_key_import_parser)

    vault_request_parser = vault_subparsers.add_parser(
        "request",
        help="Ask the user to provide a missing secret",
        description="Record a request for a secret the user hasn't stored yet. With --wait, block until they provide it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe vault request --help",
    )
    vault_request_parser.add_argument("name", help="Secret name being requested")
    vault_request_parser.add_argument("--reason", help="Why the secret is needed (shown to the user)")
    vault_request_parser.add_argument("--spec", help="Read non-secret creation hints from this JSON file, or '-' for stdin")
    vault_request_parser.add_argument("--spec-json", help="Inline JSON object with non-secret creation hints")
    vault_request_parser.add_argument("--wait", type=float, metavar="SECONDS", help="Block until fulfilled, up to SECONDS")
    vault_request_parser.add_argument("--no-wait", action="store_true", help="Return immediately (default)")
    vault_request_parser.add_argument(
        "--no-callback",
        action="store_true",
        help="Don't auto-resume this Session when the secret is provided (you'll re-check it yourself)",
    )
    _add_json_noop(vault_request_parser)

    show_parser = subparsers.add_parser(
        "show",
        help="Create, inspect, and publish session Show Pages",
        description=(
            "Manage the one visual Show Page attached to an Agent Session. "
            "Use it when an agent needs a web page for diagrams, reports, dashboards, or visual explanations."
        ),
        epilog=_show_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show --help",
        error_hint="Run one of the show subcommands below. Start with: vibe show path --session-id <session-id>",
    )
    show_parser.set_defaults(show_help_parser=show_parser)
    show_subparsers = show_parser.add_subparsers(
        dest="show_command",
        metavar="{list,path,status,update,mark,reply,marks,unmark,event,annotate}",
    )
    show_subparsers.required = False

    show_list_parser = show_subparsers.add_parser(
        "list",
        help="List existing Show Pages",
        description="List existing Show Pages across Agent Sessions without creating new pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show list --help",
    )
    show_list_parser.add_argument(
        "--visibility",
        choices=("private", "limited", "public", "offline"),
        help="Filter by Show Page visibility.",
    )
    show_list_parser.add_argument("--session-id", help="Filter by Agent Session ID prefix.")
    show_list_parser.add_argument("--updated-after", help="Filter by updated_at >= timestamp, or relative value such as 6h or 7d.")
    show_list_parser.add_argument("--updated-before", help="Filter by updated_at <= timestamp, or relative value such as 6h or 7d.")
    show_list_parser.add_argument("--q", dest="query", help="Search session ID, share ID, or visibility.")
    _add_pagination_args(show_list_parser, help_command="vibe show list --help")
    show_list_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    _data_help_lang = _configured_cli_language()
    data_parser = subparsers.add_parser(
        "data",
        help=i18n_t("data.helpCommand", _data_help_lang),
        description=i18n_t("data.helpDescription", _data_help_lang),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe data --help",
    )
    data_subparsers = data_parser.add_subparsers(dest="data_command", metavar="{query,retention}")
    data_subparsers.required = True
    data_query_parser = data_subparsers.add_parser(
        "query",
        help="Run one read-only SQL query",
        description="Run one guarded read-only SQL query against the local SQLite state database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe data query --help",
    )
    sql_group = data_query_parser.add_mutually_exclusive_group(required=True)
    sql_group.add_argument("--sql", help="SQL SELECT/WITH statement to run.")
    sql_group.add_argument("--sql-file", help="Read SQL from a UTF-8 file, or '-' for stdin.")
    _add_pagination_args(data_query_parser, help_command="vibe data query --help")
    _add_json_noop(data_query_parser)
    _retention_help_lang = _configured_cli_language()
    data_retention_parser = data_subparsers.add_parser(
        "retention",
        help=i18n_t("data.retention.helpCommand", _retention_help_lang),
        description=i18n_t("data.retention.helpDescription", _retention_help_lang),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe data retention --help",
    )
    data_retention_parser.add_argument(
        "--run",
        action="store_true",
        help=i18n_t("data.retention.helpRun", _retention_help_lang),
    )
    data_retention_parser.add_argument(
        "--days",
        type=int,
        default=None,
        help=i18n_t(
            "data.retention.helpDays",
            _retention_help_lang,
            current=_configured_trace_retention_days(_retention_help_lang),
        ),
    )
    data_retention_parser.add_argument(
        "--compact",
        action="store_true",
        help=i18n_t("data.retention.helpCompact", _retention_help_lang),
    )
    _add_json_noop(data_retention_parser)

    show_path_parser = show_subparsers.add_parser(
        "path",
        help="Create or resolve this session's Show Page directory",
        description="Create or resolve the local workspace for one session Show Page.",
        epilog=_show_path_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show path --help",
        error_hint="Pass --session-id, or run from an Avibe Agent shell where AVIBE_SESSION_ID is injected.",
    )
    show_path_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_path_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_status_parser = show_subparsers.add_parser(
        "status",
        help="Show this session's Show Page state",
        description="Inspect one Show Page without creating it.",
        epilog=_show_status_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show status --help",
        error_hint="Pass --session-id, or run from an Avibe Agent shell where AVIBE_SESSION_ID is injected.",
    )
    show_status_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_status_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_update_parser = show_subparsers.add_parser(
        "update",
        help="Update visibility, set a custom public link, or rotate the share link",
        description=(
            "Switch a Show Page between private, public, and offline states, set a custom "
            "public link suffix, or rotate its public share link."
        ),
        epilog=_show_update_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show update --help",
        error_hint="Pass --visibility private|public|offline, --share-id SLUG, or --rotate-share.",
    )
    show_update_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_update_action = show_update_parser.add_mutually_exclusive_group(required=True)
    show_update_action.add_argument(
        "--visibility",
        choices=("private", "public", "offline"),
        help="Set the active Show Page visibility.",
    )
    show_update_action.add_argument(
        "--share-id",
        metavar="SLUG",
        help=(
            "Set a custom public link suffix (the /p/<SLUG>/ segment): 3-64 chars, "
            "letters/numbers/dash/underscore, must be unique. Allowed only while public; "
            "replaces the previous public URL."
        ),
    )
    show_update_action.add_argument(
        "--rotate-share",
        action="store_true",
        help="Revoke the current public URL and create a new one. Allowed only while public.",
    )
    show_update_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_mark_parser = show_subparsers.add_parser(
        "mark",
        help="Record an assistant mark event for a Show Page",
        description="Add an assistant-authored mark event to the Show Page event stream and session transcript.",
        epilog=_show_mark_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show mark --help",
        error_hint="Pass target and --message or --message-file. Pass --session-id outside an Avibe Agent shell.",
    )
    show_mark_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_mark_parser.add_argument("--scope", default="default", help='Mark scope. Defaults to "default".')
    show_mark_parser.add_argument("target", nargs="?", help="Target mark id or selector.")
    show_mark_parser.add_argument("--target", dest="target_option", help="Alias for the positional target.")
    mark_body_group = show_mark_parser.add_mutually_exclusive_group(required=True)
    mark_body_group.add_argument("--message", "--body", dest="body", help="Assistant mark body text.")
    mark_body_group.add_argument(
        "--message-file",
        "--body-file",
        dest="body_file",
        help="Read assistant mark body from a UTF-8 file, or '-' for stdin.",
    )
    show_mark_parser.add_argument("--anchor-selector", help="Optional DOM selector for the anchored element.")
    show_mark_parser.add_argument("--anchor-text", help="Optional selected or summarized anchor text.")
    show_mark_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_reply_parser = show_subparsers.add_parser(
        "reply",
        help="Reply to a human Show Page annotation",
        description="Create or replace the assistant reply mark paired with one annotation event in this session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show reply --help",
        error_hint="Pass the dispatched annotation event id and --message or --message-file.",
    )
    show_reply_parser.add_argument("show_event_id", help="Human annotation event id from the dispatched message.")
    show_reply_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    reply_message_group = show_reply_parser.add_mutually_exclusive_group(required=True)
    reply_message_group.add_argument("--message", help="Reply body text.")
    reply_message_group.add_argument("--message-file", help="Read reply text from a UTF-8 file, or '-' for stdin.")
    show_reply_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_marks_parser = show_subparsers.add_parser(
        "marks",
        help="List active assistant marks",
        description="List non-resolved assistant reply and note marks for this Agent Session.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show marks --help",
    )
    show_marks_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    _add_pagination_args(show_marks_parser, help_command="vibe show marks --help")
    show_marks_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_unmark_parser = show_subparsers.add_parser(
        "unmark",
        help="Resolve assistant marks",
        description="Resolve one or more active assistant marks by mark id or exact target.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show unmark --help",
    )
    show_unmark_parser.add_argument("identifiers", nargs="+", metavar="ID_OR_TARGET")
    show_unmark_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_unmark_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_event_parser = show_subparsers.add_parser(
        "event",
        help="Record a generic Show Page event",
        description="Record a Show Page annotation, intent, page-update, runtime, or assistant mark event.",
        epilog=_show_event_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show event --help",
        error_hint="Pass either --event-json/--event-json-file or --type with JSON fields. Pass --session-id outside an Avibe Agent shell.",
    )
    show_event_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_event_parser.add_argument("--type", help="Show event type, for example human.annotation.created.")
    event_json_group = show_event_parser.add_mutually_exclusive_group(required=True)
    event_json_group.add_argument("--event-json", help="Inline JSON object, or @path to read JSON from a file.")
    event_json_group.add_argument("--event-json-file", help="Read event JSON from a UTF-8 file, or '-' for stdin.")
    show_event_parser.add_argument(
        "--dispatch",
        action="store_true",
        help="For human intent/annotation events, request an Agent turn after recording the event.",
    )
    show_event_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_annotate_parser = show_subparsers.add_parser(
        "annotate",
        help="Control the Show Page annotation overlay",
        description="Enable, disable, or change the mode of the Show Page annotation overlay.",
        epilog=_show_annotate_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show annotate --help",
        error_hint="Pass --on, --off, or --mode smart|screenshot.",
    )
    show_annotate_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    annotate_toggle = show_annotate_parser.add_mutually_exclusive_group()
    annotate_toggle.add_argument("--on", dest="annotation_on", action="store_true", help="Enable annotation.")
    annotate_toggle.add_argument("--off", dest="annotation_off", action="store_true", help="Disable annotation.")
    show_annotate_parser.add_argument(
        "--mode",
        choices=("smart", "screenshot"),
        help="Set annotation mode; with --on, enable directly in this mode.",
    )
    show_annotate_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    task_parser = subparsers.add_parser(
        "task",
        help="Manage scheduled tasks",
        description="Create, inspect, and control scheduled Agent messages for Avibe.",
        epilog=_task_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task --help",
        error_hint="Run one of the task subcommands below. Use 'vibe task add --help' for task creation details.",
    )
    task_subparsers = task_parser.add_subparsers(
        dest="task_command",
        metavar="{add,update,list,show,pause,resume,run,remove}",
    )
    task_subparsers.required = True

    task_add_parser = task_subparsers.add_parser(
        "add",
        help="Create a scheduled task",
        description="Create a recurring or one-shot scheduled Agent message.",
        epilog=_task_add_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task add --help",
        error_hint="Use --session-id together with exactly one schedule flag and one message input flag. Use --same-scope or --scope-id when the task creates a new Session.",
    )
    task_add_parser.add_argument(
        "--name",
        help="Optional human-friendly task name",
    )
    task_add_parser.add_argument(
        "--session-id",
        help="Agent Session ID to continue when the task runs.",
    )
    task_add_parser.add_argument(
        "--session-key",
        help="Legacy compatibility target; prefer --session-id.",
    )
    task_add_parser.add_argument("--create-session", action="store_true", help="Create one reusable Avibe Session ID for this task")
    task_add_parser.add_argument("--create-session-per-run", action="store_true", help="Create a new Avibe Session ID each time this task runs")
    task_add_parser.add_argument("--same-scope", action="store_true", help="Place a created Session in the caller Session scope")
    task_add_parser.add_argument("--scope-id", help="Existing scopes.id that should own created Sessions")
    task_add_parser.add_argument("--agent", help="Avibe Agent name to use when the task runs")
    task_add_parser.add_argument(
        "--cwd",
        help=(
            "Working directory for Sessions created by this task, and for a command task, "
            "the directory its command runs in. Defaults to the caller's current directory."
        ),
    )
    delivery_group = task_add_parser.add_mutually_exclusive_group()
    delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help=argparse.SUPPRESS,
    )
    delivery_group.add_argument(
        "--deliver-key",
        help=argparse.SUPPRESS,
    )
    schedule_group = task_add_parser.add_mutually_exclusive_group(required=True)
    schedule_group.add_argument("--cron", help="Recurring schedule in 5-field crontab format")
    schedule_group.add_argument("--at", help="One-shot timestamp in ISO 8601 format")
    # Not ``required=True`` any more: a command task carries no message at all, so the
    # "message or command" choice is enforced in ``cmd_task_add`` where both inputs are
    # visible (``missing_task_action``).
    prompt_group = task_add_parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--message", help="Stored user message to send each time the task runs")
    prompt_group.add_argument("--message-file", help="Read stored user message from a UTF-8 text file")
    prompt_group.add_argument("--prompt", help=argparse.SUPPRESS)
    prompt_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    task_add_parser.add_argument("--timezone", help="IANA timezone name used for --cron and naive --at values")
    task_add_parser.add_argument(
        "--shell",
        help="Shell command to run on schedule. Use this or pass a command after '--'.",
    )
    task_add_parser.add_argument(
        "--on-failure",
        choices=["none", "agent"],
        default=None,
        help="What a failed command run does: 'none' records the failure, 'agent' starts an Agent turn to triage it. Default: none",
    )
    task_add_parser.add_argument(
        "--timeout",
        type=float,
        help="Per-run timeout in seconds for command tasks. Use 0 for no timeout. Default: 21600.",
    )
    task_add_parser.add_argument(
        "command_argv",
        nargs=argparse.REMAINDER,
        help="-- followed by the command to run on schedule",
    )
    _add_json_noop(task_add_parser)

    task_update_parser = task_subparsers.add_parser(
        "update",
        help="Update a scheduled task",
        description="Update one stored scheduled task while keeping its task ID.",
        epilog=_task_update_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task update --help",
        error_hint="Pass the task ID plus at least one field to change. Unspecified fields keep their existing values.",
    )
    task_update_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    task_update_parser.add_argument("--name", help="New human-friendly task name")
    task_update_parser.add_argument(
        "--clear-name",
        action="store_true",
        help="Remove the stored custom task name",
    )
    task_update_parser.add_argument("--session-id", help="Replace the stored Agent Session ID")
    task_update_parser.add_argument("--session-key", help="Legacy compatibility target; prefer --session-id")
    task_update_parser.add_argument("--create-session", action="store_true", help="Replace the task with one reusable newly-created Avibe Session ID")
    task_update_parser.add_argument("--create-session-per-run", action="store_true", help="Create a new Avibe Session ID each time this task runs")
    task_update_parser.add_argument("--same-scope", action="store_true", help="Place created Sessions in the caller Session scope")
    task_update_parser.add_argument("--scope-id", help="Existing scopes.id that should own created Sessions")
    task_update_parser.add_argument("--agent", help="Replace the Avibe Agent used by this task")
    task_update_parser.add_argument("--clear-agent", action="store_true", help="Clear the stored Avibe Agent override")
    task_update_parser.add_argument(
        "--cwd",
        help="Set working directory for Sessions created by this task, or for a command task, where its command runs",
    )
    update_delivery_group = task_update_parser.add_mutually_exclusive_group()
    update_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help=argparse.SUPPRESS,
    )
    update_delivery_group.add_argument(
        "--deliver-key",
        help=argparse.SUPPRESS,
    )
    task_update_parser.add_argument(
        "--reset-delivery",
        action="store_true",
        help="Clear any stored delivery override so delivery follows the session target directly",
    )
    task_update_parser.add_argument("--cron", help="Replace the schedule with a recurring 5-field crontab")
    task_update_parser.add_argument("--at", help="Replace the schedule with a one-shot ISO 8601 timestamp")
    task_update_parser.add_argument("--message", help="Replace the stored user message text")
    task_update_parser.add_argument("--message-file", help="Replace the stored user message from a UTF-8 text file")
    task_update_parser.add_argument("--prompt", help=argparse.SUPPRESS)
    task_update_parser.add_argument("--prompt-file", help=argparse.SUPPRESS)
    task_update_parser.add_argument("--timezone", help="Replace the stored IANA timezone name")
    task_update_parser.add_argument(
        "--shell",
        help="Replace the shell command a command task runs. Use this or pass a command after '--'.",
    )
    task_update_parser.add_argument(
        "--on-failure",
        choices=["none", "agent"],
        default=None,
        help=argparse.SUPPRESS,
    )
    task_update_parser.add_argument(
        "--timeout",
        type=float,
        help="Replace the per-run timeout in seconds for a command task. Use 0 for no timeout.",
    )
    # No positional landing slot here: the trailing ``-- <command ...>`` tail is lifted
    # out of argv before argparse runs, exactly as for 'vibe watch update'.
    task_update_parser.set_defaults(command_argv=None)
    _add_json_noop(task_update_parser)

    task_subparsers.add_parser(
        "list",
        help="List scheduled tasks",
        description=(
            f"List stored scheduled tasks, {DEFAULT_PAGE_LIMIT} per page. Successful one-shot tasks are hidden by default; "
            "use --include-finished to page through their history."
        ),
        epilog="Use the returned task IDs with 'vibe task show', 'vibe task update', 'vibe task run', 'vibe task pause', 'vibe task resume', or 'vibe task remove'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task list --help",
    )
    task_list_parser = task_subparsers.choices["list"]
    task_list_parser.add_argument(
        "--include-finished",
        action="store_true",
        help="Include successful one-shot task history while keeping pagination",
    )
    task_list_parser.add_argument(
        "--brief",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    _add_pagination_args(task_list_parser, help_command="vibe task list --help")
    _add_json_noop(task_list_parser)
    _add_hidden_task_alias(task_subparsers, "ls", task_list_parser)

    task_show_parser = task_subparsers.add_parser(
        "show",
        help="Show a scheduled task",
        description="Show one scheduled task by ID.",
        epilog="Find task IDs with: vibe task list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task show --help",
    )
    task_show_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    _add_json_noop(task_show_parser)

    task_pause_parser = task_subparsers.add_parser(
        "pause",
        help="Pause a scheduled task",
        description="Disable one scheduled task without deleting it.",
        epilog="Find task IDs with: vibe task list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task pause --help",
    )
    task_pause_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    _add_json_noop(task_pause_parser)

    task_resume_parser = task_subparsers.add_parser(
        "resume",
        help="Resume a scheduled task",
        description="Re-enable one paused scheduled task.",
        epilog="Find task IDs with: vibe task list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task resume --help",
    )
    task_resume_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    _add_json_noop(task_resume_parser)

    task_run_parser = task_subparsers.add_parser(
        "run",
        help="Run a scheduled task immediately",
        description="Queue one immediate execution of an existing scheduled task.",
        epilog="Find task IDs with: vibe task list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task run --help",
    )
    task_run_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    _add_json_noop(task_run_parser)

    task_rm_parser = task_subparsers.add_parser(
        "remove",
        help="Remove a scheduled task",
        description="Remove one scheduled task from active management while preserving existing run history.",
        epilog="Find task IDs with: vibe task list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task remove --help",
    )
    task_rm_parser.add_argument("task_id", help="Task ID from 'vibe task list'")
    _add_json_noop(task_rm_parser)
    _add_hidden_task_alias(task_subparsers, "rm", task_rm_parser)

    hook_parser = subparsers.add_parser(
        "hook",
        help="Deprecated compatibility one-shot async hooks",
        description="Deprecated compatibility entrypoint. Use 'vibe agent run' for new one-shot asynchronous turns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe hook --help",
        error_hint="Use 'vibe agent run --help' for the current async Agent Run command shape.",
    )
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", metavar="{send}")
    hook_subparsers.required = True
    hook_send_parser = hook_subparsers.add_parser(
        "send",
        help="Deprecated compatibility async send",
        description="Deprecated compatibility entrypoint. Use 'vibe agent run' for new one-shot asynchronous Agent Runs.",
        epilog=_hook_send_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe hook send --help",
        error_hint="Use 'vibe agent run' for new async Agent Runs.",
    )
    hook_send_parser.add_argument(
        "--session-id",
        help="Agent Session ID to continue for this one-shot async turn.",
    )
    hook_send_parser.add_argument(
        "--session-key",
        help="Legacy compatibility target; prefer --session-id.",
    )
    hook_send_parser.add_argument("--agent", help="Avibe Agent name to use for this one-shot async turn")
    hook_delivery_group = hook_send_parser.add_mutually_exclusive_group()
    hook_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help=argparse.SUPPRESS,
    )
    hook_delivery_group.add_argument(
        "--deliver-key",
        help=argparse.SUPPRESS,
    )
    hook_prompt_group = hook_send_parser.add_mutually_exclusive_group(required=True)
    hook_prompt_group.add_argument("--message", help="One-shot async user message to queue immediately")
    hook_prompt_group.add_argument("--message-file", help="Read one-shot async user message from a UTF-8 text file")
    hook_prompt_group.add_argument("--prompt", help=argparse.SUPPRESS)
    hook_prompt_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    _add_json_noop(hook_send_parser)

    watch_parser = subparsers.add_parser(
        "watch",
        help="Manage background watches",
        description="Create, inspect, and control managed background watchers for Avibe.",
        epilog=_watch_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch --help",
        error_hint="Run one of the watch subcommands below. Use 'vibe watch add --help' for watch creation details.",
    )
    watch_subparsers = watch_parser.add_subparsers(
        dest="watch_command",
        metavar="{add,update,list,show,pause,resume,remove}",
    )
    watch_subparsers.required = True

    watch_add_parser = watch_subparsers.add_parser(
        "add",
        help="Create a managed background watch",
        description="Create a managed background watch that runs a waiter command and sends a follow-up on success or terminal failure.",
        epilog=_watch_add_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch add --help",
        error_hint="Use --session-id and either --shell or a command after '--'. Retry exit codes keep either mode waiting; add --forever only when distinct successful events should re-arm the Watch.",
    )
    watch_add_parser.add_argument("--name", help="Optional human-friendly watch name")
    watch_add_parser.add_argument(
        "--session-id",
        help="Agent Session ID to continue for follow-up messages from this watch.",
    )
    watch_add_parser.add_argument(
        "--session-key",
        help="Legacy compatibility target; prefer --session-id.",
    )
    watch_add_parser.add_argument("--create-session", action="store_true", help="Create one reusable Avibe Session ID for this watch")
    watch_add_parser.add_argument("--create-session-per-run", action="store_true", help="Create a new Avibe Session ID each time this watch triggers")
    watch_add_parser.add_argument("--same-scope", action="store_true", help="Place a created Session in the caller Session scope")
    watch_add_parser.add_argument("--scope-id", help="Existing scopes.id that should own created Sessions")
    watch_add_parser.add_argument("--agent", help="Avibe Agent name to use for follow-up messages")
    watch_delivery_group = watch_add_parser.add_mutually_exclusive_group()
    watch_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help=argparse.SUPPRESS,
    )
    watch_delivery_group.add_argument(
        "--deliver-key",
        help=argparse.SUPPRESS,
    )
    watch_add_parser.add_argument(
        "--prefix",
        help="Optional follow-up instruction text prepended before waiter stdout, joined with a blank line when both exist.",
    )
    watch_message_group = watch_add_parser.add_mutually_exclusive_group()
    watch_message_group.add_argument("--message", help="Follow-up user message template sent with waiter output")
    watch_message_group.add_argument("--message-file", help="Read follow-up user message from a UTF-8 text file")
    watch_message_group.add_argument("--prompt", help=argparse.SUPPRESS)
    watch_message_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    watch_add_parser.add_argument("--cwd", help="Working directory for the waiter process")
    watch_add_parser.add_argument(
        "--timeout",
        type=float,
        default=21600,
        help="Per-cycle timeout in seconds. Use 0 for no per-cycle timeout. Default: 21600",
    )
    watch_add_parser.add_argument(
        "--forever",
        action="store_true",
        help="Monitor distinct events continuously. After each event's Agent Run settles, the Watch re-arms; terminal failures still stop it unless their exit code is retryable.",
    )
    watch_add_parser.add_argument(
        "--lifetime-timeout",
        type=float,
        default=0,
        help="Overall Watch lifetime timeout in seconds across retries and re-arms. Use 0 for no lifetime limit.",
    )
    watch_add_parser.add_argument(
        "--retry-exit-code",
        dest="retry_exit_code",
        action="append",
        type=int,
        default=None,
        help=f"Cycle exit code that should keep waiting. Repeat to add more. Default: {DEFAULT_RETRY_EXIT_CODE}",
    )
    watch_add_parser.add_argument(
        "--retry-delay",
        type=float,
        default=30,
        help="Delay in seconds before retrying an allowed cycle result. Default: 30",
    )
    watch_add_parser.add_argument(
        "--shell",
        help="Shell command to run as the waiter. Use this or pass a command after '--'.",
    )
    watch_add_parser.add_argument(
        "waiter_command",
        nargs=argparse.REMAINDER,
        help="Waiter command to run after '--'. Example: vibe watch add ... -- python3 script.py --flag value",
    )
    _add_json_noop(watch_add_parser)

    watch_update_parser = watch_subparsers.add_parser(
        "update",
        help="Update one background watch",
        description="Update stored watch metadata, target, delivery, command, or runtime options.",
        epilog="Find watch IDs with: vibe watch list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch update --help",
        error_hint="Pass at least one field to update, such as --name, --shell, --timeout, --session-id, or --scope-id.",
    )
    watch_update_parser.add_argument("watch_id", help="Watch ID from 'vibe watch list'")
    watch_update_parser.add_argument("--name", help="Set a human-friendly watch name")
    watch_update_parser.add_argument("--clear-name", action="store_true", help="Clear the stored watch name")
    watch_update_parser.add_argument(
        "--session-id",
        help="Agent Session ID to continue for follow-up messages from this watch.",
    )
    watch_update_parser.add_argument(
        "--session-key",
        help="Legacy compatibility target; prefer --session-id.",
    )
    watch_update_parser.add_argument("--create-session", action="store_true", help="Replace the watch with one reusable newly-created Avibe Session ID")
    watch_update_parser.add_argument("--create-session-per-run", action="store_true", help="Create a new Avibe Session ID each time this watch triggers")
    watch_update_parser.add_argument("--same-scope", action="store_true", help="Place created Sessions in the caller Session scope")
    watch_update_parser.add_argument("--scope-id", help="Existing scopes.id that should own created Sessions")
    watch_update_parser.add_argument("--agent", help="Replace the Avibe Agent used for follow-up messages")
    watch_update_parser.add_argument("--clear-agent", action="store_true", help="Clear the stored Avibe Agent override")
    watch_update_delivery_group = watch_update_parser.add_mutually_exclusive_group()
    watch_update_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help=argparse.SUPPRESS,
    )
    watch_update_delivery_group.add_argument(
        "--deliver-key",
        help=argparse.SUPPRESS,
    )
    watch_update_delivery_group.add_argument(
        "--reset-delivery",
        action="store_true",
        help="Clear any stored delivery override and deliver back to the continued session target.",
    )
    watch_update_parser.add_argument(
        "--prefix",
        help="Set follow-up instruction text prepended before waiter stdout.",
    )
    watch_update_parser.add_argument("--clear-prefix", action="store_true", help="Clear the stored follow-up prefix")
    watch_update_message_group = watch_update_parser.add_mutually_exclusive_group()
    watch_update_message_group.add_argument("--message", help="Replace the follow-up user message template")
    watch_update_message_group.add_argument("--message-file", help="Read replacement follow-up user message from a UTF-8 text file")
    watch_update_message_group.add_argument("--prompt", help=argparse.SUPPRESS)
    watch_update_message_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    watch_update_parser.add_argument("--cwd", help="Set working directory for the waiter process")
    watch_update_parser.add_argument("--clear-cwd", action="store_true", help="Clear the stored waiter working directory")
    watch_update_parser.add_argument("--timeout", type=float, help="Set per-cycle timeout in seconds")
    watch_update_mode_group = watch_update_parser.add_mutually_exclusive_group()
    watch_update_mode_group.add_argument("--forever", action="store_true", help="Switch this watch to forever mode")
    watch_update_mode_group.add_argument("--once", action="store_true", help="Switch this watch to one-shot mode")
    watch_update_parser.add_argument(
        "--lifetime-timeout",
        type=float,
        help="Set the overall Watch lifetime timeout across retries and re-arms. Use 0 for no lifetime limit.",
    )
    watch_update_parser.add_argument(
        "--retry-exit-code",
        dest="retry_exit_code",
        action="append",
        type=int,
        default=None,
        help="Replace exit codes that keep this Watch waiting. Repeat to add more.",
    )
    watch_update_parser.add_argument("--retry-delay", type=float, help="Set retry delay in seconds")
    watch_update_parser.add_argument("--shell", help="Replace waiter with a shell command")
    watch_update_parser.set_defaults(waiter_command=None)
    _add_json_noop(watch_update_parser)

    watch_list_parser = watch_subparsers.add_parser(
        "list",
        help="List background watches",
        description=(
            f"List stored managed background watches, {DEFAULT_PAGE_LIMIT} per page. Successful one-shot watches are hidden "
            "by default; use --include-finished to page through their history."
        ),
        epilog="Use the returned watch IDs with 'vibe watch show', 'vibe watch update', 'vibe watch pause', 'vibe watch resume', or 'vibe watch remove'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch list --help",
    )
    watch_list_parser.add_argument(
        "--include-finished",
        action="store_true",
        help="Include successful one-shot watch history while keeping pagination",
    )
    watch_list_parser.add_argument(
        "--brief",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    _add_pagination_args(watch_list_parser, help_command="vibe watch list --help")
    _add_json_noop(watch_list_parser)
    _add_hidden_task_alias(watch_subparsers, "ls", watch_list_parser)

    watch_show_parser = watch_subparsers.add_parser(
        "show",
        help="Show one background watch",
        description="Show one managed background watch by ID.",
        epilog="Find watch IDs with: vibe watch list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch show --help",
    )
    watch_show_parser.add_argument("watch_id", help="Watch ID from 'vibe watch list'")
    _add_json_noop(watch_show_parser)

    watch_pause_parser = watch_subparsers.add_parser(
        "pause",
        help="Pause one background watch",
        description="Disable one managed background watch without deleting it.",
        epilog="Find watch IDs with: vibe watch list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch pause --help",
    )
    watch_pause_parser.add_argument("watch_id", help="Watch ID from 'vibe watch list'")
    _add_json_noop(watch_pause_parser)

    watch_resume_parser = watch_subparsers.add_parser(
        "resume",
        help="Resume one background watch",
        description="Re-enable one paused managed background watch.",
        epilog="Find watch IDs with: vibe watch list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch resume --help",
    )
    watch_resume_parser.add_argument("watch_id", help="Watch ID from 'vibe watch list'")
    _add_json_noop(watch_resume_parser)

    watch_remove_parser = watch_subparsers.add_parser(
        "remove",
        help="Remove one background watch",
        description="Remove one managed background watch from active management while preserving existing run history.",
        epilog="Find watch IDs with: vibe watch list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch remove --help",
    )
    watch_remove_parser.add_argument("watch_id", help="Watch ID from 'vibe watch list'")
    _add_json_noop(watch_remove_parser)
    _add_hidden_task_alias(watch_subparsers, "rm", watch_remove_parser)
    return parser


def _dispatch_restart_supervisor(argv: list[str]) -> int:
    """Hand a spawned restart job's own argv straight to its own parser.

    `__restart-supervisor` is not a command a person types; `schedule_restart`
    builds this argv and `vibe/restart_supervisor.py` parses it back. Declaring
    those flags on the top-level parser as well made two owners for one command,
    with a hand-copied translation between them -- and the top-level one runs
    first, so a flag added only to the supervisor's parser was not merely
    unavailable, it was rejected. That is how the rollback flags shipped dead:
    every unit test called `restart_supervisor.main([...])` directly, and the one
    path that goes through this file was the one path nothing exercised.

    Passing the tail through leaves a single parser for the command, so the two
    can no longer disagree.
    """

    from vibe.restart_supervisor import main as restart_supervisor_main

    return restart_supervisor_main(argv)


def _dispatch_deferred_upgrade_activation(argv: list[str]) -> int:
    """Activate a Windows CLI upgrade after the parent launcher exits."""

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-started-at", type=float)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source-generation")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--prepare-show-runtime", action="store_true")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + DEFERRED_ACTIVATION_TIMEOUT_SECONDS
    while runtime.pid_alive(args.parent_pid):
        if args.parent_started_at is not None:
            observed = runtime.process_create_time(args.parent_pid)
            if observed is not None and observed != args.parent_started_at:
                break
        if time.monotonic() >= deadline:
            print("deferred upgrade activation timed out waiting for the parent launcher", file=sys.stderr)
            return 1
        time.sleep(0.1)

    source_generation = Path(args.source_generation) if args.source_generation else None
    activation = AtomicActivation(
        launcher=Path(args.launcher),
        candidate_launcher=Path(args.candidate),
        source_generation=source_generation,
    )
    activated = False
    try:
        with atomic_upgrade_lock():
            reason = activation_block_reason(activation)
            if reason == "restart_pending":
                discard_atomic_uv_install_generation(activation.candidate_launcher)
                print("deferred upgrade activation found another restart in progress", file=sys.stderr)
                return 1
            if reason == "superseded":
                discard_atomic_uv_install_generation(activation.candidate_launcher)
                print("deferred upgrade activation was superseded by another activation", file=sys.stderr)
                return 1
            activate_upgrade_candidate(activation)
            activated = True
            if args.restart:
                schedule_restart(
                    delay_seconds=0.0,
                    vibe_path=args.launcher,
                    trigger="upgrade",
                    prepare_show_runtime=args.prepare_show_runtime,
                    python_executable=sys.executable,
                )
    except Exception as exc:
        if not activated:
            discard_atomic_uv_install_generation(activation.candidate_launcher)
            print(f"deferred upgrade activation failed: {exc}", file=sys.stderr)
        else:
            print(f"deferred upgrade restart scheduling failed: {exc}", file=sys.stderr)
        return 1

    if not args.restart and args.prepare_show_runtime:
        _prepare_show_runtime_after_install(args.launcher)
    return 0


def _dispatch_installer_activation(argv: list[str]) -> int:
    """Activate a staged one-command install through the shared Python owner."""

    if argv == ["--protocol-version"]:
        print("2")
        return 0
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--source-generation")
    args = parser.parse_args(argv)
    if args.snapshot:
        generation = _launcher_generation(Path(args.launcher), atomic_uv_install_root())
        if generation is not None:
            print(generation)
        return 0
    if not args.candidate:
        parser.error("--candidate is required unless --snapshot is used")
    activation = AtomicActivation(
        launcher=Path(args.launcher),
        candidate_launcher=Path(args.candidate),
        source_generation=Path(args.source_generation) if args.source_generation else None,
    )
    try:
        activate_installer_candidate(activation)
    except Exception as exc:
        discard_atomic_uv_install_generation(activation.candidate_launcher)
        print(f"installer activation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main():
    cache_running_vibe_path()
    argv = sys.argv[1:]
    if argv and argv[0] == "__restart-supervisor":
        sys.exit(_dispatch_restart_supervisor(argv[1:]))
    if argv and argv[0] == "__activate-upgrade":
        sys.exit(_dispatch_deferred_upgrade_activation(argv[1:]))
    if argv and argv[0] == "__activate-install":
        sys.exit(_dispatch_installer_activation(argv[1:]))
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "stop":
        sys.exit(cmd_stop())
    if args.command == "start":
        sys.exit(cmd_start())
    if args.command == "restart":
        sys.exit(_cmd_restart_with_delay(args.delay_seconds))
    if args.command == "status":
        sys.exit(cmd_status())
    if args.command == "memory":
        sys.exit(cmd_memory(args))
    if args.command == "skill":
        sys.exit(cmd_skill(args))
    if args.command == "debug":
        sys.exit(cmd_debug_prompt(args))
    if args.command == "doctor":
        sys.exit(cmd_doctor(args))
    if args.command == "screenshot":
        sys.exit(cmd_screenshot(args))
    if args.command == "show":
        try:
            sys.exit(cmd_show(args))
        except Exception as exc:
            _print_task_error(exc, help_command="vibe show --help")
            sys.exit(1)
    if args.command == "version":
        sys.exit(cmd_version())
    if args.command == "check-update":
        sys.exit(cmd_check_update())
    if args.command == "upgrade":
        sys.exit(cmd_upgrade())
    if args.command == "runtime":
        try:
            sys.exit(cmd_runtime(args))
        except Exception as exc:
            _print_task_error(exc, help_command="vibe runtime --help")
            sys.exit(1)
    if args.command == "remote":
        if args.remote_command is None:
            sys.exit(cmd_remote_setup(args))
        if args.remote_command == "pair":
            sys.exit(cmd_remote_pair(args))
        if args.remote_command == "status":
            sys.exit(cmd_remote_status(args))
        if args.remote_command == "start":
            sys.exit(cmd_remote_start(args))
        if args.remote_command == "stop":
            sys.exit(cmd_remote_stop(args))
        parser.error("remote command is invalid")
    if args.command == "agent":
        if args.agent_command == "list":
            sys.exit(cmd_agent_list(args))
        if args.agent_command == "show":
            sys.exit(cmd_agent_show(args))
        if args.agent_command == "default":
            sys.exit(cmd_agent_default(args))
        if args.agent_command == "models":
            sys.exit(cmd_agent_models(args))
        if args.agent_command == "create":
            sys.exit(cmd_agent_create(args))
        if args.agent_command == "update":
            sys.exit(cmd_agent_update(args))
        if args.agent_command == "enable":
            sys.exit(cmd_agent_set_enabled(args, enabled=True))
        if args.agent_command == "disable":
            sys.exit(cmd_agent_set_enabled(args, enabled=False))
        if args.agent_command == "remove":
            sys.exit(cmd_agent_remove(args))
        if args.agent_command == "import":
            sys.exit(cmd_agent_import(args))
        if args.agent_command == "run":
            sys.exit(cmd_agent_run(args))
        parser.error("agent command is required")
    if args.command == "runs":
        if args.runs_command in {"list", "ls"}:
            sys.exit(cmd_runs_list(args))
        if args.runs_command == "show":
            sys.exit(cmd_runs_show(args))
        if args.runs_command == "cancel":
            sys.exit(cmd_runs_cancel(args))
        parser.error("runs command is required")
    if args.command == "harness":
        if args.harness_command == "status":
            sys.exit(cmd_harness_status(args))
        parser.error("harness command is required")
    if args.command == "session":
        if args.session_command == "list":
            sys.exit(cmd_session_list(args))
        if args.session_command == "get":
            sys.exit(cmd_session_get(args))
        if args.session_command == "queue":
            if args.session_queue_command == "list":
                sys.exit(cmd_session_queue_list(args))
            if args.session_queue_command == "remove":
                sys.exit(cmd_session_queue_remove(args))
            parser.error("session queue command is required")
        if args.session_command == "send-now":
            sys.exit(cmd_session_send_now(args))
        if args.session_command == "update":
            sys.exit(cmd_session_update(args))
        parser.error("session command is required")
    if args.command == "vault":
        if args.vault_command == "list":
            sys.exit(cmd_vault_list(args))
        if args.vault_command == "find":
            sys.exit(cmd_vault_find(args))
        if args.vault_command == "tags":
            sys.exit(cmd_vault_tags(args))
        if args.vault_command == "edit":
            sys.exit(cmd_vault_edit(args))
        if args.vault_command == "rm":
            sys.exit(cmd_vault_rm(args))
        if args.vault_command == "run":
            sys.exit(cmd_vault_run(args))
        if args.vault_command == "fetch":
            sys.exit(cmd_vault_fetch(args))
        if args.vault_command == "access":
            sys.exit(cmd_vault_access(args))
        if args.vault_command == "sign":
            sys.exit(cmd_vault_sign(args))
        if args.vault_command == "await":
            sys.exit(cmd_vault_await(args))
        if args.vault_command == "request":
            sys.exit(cmd_vault_request(args))
        if args.vault_command == "export":
            sys.exit(cmd_vault_export(args))
        if args.vault_command == "inject":
            sys.exit(cmd_vault_inject(args))
        if args.vault_command == "key":
            if args.vault_key_command == "export":
                sys.exit(cmd_vault_key_export(args))
            if args.vault_key_command == "import":
                sys.exit(cmd_vault_key_import(args))
            parser.error("vault key command is required")
        parser.error("vault command is required")
    if args.command == "data":
        if args.data_command == "query":
            sys.exit(cmd_data_query(args))
        if args.data_command == "retention":
            sys.exit(cmd_data_retention(args))
        parser.error("data command is required")
    if args.command == "task":
        if args.task_command == "add":
            sys.exit(cmd_task_add(args))
        if args.task_command == "update":
            sys.exit(cmd_task_update(args))
        if args.task_command in {"list", "ls"}:
            try:
                page_request = _page_request_from_args(args, help_command="vibe task list --help")
            except TaskCliError as exc:
                _print_task_error(exc, help_command="vibe task list --help")
                sys.exit(1)
            sys.exit(
                cmd_task_list(
                    include_finished=getattr(args, "include_finished", False),
                    brief=True,
                    page_request=page_request,
                )
            )
        if args.task_command == "show":
            sys.exit(cmd_task_show(args.task_id))
        if args.task_command == "pause":
            sys.exit(cmd_task_set_enabled(args.task_id, False))
        if args.task_command == "resume":
            sys.exit(cmd_task_set_enabled(args.task_id, True))
        if args.task_command == "run":
            sys.exit(cmd_task_run(args.task_id))
        if args.task_command in {"remove", "rm"}:
            sys.exit(cmd_task_remove(args.task_id))
        parser.error("task command is required")
    if args.command == "hook":
        if args.hook_command == "send":
            sys.exit(cmd_hook_send(args))
        parser.error("hook command is required")
    if args.command == "watch":
        if args.watch_command == "add":
            sys.exit(cmd_watch_add(args))
        if args.watch_command == "update":
            sys.exit(cmd_watch_update(args))
        if args.watch_command in {"list", "ls"}:
            try:
                page_request = _page_request_from_args(args, help_command="vibe watch list --help")
            except TaskCliError as exc:
                _print_task_error(exc, help_command="vibe watch list --help")
                sys.exit(1)
            sys.exit(
                cmd_watch_list(
                    include_finished=getattr(args, "include_finished", False),
                    brief=True,
                    page_request=page_request,
                )
            )
        if args.watch_command == "show":
            sys.exit(cmd_watch_show(args.watch_id))
        if args.watch_command == "pause":
            sys.exit(cmd_watch_set_enabled(args.watch_id, False))
        if args.watch_command == "resume":
            sys.exit(cmd_watch_set_enabled(args.watch_id, True))
        if args.watch_command in {"remove", "rm"}:
            sys.exit(cmd_watch_remove(args.watch_id))
        parser.error("watch command is required")
    sys.exit(cmd_vibe())
