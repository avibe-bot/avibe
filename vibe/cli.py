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
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent
from typing import Mapping, NamedTuple, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone_name
from sqlalchemy import select

from config import SettingsStore, paths
from config.v2_config import V2Config
from core.scheduled_tasks import (
    ScheduledTaskStore,
    TaskExecutionStore,
    parse_scope_id,
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
from core.caller_context import caller_context_from_env
from core.vibe_agents import VibeAgent, VibeAgentStore, iter_global_agent_files, parse_agent_file, validate_agent_backend
from core.watches import (
    DEFAULT_RETRY_EXIT_CODE,
    WATCH_RECONCILE_INTERVAL_SECONDS,
    ManagedWatchStore,
    WatchRuntimeStateStore,
)
from vibe import __version__, api, runtime
from vibe.restart_supervisor import schedule_restart
from vibe.screenshot import ScreenshotError, capture_screenshot
from vibe.upgrade import (
    CURRENT_VIBE_EXECUTABLE_ENV,
    LEGACY_PACKAGE_NAME,
    PACKAGE_NAME,
    build_upgrade_plan,
    cache_running_vibe_path,
    get_latest_version_info,
    get_safe_cwd,
    should_skip_show_runtime_prepare,
)
from storage.db import create_sqlite_engine
from storage.background import compute_next_run_at, normalize_run_status
from storage.models import scope_settings, scopes
from storage.pagination import DEFAULT_PAGE_LIMIT, PageRequest, make_page_request, page_sequence, pagination_payload
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
)
DOCTOR_REPAIR_DRY_RUN_MESSAGES = {
    "home-migration": "Would migrate ~/.vibe_remote to ~/.avibe when safe, or recreate the legacy compatibility symlink.",
    "stale-install-runtime": "Would stop a running legacy vibe-remote service and start the current Avibe service.",
    "duplicate-service-processes": "Would stop extra Avibe service processes outside the service lock.",
    "stale-restart-state": "Would remove stale restart metadata and refresh runtime status.",
}

DEFAULT_VAULT_APPROVAL_WAIT_SECONDS = 9 * 60
WATCH_STARTUP_STABLE_RUNNING_SECONDS = 1.5
WATCH_STARTUP_JITTER_BUFFER_SECONDS = 1.0


class VibeArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        self.error_help_command = kwargs.pop("error_help_command", None)
        self.error_hint = kwargs.pop("error_hint", None)
        super().__init__(*args, **kwargs)

    def parse_args(self, args=None, namespace=None):
        parsed_args = list(sys.argv[1:] if args is None else args)
        watch_update_waiter_command = None
        if self.prog == "vibe" and len(parsed_args) >= 4 and parsed_args[:2] == ["watch", "update"]:
            try:
                separator_index = parsed_args.index("--", 3)
            except ValueError:
                separator_index = -1
            if separator_index >= 0:
                watch_update_waiter_command = ["--", *parsed_args[separator_index + 1 :]]
                parsed_args = [*parsed_args[:separator_index]]

        parsed = super().parse_args(parsed_args, namespace)
        if watch_update_waiter_command is not None:
            setattr(parsed, "waiter_command", watch_update_waiter_command)
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


def _print_task_error(exc: Exception, *, help_command: str | None = None) -> None:
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


def _cli_payload(kind: str, **fields) -> dict:
    return {"schema_version": 1, "ok": True, "kind": kind, **fields}


def _print_cli_payload(kind: str, **fields) -> None:
    print(json.dumps(_cli_payload(kind, **fields), indent=2))


def _add_pagination_args(parser, *, help_command: str) -> None:
    parser.add_argument("--page", type=int, help="Page number to return. Defaults to 1.")
    parser.add_argument("--limit", type=int, help=f"Rows per page. Defaults to {DEFAULT_PAGE_LIMIT}.")
    parser.add_argument("--all", action="store_true", help="Return all matching rows without pagination.")
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


def _page_request_from_args(args, *, help_command: str) -> PageRequest | None:
    try:
        return make_page_request(
            page=getattr(args, "page", None),
            limit=getattr(args, "limit", None),
            all_items=bool(getattr(args, "all", False)),
        )
    except ValueError as exc:
        raise TaskCliError(str(exc), code="invalid_pagination", help_command=help_command) from exc


def _add_optional_arg(parts: list[str], flag: str, value: object) -> None:
    if value is not None and value != "":
        parts.extend([flag, str(value)])


