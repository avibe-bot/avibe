import argparse
import asyncio
import getpass
import json
import logging
import math
import os
import platform
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent
from typing import NamedTuple, Optional
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
    parse_session_key,
    resolve_session_id_target,
    session_anchor_for_target,
)
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
from storage.models import scope_settings
from storage.pagination import DEFAULT_PAGE_LIMIT, PageRequest, make_page_request, pagination_payload
from storage.read_only_query import ReadOnlyQueryError, run_read_only_query
from storage.settings_service import make_scope_id

logger = logging.getLogger(__name__)
UV_TOOL_PACKAGE_NAMES = (PACKAGE_NAME, LEGACY_PACKAGE_NAME)

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
          vibe task add --session-id sesk8m4q2p7x --post-to channel --cron '*/5 * * * *' --message 'Tell a new joke each time.'
          vibe task add --session-id sesk8m4q2p7x --at '2026-03-31T09:00:00+08:00' --message-file briefing.md
        """
    )


def _task_add_examples_text() -> str:
    return dedent(
        """\
        Session target:
          Use --session-id with the current Agent Session ID, for example sesk8m4q2p7x.

        Guidance:
          If this is your first time using this command, read this whole help entry before creating a task.
          `--session-id` chooses which Agent Session Avibe will continue using when the task runs.
          Keep the current session id when future runs should stay in the same session.
          If no session id is available, trigger this from an active Avibe conversation instead of guessing.
          `--post-to channel` changes where the message is posted, not which session is continued.
          Use --deliver-key only when delivery must go to a different explicit target.
          `--message` and `--message-file` provide the stored user message that will be sent each time the task runs.
          Use --cron for recurring jobs and --at for one-shot jobs.
          Cron weekday digits use APScheduler semantics: 0=Mon through 6=Sun; 7 is invalid. Prefer weekday names such as mon, tue, or sun when scheduling by day of week.
          --timezone controls how --cron and naive --at timestamps are interpreted.

        Examples:
          vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'
          vibe task add --session-id sesk8m4q2p7x --post-to channel --cron '*/5 * * * *' --message 'Tell a new joke each time.'
          vibe task add --session-id sesk8m4q2p7x --deliver-key 'slack::channel::C999' --cron '0 9 * * *' --message 'Post the daily summary in the announcements channel.'
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
          vibe task update 12ab34cd56ef --session-id sesk8m4q2p7x --post-to channel
          vibe task update 12ab34cd56ef --deliver-key 'slack::channel::C999'
          vibe task update 12ab34cd56ef --reset-delivery

        Guidance:
          Unspecified fields keep their existing values.
          Use --reset-delivery to return to following the session target directly.
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
          New automation should use `vibe agent run --async`.

        Session target:
          Use --session-id with the current Agent Session ID, for example sesk8m4q2p7x.

        Guidance:
          If this is your first time creating an async one-shot run, use `vibe agent run --async --help`.
          `vibe hook send` queues one deprecated asynchronous compatibility turn without persisting a scheduled task.
          `--session-id` chooses which Agent Session Avibe will continue using for that one async turn.
          Keep the current session id when the hook should continue in the same session.
          If no session id is available, trigger this from an active Avibe conversation instead of guessing.
          `--post-to channel` changes where the message is posted, not which session is continued.
          Use --deliver-key only when delivery must go to a different explicit target.
          `--message` and `--message-file` provide the one-shot async user message that will be queued immediately.

        Examples:
          vibe agent run --async --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
          vibe agent run --async --session-id sesk8m4q2p7x --message 'Share the benchmark result.'
        """
    )


def _watch_examples_text() -> str:
    return dedent(
        """\
        Examples:
          vibe watch add --session-id sesk8m4q2p7x --name 'Wait for export' --shell 'python3 scripts/wait_for_export.py'
          vibe watch add --session-id sesk8m4q2p7x --post-to channel --prefix 'The CI job finished.' -- python3 scripts/wait_for_ci.py --build 42
          vibe watch add --session-id sesk8m4q2p7x --forever --retry-exit-code 75 --retry-delay 60 --shell 'bash scripts/wait_for_log_pattern.sh'
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
          update   Switch visibility, rotate public share links, or take the page offline.
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
          4. Run `vibe show update --visibility public` only when the user asks for a shareable public link.
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
          vibe show update --session-id sesk8m4q2p7x --visibility private
          vibe show update --session-id sesk8m4q2p7x --visibility offline
          vibe show update --session-id sesk8m4q2p7x --rotate-share

        Notes:
          private uses the authenticated /show/<session-id>/ URL.
          public uses a short /p/<share-id>/ URL and disables the private path.
          offline takes the page down without deleting local files.
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
          Use --session-id with the current Agent Session ID, for example sesk8m4q2p7x.

        Guidance:
          If this is your first time using this command, read this whole help entry before creating a watch.
          Use a watch when a script should wait in the background and send a follow-up when it detects an event or reaches a terminal failure.
          `--session-id` chooses which Agent Session Avibe will continue using for follow-up messages from the watch.
          Keep the current session id when follow-up should continue in the same session.
          If no session id is available, trigger this from an active Avibe conversation instead of guessing.
          `--post-to channel` changes where the follow-up is posted, not which session is continued.
          Use --deliver-key only when delivery must go to a different explicit target.
          `--prefix` becomes the instruction text of the follow-up hook. On a successful cycle, Avibe prepends `--prefix` before waiter stdout and joins them with a blank line when both exist.
          Terminal failures also send a follow-up and disable the watch.
          In forever mode, failures are retried only when the waiter exits with an allowed `--retry-exit-code`.
          Pass either --shell '<command>' or a command after '--'.
          --timeout applies to each cycle. --lifetime-timeout applies only to the whole forever watch lifetime.

        Examples:
          vibe watch add --session-id sesk8m4q2p7x --shell 'python3 scripts/wait_for_export.py'
          vibe watch add --session-id sesk8m4q2p7x --post-to channel --prefix 'The export finished.' -- bash -lc 'sleep 120; echo done'
          vibe watch add --session-id sesk8m4q2p7x --forever --timeout 600 --lifetime-timeout 86400 --retry-exit-code 75 --retry-delay 30 -- uv run --no-project scripts/wait_pr.py --repo avibe-bot/avibe --pr 153
        """
    )


def _agent_run_examples_text() -> str:
    return dedent(
        """\
        Session target:
          Use --session-id to continue an existing Agent Session.
          Use --create-session to start a new blank Session.
          Use --fork-session to create a new Session by forking an existing Session's native backend context.

        Forking:
          --fork-session <session-id> creates a new Avibe Agent Session and asks the native backend to fork the source native session on the first turn.
          Forks keep the same backend as the source Session. Passing --agent is allowed only when that Agent uses the same backend.
          --agent, --model, and --reasoning-effort may override the forked Session's Agent/model/effort.
          Do not combine --fork-session with --session-id, --create-session, --create-session-per-run, --deliver-key, or --post-to.

        Examples:
          vibe agent run --agent release-reviewer --message 'Review the latest deployment result.'
          vibe agent run --async --session-id sesk8m4q2p7x --message 'The export finished. Share the summary.'
          vibe agent run --async --fork-session sesk8m4q2p7x --message 'Explore this alternate fix from the current context.'
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


