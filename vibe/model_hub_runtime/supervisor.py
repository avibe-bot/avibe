from __future__ import annotations

import atexit
import logging
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import paths
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.environment import engine_subprocess_environment
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateStore


logger = logging.getLogger(__name__)


MODEL_HUB_STARTUP_TIMEOUT_SECONDS = 30.0
_STARTUP_OUTPUT_TAIL_BYTES = 8 * 1024
_STARTUP_LOG_BYTES = 4 * 1024
_STARTUP_POLL_INTERVAL_SECONDS = 0.05
_REDACTED = "[REDACTED]"
_STARTUP_SECRET_RE = re.compile(
    r"(?i)([\"']?\b(?:api[-_ ]?keys?|access[-_ ]?token|auth[-_ ]?token|refresh[-_ ]?token|"
    r"gateway[-_ ]?token|management[-_ ]?(?:key|secret)|authorization|password|"
    r"secret(?:[-_ ]?key)?|token)\b[\"']?\s*[:=]\s*)"
    r"(?:bearer\s+)?(?:\[[^\]\r\n]*\]|\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_STARTUP_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_STARTUP_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|token)=)[^&\s]+"
)
_STARTUP_PREFIXED_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|rk|pk|api)-[A-Za-z0-9_-]{8,}"
)
_REDACTED_BYTES = _REDACTED.encode("ascii")


def _redact_exact_prefix(
    data: bytes,
    safe_length: int,
    exact_values: tuple[bytes, ...],
) -> tuple[bytes, bytes]:
    if not exact_values:
        return data[:safe_length], data[safe_length:]
    spans: list[tuple[int, int]] = []
    for value in exact_values:
        search_from = 0
        while True:
            position = data.find(value, search_from)
            if position < 0 or position >= safe_length:
                break
            spans.append((position, position + len(value)))
            search_from = position + 1
    if not spans:
        return data[:safe_length], data[safe_length:]
    spans.sort()
    merged_spans: list[tuple[int, int]] = []
    for start, end in spans:
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))
    redacted = bytearray()
    cursor = 0
    for start, end in merged_spans:
        redacted.extend(data[cursor:start])
        redacted.extend(_REDACTED_BYTES)
        cursor = end
    if cursor < safe_length:
        redacted.extend(data[cursor:safe_length])
        cursor = safe_length
    return bytes(redacted), data[cursor:]


class _BoundedStartupOutput:
    """Drain a child pipe while retaining an exact-redacted startup tail."""

    def __init__(
        self,
        stream: Any,
        *,
        exact_values: tuple[str, ...],
        limit: int = _STARTUP_OUTPUT_TAIL_BYTES,
    ) -> None:
        self._stream = stream
        self._limit = limit
        self._exact_values = tuple(
            sorted(
                {value.encode("utf-8") for value in exact_values if value},
                key=len,
                reverse=True,
            )
        )
        self._overlap = max((len(value) for value in self._exact_values), default=1) - 1
        self._pending = bytearray()
        self._tail = bytearray()
        self._observed_bytes = 0
        self._source_exceeded_limit = False
        self._tail_truncated = False
        self._retaining = True
        self._eof_observed = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._drain,
            name="model-hub-startup-output",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            reader = getattr(self._stream, "read1", self._stream.read)
            while True:
                chunk = reader(4096)
                if not chunk:
                    with self._lock:
                        self._eof_observed = True
                        if self._retaining:
                            self._flush_pending_locked(final=True)
                    return
                if not isinstance(chunk, bytes):
                    chunk = str(chunk).encode("utf-8", "replace")
                with self._lock:
                    if not self._retaining:
                        continue
                    self._observed_bytes += len(chunk)
                    self._source_exceeded_limit = self._observed_bytes > self._limit
                    self._pending.extend(chunk)
                    self._flush_pending_locked(final=False)
        except Exception:
            logger.debug("Model Hub startup output drain stopped")

    def _flush_pending_locked(self, *, final: bool) -> None:
        safe_length = (
            len(self._pending)
            if final
            else max(0, len(self._pending) - self._overlap)
        )
        if safe_length == 0:
            return
        redacted, pending = _redact_exact_prefix(
            bytes(self._pending),
            safe_length,
            self._exact_values,
        )
        self._pending = bytearray(pending)
        overflow = len(self._tail) + len(redacted) - self._limit
        if overflow > 0:
            del self._tail[:overflow]
            self._tail_truncated = True
        self._tail.extend(redacted)

    def snapshot_live(self) -> tuple[bytes, bool, bool]:
        """Freeze a live stream without publishing unproven overlap bytes."""

        with self._lock:
            self._pending.clear()
            self._retaining = False
            return self._snapshot_locked()

    def snapshot_terminal(self) -> tuple[bytes, bool, bool]:
        """Freeze a stopped stream; only the drain may commit bytes at EOF."""

        with self._lock:
            if not self._eof_observed:
                self._pending.clear()
            self._retaining = False
            return self._snapshot_locked()

    def _snapshot_locked(self) -> tuple[bytes, bool, bool]:
        return (
            bytes(self._tail),
            self._source_exceeded_limit or self._tail_truncated,
            self._tail_truncated,
        )

    def join(self, timeout: float = 1.0) -> None:
        self._thread.join(timeout=timeout)