def _next_command(parts: list[str], page_result, *, include_all: bool = False) -> str | None:
    if include_all or page_result.next_page is None:
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

        Examples:
          vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
          vibe task add --create-session --scope-id slack::channel::C123 --cron '*/5 * * * *' --message 'Tell a new joke each time.'
          vibe task add --create-session --scope-id slack::channel::C123 --cron '0 9 * * *' --message 'Post a visible daily summary in this scope.'
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

        Guidance:
          Unspecified fields keep their existing values.
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
          vibe watch list --brief
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
    is_apple_silicon = _is_apple_silicon_host()
    host_arch = "Apple Silicon" if is_apple_silicon else platform.machine() or "unknown"
    python_arch = platform.machine() or "unknown"
    python_status = "warn" if is_apple_silicon and _architecture_token(python_arch) == "x86_64" else "pass"

    python_item = {
        "status": python_status,
        "message": f"Python runtime architecture: {python_arch} ({sys.executable})",
    }
    if python_status == "warn":
        python_item["action"] = "Reinstall Avibe with native arm64 uv/Python"
    items.append(python_item)

    uv_path = shutil.which("uv")
    if uv_path:
        uv_arch_output = _binary_architecture(uv_path)
        uv_arch = _architecture_token(uv_arch_output) or "unknown"
        uv_status = "warn" if is_apple_silicon and uv_arch in {"x86_64", "unknown"} else "pass"
        uv_item = {
            "status": uv_status,
            "message": f"uv architecture: {uv_arch} ({uv_path})",
        }
        if is_apple_silicon and uv_arch == "x86_64":
            uv_item["action"] = "Install native arm64 uv, then reinstall Avibe"
        elif is_apple_silicon and uv_arch == "unknown":
            uv_item["action"] = "Check whether this uv wrapper launches native arm64 uv"
        items.append(uv_item)
    else:
        items.append(
            {
                "status": "warn",
                "message": "uv command not found on PATH",
                "action": "Install uv or add its bin directory to PATH",
            }
        )

    items.append(
        {
            "status": "pass",
            "message": f"Host architecture: {host_arch}",
        }
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
    explicit_home = os.environ.get(paths.AVIBE_HOME_ENV)
    if explicit_home:
        _add_doctor_item(
            items,
            "pass",
            f"AVIBE_HOME is set explicitly: {Path(explicit_home).expanduser()}",
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
            f"Default runtime home is ready to initialize at {avibe_home}",
            code="runtime.home_ready",
        )
        return items

    if avibe_present:
        if legacy_home.is_symlink() and _path_points_to(legacy_home, avibe_home):
            _add_doctor_item(
                items,
                "pass",
                f"Legacy home compatibility symlink is healthy: {legacy_home} -> {avibe_home}",
                code="runtime.legacy_home_link_ok",
            )
        elif not legacy_present:
            _add_doctor_item(
                items,
                "warn",
                f"Legacy home compatibility path is missing: {legacy_home}",
                "Run `vibe doctor repair home-migration` to create the compatibility symlink.",
                code="runtime.legacy_home_link_missing",
                repair_target="home-migration",
                repair_risk="low",
            )
        elif legacy_home.is_symlink():
            _add_doctor_item(
                items,
                "warn",
                f"Legacy home symlink does not point to the active home: {legacy_home}",
                "Run `vibe doctor repair home-migration` to recreate the compatibility symlink.",
                code="runtime.legacy_home_link_wrong",
                repair_target="home-migration",
                repair_risk="low",
            )
        else:
            _add_doctor_item(
                items,
                "fail",
                f"Both {avibe_home} and {legacy_home} are real directories",
                "Back up and merge the two homes manually before running repair.",
                code="runtime.home_conflict",
            )
        return items

    if legacy_home.is_symlink():
        _add_doctor_item(
            items,
            "warn",
            f"Legacy home symlink exists but canonical {avibe_home} is missing",
            "Inspect the symlink target before repair; Avibe will not guess which state to keep.",
            code="runtime.legacy_home_symlink_without_canonical",
        )
        return items

    _add_doctor_item(
        items,
        "warn",
        f"Runtime home still uses the legacy path: {legacy_home}",
        "Run `vibe doctor repair home-migration` to move it to ~/.avibe and keep a back-symlink.",
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
            "Running service process still comes from the legacy vibe-remote installation: "
            f"pids={','.join(map(str, stale_pids))}",
            "Run `vibe doctor repair stale-install-runtime` to stop the stale service and start the current Avibe install.",
            code="runtime.stale_install_process",
            repair_target="stale-install-runtime",
            repair_risk="medium",
        )
    elif owner_pid and current_family:
        _add_doctor_item(items, "pass", f"No legacy install mismatch detected for running service: pid={owner_pid}")
    elif owner_pid:
        _add_doctor_item(items, "pass", f"Current CLI install family is not a uv tool install; skipped install mismatch check: pid={owner_pid}")
    else:
        _add_doctor_item(items, "pass", "No running service install mismatch detected")
    return items


def _restart_status_is_stale(payload: dict, path: Path) -> bool:
    state = payload.get("state")
    if state in {"scheduled", "running"}:
        supervisor_pid = payload.get("supervisor_pid")
        if isinstance(supervisor_pid, int) and runtime.pid_alive(supervisor_pid):
            started_at = payload.get("supervisor_started_at")
            if started_at is not None:
                current_started_at = runtime.process_create_time(supervisor_pid)
                return current_started_at is not None and current_started_at != started_at
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > DOCTOR_RESTART_SEED_GRACE_SECONDS

    if state in {"succeeded", "failed", "error", "cancelled"}:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        return age > DOCTOR_RESTART_RESULT_RETENTION_SECONDS
    return False


def _restart_state_items() -> list[dict]:
    items: list[dict] = []
    restart_path = runtime.get_restart_status_path()
    payload = runtime.read_json(restart_path) or {}
    if not payload:
        _add_doctor_item(items, "pass", "No restart metadata is present", code="runtime.restart_state_absent")
        return items

    if _restart_status_is_stale(payload, restart_path):
        state = payload.get("state") or "unknown"
        _add_doctor_item(
            items,
            "warn",
            f"Stale restart metadata is present: state={state}",
            "Run `vibe doctor repair stale-restart-state` to clear the stale restart marker and refresh status.",
            code="runtime.stale_restart_state",
            repair_target="stale-restart-state",
            repair_risk="low",
        )
    else:
        state = payload.get("state") or "unknown"
        _add_doctor_item(items, "pass", f"Restart metadata is current: state={state}")
    return items


def _service_lifecycle_items(*, detect_extra_processes: bool = True) -> list[dict]:
    items: list[dict] = []
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
        _add_doctor_item(items, "pass", f"Service lock owner: pid={owner_pid}", code="runtime.service_lock_owner")
    elif lock_holder_pid:
        _add_doctor_item(
            items,
            "warn",
            f"Service lock is held by pid={lock_holder_pid}, but the owner could not be verified",
            "Run `vibe status` and inspect service logs before starting another service.",
            code="runtime.unverified_service_lock",
        )
    else:
        _add_doctor_item(items, "pass", "Service lock is free", code="runtime.service_lock_free")

    if owner_pid and recorded_pid != owner_pid:
        _add_doctor_item(
            items,
            "warn",
            f"Service pid file does not match the lock owner: pidfile={recorded_pid or 'missing'} lock_owner={owner_pid}",
            "Run `vibe restart` once the current work is idle so Avibe can rewrite runtime ownership files.",
            code="runtime.service_pidfile_mismatch",
        )
    elif recorded_pid:
        _add_doctor_item(items, "pass", f"Service pid file points to pid={recorded_pid}")
    else:
        _add_doctor_item(items, "pass", "Service pid file is absent")

    if owner_pid and status_pid != owner_pid:
        _add_doctor_item(
            items,
            "warn",
            f"Runtime status service_pid does not match the lock owner: status={status_pid or 'missing'} lock_owner={owner_pid}",
            "Refresh status; if it remains stale, restart Avibe when safe.",
            code="runtime.status_pid_mismatch",
        )
    elif status_pid:
        _add_doctor_item(items, "pass", f"Runtime status service_pid: {status_pid}")
    else:
        _add_doctor_item(items, "pass", "Runtime status service_pid is absent")

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
                f"Extra Avibe service process detected outside the service lock: pids={','.join(map(str, extra_service_pids))}",
                "Run `vibe doctor repair duplicate-service-processes` to stop extra service processes.",
                code="runtime.extra_service_process",
                repair_target="duplicate-service-processes",
                repair_risk="medium",
            )
        elif unverified_service_pids:
            _add_doctor_item(
                items,
                "warn",
                f"Possible extra Avibe service process could not be matched to AVIBE_HOME: pids={','.join(map(str, unverified_service_pids))}",
                "Inspect the process environment before starting another service.",
                code="runtime.unverified_service_process",
            )
        else:
            _add_doctor_item(items, "pass", "No extra Avibe service process detected")
    else:
        _add_doctor_item(
            items,
            "pass",
            "Deep service process scan skipped in fast diagnostics",
            "Run deep diagnostics to check duplicate service processes.",
            code="runtime.deep_service_process_scan_skipped",
        )

    return items


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
    return dedent(
        """\
        A Show Page is one session-scoped visual page that Avibe serves through the Web UI / Avibe Cloud tunnel.
        One Agent Session has exactly one Show Page.

        Commands:
          list     List existing Show Pages across sessions.
          path     Create or resolve the local workspace.
          status   Inspect local path, visibility, active URL, and share state.
          update   Switch visibility, set a custom public link, rotate share links, or take the page offline.
          mark     Add an assistant mark event to the session.
          event    Record a generic annotation-layer event.

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
          vibe show mark --session-id sesk8m4q2p7x --target mark-default-summary --body "Review this summary."
          vibe show event --session-id sesk8m4q2p7x --event-json @./show-event.json --json

        More:
          vibe show list --help
          vibe show path --help
          vibe show status --help
          vibe show update --help
          vibe show mark --help
          vibe show event --help
        """
    )


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
          vibe show mark --session-id sesk8m4q2p7x --target mark-default-summary --body "Review this summary."
          vibe show mark --session-id sesk8m4q2p7x --scope default --target summary --body-file ./comment.txt --json
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
          In forever mode, failures are retried only when the waiter exits with an allowed `--retry-exit-code`.
          Pass either --shell '<command>' or a command after '--'.
          --timeout applies to each cycle. --lifetime-timeout applies only to the whole forever watch lifetime.

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
          Omit --session-id/--fork-self/--fork-session to create a private background Session for --agent.
          Use --same-scope to place a new Session in the caller/source Session's scope.
          Use --scope-id <scopes.id> to place a new Session in a specific existing scope.
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
          vibe agent run --agent release-reviewer --same-scope --message 'Review this project in a visible sibling Session.'

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
    stdout_path = paths.get_runtime_dir() / stdout_name
    stderr_path = paths.get_runtime_dir() / stderr_name
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    process = subprocess.Popen(
        args,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    stdout.close()
    stderr.close()
    pid_path.write_text(str(process.pid), encoding="utf-8")
    return process.pid


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
    mode: str,
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
    if lifetime_timeout_seconds and mode != "forever":
        raise TaskCliError(
            "--lifetime-timeout requires --forever",
            code="invalid_watch_lifetime_timeout",
            hint="Use --lifetime-timeout only on forever watches.",
            help_command=help_command,
    )


def _task_message_preview(message: str, *, max_chars: int = 72) -> str:
    compact = " ".join((message or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _task_display_name(task) -> str:
    return task.name or _task_message_preview(task.prompt)


def _task_state(task) -> str:
    if task.enabled:
        return "active"
    if _is_completed_one_shot(task):
        return "completed"
    return "paused"


def _task_last_status(task) -> str:
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
    if task.schedule_type == "cron":
        return f"cron:{task.cron}" if task.cron else "cron"
    if task.schedule_type == "at":
        return f"at:{task.run_at}" if task.run_at else "at"
    return task.schedule_type


def _task_next_run_sort_key(task):
    next_run_at = _task_next_run_at(task)
    if not next_run_at:
        return (True, datetime.max.replace(tzinfo=timezone.utc))
    try:
        instant = datetime.fromisoformat(next_run_at)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
    except ValueError:
        return (True, datetime.max.replace(tzinfo=timezone.utc))
    return (False, instant)


def _task_payload(task, *, brief: bool = False):
    derived = {
        "display_name": _task_display_name(task),
        "message_preview": _task_message_preview(task.prompt),
        "state": _task_state(task),
        "last_status": _task_last_status(task),
        "next_run_at": _task_next_run_at(task),
        "schedule_summary": _task_schedule_summary(task),
    }
    if brief:
        return {
            "id": task.id,
            "name": task.name,
            "display_name": derived["display_name"],
            "state": derived["state"],
            "last_status": derived["last_status"],
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


def _sort_tasks_for_display(tasks):
    return sorted(
        tasks,
        key=lambda item: (
            *_task_next_run_sort_key(item),
            item.created_at,
            item.id,
        ),
    )


def _task_store() -> ScheduledTaskStore:
    return ScheduledTaskStore()


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
    return task.schedule_type == "at" and not task.enabled and bool(task.last_run_at)


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


def _scope_id_from_session_id(session_id: str, *, help_command: str) -> str:
    resolved = resolve_session_id_target(session_id)
    return resolved.session_key.to_key(include_thread=False)


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
            return _scope_id_from_session_id(source_session_id, help_command="vibe agent run --help")
        caller_session_id = _require_caller_session_id(
            caller_context,
            purpose="--same-scope",
            help_command="vibe agent run --help",
        )
        return _scope_id_from_session_id(caller_session_id, help_command="vibe agent run --help")
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
        return _scope_id_from_session_id(caller_session_id, help_command=help_command)
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
        return resolve_session_id_target(session_id).session_key.to_key(include_thread=False)
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


def _resolve_scope_routing_target(session_key: str) -> _ScopeRoutingTarget:
    if not session_key:
        return _ScopeRoutingTarget(None)
    try:
        parsed = parse_scope_id(session_key)
    except ValueError:
        try:
            parsed = parse_session_key(session_key)
        except ValueError:
            return _ScopeRoutingTarget(None)
    scope_id = make_scope_id(parsed.platform, parsed.scope_type, parsed.scope_id)
    _ensure_cli_sqlite_state()
    engine = create_sqlite_engine(paths.get_sqlite_state_path())
    try:
        with engine.connect() as conn:
            row = conn.execute(
                select(scope_settings.c.agent_name)
                .where(scope_settings.c.scope_id == scope_id)
                .limit(1)
            ).first()
            if row is None:
                return _ScopeRoutingTarget(None)
            agent_name = str(row.agent_name).strip() if row.agent_name else None
            return _ScopeRoutingTarget(agent_name)
    finally:
        engine.dispose()


def _resolve_scope_agent_name(session_key: str) -> Optional[str]:
    return _resolve_scope_routing_target(session_key).agent_name


def _resolve_agent_for_target(
    *,
    agent_name: Optional[str],
    session_id: Optional[str],
    session_key: str,
    help_command: str,
):
    store = _agent_store()
    try:
        requested = store.require_enabled(agent_name) if agent_name else None
        if session_id:
            target = resolve_session_id_target(session_id)
            session_agent = store.require_enabled(target.agent_name) if target.agent_name else None
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
            return session_agent or requested

        if requested is not None:
            return requested

        if session_key:
            scope_target = _resolve_scope_routing_target(session_key)
            if scope_target.agent_name:
                return store.require_enabled(scope_target.agent_name)

        return store.get_default_agent()
    finally:
        store.close()


def _resolve_agent_for_session_reservation(
    *,
    agent_name: Optional[str],
    deliver_key: str,
    help_command: str,
) -> Optional[VibeAgent]:
    resolved_agent_name = agent_name
    scope_target = _ScopeRoutingTarget(None)
    if not resolved_agent_name:
        scope_target = _resolve_scope_routing_target(deliver_key)
        resolved_agent_name = scope_target.agent_name

    store = _agent_store()
    try:
        if resolved_agent_name:
            return store.require_enabled(resolved_agent_name)
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


def _watch_command_preview(watch, *, max_chars: int = 120) -> str:
    preview = watch.shell_command or shlex.join(watch.command)
    preview = preview.strip()
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 1].rstrip() + "…"


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


def _agent_payload(agent, *, brief: bool = False) -> dict:
    payload = agent.to_dict()
    if brief:
        return {
            "id": payload["id"],
            "name": payload["name"],
            "backend": payload["backend"],
            "model": payload["model"],
            "reasoning_effort": payload["reasoning_effort"],
            "enabled": payload["enabled"],
            "source": payload["source"],
            "updated_at": payload["updated_at"],
        }
    return payload


def _run_payload(run: dict, *, brief: bool = False) -> dict:
    normalized = dict(run)
    normalized["status"] = normalize_run_status(normalized.get("status"))
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
            "completed_at": normalized.get("completed_at"),
            "error": normalized.get("error"),
            "callback_session_id": normalized.get("callback_session_id"),
            "callback_status": normalized.get("callback_status"),
            "callback_run_id": normalized.get("callback_run_id"),
        }
    return normalized


def _seconds_since_iso(timestamp: object) -> float | None:
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    try:
        started_at = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())


def _default_watch_startup_timeout_seconds(*, stable_running_seconds: float = WATCH_STARTUP_STABLE_RUNNING_SECONDS) -> float:
    return WATCH_RECONCILE_INTERVAL_SECONDS + stable_running_seconds + WATCH_STARTUP_JITTER_BUFFER_SECONDS


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
        timeout_seconds = _default_watch_startup_timeout_seconds(stable_running_seconds=stable_running_seconds)
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
                details={"watch": _watch_payload(watch, runtime_entry)},
            )
        if watch.mode == "once" and watch.last_finished_at and not watch.last_error and watch.last_exit_code == 0:
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
            details={"watch": _watch_payload(watch, runtime_entry)},
        )
    raise TaskCliError(
        f"watch '{watch_id}' was created but startup was not confirmed within {timeout_seconds:.0f} second(s)",
        code="watch_startup_unconfirmed",
        hint="Confirm that the Avibe service is running, then inspect the watch state before reporting that monitoring is active.",
        example=inspect_command,
        help_command=inspect_command,
        details={"watch": _watch_payload(watch, runtime_entry) if watch is not None else {"id": watch_id}},
    )


