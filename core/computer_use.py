"""Shared Runtime contract for the desktop-owned computer-use daemon.

The desktop shell is the only process allowed to start the Cua daemon and the
only writer of ``computer-use.json``. Runtime processes use this module to:

* derive the effective user-facing status,
* decide whether the managed MCP server belongs in a backend launch, and
* notice configuration changes without treating daemon availability as config.

The state directory intentionally does not use ``AVIBE_HOME``. It belongs to
the operating-system desktop application and must be shared by every Runtime
that the shell can adopt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

COMPUTER_USE_SCHEMA_VERSION = 1
COMPUTER_USE_SERVER_NAME = "avibe_computer"
COMPUTER_USE_STATE_DIR_ENV = "AVIBE_COMPUTER_USE_STATE_DIR"
COMPUTER_USE_STATE_FILE = "computer-use.json"
COMPUTER_USE_LOCK_FILE = "computer-use.lock"
COMPUTER_USE_LEASE_FILE = "computer-lease.json"
COMPUTER_USE_LEASE_LOCK_FILE = "computer-lease.lock"
COMPUTER_USE_RECONCILE_INTERVAL_SECONDS = 2.0
COMPUTER_USE_TOOL_SNAPSHOT_SHA256 = (
    "b03c3e48d1b00c8fe7c0e8b9813eb5ea104ad38313672fc4b68af203a827f43b"
)

COMPUTER_USE_TOOL_NAMES = (
    "list_apps",
    "list_windows",
    "get_window_state",
    "verify_state",
    "get_accessibility_tree",
    "get_screen_size",
    "get_desktop_state",
    "get_cursor_position",
    "zoom",
    "launch_app",
    "kill_app",
    "set_window_frame",
    "click",
    "double_click",
    "right_click",
    "drag",
    "scroll",
    "type_text",
    "press_key",
    "hotkey",
    "set_value",
    "clipboard_read",
    "clipboard_write",
    "start_session",
    "end_session",
    "get_session",
    "list_sessions",
    "health_report",
)

_STATE_VALUES = frozenset(
    {
        "off",
        "needs_permission",
        "starting",
        "ready",
        "error",
        "stopped",
        "needs_runtime",
    }
)
_SHA256_HEX_LENGTH = 64


class ComputerUseStateError(ValueError):
    """The desktop state file does not satisfy the versioned contract."""


@dataclass(frozen=True)
class ToolSnapshot:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ComputerUseState:
    schema_version: int
    enabled: bool
    state: str
    reason: str | None
    shell_pid: int
    instance_id: str
    generation: int
    driver_version: str
    tool_snapshot: ToolSnapshot
    socket_path: str | None = None
    proxy_executable: str | None = None
    host_bundle_id: str | None = None

    @property
    def daemon_key(self) -> tuple[str, int]:
        return (self.instance_id, self.generation)


@dataclass(frozen=True)
class ComputerUseStatus:
    status: str
    reason: str | None
    state: ComputerUseState | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status, "reason": self.reason}
        if self.state is not None:
            payload.update(
                {
                    "enabled": self.state.enabled,
                    "state": self.state.state,
                    "instance_id": self.state.instance_id,
                    "generation": self.state.generation,
                    "driver_version": self.state.driver_version,
                }
            )
        return payload


@dataclass(frozen=True)
class ManagedMcpServerSpec:
    name: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    fingerprint: str

    def claude_config(self) -> dict[str, Any]:
        return {
            "type": "stdio",
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
        }


@dataclass(frozen=True)
class ComputerUseConfigFingerprint:
    enabled: bool
    expected_snapshot_sha256: str | None
    snapshot_valid: bool

    def digest(self) -> str:
        value = [
            self.enabled,
            self.expected_snapshot_sha256,
            self.snapshot_valid,
        ]
        return hashlib.sha256(
            json.dumps(value, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


@dataclass
class _SnapshotCacheEntry:
    size: int
    mtime_ns: int
    digest: str


class SnapshotVerifier:
    """Hash snapshots only when their filesystem identity changes."""

    def __init__(self) -> None:
        self._cache: dict[Path, _SnapshotCacheEntry] = {}

    def digest(self, path: Path) -> str | None:
        try:
            stat_result = path.stat()
        except OSError:
            self._cache.pop(path, None)
            return None
        cached = self._cache.get(path)
        if (
            cached is not None
            and cached.size == stat_result.st_size
            and cached.mtime_ns == stat_result.st_mtime_ns
        ):
            return cached.digest
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            self._cache.pop(path, None)
            return None
        self._cache[path] = _SnapshotCacheEntry(
            size=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            digest=digest,
        )
        return digest

    def verifies(self, snapshot: ToolSnapshot) -> bool:
        return self.digest(snapshot.path) == snapshot.sha256


_snapshot_verifier = SnapshotVerifier()
_warned_snapshot_failures: set[tuple[str, str]] = set()


def desktop_computer_use_dir(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Path:
    """Resolve the desktop app-data directory independently of ``AVIBE_HOME``."""

    env = os.environ if environ is None else environ
    override = str(env.get(COMPUTER_USE_STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()

    platform_name = platform or sys.platform
    user_home = Path.home() if home is None else home
    if platform_name == "darwin":
        return user_home / "Library" / "Application Support" / "bot.avibe.desktop"
    if platform_name == "win32":
        appdata = str(env.get("APPDATA") or "").strip()
        if appdata:
            return Path(appdata) / "bot.avibe.desktop"
        return user_home / "AppData" / "Roaming" / "bot.avibe.desktop"
    xdg_data_home = str(env.get("XDG_DATA_HOME") or "").strip()
    base = Path(xdg_data_home).expanduser() if xdg_data_home else user_home / ".local" / "share"
    return base / "bot.avibe.desktop"


def computer_use_state_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / COMPUTER_USE_STATE_FILE


def computer_use_lock_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / COMPUTER_USE_LOCK_FILE


def computer_use_lease_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / COMPUTER_USE_LEASE_FILE


def computer_use_lease_lock_path(**kwargs: Any) -> Path:
    return desktop_computer_use_dir(**kwargs) / COMPUTER_USE_LEASE_LOCK_FILE


def _required_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ComputerUseStateError(f"{key} must be a boolean")
    return value


def _required_int(payload: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ComputerUseStateError(f"{key} must be an integer >= {minimum}")
    return value


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ComputerUseStateError(f"{key} must be a non-empty string")
    return value


def _optional_reason(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("reason")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ComputerUseStateError("reason must be null or a non-empty string")
    return value


def _absolute_path(payload: Mapping[str, Any], key: str) -> str:
    value = _required_string(payload, key)
    if not Path(value).is_absolute():
        raise ComputerUseStateError(f"{key} must be an absolute path")
    return value


def parse_computer_use_state(payload: Any) -> ComputerUseState:
    if not isinstance(payload, dict):
        raise ComputerUseStateError("state file must contain a JSON object")
    schema_version = _required_int(payload, "schema_version", minimum=1)
    if schema_version != COMPUTER_USE_SCHEMA_VERSION:
        raise ComputerUseStateError(f"unsupported schema_version: {schema_version}")

    state_name = _required_string(payload, "state")
    if state_name not in _STATE_VALUES:
        raise ComputerUseStateError(f"unknown state: {state_name}")

    raw_snapshot = payload.get("tool_snapshot")
    if not isinstance(raw_snapshot, dict):
        raise ComputerUseStateError("tool_snapshot must be an object")
    snapshot_path = Path(_absolute_path(raw_snapshot, "path"))
    snapshot_sha256 = _required_string(raw_snapshot, "sha256").lower()
    if len(snapshot_sha256) != _SHA256_HEX_LENGTH or any(
        character not in "0123456789abcdef" for character in snapshot_sha256
    ):
        raise ComputerUseStateError("tool_snapshot.sha256 must be a SHA-256 hex digest")

    socket_path: str | None = None
    proxy_executable: str | None = None
    host_bundle_id: str | None = None
    if state_name == "ready":
        socket_path = _absolute_path(payload, "socket_path")
        proxy_executable = _absolute_path(payload, "proxy_executable")
        host_bundle_id = _required_string(payload, "host_bundle_id")

    return ComputerUseState(
        schema_version=schema_version,
        enabled=_required_bool(payload, "enabled"),
        state=state_name,
        reason=_optional_reason(payload),
        shell_pid=_required_int(payload, "shell_pid", minimum=1),
        instance_id=_required_string(payload, "instance_id"),
        generation=_required_int(payload, "generation", minimum=0),
        driver_version=_required_string(payload, "driver_version"),
        tool_snapshot=ToolSnapshot(snapshot_path, snapshot_sha256),
        socket_path=socket_path,
        proxy_executable=proxy_executable,
        host_bundle_id=host_bundle_id,
    )


def read_computer_use_state(path: Path | None = None) -> ComputerUseState | None:
    target = path or computer_use_state_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ComputerUseStateError(f"state file is unreadable: {exc}") from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ComputerUseStateError("state file is malformed JSON") from exc
    return parse_computer_use_state(payload)


def _snapshot_valid(state: ComputerUseState, verifier: SnapshotVerifier) -> bool:
    valid = verifier.verifies(state.tool_snapshot)
    if not valid:
        warning_key = (str(state.tool_snapshot.path), state.tool_snapshot.sha256)
        if warning_key not in _warned_snapshot_failures:
            _warned_snapshot_failures.add(warning_key)
            logger.warning(
                "Computer-use tool snapshot failed verification: path=%s expected_sha256=%s",
                state.tool_snapshot.path,
                state.tool_snapshot.sha256,
            )
    return valid


def computer_use_config_fingerprint(
    *,
    state_path: Path | None = None,
    verifier: SnapshotVerifier | None = None,
) -> ComputerUseConfigFingerprint:
    checker = verifier or _snapshot_verifier
    try:
        state = read_computer_use_state(state_path)
    except ComputerUseStateError:
        return ComputerUseConfigFingerprint(False, None, False)
    if state is None or not state.enabled:
        return ComputerUseConfigFingerprint(False, None, False)
    return ComputerUseConfigFingerprint(
        True,
        state.tool_snapshot.sha256,
        _snapshot_valid(state, checker),
    )


def managed_mcp_server_spec(
    *,
    state_path: Path | None = None,
    verifier: SnapshotVerifier | None = None,
    python_executable: str | None = None,
) -> ManagedMcpServerSpec | None:
    checker = verifier or _snapshot_verifier
    try:
        state = read_computer_use_state(state_path)
    except ComputerUseStateError:
        return None
    if state is None or not state.enabled or not _snapshot_valid(state, checker):
        return None

    env: dict[str, str] = {}
    resolved_state_path = state_path or computer_use_state_path()
    default_path = computer_use_state_path(environ={})
    if resolved_state_path != default_path:
        env[COMPUTER_USE_STATE_DIR_ENV] = str(resolved_state_path.parent)
    fingerprint = ComputerUseConfigFingerprint(
        True,
        state.tool_snapshot.sha256,
        True,
    ).digest()
    return ManagedMcpServerSpec(
        name=COMPUTER_USE_SERVER_NAME,
        command=python_executable or sys.executable,
        args=(
            "-I",
            str(Path(__file__).resolve().with_name("computer_server.py")),
        ),
        env=env,
        fingerprint=fingerprint,
    )


def _shell_lock_is_held(lock_path: Path) -> bool:
    if os.name == "nt":
        # Phase 1 is macOS-only. A Windows state file cannot claim a live shell
        # until the deferred Windows lock implementation is added.
        return False
    import fcntl

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def desktop_computer_use_shell_is_live(
    *,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    shell_lock_held: Callable[[Path], bool] = _shell_lock_is_held,
) -> bool:
    """Whether the native shell owns the Computer Use lock.

    Missing or damaged optional state does not mean that the native shell is
    absent: the shell acquires the lock before it reads or repairs that state.
    The lock is therefore the support signal, while a valid state record
    remains the source for the feature's effective status.
    """

    target = state_path or computer_use_state_path()
    try:
        state = read_computer_use_state(target)
    except ComputerUseStateError:
        state = None
    resolved_lock_path = lock_path or target.with_name(COMPUTER_USE_LOCK_FILE)
    if not shell_lock_held(resolved_lock_path):
        return False
    return state is None or state.state != "stopped"


def _unix_socket_accepts(path: str, *, timeout: float = 0.1) -> bool:
    if os.name == "nt":
        return False
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
    except OSError:
        return False
    finally:
        client.close()
    return True


def effective_computer_use_status(
    *,
    state_path: Path | None = None,
    lock_path: Path | None = None,
    verifier: SnapshotVerifier | None = None,
    shell_lock_held: Callable[[Path], bool] = _shell_lock_is_held,
    socket_accepts: Callable[[str], bool] = _unix_socket_accepts,
) -> ComputerUseStatus:
    target = state_path or computer_use_state_path()
    checker = verifier or _snapshot_verifier
    try:
        state = read_computer_use_state(target)
    except ComputerUseStateError:
        return ComputerUseStatus("unavailable", "invalid_state_file")
    if state is None:
        return ComputerUseStatus("off", "never_enabled")
    if not state.enabled:
        return ComputerUseStatus("off", "toggle_off", state)
    if not _snapshot_valid(state, checker):
        return ComputerUseStatus("unavailable", "snapshot_invalid", state)
    resolved_lock_path = lock_path or target.with_name(COMPUTER_USE_LOCK_FILE)
    if not shell_lock_held(resolved_lock_path) or state.state == "stopped":
        return ComputerUseStatus("unavailable", "shell_not_running", state)
    if state.state != "ready":
        return ComputerUseStatus(state.state, state.reason, state)
    assert state.socket_path is not None
    if not socket_accepts(state.socket_path):
        return ComputerUseStatus("unavailable", "daemon_unreachable", state)
    return ComputerUseStatus("ready", None, state)


class ComputerUseConfigReconciler:
    """Renew live backend generations only when managed MCP config changes."""

    def __init__(
        self,
        renew_backend: Callable[[str], Awaitable[None]],
        backend_names: Callable[[], list[str]],
        *,
        state_path: Path | None = None,
        verifier: SnapshotVerifier | None = None,
        interval_seconds: float = COMPUTER_USE_RECONCILE_INTERVAL_SECONDS,
    ) -> None:
        self._renew_backend = renew_backend
        self._backend_names = backend_names
        self._state_path = state_path
        self._verifier = verifier or SnapshotVerifier()
        self._interval_seconds = interval_seconds
        self._fingerprint = computer_use_config_fingerprint(
            state_path=state_path,
            verifier=self._verifier,
        )
        self._consumer_fingerprints = {
            backend: self._fingerprint
            for backend in dict.fromkeys(self._backend_names())
        }
        self._task: asyncio.Task[None] | None = None

    @property
    def fingerprint(self) -> ComputerUseConfigFingerprint:
        return self._fingerprint

    async def check_once(self) -> bool:
        current = computer_use_config_fingerprint(
            state_path=self._state_path,
            verifier=self._verifier,
        )
        self._fingerprint = current
        live_backends = list(dict.fromkeys(self._backend_names()))
        live_set = set(live_backends)
        for backend in tuple(self._consumer_fingerprints):
            if backend not in live_set:
                self._consumer_fingerprints.pop(backend, None)
        for backend in live_backends:
            # A consumer that appeared since the prior poll was built from the
            # current managed MCP spec, so it starts converged.
            self._consumer_fingerprints.setdefault(backend, current)

        changed = False
        for backend in live_backends:
            if self._consumer_fingerprints[backend] == current:
                continue
            changed = True
            try:
                await self._renew_backend(backend)
            except Exception:
                logger.warning(
                    "Computer-use configuration did not reconcile backend %s; retrying",
                    backend,
                    exc_info=True,
                )
                continue
            self._consumer_fingerprints[backend] = current
        return changed

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                try:
                    await self.check_once()
                except Exception:
                    logger.warning(
                        "Computer-use backend configuration reconciliation failed",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            raise

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(),
                name="computer-use-config-reconciler",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