def _sanitize_startup_output(
    raw: bytes,
    *,
    exact_values: tuple[str, ...],
    truncated: bool,
    partial_line: bool,
) -> tuple[str, bool]:
    if partial_line:
        _partial_line, separator, raw = raw.partition(b"\n")
        if not separator:
            raw = b""
    text = raw.decode("utf-8", "replace")
    text = "".join(character if character.isprintable() or character.isspace() else " " for character in text)
    for value in sorted((item for item in exact_values if item), key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    text = _STARTUP_SECRET_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    text = _STARTUP_BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _STARTUP_QUERY_SECRET_RE.sub(lambda match: match.group(1) + _REDACTED, text)
    text = _STARTUP_PREFIXED_KEY_RE.sub(_REDACTED, text)
    compact = " ".join(text.split())
    encoded = compact.encode("utf-8")
    if len(encoded) > _STARTUP_LOG_BYTES:
        encoded = encoded[-_STARTUP_LOG_BYTES:]
        compact = encoded.decode("utf-8", "ignore")
        truncated = True
    return compact or "<none>", truncated


class EngineUnavailableError(RuntimeError):
    """The Hub path is unavailable; callers may use explicitly configured Direct mode."""

    def __init__(self, error_key: str, *, reason: str | None = None) -> None:
        super().__init__(error_key)
        self.error_key = error_key
        self.reason = reason
        self.direct_mode_available = True


class EngineSupervisor:
    """Start-on-demand supervisor for one loopback-only Model Hub engine."""

    def __init__(
        self,
        *,
        installer: EngineRuntimeManager | Any | None = None,
        state_store: EngineStateStore | None = None,
        startup_timeout: float = MODEL_HUB_STARTUP_TIMEOUT_SECONDS,
        process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        port_allocator: Callable[[], int] | None = None,
    ) -> None:
        self.installer = installer or EngineRuntimeManager()
        self.state_store = state_store or EngineStateStore(paths.get_runtime_dir() / "model-hub" / "state")
        self.startup_timeout = startup_timeout
        self._process_factory = process_factory
        self._port_allocator = port_allocator or _allocate_loopback_port
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._connection: EngineConnection | None = None
        self._startup_output: _BoundedStartupOutput | None = None
        self._last_check: str | None = None
        self._start_attempted = False

    def ensure_running(self) -> EngineConnection:
        with self._lock:
            if self._is_running_locked() and self._healthy_locked():
                assert self._connection is not None
                return self._connection
            self._stop_locked()
            return self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def restart_if_running(self) -> None:
        with self._lock:
            if not self._is_running_locked():
                return
            self._stop_locked()
            self._start_locked()

    def note_installation_settled(self) -> None:
        """Expose a newly verified binary as lazy-started, not previously down."""
        with self._lock:
            if not self._is_running_locked():
                self._start_attempted = False

    def invalidate_configs(self) -> None:
        """Remove secret-bearing configs and recreate one only for a live engine."""
        with self._lock:
            should_restart = self._is_running_locked() and self._healthy_locked()
            if self._is_running_locked():
                self._stop_locked()
            self.state_store.clear_runtime_configs()
            if should_restart:
                try:
                    self._start_locked()
                except EngineUnavailableError:
                    logger.warning("Model Hub engine remains stopped after credential revocation")

    def status(self) -> dict[str, Any]:
        with self._lock:
            managed = self.installer.status()
            installed = bool(managed.get("installed"))
            install_state_reader = getattr(self.installer, "install_state", None)
            install_state = install_state_reader() if callable(install_state_reader) else None
            listening = None
            if self._is_running_locked() and self._connection is not None:
                parsed_port = int(self._connection.base_url.rsplit(":", 1)[1])
                listening = {"host": "127.0.0.1", "port": parsed_port}
                health = "ok" if self._healthy_locked() else "degraded"
            elif install_state and install_state.get("state") == "installing":
                health = "installing"
            elif install_state and install_state.get("state") == "not_installed":
                health = "not_installed"
            elif installed:
                health = "down" if self._start_attempted else "not_started"
            else:
                # A missing or unverifiable binary remains installable even
                # after an earlier start attempt exposed its absence.
                health = "not_installed"
            host_platform_reader = getattr(self.installer, "host_platform", None)
            host_platform = (
                host_platform_reader()
                if callable(host_platform_reader)
                else str(managed.get("platform") or "")
            )
            return {
                "host_platform": host_platform,
                "manifest": self.installer.contract_manifest(),
                "status": {
                    "installed_version": (
                        managed.get("version")
                        if installed and health != "installing"
                        else None
                    ),
                    "verified": installed and health != "installing",
                    "listening": listening,
                    "health": health,
                    "last_check": self._last_check,
                    "error_key": (
                        install_state.get("error_key")
                        if health == "not_installed" and install_state
                        else None
                    ),
                },
            }

    def client(self) -> EngineClient:
        return EngineClient(self.ensure_running())

    def client_if_running(self) -> EngineClient | None:
        """Return a client for the current process without starting or repairing it."""
        with self._lock:
            if not self._is_running_locked() or self._connection is None:
                return None
            return EngineClient(self._connection)

    def _start_locked(self) -> EngineConnection:
        self._start_attempted = True
        managed = self.installer.status()
        binary = self.installer.resolve_engine_path()
        if binary is None:
            reason = str(managed.get("reason") or "engine_not_installed")
            raise EngineUnavailableError("models.engine.install_failed", reason=reason)
        install_id = Path(str(managed.get("install_dir") or binary.parent)).name
        instance_dir, runtime_secrets = self.state_store.prepare_instance(
            install_id,
            rotate=False,
        )
        port = self._port_allocator()
        config_path = instance_dir / "config.yaml"
        sources = self.state_store.list_sources()
        upstream_api_keys = tuple(
            value
            for source in sources
            for value in (self.state_store.credential_metadata(source.credential_ref).get("value"),)
            if isinstance(value, str) and value
        )
        write_engine_config(
            config_path,
            host="127.0.0.1",
            port=port,
            auth_dir=self.state_store.auth_dir,
            runtime_secrets=runtime_secrets,
            sources=sources,
            state_store=self.state_store,
        )
        startup_redaction_values = (
            runtime_secrets.management_key,
            runtime_secrets.gateway_token,
            *upstream_api_keys,
            str(binary),
            str(instance_dir),
            str(config_path),
        )
        connection = EngineConnection(
            base_url=f"http://127.0.0.1:{port}",
            management_key=runtime_secrets.management_key,
            gateway_token=runtime_secrets.gateway_token,
        )
        try:
            process = self._process_factory(
                [str(binary), "-config", str(config_path)],
                cwd=instance_dir,
                env=engine_subprocess_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                umask=0o077,
                **isolated_subprocess_kwargs(),
            )
        except (OSError, ValueError) as exc:
            raise EngineUnavailableError("models.engine.start_failed") from exc
        self._process = process
        self._connection = connection
        if process.stdout is None:
            self._stop_locked()
            raise EngineUnavailableError("models.engine.start_failed")
        output = _BoundedStartupOutput(
            process.stdout,
            exact_values=startup_redaction_values,
        )
        self._startup_output = output
        started_at = time.monotonic()
        deadline = started_at + self.startup_timeout
        exit_code: int | None = None
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            client = EngineClient(connection, timeout=min(1.0, remaining / 2))
            if client.health():
                try:
                    self.state_store.audit_auth_permissions()
                except Exception as exc:
                    self._stop_locked()
                    raise EngineUnavailableError("models.engine.unsafe_permissions") from exc
                self._last_check = _utc_now()
                raw_output, output_truncated, partial_line = output.snapshot_live()
                diagnostic, output_truncated = _sanitize_startup_output(
                    raw_output,
                    exact_values=startup_redaction_values,
                    truncated=output_truncated,
                    partial_line=partial_line,
                )
                logger.info(
                    "Model Hub engine startup outcome=ready managed_version=%s "
                    "elapsed_seconds=%.3f startup_output_truncated=%s startup_output=%s",
                    managed.get("version"),
                    time.monotonic() - started_at,
                    str(output_truncated).lower(),
                    diagnostic,
                )
                return connection
            time.sleep(min(_STARTUP_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        self._stop_locked()
        raw_output, output_truncated, partial_line = output.snapshot_terminal()
        diagnostic, output_truncated = _sanitize_startup_output(
            raw_output,
            exact_values=startup_redaction_values,
            truncated=output_truncated,
            partial_line=partial_line,
        )
        logger.warning(
            "Model Hub engine startup outcome=%s managed_version=%s exit_code=%s "
            "elapsed_seconds=%.3f readiness_budget_seconds=%.3f "
            "startup_output_truncated=%s startup_output=%s",
            "process_exit" if exit_code is not None else "timeout",
            managed.get("version"),
            exit_code,
            time.monotonic() - started_at,
            self.startup_timeout,
            str(output_truncated).lower(),
            diagnostic,
        )
        raise EngineUnavailableError("models.engine.health_failed")

    def _healthy_locked(self) -> bool:
        if not self._is_running_locked() or self._connection is None:
            return False
        healthy = EngineClient(self._connection, timeout=1.0).health()
        self._last_check = _utc_now()
        return healthy

    def _is_running_locked(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _stop_locked(self) -> None:
        process = self._process
        output = self._startup_output
        self._process = None
        self._connection = None
        self._startup_output = None
        if process is None or process.poll() is not None:
            if output is not None:
                output.join()
            return
        try:
            signal_process_tree(process, signal.SIGTERM, logger, "Model Hub engine")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                signal_process_tree(process, KILL_SIGNAL, logger, "Model Hub engine")
                process.wait(timeout=3)
        finally:
            if output is not None:
                output.join()


def _allocate_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_supervisor: EngineSupervisor | None = None


def get_engine_supervisor() -> EngineSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = EngineSupervisor()
    return _supervisor


def set_engine_supervisor_for_tests(supervisor: EngineSupervisor | None) -> None:
    global _supervisor
    if _supervisor is not None and _supervisor is not supervisor:
        _supervisor.stop()
    _supervisor = supervisor


def stop_engine_supervisor() -> None:
    global _supervisor
    if _supervisor is not None:
        _supervisor.stop()
        _supervisor = None


atexit.register(stop_engine_supervisor)
