from __future__ import annotations

import atexit
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
from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree
from vibe.model_hub_runtime.client import EngineClient, EngineConnection
from vibe.model_hub_runtime.config import write_engine_config
from vibe.model_hub_runtime.environment import engine_subprocess_environment
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.state import EngineStateStore


logger = logging.getLogger(__name__)


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
        startup_timeout: float = 10.0,
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
            listening = None
            if self._is_running_locked() and self._connection is not None:
                parsed_port = int(self._connection.base_url.rsplit(":", 1)[1])
                listening = {"host": "127.0.0.1", "port": parsed_port}
                health = "ok" if self._healthy_locked() else "degraded"
            elif installed:
                health = "down"
            else:
                health = "not_installed"
            return {
                "manifest": self.installer.contract_manifest(),
                "status": {
                    "installed_version": managed.get("version") if installed else None,
                    "verified": installed,
                    "listening": listening,
                    "health": health,
                    "last_check": self._last_check,
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
        install = self.installer.ensure()
        if not install.get("ok"):
            reason = str(install.get("reason") or "engine_install_failed")
            raise EngineUnavailableError("models.engine.install_failed", reason=reason)
        binary = Path(str(install["path"]))
        install_id = Path(str(install.get("install_dir") or binary.parent)).name
        instance_dir, runtime_secrets = self.state_store.prepare_instance(
            install_id,
            rotate=bool(install.get("changed")),
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
        try:
            process = self._process_factory(
                [str(binary), "-config", str(config_path)],
                cwd=instance_dir,
                env=engine_subprocess_environment(),
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
        deadline = time.monotonic() + self.startup_timeout
        client = EngineClient(connection, timeout=1.0)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if client.health():
                try:
                    self.state_store.audit_auth_permissions()
                except Exception as exc:
                    self._stop_locked()
                    raise EngineUnavailableError("models.engine.unsafe_permissions") from exc
                self._last_check = _utc_now()
                logger.info("Model Hub engine started on 127.0.0.1 with managed version %s", install.get("version"))
                return connection
            time.sleep(0.05)
        self._stop_locked()
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
        self._process = None
        self._connection = None
        if process is None or process.poll() is not None:
            return
        signal_process_tree(process, signal.SIGTERM, logger, "Model Hub engine")
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            signal_process_tree(process, KILL_SIGNAL, logger, "Model Hub engine")
            process.wait(timeout=3)


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