def cmd_task_add(args):
    try:
        caller_context = caller_context_from_env()
        session_default_notice = _apply_caller_session_default(
            args,
            caller_context,
            purpose="Task target Session",
        )
        schedule_type = "cron" if args.cron else "at"
        session_policy = _validate_definition_session_policy(
            args,
            schedule_type=schedule_type,
            help_command="vibe task add --help",
            allow_caller_session_default=caller_context is not None,
        )
        scope_key = _resolve_definition_scope_key(args, caller_context=caller_context, help_command="vibe task add --help")
        message = _resolve_prompt_input(
            args,
            help_command="vibe task add --help",
            example_command="vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *'",
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
            help_command="vibe task add --help",
        )
        agent = _resolve_agent_for_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key or scope_key or "",
            help_command="vibe task add --help",
        )
        agent_name = agent.name if agent else None
        if session_policy == "create_once":
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                deliver_key=scope_key or "",
                workdir=cwd,
                help_command="vibe task add --help",
            )
        session_target, delivery_target = _validate_definition_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=getattr(args, "post_to", None),
            deliver_key=getattr(args, "deliver_key", None),
            scope_key=scope_key,
            help_command="vibe task add --help",
        )
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
                metadata=_definition_metadata_with_scope(caller_context, scope_id=scope_key, session_workdir=cwd),
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
                metadata=_definition_metadata_with_scope(caller_context, scope_id=scope_key, session_workdir=cwd),
            )
        warnings = _collect_target_warnings(session_target, delivery_target)
        task_payload = _task_payload(task)
        payload_fields = {
            "definition": task_payload,
            "task": task_payload,
            "warnings": warnings,
        }
        if session_default_notice:
            payload_fields["session_default_notice"] = session_default_notice
        _print_cli_payload(
            "run_definition",
            **payload_fields,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe task add --help")
        return 1


def cmd_task_list(*, include_all: bool = False, brief: bool = False):
    store = _task_store()
    tasks = store.list_tasks()
    if not include_all:
        tasks = [task for task in tasks if not _is_completed_one_shot(task)]
    tasks = _sort_tasks_for_display(tasks)
    _print_cli_payload(
        "run_definitions",
        definitions=[_task_payload(task, brief=brief) for task in tasks],
        tasks=[_task_payload(task, brief=brief) for task in tasks],
    )
    return 0


def cmd_task_show(task_id: str):
    store = _task_store()
    task = store.get_task(task_id)
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
    task_payload = _task_payload(task)
    _print_cli_payload("run_definition", definition=task_payload, task=task_payload)
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
    updated = store.set_enabled(task_id, enabled)
    task_payload = _task_payload(updated)
    _print_cli_payload("run_definition", definition=task_payload, task=task_payload)
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


def cmd_task_update(args):
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

        if getattr(args, "name", None) is not None and getattr(args, "clear_name", False):
            raise TaskCliError(
                "use either --name or --clear-name, not both",
                code="conflicting_name_update",
                hint="Pass a new name with --name, or remove the stored name with --clear-name.",
                help_command="vibe task update --help",
            )
        if getattr(args, "clear_name", False):
            name = None
        elif getattr(args, "name", None) is not None:
            name = _normalize_task_name(args.name)
        else:
            name = task.name

        if getattr(args, "clear_agent", False):
            agent_name = None
        elif getattr(args, "agent", None) is not None:
            agent_name = _validate_agent_name_arg(args.agent)
        else:
            agent_name = task.agent_name

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

        timezone_name = args.timezone or task.timezone
        try:
            timezone = ZoneInfo(timezone_name)
        except Exception as exc:
            raise TaskCliError(
                f"invalid timezone: {timezone_name}",
                code="invalid_timezone",
                hint="Use a valid IANA timezone such as UTC, Asia/Shanghai, or America/Los_Angeles.",
                example="Asia/Shanghai",
                help_command="vibe task update --help",
                details={"timezone": timezone_name},
            ) from exc

        if args.cron and args.at:
            raise TaskCliError(
                "use either --cron or --at when updating the schedule",
                code="conflicting_schedule_inputs",
                hint="Pass only one schedule update flag at a time.",
                help_command="vibe task update --help",
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
                    help_command="vibe task update --help",
                    details={"cron": args.cron},
                ) from exc
            schedule_type = "cron"
            cron = args.cron
            run_at = None
        elif args.at:
            try:
                run_at = _normalize_run_at(args.at, timezone_name)
            except ValueError as exc:
                raise TaskCliError(
                    f"invalid --at timestamp: {args.at}",
                    code="invalid_run_at",
                    hint="Use ISO 8601, for example 2026-03-31T09:00:00+08:00 or 2026-03-31T09:00:00.",
                    example="2026-03-31T09:00:00+08:00",
                    help_command="vibe task update --help",
                    details={"at": args.at, "timezone": timezone_name},
                ) from exc
            schedule_type = "at"
            cron = None
            run_at = run_at
        else:
            schedule_type = task.schedule_type
            cron = task.cron
            run_at = task.run_at

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
            help_command="vibe task update --help",
        )
        if session_policy == "existing":
            cwd = _resolve_definition_session_cwd(
                explicit_cwd=explicit_cwd,
                existing_cwd=None,
                session_policy=session_policy,
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
                existing_cwd=task.cwd,
                session_policy=session_policy,
                scoped_session=_has_modern_scope_target(args) or bool(str(metadata.get("session_scope_id") or "").strip()),
                help_command="vibe task update --help",
            )
        else:
            cwd = task.cwd
        scope_key = requested_scope_key or str(metadata.get("session_scope_id") or "").strip() or _legacy_scope_key_from_target(deliver_key)
        if session_policy in {"create_once", "create_per_run"} and not scope_key:
            raise TaskCliError(
                "--scope-id or --same-scope is required when a stored definition creates sessions",
                code="missing_delivery_target",
                hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
                help_command="vibe task update --help",
            )
        if agent_name is None and session_policy != "existing":
            agent = _resolve_agent_for_target(
                agent_name=None,
                session_id=None,
                session_key=scope_key,
                help_command="vibe task update --help",
            )
            agent_name = agent.name if agent else None
        elif agent_name is not None or session_id or session_key:
            agent = _resolve_agent_for_target(
                agent_name=agent_name,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe task update --help",
            )
            agent_name = agent.name if agent else None
        if session_policy == "create_once" and (
            getattr(args, "create_session", False) or not session_id
        ):
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                deliver_key=scope_key,
                workdir=cwd,
                help_command="vibe task update --help",
            )
            session_key = ""
        if session_policy == "existing":
            metadata.pop("session_workdir", None)
        elif cwd:
            metadata["session_workdir"] = cwd
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
            metadata=metadata,
        )
        warnings = _collect_target_warnings(session_target, delivery_target)
        task_payload = _task_payload(updated)
        _print_cli_payload(
            "run_definition",
            definition=task_payload,
            task=task_payload,
            warnings=warnings,
        )
        return 0
    except Exception as exc:
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
        agent = _resolve_agent_for_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key,
            help_command="vibe hook send --help",
        )
        request = _task_request_store().enqueue_hook_send(
            session_key=session_key,
            session_id=session_id,
            post_to=args.post_to,
            deliver_key=args.deliver_key,
            prompt=message,
            agent_name=agent.name if agent else None,
            run_type="agent_run",
            source_kind="cli",
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
    store = _agent_store()
    backend = getattr(args, "backend", None)
    if getattr(args, "disabled", False):
        include_disabled = True
    else:
        include_disabled = bool(getattr(args, "all", False))
    agents = store.list_agents(include_disabled=include_disabled)
    if backend:
        agents = [agent for agent in agents if agent.backend == backend]
    if getattr(args, "disabled", False):
        agents = [agent for agent in agents if not agent.enabled]
    agents = [_agent_payload(agent, brief=getattr(args, "brief", False)) for agent in agents]
    _print_cli_payload("agents", agents=agents)
    return 0


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
        if model:
            models = [entry for entry in models if entry.get("value") == model]
        _print_cli_payload(
            "agent_models",
            agent=agent.name if agent else None,
            backend=backend,
            current=current,
            default_model=options.get("default_model"),
            providers=options.get("providers"),
            models=models,
            source=options.get("source"),
            live=options.get("live", False),
            notes=options.get("notes"),
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
    except Exception as exc:
        _print_task_error(exc)
        return 1


def cmd_agent_set_enabled(args, *, enabled: bool):
    try:
        agent = _agent_store().set_enabled(args.name, enabled)
        _print_cli_payload("agent", agent=_agent_payload(agent))
        return 0
    except Exception as exc:
        _print_task_error(exc)
        return 1


def cmd_agent_remove(args):
    try:
        store = _agent_store()
        counts = store.reference_counts(args.name)
        if any(counts.values()):
            raise TaskCliError(
                f"agent '{args.name}' is still referenced",
                code="agent_in_use",
                hint="Reassign or remove the referencing scopes, sessions, tasks, or watches before deleting this Agent.",
                details={"agent": args.name, "references": counts},
            )
        try:
            removed = store.remove(args.name)
        except ValueError as exc:
            raise TaskCliError(
                str(exc),
                code="agent_builtin",
                hint="Built-in default Agents are created from enabled Backends and cannot be deleted.",
                details={"agent": args.name},
            ) from exc
        if not removed:
            raise TaskCliError(f"agent '{args.name}' not found", code="agent_not_found", details={"agent": args.name})
        _print_cli_payload("agent", removed_agent=args.name)
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
    if (create_session or create_per_run) and not (deliver_key or scope_id or same_scope):
        raise TaskCliError(
            "--scope-id or --same-scope is required when a stored definition creates sessions",
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
    help_command: str,
) -> None:
    if explicit_cwd is None or create_session:
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
            raise TaskCliError(
                "--scope-id or --same-scope is required when a stored definition creates sessions",
                code="missing_delivery_target",
                hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
                help_command=help_command,
            )
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
    help_command: str,
) -> Optional[str]:
    """Working directory for a session this run RESERVES.

    An explicit ``--cwd`` must exist and always wins for blank session creation.
    Without it, scoped sessions snapshot the scope's default workdir, while
    private/background sessions follow the CLI invocation's cwd.
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
    return os.getcwd()


def _resolve_definition_session_cwd(
    *,
    explicit_cwd: Optional[str],
    existing_cwd: Optional[str],
    session_policy: str,
    scoped_session: bool = False,
    help_command: str,
) -> Optional[str]:
    raw = (explicit_cwd or "").strip()
    if session_policy == "existing":
        if raw:
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
    return os.getcwd()


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
            metadata={"scope_placement": "explicit", **dict(metadata or {})},
        )
    else:
        platform = _primary_platform()
        session_anchor = f"{platform}_private-agent-{uuid4().hex[:12]}"
        session_id = sessions_service.reserve_private_agent_session(
            platform=platform,
            agent_backend=agent.backend,
            session_anchor=session_anchor,
            agent_id=agent.id,
            agent_name=agent.name,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            workdir=workdir,
            metadata=metadata,
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
):
    from core.services.session_fork import SessionForkError, reserve_forked_session

    try:
        return reserve_forked_session(
            source_session_id=source_session_id,
            agent_name=agent_name or None,
            model=model,
            reasoning_effort=reasoning_effort,
            scope_id=scope_key,
            db_path=paths.get_sqlite_state_path(),
        )
    except SessionForkError as exc:
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
    deliver_key: str,
    help_command: str,
    workdir: Optional[str] = None,
) -> str:
    from core.services import sessions as sessions_service

    try:
        target = _parse_validated_scope_id(deliver_key, help_command=help_command)
    except TaskCliError:
        target = _parse_validated_session_key(deliver_key, help_command=help_command)
    agent = _resolve_agent_for_session_reservation(
        agent_name=agent_name,
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
    )
    if not session_id:
        raise TaskCliError(
            "failed to reserve a new Agent Session ID",
            code="session_reservation_failed",
            help_command=help_command,
        )
    return session_id


def cmd_agent_run(args):
    try:
        caller_context = caller_context_from_env()
        run_async = _agent_run_is_async(args)
        message = _resolve_message_input(
            args,
            help_command="vibe agent run --help",
            example_command="vibe agent run --agent default",
        )
        session_policy = _validate_run_session_policy(args, help_command="vibe agent run --help")
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
        scope_key = _resolve_agent_run_scope_key(args, caller_context=caller_context, source_session_id=source_session_id)
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
            scoped_session=bool(scope_key),
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
            )
        elif session_policy == "none":
            session_id = _reserve_cli_session(
                agent=agent,
                scope_key=scope_key,
                workdir=run_cwd,
                metadata=session_metadata,
            )
        elif session_policy == "fork":
            fork_result = _reserve_forked_cli_session(
                source_session_id=source_session_id or "",
                agent_name=agent_name or None,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                scope_key=scope_key,
            )
            session_id = fork_result.session_id
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
            metadata=provenance_metadata or None,
        )
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
                "callback_session_id": callback_session_id,
                "source_kind": source_kind,
                "source_actor": source_actor,
                "parent_run_id": parent_run_id,
            },
        }
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
        page_payload = pagination_payload(result, next_command=_next_command(command, result, include_all=bool(getattr(args, "all", False))))
        message = _pagination_message(page_payload)
        payload = {
            "runs": [_run_payload(run, brief=getattr(args, "brief", False)) for run in result.items],
            "pagination": page_payload,
        }
        if message:
            payload["message"] = message
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
    payload_fields = {"run": _run_payload(run)}
    if run_default_notice:
        payload_fields["run_default_notice"] = run_default_notice
    _print_cli_payload("agent_run", **payload_fields)
    return 0


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


async def _request_live_run_cancel(session_id: str) -> dict:
    from vibe import internal_client

    return await internal_client.cancel_dispatch(session_id)


def _cancel_live_agent_run(store: TaskExecutionStore, run: dict) -> dict:
    session_id = _run_session_id(run)
    from vibe import internal_client

    try:
        controller_result = asyncio.run(_request_live_run_cancel(session_id))
    except internal_client.InternalServerUnavailable as exc:
        return _recorded_only_cancel_result(reason_code="internal_unavailable", detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        return _recorded_only_cancel_result(reason_code="live_cancel_failed", detail=str(exc))

    status_code = controller_result.get("status_code")
    try:
        normalized_status_code = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        normalized_status_code = None
    body = controller_result.get("body") or {}
    if not _live_cancel_was_confirmed(normalized_status_code, body):
        return _recorded_only_cancel_result(
            reason_code=_live_cancel_failure_code(normalized_status_code, body),
            detail={
                "controller_status_code": normalized_status_code,
                "controller_response": body,
            },
        )

    run_terminalized = store.mark_run_canceled(str(run.get("id") or ""))
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
    canceled = store.cancel_run(args.run_id)
    if not canceled:
        _print_task_error(TaskCliError(f"run '{args.run_id}' not found", code="run_not_found", details={"run_id": args.run_id}))
        return 1
    cancel_result = _initial_cancel_result(existing)
    if _should_attempt_live_run_cancel(existing):
        cancel_result = _cancel_live_agent_run(store, existing)
    run = store.get_run(args.run_id)
    _print_cli_payload(
        "agent_run",
        cancel_requested=True,
        cancel_code=cancel_result["code"],
        cancel_result=cancel_result,
        run=_run_payload(run or {"id": args.run_id}),
    )
    return 0


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
        page_payload = pagination_payload(
            result.pagination,
            next_command=_next_command(
                command,
                result.pagination,
                include_all=bool(getattr(args, "all", False)) or omit_next_command,
            ),
        )
        message = _pagination_message(page_payload)
        payload = {
            "columns": result.columns,
            "rows": result.rows,
            "pagination": page_payload,
        }
        if message:
            payload["message"] = message
        _print_cli_payload("data_query", **payload)
        return 0
    except ReadOnlyQueryError as exc:
        _print_task_error(TaskCliError(str(exc), code=exc.code, help_command="vibe data query --help"))
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe data query --help")
        return 1


# ``vibe session`` — Agent-facing session management. ``list`` / ``get`` are
# read-only; ``update`` renames a title only. All three go through the shared
# ``core.services.sessions`` business API (same entry the UI server uses) and
# never surface archived (soft-deleted) sessions.
_SESSION_PAGE_SIZE = 10
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
        page = getattr(args, "page", None)
        page = int(page) if page is not None else 1
        if page < 1:
            raise TaskCliError("page must be >= 1", code="invalid_pagination", help_command="vibe session list --help")
        from core.services import sessions as sessions_service

        engine = _open_session_engine()
        with engine.connect() as conn:
            result = sessions_service.list_sessions_page(
                conn, platform=platform, page=page, limit=_SESSION_PAGE_SIZE
            )
        command = ["vibe", "session", "list"]
        _add_optional_arg(command, "--type", platform)
        next_command = (
            shlex.join([*command, "--page", str(result.next_page)])
            if result.next_page is not None
            else None
        )
        _print_cli_payload(
            "agent_sessions",
            sessions=[_session_row(row, brief=True) for row in result.items],
            pagination=pagination_payload(result, next_command=next_command),
            message=_session_list_hint(),
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


def cmd_session_update(args):
    from core.services import sessions as sessions_service

    try:
        session_id, session_default_notice = _resolve_caller_session_id(
            args,
            purpose="Session",
            help_command="vibe session update --help",
        )
        engine = _open_session_engine()
        with engine.begin() as conn:
            # Validate first so an archived/missing id is a clean not-found rather
            # than silently writing a title onto a soft-deleted row.
            sessions_service.get_active_session(conn, session_id)
            # title_source="agent": this is the agent setting its own session title (vs
            # "user" for a human Web UI edit). Both are deliberate, so neither gets
            # auto-overwritten nor re-nudged — see DELIBERATE_TITLE_SOURCES.
            payload = sessions_service.update_session(
                conn, session_id, title=args.title, title_source="agent"
            )
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
    _post_session_activity_to_live_ui(session_id)
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
    page_payload = pagination_payload(
        result,
        next_command=_next_command(command, result, include_all=bool(getattr(args, "all", False))),
    )
    return result.items, page_payload, _pagination_message(page_payload)


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


def _write_private_file(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` as a 0600 file.

    ``tempfile.mkstemp`` creates the temp file 0600 from the start, so the secret is never
    momentarily world-readable even when ``path`` pre-existed with looser perms (``O_TRUNC``
    would have kept the old mode until a late ``chmod``). ``os.replace`` swaps it in
    atomically — a crash mid-write leaves either the old file or the complete new one, never
    a truncated/partial secret.
    """
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
            # Create 0600 from the start (the blob holds the passphrase-wrapped key) —
            # no window where it's world-readable under a permissive umask.
            _write_private_file(Path(out), json.dumps(blob, indent=2) + "\n")
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
    try:
        caller_context = caller_context_from_env()
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
        agent = _resolve_agent_for_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key or scope_key or "",
            help_command="vibe watch add --help",
        )
        agent_name = agent.name if agent else None
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
                deliver_key=scope_key or "",
                workdir=session_workdir,
                help_command="vibe watch add --help",
            )
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
            mode=mode,
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
        )
        runtime_store = _watch_runtime_store()
        watch, runtime_entry = _wait_for_watch_startup(store, runtime_store, watch.id)
        warnings = _collect_target_warnings(session_target, delivery_target)
        watch_payload = _watch_payload(watch, runtime_entry)
        payload_fields = {
            "definition": watch_payload,
            "watch": watch_payload,
            "warnings": warnings,
        }
        if session_default_notice:
            payload_fields["session_default_notice"] = session_default_notice
        _print_cli_payload(
            "run_definition",
            **payload_fields,
        )
        return 0
    except Exception as exc:
        _print_task_error(exc, help_command="vibe watch add --help")
        return 1


