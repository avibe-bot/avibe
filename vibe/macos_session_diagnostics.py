"""Quiet macOS login-session and DNS diagnostics for the Avibe service."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - Windows import path
    pwd = None  # type: ignore[assignment]


DEFAULT_POLL_INTERVAL_SECONDS = 60.0
DEFAULT_DNS_INTERVAL_SECONDS = 300.0
DEFAULT_COMMAND_TIMEOUT_SECONDS = 2.0
DNS_PROBE_HOSTNAME = "api.github.com"
_MAX_CAPTURE_CHARS = 64 * 1024
_DNS_PROBE_SCRIPT = "import socket, sys; socket.getaddrinfo(sys.argv[1], 443)"

_SECURITY_CONTEXT_RE = re.compile(r"security context\s*=\s*\{(?P<body>.*?)\}", re.DOTALL)
_ASID_RE = re.compile(r"^\s*asid\s*=\s*(?P<asid>\d+)\s*$", re.MULTILINE)
_GUI_DOMAIN_RE = re.compile(r"^gui/\d+\s*=\s*\{", re.MULTILINE)
_AQUA_SESSION_RE = re.compile(r"^\s*session\s*=\s*Aqua\s*$", re.MULTILINE)


@dataclass(frozen=True)
class CommandResult:
    returncode: int | None
    output: str = ""
    error_class: str | None = None


CommandRunner = Callable[[Sequence[str], float], CommandResult]
ConsoleUserReader = Callable[[], tuple[str | None, str | None]]


def run_bounded_command(command: Sequence[str], timeout: float) -> CommandResult:
    """Run a diagnostic command without exposing its raw output to logs."""
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return CommandResult(None, error_class="tool_missing")
    except subprocess.TimeoutExpired:
        return CommandResult(None, error_class="timeout")
    except PermissionError:
        return CommandResult(None, error_class="permission")
    except OSError:
        return CommandResult(None, error_class="os_error")

    output = (result.stdout or result.stderr or "")[:_MAX_CAPTURE_CHARS]
    return CommandResult(result.returncode, output=output)


def read_console_user() -> tuple[str | None, str | None]:
    """Read the foreground console owner without spawning ``stat``."""
    if pwd is None:
        return None, "tool_missing"
    try:
        console_uid = os.stat("/dev/console").st_uid
        return pwd.getpwuid(console_uid).pw_name, None
    except FileNotFoundError:
        return None, "console_missing"
    except KeyError:
        return None, "user_lookup_failed"
    except PermissionError:
        return None, "permission"
    except OSError:
        return None, "os_error"


def parse_launchctl_asid(output: str) -> int | None:
    """Extract the audit session ID from a launchctl security context."""
    security_context = _SECURITY_CONTEXT_RE.search(output)
    if security_context is None:
        return None
    match = _ASID_RE.search(security_context.group("body"))
    return int(match.group("asid")) if match is not None else None


def parse_gui_domain_asid(output: str) -> int | None:
    """Extract an Aqua GUI domain ASID, rejecting unrelated launchctl output."""
    if _GUI_DOMAIN_RE.search(output) is None or _AQUA_SESSION_RE.search(output) is None:
        return None
    return parse_launchctl_asid(output)


def _command_error_class(result: CommandResult) -> str:
    if result.error_class:
        return result.error_class
    normalized = result.output.lower()
    if "could not find domain" in normalized or "no such process" in normalized:
        return "absent"
    if "not privileged" in normalized or "permission denied" in normalized:
        return "permission"
    return "command_failed"


@dataclass(frozen=True)
class SessionContext:
    service_pid: int
    service_uid: int
    console_user: str | None
    service_asid: int | None
    gui_asid: int | None
    gui_domain_available: bool | None
    console_error_class: str | None = None
    service_error_class: str | None = None
    gui_error_class: str | None = None

    @property
    def asid_match(self) -> bool | None:
        if self.service_asid is None or self.gui_asid is None:
            return None
        return self.service_asid == self.gui_asid


@dataclass(frozen=True)
class DnsProbeState:
    state: str
    error_class: str | None = None


@dataclass(frozen=True)
class SessionDiagnosticSnapshot:
    context: SessionContext
    dns: DnsProbeState

    def event(self, reasons: Sequence[str]) -> dict[str, object]:
        payload: dict[str, object] = {
            "event": "macos_session_snapshot",
            "reason": list(reasons),
            "service_pid": self.context.service_pid,
            "service_uid": self.context.service_uid,
            "console_user": self.context.console_user,
            "service_asid": self.context.service_asid,
            "gui_asid": self.context.gui_asid,
            "gui_domain_available": self.context.gui_domain_available,
            "asid_match": self.context.asid_match,
            "dns_state": self.dns.state,
        }
        optional_fields = {
            "console_error_class": self.context.console_error_class,
            "service_error_class": self.context.service_error_class,
            "gui_error_class": self.context.gui_error_class,
            "dns_error_class": self.dns.error_class,
        }
        payload.update({key: value for key, value in optional_fields.items() if value is not None})
        return payload


class MacOSSessionProbe:
    """Collect the small, redacted state used by the transition monitor."""

    def __init__(
        self,
        *,
        service_pid: int | None = None,
        service_uid: int | None = None,
        command_runner: CommandRunner = run_bounded_command,
        console_user_reader: ConsoleUserReader = read_console_user,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        dns_hostname: str = DNS_PROBE_HOSTNAME,
    ) -> None:
        self.service_pid = service_pid if service_pid is not None else os.getpid()
        self.service_uid = service_uid if service_uid is not None else os.getuid()
        self._command_runner = command_runner
        self._console_user_reader = console_user_reader
        self._command_timeout = command_timeout
        self._dns_hostname = dns_hostname
        self._service_asid: int | None = None

    def capture_context(self) -> SessionContext:
        try:
            console_user, console_error = self._console_user_reader()
        except Exception:
            console_user, console_error = None, "probe_error"

        service_error = None
        if self._service_asid is None:
            result = self._command_runner(
                ["/bin/launchctl", "print", f"pid/{self.service_pid}"],
                self._command_timeout,
            )
            if result.returncode == 0:
                self._service_asid = parse_launchctl_asid(result.output)
                if self._service_asid is None:
                    service_error = "parse_error"
            else:
                service_error = _command_error_class(result)

        gui_result = self._command_runner(
            ["/bin/launchctl", "print", f"gui/{self.service_uid}"],
            self._command_timeout,
        )
        gui_asid = None
        gui_available: bool | None
        gui_error = None
        if gui_result.returncode == 0:
            gui_asid = parse_gui_domain_asid(gui_result.output)
            gui_available = gui_asid is not None
            if gui_asid is None:
                gui_available = None
                gui_error = "parse_error"
        else:
            gui_error = _command_error_class(gui_result)
            gui_available = False if gui_error == "absent" else None

        return SessionContext(
            service_pid=self.service_pid,
            service_uid=self.service_uid,
            console_user=console_user,
            service_asid=self._service_asid,
            gui_asid=gui_asid,
            gui_domain_available=gui_available,
            console_error_class=console_error,
            service_error_class=service_error,
            gui_error_class=gui_error,
        )

    def capture_dns(self) -> DnsProbeState:
        result = self._command_runner(
            [sys.executable, "-c", _DNS_PROBE_SCRIPT, self._dns_hostname],
            self._command_timeout,
        )
        if result.returncode == 0:
            return DnsProbeState("healthy")
        error_class = _command_error_class(result)
        if error_class in {"tool_missing", "permission", "os_error"}:
            return DnsProbeState("unknown", error_class)
        return DnsProbeState("failed", "resolver_timeout" if error_class == "timeout" else "resolver_error")


def _context_transition_reasons(previous: SessionContext, current: SessionContext) -> list[str]:
    reasons: list[str] = []
    if (previous.console_user, previous.console_error_class) != (
        current.console_user,
        current.console_error_class,
    ):
        reasons.append("console_user_change")
    if (
        previous.gui_domain_available,
        previous.gui_asid,
        previous.gui_error_class,
    ) != (
        current.gui_domain_available,
        current.gui_asid,
        current.gui_error_class,
    ):
        reasons.append("gui_domain_change")
    if previous.asid_match != current.asid_match:
        reasons.append("asid_match_change")
    return reasons


def _dns_transition_reasons(previous: DnsProbeState, current: DnsProbeState) -> list[str]:
    if previous.state == "healthy" and current.state == "failed":
        return ["dns_failed"]
    if previous.state == "failed" and current.state == "healthy":
        return ["dns_recovered"]
    return []


class MacOSSessionMonitor:
    """Record state transitions without emitting periodic success messages."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        platform: str = sys.platform,
        probe: MacOSSessionProbe | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        dns_interval: float = DEFAULT_DNS_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        self._logger = logger
        self._enabled = platform == "darwin"
        self._probe = probe or (MacOSSessionProbe() if self._enabled else None)
        self._poll_interval = poll_interval
        self._dns_interval = dns_interval
        self._monotonic = monotonic
        self._thread_factory = thread_factory
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_snapshot: SessionDiagnosticSnapshot | None = None
        self._last_dns_probe_at: float | None = None

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        try:
            thread = self._thread_factory(
                target=self._run,
                name="macos-session-diagnostics",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        except Exception:
            self._thread = None
            context = SessionContext(
                service_pid=getattr(self._probe, "service_pid", os.getpid()),
                service_uid=getattr(self._probe, "service_uid", os.getuid()),
                console_user=None,
                service_asid=None,
                gui_asid=None,
                gui_domain_available=None,
                service_error_class="thread_start_failed",
                gui_error_class="thread_start_failed",
            )
            self._last_snapshot = SessionDiagnosticSnapshot(
                context,
                DnsProbeState("unknown", "thread_start_failed"),
            )
            self._log_snapshot(self._last_snapshot, ["service_start"])

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS * 3 + 1.0)
        self._thread = None

    def _initialize_once(self, *, now: float | None = None) -> None:
        if not self._enabled or self._last_snapshot is not None:
            return
        current_time = self._monotonic() if now is None else now
        snapshot = SessionDiagnosticSnapshot(self._capture_context(), self._capture_dns())
        self._last_snapshot = snapshot
        self._last_dns_probe_at = current_time
        self._log_snapshot(snapshot, ["service_start"])

    def poll_once(self, *, now: float | None = None) -> None:
        if not self._enabled:
            return
        if self._last_snapshot is None:
            self._initialize_once(now=now)
            return

        current_context = self._capture_context()
        context_reasons = _context_transition_reasons(self._last_snapshot.context, current_context)
        current_time = self._monotonic() if now is None else now
        last_dns_probe_at = self._last_dns_probe_at
        dns_due = last_dns_probe_at is None or current_time - last_dns_probe_at >= self._dns_interval
        if context_reasons or dns_due:
            current_dns = self._capture_dns()
            self._last_dns_probe_at = current_time
        else:
            current_dns = self._last_snapshot.dns

        snapshot = SessionDiagnosticSnapshot(current_context, current_dns)
        reasons = context_reasons + _dns_transition_reasons(self._last_snapshot.dns, current_dns)
        if reasons:
            self._log_snapshot(snapshot, reasons)
        self._last_snapshot = snapshot

    def _run(self) -> None:
        self._initialize_once()
        while not self._stop_event.wait(self._poll_interval):
            self.poll_once()

    def _capture_context(self) -> SessionContext:
        try:
            return self._probe.capture_context()
        except Exception:
            return SessionContext(
                service_pid=getattr(self._probe, "service_pid", os.getpid()),
                service_uid=getattr(self._probe, "service_uid", os.getuid()),
                console_user=None,
                service_asid=None,
                gui_asid=None,
                gui_domain_available=None,
                console_error_class="probe_error",
                service_error_class="probe_error",
                gui_error_class="probe_error",
            )

    def _capture_dns(self) -> DnsProbeState:
        try:
            return self._probe.capture_dns()
        except Exception:
            return DnsProbeState("unknown", "probe_error")

    def _log_snapshot(self, snapshot: SessionDiagnosticSnapshot, reasons: Sequence[str]) -> None:
        payload = json.dumps(snapshot.event(reasons), sort_keys=True, separators=(",", ":"))
        context = snapshot.context
        warning = (
            context.gui_domain_available is not True
            or context.asid_match is False
            or snapshot.dns.state == "failed"
            or any(
                error is not None
                for error in (
                    context.console_error_class,
                    context.service_error_class,
                    context.gui_error_class,
                )
            )
        )
        if warning:
            self._logger.warning("macos_session_diagnostic %s", payload)
        else:
            self._logger.info("macos_session_diagnostic %s", payload)


def start_macos_session_diagnostics(
    logger: logging.Logger,
    *,
    platform: str = sys.platform,
) -> MacOSSessionMonitor:
    """Start diagnostics best-effort; probe failures never block the service."""
    monitor = MacOSSessionMonitor(logger, platform=platform)
    try:
        monitor.start()
    except Exception:
        # ``MacOSSessionMonitor`` already contains probe failures. This final
        # guard protects service startup from construction/threading surprises.
        pass
    return monitor
