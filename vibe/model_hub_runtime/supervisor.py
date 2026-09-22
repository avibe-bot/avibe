from __future__ import annotations

import atexit
import json
import logging
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from config import paths
from config.atomic_io import write_atomic
from core.process_isolation import (
    KILL_SIGNAL,
    PROCESS_IDENTITY_ENV,
    PersistedProcessIdentity,
    capture_spawned_process_identity,
    isolated_subprocess_kwargs,
    new_process_identity_marker,
    process_identity_from_payload,
    reap_orphaned_process_tree,
    serialize_process_identity,
    signal_process_tree,
)
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.environment import engine_subprocess_environment
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateStore


logger = logging.getLogger(__name__)


MODEL_HUB_STARTUP_TIMEOUT_SECONDS = 30.0
# Durable identity of the engine this state root last spawned. A service that
# dies without running ``atexit`` (SIGKILL, crash, forced restart) leaves its
# isolated engine group running; the next start reaps it by identity.
_ENGINE_PROCESS_RECORD = "engine-process.json"
_STARTUP_POLL_INTERVAL_SECONDS = 0.05


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
        self._last_check: str | None = None
        self._start_attempted = False
        self._health_failure_signature: tuple[str, str, int | None] | None = None

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

    def disable(self) -> None:
        """Stop the managed engine and restore explicit lazy-start idleness."""
        with self._lock:
            self._stop_locked()
            self._start_attempted = False

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
            elif (
                not installed
                and install_state
                and install_state.get("state") == "not_installed"
            ):
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
        # Every start binds a fresh port, so an unverifiable survivor cannot collide
        # with the new engine; it stays in the record beside it instead of blocking.
        survivors = self._reap_recorded_engines_locked()
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
        write_engine_config(
            config_path,
            host="127.0.0.1",
            port=port,
            auth_dir=self.state_store.auth_dir,
            runtime_secrets=runtime_secrets,
            sources=self.state_store.list_sources(),
            state_store=self.state_store,
        )
        connection = EngineConnection(
            base_url=f"http://127.0.0.1:{port}",
            management_key=runtime_secrets.management_key,
            gateway_token=runtime_secrets.gateway_token,
        )
        marker = new_process_identity_marker()
        environment = engine_subprocess_environment()
        environment[PROCESS_IDENTITY_ENV] = marker
        try:
            process = self._process_factory(
                [str(binary), "-config", str(config_path)],
                cwd=instance_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                umask=0o077,
                **isolated_subprocess_kwargs(),
            )
        except (OSError, ValueError) as exc:
            raise EngineUnavailableError("models.engine.start_failed") from exc
        self._process = process
        self._connection = connection
        self._record_engine_locked(process, marker, survivors)
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
            if self._check_health_locked(client):
                try:
                    self.state_store.audit_auth_permissions()
                except Exception as exc:
                    self._stop_locked()
                    raise EngineUnavailableError("models.engine.unsafe_permissions") from exc
                self._last_check = _utc_now()
                logger.info(
                    "Model Hub engine startup outcome=ready managed_version=%s "
                    "elapsed_seconds=%.3f child_output_retained=false",
                    managed.get("version"),
                    time.monotonic() - started_at,
                )
                return connection
            time.sleep(min(_STARTUP_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        self._stop_locked()
        logger.warning(
            "Model Hub engine startup outcome=%s managed_version=%s exit_code=%s "
            "elapsed_seconds=%.3f readiness_budget_seconds=%.3f "
            "child_output_retained=false",
            "process_exit" if exit_code is not None else "timeout",
            managed.get("version"),
            exit_code,
            time.monotonic() - started_at,
            self.startup_timeout,
        )
        raise EngineUnavailableError("models.engine.health_failed")

    def _healthy_locked(self) -> bool:
        if not self._is_running_locked() or self._connection is None:
            return False
        return self._check_health_locked(EngineClient(self._connection, timeout=1.0))

    def _check_health_locked(self, client: EngineClient) -> bool:
        healthy = client.health()
        self._last_check = _utc_now()
        failure = client.health_failure
        if failure is not None:
            signature = (failure.path, failure.reason, failure.http_status)
            if signature != self._health_failure_signature:
                logger.warning(
                    "Model Hub engine health outcome=failed endpoint=%s reason=%s "
                    "http_status=%s elapsed_seconds=%.3f",
                    failure.path, failure.reason, failure.http_status, failure.elapsed_seconds,
                )
            self._health_failure_signature = signature
        elif healthy:
            if self._health_failure_signature is not None:
                logger.info("Model Hub engine health outcome=recovered")
            self._health_failure_signature = None
        return healthy

    def _is_running_locked(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._connection = None
        self._health_failure_signature = None
        if process is not None and process.poll() is None:
            signal_process_tree(process, signal.SIGTERM, logger, "Model Hub engine")
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                signal_process_tree(process, KILL_SIGNAL, logger, "Model Hub engine")
                process.wait(timeout=3)
        # Also reached with no local handle (a restarted service): the record may
        # still name an engine the previous service left running.
        self._reap_recorded_engines_locked()

    @property
    def _engine_record_path(self) -> Path:
        return self.state_store.root / _ENGINE_PROCESS_RECORD

    def _load_engine_records_locked(self) -> list[PersistedProcessIdentity] | None:
        """Return the recorded engine identities, or ``None`` if the record is unreadable."""

        try:
            text = self._engine_record_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            logger.warning("Model Hub engine process record could not be read", exc_info=True)
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        entries = payload.get("engines") if isinstance(payload, dict) else None
        records: list[PersistedProcessIdentity] = []
        for entry in entries if isinstance(entries, list) else ():
            pid = entry.get("pid") if isinstance(entry, dict) else None
            identity = process_identity_from_payload(entry, pid) if isinstance(pid, int) else None
            if identity is not None:
                records.append(identity)
        return records

    def _store_engine_records_locked(self, records: list[PersistedProcessIdentity]) -> None:
        path = self._engine_record_path
        try:
            if not records:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(
                path,
                json.dumps(
                    {"engines": [serialize_process_identity(identity) for identity in records]},
                    sort_keys=True,
                )
                + "\n",
            )
        except OSError:
            logger.warning("Model Hub engine process record could not be written", exc_info=True)

    def _record_engine_locked(
        self,
        process: Any,
        marker: str,
        survivors: list[PersistedProcessIdentity] | None,
    ) -> None:
        if survivors is None:
            # Rewriting a record we could not read would drop identities it may hold.
            return
        pid = getattr(process, "pid", None)
        identity = capture_spawned_process_identity(pid, marker) if isinstance(pid, int) else None
        if identity is not None:
            self._store_engine_records_locked([*survivors, identity])

    def _reap_recorded_engines_locked(self) -> list[PersistedProcessIdentity] | None:
        """Stop every recorded engine tree; return those still unconfirmed.

        ``None`` means the record could not be read and was left untouched.
        """

        records = self._load_engine_records_locked()
        if records is None:
            return None
        survivors: list[PersistedProcessIdentity] = []
        for identity in records:
            outcome = reap_orphaned_process_tree(logger, "Model Hub engine", expected_identity=identity)
            if outcome == "unconfirmed":
                survivors.append(identity)
            elif outcome == "reaped":
                logger.warning("Reaped a Model Hub engine left running by an earlier service")
        if survivors:
            logger.warning(
                "Could not confirm %d earlier Model Hub engine(s) exited; keeping them tracked",
                len(survivors),
            )
        self._store_engine_records_locked(survivors)
        return survivors


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