def cmd_watch_list(*, brief: bool = False):
    store = _watch_store()
    runtime_state = _watch_runtime_store().load().get("watches", {})
    watches = store.list_watches()
    watches.sort(key=lambda item: (item.enabled is False, item.created_at, item.id))
    watch_payloads = [_watch_payload(watch, runtime_state.get(watch.id), brief=brief) for watch in watches]
    _print_cli_payload("run_definitions", definitions=watch_payloads, watches=watch_payloads)
    return 0


def cmd_watch_show(watch_id: str):
    store = _watch_store()
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
    runtime_entry = _watch_runtime_store().load().get("watches", {}).get(watch.id)
    watch_payload = _watch_payload(watch, runtime_entry)
    _print_cli_payload("run_definition", definition=watch_payload, watch=watch_payload)
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
    updated = store.set_enabled(watch_id, enabled)
    runtime_entry = _watch_runtime_store().load().get("watches", {}).get(updated.id)
    watch_payload = _watch_payload(updated, runtime_entry)
    _print_cli_payload("run_definition", definition=watch_payload, watch=watch_payload)
    return 0


def cmd_watch_update(args):
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
        if getattr(args, "clear_agent", False):
            agent_name = None
        elif getattr(args, "agent", None) is not None:
            agent_name = _validate_agent_name_arg(args.agent)
        else:
            agent_name = watch.agent_name
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
            mode=mode,
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
        if session_policy in {"create_once", "create_per_run"} and not scope_key:
            raise TaskCliError(
                "--scope-id or --same-scope is required when a stored definition creates sessions",
                code="missing_delivery_target",
                hint="Pass --scope-id <scopes.id>, or run from an Avibe Agent Session and pass --same-scope.",
                help_command="vibe watch update --help",
            )
        if agent_name is None and session_policy != "existing":
            agent = _resolve_agent_for_target(
                agent_name=None,
                session_id=None,
                session_key=scope_key,
                help_command="vibe watch update --help",
            )
            agent_name = agent.name if agent else None
        elif agent_name is not None or session_id or session_key:
            agent = _resolve_agent_for_target(
                agent_name=agent_name,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe watch update --help",
            )
            agent_name = agent.name if agent else None
        if session_policy == "create_once" and (
            getattr(args, "create_session", False) or not session_id
        ):
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                deliver_key=scope_key,
                workdir=session_workdir,
                help_command="vibe watch update --help",
            )
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

        updated = store.update_watch(args.watch_id, **changes)
        runtime_entry = _watch_runtime_store().load().get("watches", {}).get(updated.id)
        warnings = _collect_target_warnings(session_target, delivery_target)
        watch_payload = _watch_payload(updated, runtime_entry)
        _print_cli_payload(
            "run_definition",
            definition=watch_payload,
            watch=watch_payload,
            warnings=warnings,
        )
        return 0
    except Exception as exc:
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

    home_items = _home_migration_items()
    for item in home_items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    groups.append({"name": "Runtime Home", "items": home_items})

    # Configuration Group
    config_items = []
    config_path = paths.get_config_path()

    if config_path.exists():
        config_items.append(
            {
                "status": "pass",
                "message": f"Configuration file found: {config_path}",
            }
        )
        summary["pass"] += 1
    else:
        config_items.append(
            {
                "status": "fail",
                "message": "Configuration file not found",
                "action": "Run 'vibe' to create initial configuration",
            }
        )
        summary["fail"] += 1

    config = None
    try:
        config = V2Config.load(config_path)
        config_items.append(
            {
                "status": "pass",
                "message": "Configuration loaded successfully",
            }
        )
        summary["pass"] += 1
    except Exception as exc:
        config_items.append(
            {
                "status": "fail",
                "message": f"Failed to load configuration: {exc}",
                "action": "Check config.json syntax or delete and reconfigure",
            }
        )
        summary["fail"] += 1

    groups.append({"name": "Configuration", "items": config_items})

    # Slack Group
    slack_items = []
    if config:
        try:
            config.slack.validate()
            slack_items.append(
                {
                    "status": "pass",
                    "message": "Slack token format is valid",
                }
            )
            summary["pass"] += 1

            # Check if tokens are actually set
            if config.slack.bot_token:
                slack_items.append(
                    {
                        "status": "pass",
                        "message": "Bot token is configured",
                    }
                )
                summary["pass"] += 1
            else:
                slack_items.append(
                    {
                        "status": "warn",
                        "message": "Bot token is not configured",
                        "action": "Add your Slack bot token in the setup wizard",
                    }
                )
                summary["warn"] += 1

            if config.slack.app_token:
                slack_items.append(
                    {
                        "status": "pass",
                        "message": "App token is configured (Socket Mode)",
                    }
                )
                summary["pass"] += 1
            else:
                slack_items.append(
                    {
                        "status": "warn",
                        "message": "App token is not configured",
                        "action": "Add your Slack app token for Socket Mode",
                    }
                )
                summary["warn"] += 1

        except Exception as exc:
            slack_items.append(
                {
                    "status": "fail",
                    "message": f"Slack token validation failed: {exc}",
                    "action": "Check your Slack tokens in the setup wizard",
                }
            )
            summary["fail"] += 1
    else:
        slack_items.append(
            {
                "status": "fail",
                "message": "Cannot check Slack: configuration not loaded",
            }
        )
        summary["fail"] += 1

    groups.append({"name": "Slack", "items": slack_items})

    # Agent Backends Group
    agent_items = []
    if config:
        # OpenCode
        if config.agents.opencode.enabled:
            cli_path = config.agents.opencode.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None
            if found_path:
                agent_items.append(
                    {
                        "status": "pass",
                        "message": f"OpenCode CLI found: {found_path}",
                    }
                )
                summary["pass"] += 1
            else:
                agent_items.append(
                    {
                        "status": "warn",
                        "message": f"OpenCode CLI not found: {cli_path}",
                        "action": "Install OpenCode or update CLI path",
                    }
                )
                summary["warn"] += 1
        else:
            agent_items.append(
                {
                    "status": "pass",
                    "message": "OpenCode: disabled",
                }
            )
            summary["pass"] += 1

        # Claude
        if config.agents.claude.enabled:
            cli_path = config.agents.claude.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None

            if found_path:
                agent_items.append(
                    {
                        "status": "pass",
                        "message": f"Claude CLI found: {found_path}",
                    }
                )
                summary["pass"] += 1
            else:
                agent_items.append(
                    {
                        "status": "warn",
                        "message": f"Claude CLI not found: {cli_path}",
                        "action": "Install Claude Code or update CLI path",
                    }
                )
                summary["warn"] += 1
        else:
            agent_items.append(
                {
                    "status": "pass",
                    "message": "Claude: disabled",
                }
            )
            summary["pass"] += 1

        # Codex
        if config.agents.codex.enabled:
            cli_path = config.agents.codex.cli_path
            found_path = api.detect_cli(cli_path).get("path") if cli_path else None
            if found_path:
                agent_items.append(
                    {
                        "status": "pass",
                        "message": f"Codex CLI found: {found_path}",
                    }
                )
                summary["pass"] += 1
            else:
                agent_items.append(
                    {
                        "status": "warn",
                        "message": f"Codex CLI not found: {cli_path}",
                        "action": "Install Codex or update CLI path",
                    }
                )
                summary["warn"] += 1
        else:
            agent_items.append(
                {
                    "status": "pass",
                    "message": "Codex: disabled",
                }
            )
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
        agent_items.append(
            {
                "status": "pass",
                "message": f"Default Agent: {default_agent_name or 'not configured'}",
            }
        )
        summary["pass"] += 1
    else:
        agent_items.append(
            {
                "status": "fail",
                "message": "Cannot check agents: configuration not loaded",
            }
        )
        summary["fail"] += 1

    groups.append({"name": "Agent Backends", "items": agent_items})

    # Runtime Group
    runtime_items = []
    if config:
        cwd = config.runtime.default_cwd
        if cwd and os.path.isdir(cwd):
            runtime_items.append(
                {
                    "status": "pass",
                    "message": f"Working directory: {cwd}",
                }
            )
            summary["pass"] += 1
        else:
            runtime_items.append(
                {
                    "status": "warn",
                    "message": f"Working directory does not exist: {cwd}",
                    "action": "Update default_cwd in settings",
                }
            )
            summary["warn"] += 1

        runtime_items.append(
            {
                "status": "pass",
                "message": f"Log level: {config.runtime.log_level}",
            }
        )
        summary["pass"] += 1

    # Check log file
    log_path = paths.get_logs_dir() / "vibe_remote.log"
    if log_path.exists():
        runtime_items.append(
            {
                "status": "pass",
                "message": f"Log file: {log_path}",
            }
        )
        summary["pass"] += 1
    else:
        runtime_items.append(
            {
                "status": "pass",
                "message": "Log file will be created on first run",
            }
        )
        summary["pass"] += 1

    for item in [
        *_service_lifecycle_items(detect_extra_processes=deep),
        *_service_install_family_items(detect_extra_processes=deep),
        *_restart_state_items(),
        *_runtime_architecture_items(),
    ]:
        runtime_items.append(item)
        status = item.get("status")
        if status in summary:
            summary[status] += 1

    groups.append({"name": "Runtime", "items": runtime_items})

    local_cli_items = _local_cli_installation_items()
    for item in local_cli_items:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
    groups.append({"name": "Local CLI Installation", "items": local_cli_items})

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

    vibe_paths = _path_entries_for_executable("vibe")
    preferred_vibe = (Path.home() / ".local" / "bin" / "vibe").expanduser()
    active_vibe_path: Path | None = None
    if not vibe_paths:
        _add_doctor_item(
            items,
            "warn",
            "No vibe executable found on PATH",
            "Install Avibe with uv tool or add the intended vibe executable to PATH.",
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
                f"PATH resolves vibe to {first_vibe} before {preferred_resolved}",
                "Put ~/.local/bin before system Python bin directories when using the uv tool installation.",
            )
        else:
            _add_doctor_item(items, "pass", f"PATH resolves vibe to {first_vibe}")

    site_packages_dirs = _uv_tool_site_packages_for_vibe(active_vibe_path) if active_vibe_path is not None else []
    if not site_packages_dirs:
        _add_doctor_item(
            items,
            "warn",
            "Active vibe executable is not the uv tool installation",
            "uv tool package-integrity checks are skipped for this executable.",
        )
        return items

    recognized_revisions: set[str] = set()
    for site_packages in site_packages_dirs:
        if _is_uv_tool_editable(site_packages):
            _add_doctor_item(
                items,
                "fail",
                f"uv tool installation is editable: {site_packages}",
                "Reinstall Avibe from a normal wheel. Do not use 'uv tool install --editable .' for the live local CLI.",
            )
        else:
            _add_doctor_item(items, "pass", f"uv tool installation is not editable: {site_packages}")

        alembic_dir = site_packages / "storage" / "alembic"
        versions_dir = alembic_dir / "versions"
        if not alembic_dir.exists() or not versions_dir.exists():
            _add_doctor_item(
                items,
                "fail",
                f"Packaged Alembic scripts are missing under {alembic_dir}",
                "Reinstall from a wheel that includes storage/alembic. Editable uv tool installs can miss this package data.",
            )
            continue

        revisions = _available_alembic_revisions(versions_dir)
        recognized_revisions.update(revisions)
        if revisions:
            _add_doctor_item(items, "pass", f"Packaged Alembic scripts found: {versions_dir}")
        else:
            _add_doctor_item(
                items,
                "fail",
                f"No Alembic revision files found under {versions_dir}",
                "Reinstall from a wheel that includes storage/alembic/versions.",
            )

    sqlite_revision = _current_sqlite_revision()
    if sqlite_revision is None:
        _add_doctor_item(items, "pass", "SQLite schema revision is not initialized yet")
    elif sqlite_revision in recognized_revisions:
        _add_doctor_item(items, "pass", f"SQLite schema revision is recognized by this CLI: {sqlite_revision}")
    else:
        _add_doctor_item(
            items,
            "fail",
            f"SQLite schema revision is newer than or unknown to this CLI: {sqlite_revision}",
            "Install an Avibe wheel built from code that contains this migration revision.",
        )

    return items