def _resolve_watch_cwd(value: Optional[str], *, help_command: str) -> Optional[str]:
    if not value:
        return None
    resolved = Path(value).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise TaskCliError(
            f"watch cwd does not exist: {value}",
            code="invalid_watch_cwd",
            hint="Point --cwd to an existing directory, or omit it to inherit the service working directory.",
            help_command=help_command,
            details={"cwd": value},
        )
    return str(resolved)


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
            hint="Use the current Agent Session ID from the prompt, such as sesk8m4q2p7x.",
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
            hint="Use --session-id with the current Agent Session ID.",
            example="vibe task add --session-id sesk8m4q2p7x --cron '0 * * * *' --message 'Share the hourly summary.'",
            help_command=help_command,
        )
    return session_id or None, session_key


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
            "use either --post-to or --deliver-key, not both",
            code="conflicting_delivery_target",
            hint="Use --post-to for the common thread/channel delivery choice, or --deliver-key for an explicit delivery target.",
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
                "--deliver-key must use the same platform as the session target",
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
            "--post-to thread requires a thread-bound session target or an explicit --deliver-key",
            code="invalid_delivery_target",
            hint="Use a thread-bound Agent Session ID or --deliver-key with a thread target.",
            help_command=help_command,
            details={"session_id": session_id, "session_key": session_key, "post_to": post_to},
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
            resolved = requested
            if resolved is None and target.agent_name:
                resolved = store.require_enabled(target.agent_name)
            if resolved is not None and target.agent_backend and resolved.backend != target.agent_backend:
                raise TaskCliError(
                    "agent backend does not match the existing session backend",
                    code="agent_session_backend_mismatch",
                    hint="Use an Agent with the same backend as the Session, or create a new Session.",
                    details={
                        "agent": resolved.name,
                        "agent_backend": resolved.backend,
                        "session_id": session_id,
                        "session_backend": target.agent_backend,
                    },
                    help_command=help_command,
                )
            return resolved

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
        schedule_type = "cron" if args.cron else "at"
        session_policy = _validate_definition_session_policy(
            args,
            schedule_type=schedule_type,
            help_command="vibe task add --help",
        )
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
        agent = _resolve_agent_for_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key or getattr(args, "deliver_key", None) or "",
            help_command="vibe task add --help",
        )
        agent_name = agent.name if agent else None
        if session_policy == "create_once":
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                deliver_key=args.deliver_key,
                help_command="vibe task add --help",
            )
        validation_session_key = session_key or (args.deliver_key if session_policy == "create_per_run" else "")
        session_target, delivery_target = _validate_delivery_args(
            session_id=session_id,
            session_key=validation_session_key,
            post_to=getattr(args, "post_to", None),
            deliver_key=getattr(args, "deliver_key", None),
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
                cron=args.cron,
                timezone_name=timezone_name,
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
                run_at=run_at,
                timezone_name=timezone_name,
            )
        warnings = _collect_target_warnings(session_target, delivery_target)
        task_payload = _task_payload(task)
        _print_cli_payload(
            "run_definition",
            definition=task_payload,
            task=task_payload,
            warnings=warnings,
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
            getattr(args, "post_to", None) is not None or getattr(args, "deliver_key", None) is not None
        ):
            raise TaskCliError(
                "use either --reset-delivery or a new delivery flag, not both",
                code="conflicting_delivery_target",
                hint="Pass --reset-delivery to clear delivery overrides, or pass --post-to/--deliver-key to replace them.",
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
        if session_policy in {"create_once", "create_per_run"} and not deliver_key:
            raise TaskCliError(
                "--deliver-key is required when a stored definition creates sessions",
                code="missing_delivery_target",
                hint="Pass the Scope ID that owns the new Session.",
                help_command="vibe task update --help",
            )
        if agent_name is None and session_policy != "existing":
            agent = _resolve_agent_for_target(
                agent_name=None,
                session_id=None,
                session_key=deliver_key or "",
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
                deliver_key=deliver_key or "",
                help_command="vibe task update --help",
            )
            session_key = ""
        session_target, delivery_target = _validate_definition_update_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=post_to,
            deliver_key=deliver_key,
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
            "cron": cron,
            "run_at": run_at,
            "timezone": timezone_name,
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
            "cron": task.cron,
            "run_at": task.run_at,
            "timezone": task.timezone,
        }
        if changes == current:
            raise TaskCliError(
                "no task fields were changed",
                code="no_task_changes",
                hint="Pass at least one field to update, such as --name, --cron, --message, --session-id, or --deliver-key.",
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
            cron=cron,
            run_at=run_at,
            timezone_name=timezone_name,
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
            deprecation_warning="vibe hook send is deprecated; use vibe agent run --async instead.",
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
    create_session = bool(getattr(args, "create_session", False))
    create_per_run = bool(getattr(args, "create_session_per_run", False))
    if bool(getattr(args, "async_run", False)) and getattr(args, "wait_timeout", None) is not None:
        raise TaskCliError(
            "use --async or --wait-timeout, not both",
            code="conflicting_wait_policy",
            hint="--async returns immediately. Remove --wait-timeout, or run synchronously without --async.",
            help_command=help_command,
        )
    if (getattr(args, "callback_session_id", None) or "").strip() and not bool(getattr(args, "async_run", False)):
        raise TaskCliError(
            "--callback-session-id requires --async",
            code="callback_requires_async",
            hint="Callback delivery happens after an asynchronous run completes.",
            help_command=help_command,
        )
    if fork_session and (session_id or create_session or create_per_run):
        raise TaskCliError(
            "use --fork-session without --session-id or session creation flags",
            code="conflicting_session_policy",
            hint="Fork creates a new Session from the source Session.",
            help_command=help_command,
        )
    if not fork_session and (
        (getattr(args, "model", None) or "").strip()
        or (getattr(args, "reasoning_effort", None) or "").strip()
    ):
        raise TaskCliError(
            "--model and --reasoning-effort are only valid with --fork-session",
            code="fork_override_without_fork",
            hint="Use --agent, --model, and --reasoning-effort as overrides when forking a Session.",
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
    if fork_session:
        return "fork"
    if create_session:
        return "create"
    if session_id:
        return "existing"
    return "none"


def _validate_definition_session_policy(args, *, schedule_type: str | None, help_command: str) -> str:
    session_id = (getattr(args, "session_id", None) or "").strip()
    session_key = (getattr(args, "session_key", None) or "").strip()
    create_session = bool(getattr(args, "create_session", False))
    create_per_run = bool(getattr(args, "create_session_per_run", False))
    deliver_key = (getattr(args, "deliver_key", None) or "").strip()
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
    if (create_session or create_per_run) and not deliver_key:
        raise TaskCliError(
            "--deliver-key is required when a stored definition creates sessions",
            code="missing_delivery_target",
            hint="Pass the Scope ID that owns the new Session.",
            help_command=help_command,
        )
    if create_session:
        return "create_once"
    if create_per_run:
        return "create_per_run"
    if session_id or session_key:
        return "existing"
    raise TaskCliError(
        "one session policy is required",
        code="missing_session_policy",
        hint="Use --session-id to continue a Session, or --create-session with --deliver-key to create one.",
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


def _validate_definition_update_delivery_target(
    *,
    session_policy: str,
    session_id: Optional[str],
    session_key: str,
    post_to: Optional[str],
    deliver_key: Optional[str],
    help_command: str,
):
    validation_session_key = session_key or (deliver_key if session_policy == "create_per_run" else "")
    return _validate_delivery_args(
        session_id=session_id,
        session_key=validation_session_key,
        post_to=post_to,
        deliver_key=deliver_key,
        help_command=help_command,
    )


def _resolve_run_cwd(args, *, session_policy: str, help_command: str) -> Optional[str]:
    """Working directory for a session this run RESERVES; None = scope snapshot.

    An explicit ``--cwd`` must exist and always wins. Without it, a private
    (no ``--deliver-key``) reservation follows the CLI invocation's cwd — the
    caller stands in a concrete directory, which IS the natural context, like
    every other CLI tool. A ``--deliver-key`` reservation keeps the target
    scope's configured workdir (returning None defers to the scope snapshot in
    ``create_agent_session_row``): the scope's binding is the deliberate choice
    there, not wherever the CLI happens to run. ``--session-id`` reserves
    nothing — an existing session keeps its workdir — so ``--cwd`` is an error.
    """
    raw = (getattr(args, "cwd", None) or "").strip()
    if session_policy == "existing":
        if raw:
            raise TaskCliError(
                "--cwd only applies when this run creates a session",
                code="cwd_with_existing_session",
                hint="An existing --session-id keeps its own working directory.",
                help_command=help_command,
            )
        return None
    if raw:
        resolved = os.path.abspath(os.path.expanduser(raw))
        if not os.path.isdir(resolved):
            raise TaskCliError(
                f"--cwd directory does not exist: {resolved}",
                code="cwd_not_found",
                hint="Point --cwd to an existing directory, or omit it to use the invocation directory.",
                help_command=help_command,
            )
        return resolved
    if (getattr(args, "deliver_key", None) or "").strip():
        return None
    return os.getcwd()


def _reserve_cli_session(*, agent, deliver_key: Optional[str], workdir: Optional[str] = None) -> str:
    # Route through ``core.services.sessions`` so the CLI shares the same
    # business API as the UI server and the future N3 internal endpoint;
    # see docs/plans/workbench-dispatch-architecture.md §6 (C2).
    from core.services import sessions as sessions_service

    if deliver_key:
        target = _parse_validated_session_key(deliver_key, help_command="vibe agent run --help")
        session_anchor = session_anchor_for_target(target)
        session_id = sessions_service.reserve_agent_session(
            scope_key=target.session_scope,
            agent_backend=agent.backend,
            session_anchor=session_anchor,
            agent_id=agent.id,
            agent_name=agent.name,
            model=agent.model,
            reasoning_effort=agent.reasoning_effort,
            workdir=workdir,
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
        )
    if not session_id:
        raise TaskCliError(
            "failed to reserve a new Agent Session ID",
            code="session_reservation_failed",
            help_command="vibe agent run --help",
        )
    return session_id


def _reserve_forked_cli_session(
    *,
    source_session_id: str,
    agent_name: Optional[str],
    model: Optional[str],
    reasoning_effort: Optional[str],
):
    from core.services.session_fork import SessionForkError, reserve_forked_session

    try:
        return reserve_forked_session(
            source_session_id=source_session_id,
            agent_name=agent_name or None,
            model=model,
            reasoning_effort=reasoning_effort,
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


def _reserve_definition_session(*, agent_name: Optional[str], deliver_key: str, help_command: str) -> str:
    from core.services import sessions as sessions_service

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
    session_anchor = session_anchor_for_target(target)
    session_id = sessions_service.reserve_agent_session(
        scope_key=target.session_scope,
        agent_backend=agent_backend,
        session_anchor=session_anchor,
        agent_id=agent.id if agent else None,
        agent_name=agent.name if agent else None,
        model=agent.model if agent else None,
        reasoning_effort=agent.reasoning_effort if agent else None,
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
        if session_policy in {"none", "fork"} and (args.deliver_key or args.post_to):
            raise TaskCliError(
                "delivery options require --session-id or --create-session",
                code="delivery_target_without_session_policy",
                hint="Use --create-session --deliver-key <scope-id> when a new delivered Session should be created.",
                help_command="vibe agent run --help",
            )
        session_id = (args.session_id or "").strip() or None
        session_key = ""
        if session_policy == "fork" and (args.cwd or "").strip():
            raise TaskCliError(
                "--cwd only applies when this run creates a blank session",
                code="cwd_with_fork_session",
                hint="A fork copies the source Session working directory.",
                help_command="vibe agent run --help",
            )
        run_cwd = _resolve_run_cwd(args, session_policy=session_policy, help_command="vibe agent run --help")
        agent = _agent_store().require_enabled(agent_name) if agent_name else None
        fork_result = None
        if session_policy == "create":
            session_id = _reserve_cli_session(agent=agent, deliver_key=args.deliver_key, workdir=run_cwd)
        elif session_policy == "none":
            session_id = _reserve_cli_session(agent=agent, deliver_key=None, workdir=run_cwd)
        elif session_policy == "fork":
            fork_result = _reserve_forked_cli_session(
                source_session_id=(args.fork_session or "").strip(),
                agent_name=agent_name or None,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
            )
            session_id = fork_result.session_id
            if agent_name:
                agent = _agent_store().require_enabled(agent_name)
        if session_id:
            target = resolve_session_id_target(session_id)
            session_key = target.session_key.to_key()
            agent = _resolve_agent_for_target(
                agent_name=agent_name or None,
                session_id=session_id,
                session_key=session_key,
                help_command="vibe agent run --help",
            )
        if session_policy != "none" or args.post_to or args.deliver_key:
            _validate_delivery_args(
                session_id=session_id,
                session_key=session_key,
                post_to=args.post_to,
                deliver_key=args.deliver_key,
                help_command="vibe agent run --help",
            )
        callback_session_id = (getattr(args, "callback_session_id", None) or "").strip() or None
        if callback_session_id:
            resolve_session_id_target(callback_session_id)
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
            deliver_key=args.deliver_key,
            message=message,
            callback_session_id=callback_session_id,
            metadata={
                "session_fork": fork_result.fork.to_metadata(),
            }
            if fork_result
            else None,
        )
        payload = {
            "accepted": True,
            "request_type": request.request_type,
            "run_id": request.id,
            "execution_id": request.id,
            "agent": agent.name if agent else None,
            "session_policy": session_policy,
            "session_id": session_id,
            "deliver_key": args.deliver_key,
            "callback_session_id": callback_session_id,
            "async": bool(args.async_run),
            "run": {
                "id": request.id,
                "status": "queued",
                "run_type": request.request_type,
                "agent_name": agent.name if agent else None,
                "session_id": session_id,
                "callback_session_id": callback_session_id,
            },
        }
        if fork_result:
            payload["forked_from_session_id"] = fork_result.fork.source_session_id
        if fork_result:
            payload["run"]["forked_from_session_id"] = fork_result.fork.source_session_id
        if not args.async_run:
            payload["run"] = _wait_for_run_result(request_store, request.id, wait_timeout=args.wait_timeout)
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
            session_id=getattr(args, "session_id", None),
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
    run = _task_request_store().get_run(args.run_id)
    if run is None:
        _print_task_error(TaskCliError(f"run '{args.run_id}' not found", code="run_not_found", details={"run_id": args.run_id}))
        return 1
    _print_cli_payload("agent_run", run=_run_payload(run))
    return 0


def cmd_runs_cancel(args):
    canceled = _task_request_store().cancel_run(args.run_id)
    if not canceled:
        _print_task_error(TaskCliError(f"run '{args.run_id}' not found", code="run_not_found", details={"run_id": args.run_id}))
        return 1
    run = _task_request_store().get_run(args.run_id)
    _print_cli_payload("agent_run", cancel_requested=True, run=_run_payload(run or {"id": args.run_id}))
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
        engine = _open_session_engine()
        with engine.connect() as conn:
            payload = sessions_service.get_active_session(conn, args.session_id)
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{args.session_id}' not found",
                code="session_not_found",
                details={"session_id": args.session_id},
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
        message=_session_get_hint(args.session_id),
    )
    return 0


def cmd_session_update(args):
    from core.services import sessions as sessions_service

    try:
        engine = _open_session_engine()
        with engine.begin() as conn:
            # Validate first so an archived/missing id is a clean not-found rather
            # than silently writing a title onto a soft-deleted row.
            sessions_service.get_active_session(conn, args.session_id)
            # title_source="agent": this is the agent setting its own session title (vs
            # "user" for a human Web UI edit). Both are deliberate, so neither gets
            # auto-overwritten nor re-nudged — see DELIBERATE_TITLE_SOURCES.
            payload = sessions_service.update_session(
                conn, args.session_id, title=args.title, title_source="agent"
            )
    except LookupError:
        _print_task_error(
            TaskCliError(
                f"session '{args.session_id}' not found",
                code="session_not_found",
                details={"session_id": args.session_id},
            ),
            help_command="vibe session update --help",
        )
        return 1
    except Exception as exc:
        _print_task_error(exc, help_command="vibe session update --help")
        return 1
    # The DB write is committed above; ping a running UI so the rename shows live
    # (best-effort — never affects this command's result).
    _post_session_activity_to_live_ui(args.session_id)
    _print_cli_payload("agent_session", updated=True, session=_session_row(payload, brief=False))
    return 0


def cmd_watch_add(args):
    try:
        session_policy = _validate_definition_session_policy(
            args,
            schedule_type="watch",
            help_command="vibe watch add --help",
        )
        command, shell_command = _resolve_watch_command(args, help_command="vibe watch add --help")
        session_id, session_key = _resolve_session_target_args(
            args,
            required=session_policy == "existing",
            help_command="vibe watch add --help",
        )
        agent = _resolve_agent_for_target(
            agent_name=getattr(args, "agent", None),
            session_id=session_id,
            session_key=session_key or getattr(args, "deliver_key", None) or "",
            help_command="vibe watch add --help",
        )
        agent_name = agent.name if agent else None
        if session_policy == "create_once":
            session_id = _reserve_definition_session(
                agent_name=agent_name,
                deliver_key=args.deliver_key,
                help_command="vibe watch add --help",
            )
        validation_session_key = session_key or (args.deliver_key if session_policy == "create_per_run" else "")
        session_target, delivery_target = _validate_delivery_args(
            session_id=session_id,
            session_key=validation_session_key,
            post_to=getattr(args, "post_to", None),
            deliver_key=getattr(args, "deliver_key", None),
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
        cwd = _resolve_watch_cwd(args.cwd, help_command="vibe watch add --help")
        prefix = _normalize_task_name(getattr(args, "prefix", None))
        message = _resolve_optional_message_input(
            args,
            help_command="vibe watch add --help",
            example_command="vibe watch add --session-id sesk8m4q2p7x",
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
        )
        runtime_store = _watch_runtime_store()
        watch, runtime_entry = _wait_for_watch_startup(store, runtime_store, watch.id)
        warnings = _collect_target_warnings(session_target, delivery_target)
        watch_payload = _watch_payload(watch, runtime_entry)
        _print_cli_payload(
            "run_definition",
            definition=watch_payload,
            watch=watch_payload,
            warnings=warnings,
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
            getattr(args, "post_to", None) is not None or getattr(args, "deliver_key", None) is not None
        ):
            raise TaskCliError(
                "use either --reset-delivery or a new delivery flag, not both",
                code="conflicting_delivery_target",
                hint="Pass --reset-delivery to clear delivery overrides, or pass --post-to/--deliver-key to replace them.",
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
        if session_policy in {"create_once", "create_per_run"} and not deliver_key:
            raise TaskCliError(
                "--deliver-key is required when a stored definition creates sessions",
                code="missing_delivery_target",
                hint="Pass the Scope ID that owns the new Session.",
                help_command="vibe watch update --help",
            )
        if agent_name is None and session_policy != "existing":
            agent = _resolve_agent_for_target(
                agent_name=None,
                session_id=None,
                session_key=deliver_key or "",
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
                deliver_key=deliver_key or "",
                help_command="vibe watch update --help",
            )
            session_key = ""
        session_target, delivery_target = _validate_definition_update_delivery_target(
            session_policy=session_policy,
            session_id=session_id,
            session_key=session_key,
            post_to=post_to,
            deliver_key=deliver_key,
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
        }
        if changes == current:
            raise TaskCliError(
                "no watch fields were changed",
                code="no_watch_changes",
                hint="Pass at least one field to update, such as --name, --shell, --timeout, --session-id, or --deliver-key.",
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


def _doctor():
    """Run diagnostic checks and return results in UI-compatible format.

    Returns:
        {
            "groups": [{"name": "...", "items": [{"status": "pass|warn|fail", "message": "...", "action": "..."}]}],
            "summary": {"pass": 0, "warn": 0, "fail": 0},
            "ok": bool
        }
    """
    groups = []
    summary = {"pass": 0, "warn": 0, "fail": 0}

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

    for item in _runtime_architecture_items():
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
        "groups": groups,
        "summary": summary,
        "ok": ok,
    }

    _write_json(paths.get_runtime_doctor_path(), result)
    return result


def _add_doctor_item(items: list[dict], status: str, message: str, action: str | None = None) -> None:
    item = {"status": status, "message": message}
    if action:
        item["action"] = action
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


def cmd_start():
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
    try:
        raw_pid = pid_path.read_text(encoding="utf-8").strip()
        pid = int(raw_pid)
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _runtime_process_was_running() -> bool:
    return runtime.service_pid_file_points_to_running_service() or runtime.ui_pid_file_points_to_running_ui()


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
    if getattr(args, "json", False):
        _print_json(result)
        return 0 if result.get("ok") else 1

    if not result.get("ok"):
        _print_remote_pair_failure(result)
        return 1

    start_result = result.get("start") if isinstance(result.get("start"), dict) else {}
    _print_remote_pair_success(result, start_result)
    return 0


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
        "help_command": "vibe show --help",
    }
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
        page = store.ensure(args.session_id)
        page_dir = ensure_show_page_dir(args.session_id)
        _prewarm_show_page_session_best_effort(args.session_id)
        payload = _show_page_result(page, message=f"Show Page workspace is ready at {page_dir}.")
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
        page = store.get(args.session_id)
        if page is None:
            payload = {
                "ok": False,
                "code": "show_page_not_found",
                "session_id": args.session_id,
                "message": "No Show Page exists for this session.",
                "next_actions": [f"Run `vibe show path --session-id {args.session_id}` to create the workspace."],
            }
            if getattr(args, "json", False):
                _print_json(payload)
            else:
                print("No Show Page exists for this session.")
                print(f"Run: vibe show path --session-id {args.session_id}")
            return 1
        payload = _show_page_result(page, message=f"Show Page is {page.visibility}.")
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
        existing = store.ensure(args.session_id)
        previous = show_page_payload(existing)
        previous_active_url = previous.get("active_url")
        previous_public_url = previous.get("public_url")
        previous_private_url = previous.get("private_url")
        extra: dict = {}

        if getattr(args, "rotate_share", False):
            updated, previous_share_id = store.rotate_share(args.session_id)
            extra = {
                "previous_public_url": public_url(previous_share_id),
                "previous_share_id": previous_share_id,
                "message_detail": "Previous public share URL was revoked.",
            }
            message = "Public share link rotated."
        else:
            updated = store.update_visibility(args.session_id, args.visibility)
            message = f"Show Page is now {updated.visibility}."
            if existing.visibility == "private" and updated.visibility == "public":
                extra["previous_private_url"] = previous_private_url
            elif existing.visibility == "public" and updated.visibility == "private":
                extra["previous_public_url"] = previous_public_url
            elif updated.visibility == "offline":
                extra["previous_active_url"] = previous_active_url
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
        page = page_store.ensure(args.session_id)
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
        event = _post_show_mark_to_live_ui(args.session_id, payload)
        if event is None:
            event_store = ShowSessionEventStore()
            event = event_store.append(args.session_id, payload)
        result = _show_page_result(
            page,
            message="Assistant mark recorded.",
            extra={
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
        page = page_store.ensure(args.session_id)
        payload = _read_event_json_argument(args.event_json, args.event_json_file)
        if args.type:
            payload = {**payload, "type": args.type}
        if args.dispatch:
            payload = _with_show_event_dispatch(payload)
        event = _post_show_event_to_live_ui(args.session_id, payload)
        if event is None:
            if args.dispatch:
                from vibe.ui_server import record_local_show_event

                event = record_local_show_event(args.session_id, payload, dispatch_sync=True)
            else:
                event_store = ShowSessionEventStore()
                event = event_store.append(args.session_id, payload)
        result = _show_page_result(
            page,
            message="Show event recorded.",
            extra={
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


def cmd_doctor():
    result = _doctor()

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
        payload["askill"] = askill
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
    if os.environ.get("VIBE_INSTALL_SKIP_ASKILL", "").strip().lower() in {"1", "true", "yes", "on"}:
        return {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_ASKILL"}
    try:
        return api.ensure_askill_installed(force=True)
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
    subparsers.add_parser("doctor", help="Run diagnostics")
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
        description="Run an Avibe Agent turn. Use --async to queue it as a background run.",
        epilog=_agent_run_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe agent run --help",
    )
    agent_run_parser.add_argument("--agent", help="Avibe Agent name")
    agent_run_parser.add_argument("--session-id", help="Existing Agent Session ID to continue")
    agent_run_parser.add_argument("--fork-session", help="Existing Agent Session ID to fork into a new Session")
    agent_run_parser.add_argument("--create-session", action="store_true", help="Create a new Avibe Session ID before running")
    agent_run_parser.add_argument("--create-session-per-run", action="store_true", help="Create a new Avibe Session ID for each definition run")
    agent_run_parser.add_argument("--deliver-key", help="Scope ID used as delivery target when creating or sending to a target")
    agent_run_parser.add_argument("--model", help="Model override for the new forked Session")
    agent_run_parser.add_argument("--reasoning-effort", help="Reasoning effort override for the new forked Session")
    agent_run_parser.add_argument(
        "--cwd",
        help=(
            "Working directory for the NEW session. Defaults to the directory the command is "
            "invoked from; sessions created with --deliver-key default to the target scope's "
            "configured workdir instead. Invalid with --session-id (an existing session keeps "
            "its own working directory)."
        ),
    )
    agent_run_parser.add_argument("--post-to", choices=("thread", "channel"))
    agent_run_parser.add_argument("--callback-session-id", help="Caller Session ID to receive the completed async run result")
    agent_run_parser.add_argument("--async", dest="async_run", action="store_true", help="Queue the run and return immediately")
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
    runs_list_parser.add_argument("--definition-id", help="Filter by task or watch definition ID")
    runs_list_parser.add_argument("--created-after", help="Filter by created_at >= timestamp, or relative value such as 6h or 7d")
    runs_list_parser.add_argument("--created-before", help="Filter by created_at <= timestamp, or relative value such as 6h or 7d")
    runs_list_parser.add_argument("--q", dest="query", help="Search common run text fields")
    runs_list_parser.add_argument("--brief", action="store_true", help="Show compact run rows")
    _add_pagination_args(runs_list_parser, help_command="vibe runs list --help")
    _add_json_noop(runs_list_parser)
    runs_show_parser = runs_subparsers.add_parser("show", help="Show one Agent run")
    runs_show_parser.add_argument("run_id")
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
    session_get_parser.add_argument("session_id", help="Agent Session ID")
    _add_json_noop(session_get_parser)
    session_update_parser = session_subparsers.add_parser(
        "update",
        help="Update a session's title (title only)",
        description="Update only the title of one active session. No other field can be changed here.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe session update --help",
    )
    session_update_parser.add_argument("session_id", help="Agent Session ID")
    session_update_parser.add_argument(
        "--title", required=True, help="New title. Pass an empty string to clear it (reverts to id-based display)."
    )
    _add_json_noop(session_update_parser)

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
        error_hint="Pass the current Agent Session ID explicitly.",
    )
    show_path_parser.add_argument("--session-id", required=True, help="Agent Session ID for the Show Page.")
    show_path_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_status_parser = show_subparsers.add_parser(
        "status",
        help="Show this session's Show Page state",
        description="Inspect one Show Page without creating it.",
        epilog=_show_status_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show status --help",
        error_hint="Pass the current Agent Session ID explicitly.",
    )
    show_status_parser.add_argument("--session-id", required=True, help="Agent Session ID for the Show Page.")
    show_status_parser.add_argument("--json", action="store_true", help="Print machine-readable state.")

    show_update_parser = show_subparsers.add_parser(
        "update",
        help="Update visibility or rotate the public share link",
        description="Switch a Show Page between private, public, and offline states, or rotate its public share link.",
        epilog=_show_update_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe show update --help",
        error_hint="Pass --visibility private|public|offline or --rotate-share.",
    )
    show_update_parser.add_argument("--session-id", required=True, help="Agent Session ID for the Show Page.")
    show_update_action = show_update_parser.add_mutually_exclusive_group(required=True)
    show_update_action.add_argument(
        "--visibility",
        choices=("private", "public", "offline"),
        help="Set the active Show Page visibility.",
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
        error_hint="Pass --session-id, --target, and --body or --body-file.",
    )
    show_mark_parser.add_argument("--session-id", required=True, help="Agent Session ID for the Show Page.")
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
        error_hint="Pass --session-id and either --event-json/--event-json-file or --type with JSON fields.",
    )
    show_event_parser.add_argument("--session-id", required=True, help="Agent Session ID for the Show Page.")
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
        error_hint="Use --session-id together with exactly one schedule flag and one message input flag. Add --post-to or --deliver-key only when delivery must differ from the session target.",
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
    task_add_parser.add_argument("--agent", help="Avibe Agent name to use when the task runs")
    delivery_group = task_add_parser.add_mutually_exclusive_group()
    delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help="Delivery location override. This changes where the message is posted, not which session is continued.",
    )
    delivery_group.add_argument(
        "--deliver-key",
        help="Explicit delivery target key. Use this only when delivery must go to a different target than the continued session.",
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
    task_update_parser.add_argument("--agent", help="Replace the Avibe Agent used by this task")
    task_update_parser.add_argument("--clear-agent", action="store_true", help="Clear the stored Avibe Agent override")
    update_delivery_group = task_update_parser.add_mutually_exclusive_group()
    update_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help="Replace the delivery location override",
    )
    update_delivery_group.add_argument(
        "--deliver-key",
        help="Replace the explicit delivery target key",
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
        description="Deprecated compatibility entrypoint. Use 'vibe agent run --async' for new one-shot asynchronous turns.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe hook --help",
        error_hint="Use 'vibe agent run --async --help' for the current async Agent Run command shape.",
    )
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", metavar="{send}")
    hook_subparsers.required = True
    hook_send_parser = hook_subparsers.add_parser(
        "send",
        help="Deprecated compatibility async send",
        description="Deprecated compatibility entrypoint. Use 'vibe agent run --async' for new one-shot asynchronous Agent Runs.",
        epilog=_hook_send_examples_text(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        error_help_command="vibe hook send --help",
        error_hint="Use --session-id together with exactly one message input flag. Add --post-to or --deliver-key only when delivery must differ from the session target.",
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
        help="Delivery location override. This changes where the message is posted, not which session is continued.",
    )
    hook_delivery_group.add_argument(
        "--deliver-key",
        help="Explicit delivery target key. Use this only when delivery must go to a different target than the continued session.",
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
    watch_add_parser.add_argument("--agent", help="Avibe Agent name to use for follow-up messages")
    watch_delivery_group = watch_add_parser.add_mutually_exclusive_group()
    watch_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help="Delivery location override. This changes where the follow-up is posted, not which session is continued.",
    )
    watch_delivery_group.add_argument(
        "--deliver-key",
        help="Explicit delivery target key. Use this only when delivery must go to a different target than the continued session.",
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
        error_hint="Pass at least one field to update, such as --name, --shell, --timeout, --session-id, or --deliver-key.",
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
    watch_update_parser.add_argument("--agent", help="Replace the Avibe Agent used for follow-up messages")
    watch_update_parser.add_argument("--clear-agent", action="store_true", help="Clear the stored Avibe Agent override")
    watch_update_delivery_group = watch_update_parser.add_mutually_exclusive_group()
    watch_update_delivery_group.add_argument(
        "--post-to",
        choices=("thread", "channel"),
        help="Delivery location override. This changes where the follow-up is posted, not which session is continued.",
    )
    watch_update_delivery_group.add_argument(
        "--deliver-key",
        help="Explicit delivery target key. Use this only when delivery must go to a different target than the continued session.",
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
        sys.exit(cmd_doctor())
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