def _doctor_repair_result(target: str, status: str, message: str, **details) -> dict:
    payload = {"target": target, "status": status, "message": message}
    payload.update({key: value for key, value in details.items() if value is not None})
    return payload


def _write_refreshed_runtime_status() -> None:
    status = runtime.read_status()
    ui_pid = status.get("ui_pid")
    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    extra_pids = runtime.extra_service_process_pids(owner_pid=owner_pid)
    if owner_pid:
        detail = f"pid={owner_pid}"
        if extra_pids:
            detail = f"{detail}; extra_service_pids={','.join(map(str, extra_pids))}"
        runtime.write_status("running", detail, owner_pid, ui_pid)
    elif extra_pids:
        runtime.write_status("degraded", f"lockless service process detected pid={extra_pids[0]}", extra_pids[0], ui_pid)
    else:
        runtime.write_status("stopped", "process not running", None, ui_pid)


def _start_service_after_repair(target: str, success_message: str, failure_message: str, *, stopped_pids: list[int]) -> dict:
    try:
        new_pid = runtime.start_service()
    except Exception as exc:
        _write_refreshed_runtime_status()
        return _doctor_repair_result(
            target,
            "failed",
            f"{failure_message}: {exc}",
            stopped_pids=stopped_pids,
        )
    runtime.write_status("running", f"pid={new_pid}", new_pid, runtime.read_status().get("ui_pid"))
    return _doctor_repair_result(
        target,
        "repaired",
        success_message,
        stopped_pids=stopped_pids,
        service_pid=new_pid,
    )


def _runtime_home_exists_for_repair() -> bool:
    runtime_home = paths.get_vibe_remote_dir()
    return runtime_home.is_dir()


def _repair_home_migration(*, dry_run: bool = False) -> dict:
    target = "home-migration"
    if os.environ.get(paths.AVIBE_HOME_ENV):
        return _doctor_repair_result(target, "skipped", "AVIBE_HOME is explicit; default home migration does not apply.")

    avibe_home = Path.home() / paths.AVIBE_HOME_DIRNAME
    legacy_home = Path.home() / paths.LEGACY_HOME_DIRNAME
    avibe_present = avibe_home.exists() or avibe_home.is_symlink()
    legacy_present = legacy_home.exists() or legacy_home.is_symlink()

    if not avibe_present and not legacy_present:
        return _doctor_repair_result(target, "skipped", "No runtime home exists yet; nothing needs migration.")

    if avibe_present and legacy_present and not legacy_home.is_symlink():
        return _doctor_repair_result(
            target,
            "failed",
            "Both ~/.avibe and ~/.vibe_remote are real directories; manual merge is required.",
        )

    if not avibe_present and legacy_home.is_symlink():
        return _doctor_repair_result(
            target,
            "failed",
            "Legacy home is a symlink but ~/.avibe is missing; inspect the symlink target manually.",
        )

    if avibe_present and legacy_home.is_symlink() and _path_points_to(legacy_home, avibe_home):
        return _doctor_repair_result(target, "skipped", "Runtime home migration is already healthy.")

    if dry_run:
        if not avibe_present and legacy_present and not legacy_home.is_symlink():
            return _doctor_repair_result(target, "planned", "Would move ~/.vibe_remote to ~/.avibe and create a back-symlink.")
        if avibe_present:
            return _doctor_repair_result(target, "planned", "Would recreate the ~/.vibe_remote compatibility symlink.")
        return _doctor_repair_result(target, "skipped", "No runtime home migration is needed.")

    if avibe_present:
        if legacy_home.is_symlink() or not legacy_present:
            legacy_home.unlink(missing_ok=True)
            try:
                legacy_home.symlink_to(avibe_home, target_is_directory=True)
            except OSError as exc:
                return _doctor_repair_result(target, "failed", f"Failed to create compatibility symlink: {exc}")
            return _doctor_repair_result(target, "repaired", "Created ~/.vibe_remote compatibility symlink.")
        return _doctor_repair_result(target, "skipped", "No runtime home migration is needed.")

    migrated_home = paths.migrate_default_home()
    if not _path_points_to(migrated_home, avibe_home):
        return _doctor_repair_result(target, "failed", f"Default home remains at {migrated_home}; migration did not complete.")
    if not _path_points_to(legacy_home, avibe_home):
        return _doctor_repair_result(
            target,
            "failed",
            "Migrated ~/.vibe_remote to ~/.avibe, but failed to create the compatibility symlink.",
        )
    paths.ensure_data_dirs()
    return _doctor_repair_result(target, "repaired", "Migrated ~/.vibe_remote to ~/.avibe.")


def _repair_stale_restart_state(*, dry_run: bool = False) -> dict:
    target = "stale-restart-state"
    restart_path = runtime.get_restart_status_path()
    payload = runtime.read_json(restart_path) or {}
    if not payload:
        return _doctor_repair_result(target, "skipped", "No restart metadata is present.")
    if not _restart_status_is_stale(payload, restart_path):
        return _doctor_repair_result(target, "skipped", "Restart metadata is still current.")
    if dry_run:
        return _doctor_repair_result(target, "planned", "Would remove stale restart metadata and refresh runtime status.")
    restart_path.unlink(missing_ok=True)
    _write_refreshed_runtime_status()
    return _doctor_repair_result(target, "repaired", "Removed stale restart metadata and refreshed runtime status.")


def _repair_duplicate_service_processes(*, dry_run: bool = False) -> dict:
    target = "duplicate-service-processes"
    if runtime.service_instance_lock_attached_to_process():
        return _doctor_repair_result(target, "failed", "Run this repair from the CLI, not from inside the service process.")
    if not _runtime_home_exists_for_repair():
        return _doctor_repair_result(target, "skipped", "No runtime home exists yet; no service process state needs repair.")

    owner_pid = runtime.resolve_service_owner_pid(include_starting=False)
    extra_pids = runtime.extra_service_process_pids(owner_pid=owner_pid)
    if not extra_pids:
        return _doctor_repair_result(target, "skipped", "No extra Avibe service process was detected.")
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            f"Would stop extra Avibe service process(es): {','.join(map(str, extra_pids))}.",
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
            "Stopped lockless service process(es) and started a clean service.",
            "Stopped lockless service process(es), but failed to start a clean service",
            stopped_pids=stopped,
        )

    _write_refreshed_runtime_status()
    if failed:
        return _doctor_repair_result(
            target,
            "failed",
            "Some extra service processes could not be stopped.",
            stopped_pids=stopped,
            failed_pids=failed,
        )
    return _doctor_repair_result(target, "repaired", "Stopped extra Avibe service process(es).", stopped_pids=stopped)


def _repair_stale_install_runtime(*, dry_run: bool = False) -> dict:
    target = "stale-install-runtime"
    if runtime.service_instance_lock_attached_to_process():
        return _doctor_repair_result(target, "failed", "Run this repair from the CLI, not from inside the service process.")
    if not _runtime_home_exists_for_repair():
        return _doctor_repair_result(target, "skipped", "No runtime home exists yet; no service process state needs repair.")

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
        return _doctor_repair_result(target, "skipped", "No legacy vibe-remote service process was detected.")
    if dry_run:
        return _doctor_repair_result(
            target,
            "planned",
            f"Would stop legacy service process(es) and start current Avibe: {','.join(map(str, stale_pids))}.",
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
            "Some legacy vibe-remote service processes could not be stopped.",
            stopped_pids=stopped,
            failed_pids=failed,
        )

    if current_pids or (owner_pid is not None and owner_pid not in stale_pids):
        _write_refreshed_runtime_status()
        return _doctor_repair_result(
            target,
            "repaired",
            "Stopped legacy vibe-remote service process(es).",
            stopped_pids=stopped,
        )

    return _start_service_after_repair(
        target,
        "Stopped legacy vibe-remote service process and started the current Avibe service.",
        "Stopped legacy vibe-remote service process, but failed to start the current Avibe service",
        stopped_pids=stopped,
    )


def _repair_doctor_targets(targets: list[str], *, dry_run: bool = False) -> dict:
    requested_targets = targets or list(DOCTOR_REPAIR_TARGETS)
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
                    f"Unknown repair target. Known targets: {', '.join(DOCTOR_REPAIR_TARGETS)}.",
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
                    DOCTOR_REPAIR_DRY_RUN_MESSAGES[target],
                )
                for target in requested_targets
            ],
        }

    handlers = {
        "home-migration": _repair_home_migration,
        "stale-install-runtime": _repair_stale_install_runtime,
        "duplicate-service-processes": _repair_duplicate_service_processes,
        "stale-restart-state": _repair_stale_restart_state,
    }
    results = [handlers[target](dry_run=dry_run) for target in requested_targets]
    payload = {
        "ok": not any(result["status"] == "failed" for result in results),
        "kind": "doctor_repair",
        "dry_run": dry_run,
        "results": results,
    }
    if not dry_run and any(result["status"] != "skipped" for result in results):
        payload["doctor"] = _doctor(deep=True)
    return payload


def _confirm_doctor_repair(targets: list[str]) -> bool:
    if not sys.stdin.isatty():
        return False
    target_text = ", ".join(targets or DOCTOR_REPAIR_TARGETS)
    answer = input(f"Repair Avibe doctor target(s): {target_text}? Type 'yes' to continue: ")
    return answer.strip().lower() == "yes"


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

    service_pid = runtime.start_service(wait_for_ready=False)
    bind_host = runtime.effective_ui_bind_host(config)
    ui_pid = runtime.start_ui(bind_host, config.ui.setup_port)
    service_ready = runtime.service_pid_recorded(service_pid)
    if not service_ready:
        runtime.write_status("starting", "waiting for service process", service_pid, ui_pid)
        service_ready = runtime.wait_for_service_pid(
            service_pid,
            timeout=runtime.SERVICE_SLOW_START_TIMEOUT_SECONDS,
        )
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


def _show_page_result(page, *, message: str, previous_payload: dict | None = None, extra: dict | None = None) -> dict:
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
    payload["next_actions"] = _show_page_next_actions(payload)
    return payload


def _show_page_next_actions(payload: dict) -> list[str]:
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
        page_payload = pagination_payload(result, next_command=_next_command(command, result, include_all=bool(getattr(args, "all", False))))
        message = _pagination_message(page_payload)
        payload = {
            "ok": True,
            "count": len(result.items),
            "visibility": getattr(args, "visibility", None),
            "pages": [show_page_payload(page) for page in result.items],
            "pagination": page_payload,
            "url_guidance": avibe_cloud_connect_guidance(),
        }
        if message:
            payload["message"] = message
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

    store = _load_show_page_store()
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show path --help")
        page = store.ensure(session_id)
        page_dir = ensure_show_page_dir(session_id)
        _prewarm_show_page_session_best_effort(session_id)
        payload = _show_page_result(
            page,
            message=f"Show Page workspace is ready at {page_dir}.",
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


def _prewarm_show_page_session_best_effort(session_id: str, *, base_path: str | None = None) -> None:
    if _request_show_page_prewarm_best_effort(session_id, base_path=base_path) is None:
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
            base_path = f"/p/{updated.share_id}/" if updated.visibility == "public" and updated.share_id else None
            _prewarm_show_page_session_best_effort(updated.session_id, base_path=base_path)
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


def _read_cli_text_argument(*, value: str | None, file_path: str | None, field_name: str) -> str:
    if file_path:
        source = sys.stdin.read() if file_path == "-" else Path(file_path).read_text(encoding="utf-8")
        text = source.strip()
    else:
        text = (value or "").strip()
    if not text:
        raise TaskCliError(
            f"{field_name} is required",
            code="invalid_arguments",
            help_command="vibe show mark --help",
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


def _request_show_page_prewarm_best_effort(session_id: str, *, base_path: str | None = None) -> dict | None:
    from core.show_pages import SHOW_CLI_EVENT_TOKEN_HEADER, show_cli_event_token

    targets = _local_show_prewarm_targets(session_id)
    if not targets:
        return None
    payload = {"base_path": base_path} if base_path else {}
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
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, ValueError):
        return None
    return parsed.get("event") if isinstance(parsed, dict) and parsed.get("ok") is True else None


def _post_show_mark_to_live_ui(session_id: str, payload: dict) -> dict | None:
    return _post_show_event_to_live_ui(session_id, payload)


def _post_session_activity_to_live_ui(session_id: str) -> None:
    """Best-effort: ping a running UI so it broadcasts a ``session.activity`` update
    for this session (e.g. after ``vibe session update`` renames it). The CLI writes
    the DB in a separate process from the in-proc SSE broker, so without this the
    rename only shows after a page refresh. Silently no-ops when the UI isn't running
    or is unreachable — it must never affect the CLI command's own result."""
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
    http_request = urllib.request.Request(
        url,
        data=b"{}",
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
        raw = _read_cli_text_argument(value=None, file_path=file_path, field_name="--event-json-file")
    else:
        raw = value or ""
        if raw.startswith("@"):
            raw = _read_cli_text_argument(value=None, file_path=raw[1:], field_name="--event-json")
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


def cmd_show_mark(args):
    from core.show_pages import ShowPageStore
    from core.show_session_events import ShowSessionEventStore

    page_store = ShowPageStore()
    event_store = None
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show mark --help")
        page = page_store.ensure(session_id)
        target = _read_cli_text_argument(value=args.target, file_path=None, field_name="--target")
        body = _read_cli_text_argument(value=args.body, file_path=args.body_file, field_name="--body")
        payload = {
            "type": "assistant.mark.created",
            "mark": {
                "scope": args.scope or "default",
                "target": target,
                "body": body,
            },
        }
        if args.anchor_selector:
            payload["anchor"] = {"selector": args.anchor_selector}
            if args.anchor_text:
                payload["anchor"]["text"] = args.anchor_text
        event = _post_show_mark_to_live_ui(session_id, payload)
        if event is None:
            event_store = ShowSessionEventStore()
            event = event_store.append(session_id, payload)
        result = _show_page_result(
            page,
            message="Assistant mark recorded.",
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
        if event_store is not None:
            event_store.close()


def cmd_show_event(args):
    from core.show_pages import ShowPageStore
    from core.show_session_events import ShowSessionEventStore

    page_store = ShowPageStore()
    event_store = None
    try:
        session_id, session_default_notice = _resolve_show_session_id(args, help_command="vibe show event --help")
        page = page_store.ensure(session_id)
        payload = _read_event_json_argument(args.event_json, args.event_json_file)
        if args.type:
            payload = {**payload, "type": args.type}
        if args.dispatch:
            payload = _with_show_event_dispatch(payload)
        event = _post_show_event_to_live_ui(session_id, payload)
        if event is None:
            if args.dispatch:
                from vibe.ui_server import record_local_show_event

                event = record_local_show_event(session_id, payload, dispatch_sync=True)
            else:
                event_store = ShowSessionEventStore()
                event = event_store.append(session_id, payload)
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
    if args.show_command == "event":
        return cmd_show_event(args)
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
    title = "Avibe Doctor Repair"
    if result.get("dry_run"):
        title = f"{title} (dry run)"
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
            print("Doctor repair was not run. Pass --yes to confirm non-interactively.", file=sys.stderr)
            return 2
        result = _repair_doctor_targets(targets, dry_run=dry_run)
        _print_doctor_repair_result(result)
        return 0 if result.get("ok") else 1

    deep = bool(getattr(args, "doctor_deep", False)) if args is not None else False
    result = _doctor(deep=deep)

    # Terminal-friendly output
    print("\n  Avibe Diagnostics")
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
        f"  \033[32m{summary.get('pass', 0)} passed\033[0m  "
        f"\033[33m{summary.get('warn', 0)} warnings\033[0m  "
        f"\033[31m{summary.get('fail', 0)} failed\033[0m"
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
        print("Attempting upgrade anyway...")
    elif not info["has_update"]:
        print("\033[32mYou are already using the latest version.\033[0m")
        return 0
    else:
        print(f"New version available: {info['latest']}")

    print("\nUpgrading...")

    current_vibe_path = cache_running_vibe_path()
    plan = build_upgrade_plan(vibe_path=current_vibe_path)
    print(f"Using {plan.method}: {' '.join(plan.command)}")
    runtime_was_running = _runtime_process_was_running()

    # Use a stable directory as cwd to avoid issues when running from a
    # directory that uv may delete during upgrade (e.g. inside the uv tool venv).
    safe_cwd = get_safe_cwd()

    try:
        result = subprocess.run(plan.command, capture_output=True, text=True, env=plan.env, cwd=safe_cwd)
        if result.returncode == 0:
            print("\033[32mUpgrade successful!\033[0m")
            if runtime_was_running:
                try:
                    restart = schedule_restart(
                        delay_seconds=0.0,
                        vibe_path=current_vibe_path,
                        trigger="upgrade",
                        prepare_show_runtime=not should_skip_show_runtime_prepare(),
                    )
                except Exception as exc:
                    print("\033[33mUpgrade installed, but restart scheduling failed.\033[0m")
                    print(f"Restart error: {exc}")
                    print("Run `vibe restart` to use the new version.")
                    return 2
                else:
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
    print(f"  Installed: {'yes' if payload.get('installed') else 'no'}")
    if payload.get("install_dir"):
        print(f"  Install dir: {payload.get('install_dir')}")
    if payload.get("reason"):
        print(f"  Reason: {payload.get('reason')}")


def cmd_runtime(args) -> int:
    manager = _show_runtime_manager_from_args(args)
    command = getattr(args, "runtime_command", None)
    if command == "status":
        payload = manager.status()
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            _print_runtime_status(payload)
        return 0
    if command == "prepare":
        offline = True if getattr(args, "offline", False) else None
        payload = manager.prepare(force=getattr(args, "force", False), offline=offline)
        askill = _ensure_askill_during_prepare(offline=bool(offline))
        tmux = _ensure_tmux_during_prepare(offline=bool(offline), force=getattr(args, "force", False))
        avault = _ensure_avault_during_prepare(offline=bool(offline))
        payload["askill"] = askill
        payload["avault"] = avault
        payload["tmux"] = tmux
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            if payload.get("ok"):
                print("Show Runtime ready.")
                status = payload.get("status") or {}
                if status.get("install_dir"):
                    print(f"Install dir: {status['install_dir']}")
            else:
                reason = payload.get("reason") or "unknown"
                print(f"Show Runtime prepare failed: {reason}", file=sys.stderr)
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
            if tmux.get("skipped") or tmux.get("status") == "skipped":
                print(f"tmux: skipped ({tmux.get('reason') or 'skipped'}).")
            elif tmux.get("ok"):
                print("tmux installed." if tmux.get("changed") else "tmux ready.")
            else:
                print(f"tmux not ready: {tmux.get('message') or tmux.get('reason') or 'install failed'}", file=sys.stderr)
        return 1 if getattr(args, "strict", False) and not payload.get("ok") else 0
    if command == "clean":
        payload = manager.clean(keep_previous=getattr(args, "keep_previous", 1))
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            removed = payload.get("removed") or []
            print(f"Removed {len(removed)} Show Runtime cache item(s).")
        return 0
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


def _ensure_askill_during_prepare(offline: bool = False) -> dict:
    """Ensure askill (a required local dependency) alongside the Show Runtime.

    Folded into ``vibe runtime prepare`` so askill auto-installs at exactly the
    same lifecycle points as the Show Page runtime (post install / upgrade),
    with a ``VIBE_INSTALL_SKIP_ASKILL`` escape hatch mirroring the Show Runtime
    one. Skipped under ``--offline`` (the askill installer needs the network).
    Refreshes askill to latest even when a binary already exists — prepare is
    the chokepoint that keeps required local deps current on upgrade. An askill
    hiccup never fails the prepare; the Dependencies page offers a manual retry.
    """
    if offline:
        return {"ok": True, "skipped": True, "reason": "offline"}
    if os.environ.get("VIBE_INSTALL_SKIP_ASKILL", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_ASKILL"}
    try:
        return api.ensure_askill_installed(force=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}


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


def _ensure_avault_during_prepare(offline: bool = False) -> dict:
    """Ensure avault (the Vault custody core) alongside other local deps."""
    if offline:
        return {"ok": True, "skipped": True, "reason": "offline"}
    if os.environ.get("VIBE_INSTALL_SKIP_AVAULT", "").strip().lower() in _TRUTHY_ENV_VALUES:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_AVAULT"}
    try:
        return api.ensure_avault_installed(force=True)
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
    supervisor_parser = subparsers.add_parser("__restart-supervisor", help=argparse.SUPPRESS)
    supervisor_parser.add_argument("--job-id", required=True)
    supervisor_parser.add_argument("--delay-seconds", type=_non_negative_float, default=0)
    supervisor_parser.add_argument("--trigger", default="cli")
    supervisor_parser.add_argument("--scope", default="all", choices=("all", "service"))
    supervisor_parser.add_argument("--vibe-path")
    supervisor_parser.add_argument("--prepare-show-runtime", action="store_true")
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
    runtime_parser = subparsers.add_parser(
        "runtime",
        help="Inspect and prepare the managed Show Runtime",
        description="Inspect, prepare, and clean the global Show Runtime cache used by Show Pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe runtime --help",
    )
    runtime_subparsers = runtime_parser.add_subparsers(dest="runtime_command", metavar="{status,prepare,clean}")
    runtime_subparsers.required = True

    def add_runtime_provider_args(runtime_command_parser):
        runtime_command_parser.add_argument(
            "--source",
            choices=("manifest-cache", "manifest", "archive", "prebuilt", "github", "github-source", "npm"),
            help="Runtime provider override. Defaults to the packaged manifest cache.",
        )
        manifest_group = runtime_command_parser.add_mutually_exclusive_group()
        manifest_group.add_argument("--manifest", help="Read a development manifest from a local path.")
        manifest_group.add_argument("--manifest-url", help="Read a development manifest from a URL.")

    runtime_status_parser = runtime_subparsers.add_parser("status", help="Show managed Show Runtime status")
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

    runtime_clean_parser = runtime_subparsers.add_parser("clean", help="Clean stale Show Runtime cache entries")
    runtime_clean_parser.add_argument("--keep-previous", type=int, default=1, help="Number of previous runtime versions to keep.")
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
    agent_list_parser.add_argument("--brief", action="store_true", help="Show compact Agent rows")
    agent_list_parser.add_argument("--backend", choices=("codex", "claude", "opencode"), help="Filter by backend")
    agent_list_parser.add_argument("--all", action="store_true", help="Include disabled Agents")
    agent_list_parser.add_argument("--disabled", action="store_true", help="Show only disabled Agents")
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
    agent_run_parser.add_argument("--fork-session", help="Existing Agent Session ID to fork into a new Session")
    agent_run_parser.add_argument("--fork-self", action="store_true", help="Fork this current Agent Session")
    agent_run_parser.add_argument("--create-session", action="store_true", help="Create a new Avibe Session ID before running")
    agent_run_parser.add_argument("--create-session-per-run", action="store_true", help=argparse.SUPPRESS)
    agent_run_parser.add_argument("--same-scope", action="store_true", help="Place a new or forked Session in the caller/source Session scope")
    agent_run_parser.add_argument("--scope-id", help="Existing scopes.id that should own the new or forked Session")
    agent_run_parser.add_argument("--deliver-key", help=argparse.SUPPRESS)
    agent_run_parser.add_argument("--model", help="Model override for the new forked Session")
    agent_run_parser.add_argument("--reasoning-effort", help="Reasoning effort override for the new forked Session")
    agent_run_parser.add_argument(
        "--cwd",
        help=(
            "Working directory for the NEW session. Private sessions default to the invocation "
            "directory; scoped sessions default to the scope workdir. Invalid with --session-id "
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
    runs_list_parser.add_argument("--brief", action="store_true", help="Show compact run rows")
    _add_pagination_args(runs_list_parser, help_command="vibe runs list --help")
    _add_json_noop(runs_list_parser)
    runs_show_parser = runs_subparsers.add_parser("show", help="Show one Agent run")
    runs_show_parser.add_argument("run_id", nargs="?")
    _add_json_noop(runs_show_parser)
    runs_cancel_parser = runs_subparsers.add_parser("cancel", help="Request best-effort cancellation for one run")
    runs_cancel_parser.add_argument("run_id")
    _add_json_noop(runs_cancel_parser)

    session_parser = subparsers.add_parser(
        "session",
        help="List, inspect, and rename Agent sessions",
        description=(
            "Manage Avibe Agent sessions. 'list' and 'get' are read-only views; "
            "'update' renames a session's title. Archived sessions are soft-deleted "
            "and never surfaced."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session --help",
        error_hint="Run one of the session subcommands below. Start with: vibe session list",
    )
    session_subparsers = session_parser.add_subparsers(dest="session_command", metavar="{list,get,update}")
    session_subparsers.required = True
    session_list_parser = session_subparsers.add_parser(
        "list",
        help="List active sessions, most-recently-active first",
        description="List active (non-archived) Agent sessions, 10 per page, newest activity first.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session list --help",
    )
    session_list_parser.add_argument(
        "--type",
        help="Filter by platform: avibe (Web/Workbench), slack, discord, telegram, lark, wechat.",
    )
    session_list_parser.add_argument("--page", type=int, help="Page number to return (10 per page). Defaults to 1.")
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
    session_update_parser = session_subparsers.add_parser(
        "update",
        help="Update a session's title (title only)",
        description="Update only the title of one active session. No other field can be changed here.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session update --help",
    )
    session_update_parser.add_argument("session_id", nargs="?", help="Agent Session ID")
    session_update_parser.add_argument(
        "--title", required=True, help="New title. Pass an empty string to clear it (reverts to id-based display)."
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
    show_subparsers = show_parser.add_subparsers(dest="show_command", metavar="{list,path,status,update,mark,event}")
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
        choices=("private", "public", "offline"),
        help="Filter by Show Page visibility.",
    )
    show_list_parser.add_argument("--session-id", help="Filter by Agent Session ID prefix.")
    show_list_parser.add_argument("--updated-after", help="Filter by updated_at >= timestamp, or relative value such as 6h or 7d.")
    show_list_parser.add_argument("--updated-before", help="Filter by updated_at <= timestamp, or relative value such as 6h or 7d.")
    show_list_parser.add_argument("--q", dest="query", help="Search session ID, share ID, or visibility.")
    _add_pagination_args(show_list_parser, help_command="vibe show list --help")
    show_list_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    data_parser = subparsers.add_parser(
        "data",
        help="Run read-only queries against Avibe data",
        description="Inspect local Avibe SQLite state with guarded read-only SQL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe data --help",
    )
    data_subparsers = data_parser.add_subparsers(dest="data_command", metavar="{query}")
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
        error_hint="Pass --target and --body or --body-file. Pass --session-id outside an Avibe Agent shell.",
    )
    show_mark_parser.add_argument("--session-id", help="Agent Session ID for the Show Page.")
    show_mark_parser.add_argument("--scope", default="default", help='Mark scope. Defaults to "default".')
    show_mark_parser.add_argument("--target", required=True, help="Target mark id or selector.")
    mark_body_group = show_mark_parser.add_mutually_exclusive_group(required=True)
    mark_body_group.add_argument("--body", help="Assistant mark body text.")
    mark_body_group.add_argument("--body-file", help="Read assistant mark body from a UTF-8 file, or '-' for stdin.")
    show_mark_parser.add_argument("--anchor-selector", help="Optional DOM selector for the anchored element.")
    show_mark_parser.add_argument("--anchor-text", help="Optional selected or summarized anchor text.")
    show_mark_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

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
    task_add_parser.add_argument("--cwd", help="Working directory for Sessions created by this task. Defaults to the caller's current directory.")
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
    prompt_group = task_add_parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--message", help="Stored user message to send each time the task runs")
    prompt_group.add_argument("--message-file", help="Read stored user message from a UTF-8 text file")
    prompt_group.add_argument("--prompt", help=argparse.SUPPRESS)
    prompt_group.add_argument("--prompt-file", help=argparse.SUPPRESS)
    task_add_parser.add_argument("--timezone", help="IANA timezone name used for --cron and naive --at values")
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
    task_update_parser.add_argument("--cwd", help="Set working directory for Sessions created by this task")
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
    _add_json_noop(task_update_parser)

    task_subparsers.add_parser(
        "list",
        help="List scheduled tasks",
        description="List stored scheduled tasks. Completed one-shot tasks are hidden unless --all is used.",
        epilog="Use the returned task IDs with 'vibe task show', 'vibe task update', 'vibe task run', 'vibe task pause', 'vibe task resume', or 'vibe task remove'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe task list --help",
    )
    task_list_parser = task_subparsers.choices["list"]
    task_list_parser.add_argument(
        "--all",
        action="store_true",
        help="Include completed one-shot tasks that are hidden by default",
    )
    task_list_parser.add_argument(
        "--brief",
        action="store_true",
        help="Show a compact scheduling-focused view instead of the full stored task payload",
    )
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
        error_hint="Use --session-id and either --shell or a command after '--'. Add --forever only when the waiter should re-arm after successful cycles and only retry failures for explicit retry exit codes.",
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
        help="Keep re-arming the watch after each successful cycle instead of stopping after the first event. Terminal failures still stop the watch unless a retry exit code is allowed.",
    )
    watch_add_parser.add_argument(
        "--lifetime-timeout",
        type=float,
        default=0,
        help="Overall forever-watch lifetime timeout in seconds. Use 0 for no lifetime limit. Requires --forever.",
    )
    watch_add_parser.add_argument(
        "--retry-exit-code",
        dest="retry_exit_code",
        action="append",
        type=int,
        default=None,
        help=f"Cycle exit code that should be retried in forever mode. Repeat to add more. Default: {DEFAULT_RETRY_EXIT_CODE}",
    )
    watch_add_parser.add_argument(
        "--retry-delay",
        type=float,
        default=30,
        help="Delay in seconds before retrying an allowed forever cycle failure. Default: 30",
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
        help="Set overall forever-watch lifetime timeout in seconds. Use 0 for no lifetime limit.",
    )
    watch_update_parser.add_argument(
        "--retry-exit-code",
        dest="retry_exit_code",
        action="append",
        type=int,
        default=None,
        help="Replace retryable forever-mode exit codes. Repeat to add more.",
    )
    watch_update_parser.add_argument("--retry-delay", type=float, help="Set retry delay in seconds")
    watch_update_parser.add_argument("--shell", help="Replace waiter with a shell command")
    watch_update_parser.set_defaults(waiter_command=None)
    _add_json_noop(watch_update_parser)

    watch_list_parser = watch_subparsers.add_parser(
        "list",
        help="List background watches",
        description="List stored managed background watches.",
        epilog="Use the returned watch IDs with 'vibe watch show', 'vibe watch update', 'vibe watch pause', 'vibe watch resume', or 'vibe watch remove'.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe watch list --help",
    )
    watch_list_parser.add_argument(
        "--brief",
        action="store_true",
        help="Show a compact watcher-focused view instead of the full stored watch payload",
    )
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


def main():
    cache_running_vibe_path()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "stop":
        sys.exit(cmd_stop())
    if args.command == "start":
        sys.exit(cmd_start())
    if args.command == "restart":
        sys.exit(_cmd_restart_with_delay(args.delay_seconds))
    if args.command == "__restart-supervisor":
        from vibe.restart_supervisor import main as restart_supervisor_main

        sys.exit(
            restart_supervisor_main(
                [
                    "--job-id",
                    args.job_id,
                    "--delay-seconds",
                    str(args.delay_seconds),
                    "--trigger",
                    args.trigger,
                    *(["--scope", args.scope] if args.scope != "all" else []),
                    *(["--prepare-show-runtime"] if args.prepare_show_runtime else []),
                    *(["--vibe-path", args.vibe_path] if args.vibe_path else []),
                ]
            )
        )
    if args.command == "status":
        sys.exit(cmd_status())
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
    if args.command == "session":
        if args.session_command == "list":
            sys.exit(cmd_session_list(args))
        if args.session_command == "get":
            sys.exit(cmd_session_get(args))
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
        parser.error("data command is required")
    if args.command == "task":
        if args.task_command == "add":
            sys.exit(cmd_task_add(args))
        if args.task_command == "update":
            sys.exit(cmd_task_update(args))
        if args.task_command in {"list", "ls"}:
            sys.exit(cmd_task_list(include_all=getattr(args, "all", False), brief=getattr(args, "brief", False)))
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
            sys.exit(cmd_watch_list(brief=getattr(args, "brief", False)))
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
