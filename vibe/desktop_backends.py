"""App-private agent backend installation for the self-contained desktop app."""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import psutil

from config import paths
from config.atomic_io import write_atomic
from core.process_isolation import (
    KILL_SIGNAL,
    PROCESS_IDENTITY_ENV,
    PersistedProcessIdentity,
    capture_spawned_process_identity,
    fingerprint_process_marker,
    is_valid_worker_fingerprint,
    isolated_subprocess_kwargs,
    new_process_identity_marker,
    process_group_exists,
    processes_carrying_marker,
    process_identity_from_payload,
    reap_marked_processes,
    reap_orphaned_process_tree,
    serialize_process_identity,
    signal_process_tree,
)
from storage.lock import MigrationFileLock, MigrationLockTimeout
from vibe.desktop_runtime import (
    DESKTOP_INSTALLER_OWNER_ENV,
    DESKTOP_INSTALLER_ROLE,
    DESKTOP_ROLE_ENV,
    DESKTOP_RUNTIME_ID_ENV,
    desktop_runtime_id,
    private_desktop_backends_root,
    private_desktop_node_bin,
    private_desktop_npm_cli,
)


logger = logging.getLogger(__name__)

CURRENT_DESCRIPTOR_SCHEMA_VERSION = 1
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_INSTALL_OUTPUT_CHARS = 8192
NPM_REGISTRY = "https://registry.npmjs.org/"
DESKTOP_BACKEND_LOCK_TIMEOUT_SECONDS = 30.0
# Removal runs once the Runtime has stopped, when no install should be left to
# wait for.
DESKTOP_BACKEND_REMOVAL_LOCK_TIMEOUT_SECONDS = 5.0
DESKTOP_BACKEND_INSTALL_TIMEOUT_SECONDS = 300.0
DESKTOP_BACKEND_PROBE_TIMEOUT_SECONDS = 15.0
DESKTOP_BACKEND_PROCESS_DRAIN_TIMEOUT_SECONDS = 10.0
# Per phase of stopping an installer tree. Two phases fit inside the grace a
# managed stop gives the owning UI before it escalates to SIGKILL (5 s).
DESKTOP_BACKEND_TERMINATE_TIMEOUT_SECONDS = 1.5
DESKTOP_BACKEND_INSTALL_LABEL = "desktop backend install"
INSTALL_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DesktopBackendSpec:
    package: str
    package_path: tuple[str, ...]


@dataclass(frozen=True)
class DesktopBackendToolchain:
    runtime_root: Path
    backends_root: Path
    node: Path
    npm_cli: Path


@dataclass(frozen=True)
class DesktopBackendInstallResult:
    backend: str
    package: str
    version: str
    path: str
    output: str | None


class DesktopBackendError(RuntimeError):
    def __init__(self, message: str, *, code: str, output: str | None = None):
        super().__init__(message)
        self.code = code
        self.output = output


BACKEND_SPECS: dict[str, DesktopBackendSpec] = {
    "codex": DesktopBackendSpec(
        package="@openai/codex",
        package_path=("@openai", "codex"),
    ),
    "claude": DesktopBackendSpec(
        package="@anthropic-ai/claude-code",
        package_path=("@anthropic-ai", "claude-code"),
    ),
    "opencode": DesktopBackendSpec(
        package="opencode-ai",
        package_path=("opencode-ai",),
    ),
}


ActivationCallback = Callable[[str], None]


def desktop_backend_toolchain(
    base_env: Mapping[str, str] | None = None,
) -> DesktopBackendToolchain | None:
    """Return the complete launcher-supplied private install contract."""

    from vibe.desktop_runtime import private_desktop_runtime_root

    runtime_root = private_desktop_runtime_root(base_env)
    backends_root = private_desktop_backends_root(base_env)
    node = private_desktop_node_bin(base_env)
    npm_cli = private_desktop_npm_cli(base_env)
    if runtime_root is None or backends_root is None or node is None or npm_cli is None:
        return None
    return DesktopBackendToolchain(
        runtime_root=runtime_root,
        backends_root=backends_root,
        node=node,
        npm_cli=npm_cli,
    )


def resolve_published_desktop_backend(
    backend: str,
    base_env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve one verified executable from its bounded ``current.json``."""

    if backend not in BACKEND_SPECS:
        return None
    root = private_desktop_backends_root(base_env)
    if root is None:
        return None
    backend_root = root / backend
    descriptor_path = backend_root / "current.json"
    try:
        if descriptor_path.is_symlink() or not descriptor_path.is_file():
            return None
        if descriptor_path.stat().st_size > MAX_DESCRIPTOR_BYTES:
            return None
        payload = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    spec = BACKEND_SPECS[backend]
    if (
        payload.get("schema_version") != CURRENT_DESCRIPTOR_SCHEMA_VERSION
        or payload.get("backend") != backend
        or payload.get("package") != spec.package
        or not isinstance(payload.get("version"), str)
    ):
        return None

    executable = _descriptor_relative_path(payload.get("executable"))
    if executable is None or not executable.parts or executable.parts[0] != backend:
        return None
    candidate = root / executable
    if not _verified_direct_executable(candidate, root=backend_root, require_native=True):
        return None
    try:
        _backend_runtime_path_entries(backend, candidate, root=backend_root)
    except DesktopBackendError:
        return None
    return str(candidate.resolve(strict=True))


def is_desktop_backend_path(
    path: str | os.PathLike[str] | None,
    base_env: Mapping[str, str] | None = None,
) -> bool:
    """Whether *path* is contained by the mutable private backend root."""

    if not path:
        return False
    root = private_desktop_backends_root(base_env)
    if root is None:
        return False
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def desktop_backend_subprocess_environment(
    backend: str,
    executable: str | os.PathLike[str],
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return the launch environment required by an app-private backend.

    The desktop shell starts before a backend may be installed, so mutable
    backend support directories cannot be added to the Runtime's process-wide
    ``PATH``. Derive them from the exact configured executable at the backend
    process boundary instead.
    """

    source = os.environ if base_env is None else base_env
    root = private_desktop_backends_root(source)
    if (
        backend not in BACKEND_SPECS
        or root is None
        or not is_desktop_backend_path(executable, source)
    ):
        return None
    backend_root = root / backend
    candidate = Path(executable).expanduser()
    if not _verified_direct_executable(candidate, root=backend_root, require_native=True):
        raise DesktopBackendError(
            f"Published {backend} executable failed validation.",
            code="invalid_executable",
        )
    runtime_paths = _backend_runtime_path_entries(backend, candidate, root=backend_root)
    environment = dict(source)
    if runtime_paths:
        _prepend_path_entries(environment, runtime_paths)
    return environment


def install_desktop_backend(
    backend: str,
    *,
    base_env: Mapping[str, str] | None = None,
    activate: ActivationCallback | None = None,
    timeout_seconds: float = DESKTOP_BACKEND_INSTALL_TIMEOUT_SECONDS,
) -> DesktopBackendInstallResult:
    """Install or update one backend and atomically publish its descriptor.

    ``activate`` lets the config owner persist the final executable while the
    cross-process install lock is still held. The descriptor is published first
    so configuration can never point at an unpublished release.
    """

    spec = BACKEND_SPECS.get(backend)
    if spec is None:
        raise DesktopBackendError(
            f"Unknown desktop backend: {backend}",
            code="unknown_backend",
        )
    toolchain = desktop_backend_toolchain(base_env)
    if toolchain is None:
        raise DesktopBackendError(
            "The desktop backend installer is unavailable.",
            code="desktop_toolchain_unavailable",
        )

    backend_root = toolchain.backends_root / backend
    _prepare_backend_root(toolchain.backends_root, backend_root)
    lock = _claim_backend_root(backend_root, timeout_seconds=DESKTOP_BACKEND_LOCK_TIMEOUT_SECONDS)

    staging = backend_root / f".staging-{uuid.uuid4().hex}"
    # The lock says "no installer is working in this root". An installer tree
    # that could not be shown gone may still be writing into staging, so both
    # the lock and staging stay as they are until this process exits.
    installer_undrained = False
    try:
        staging.mkdir(mode=0o700)
        user_config = staging / "npm-user.conf"
        global_config = staging / "npm-global.conf"
        user_config.write_text("", encoding="utf-8")
        global_config.write_text("", encoding="utf-8")
        command = [
            str(toolchain.node),
            str(toolchain.npm_cli),
            "install",
            "--prefix",
            str(staging),
            "--no-save",
            "--package-lock=false",
            "--omit=dev",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            f"--registry={NPM_REGISTRY}",
            spec.package,
        ]
        logger.info("Installing desktop backend package %s", spec.package)
        completed = _run_command(
            command,
            cwd=staging,
            env=_npm_environment(toolchain, staging, user_config, global_config, base_env),
            timeout_seconds=timeout_seconds,
        )
        output = _bounded_output(completed.stdout, completed.stderr)
        if completed.returncode != 0:
            raise DesktopBackendError(
                f"Desktop backend install failed (exit code {completed.returncode})",
                code="npm_install_failed",
                output=output,
            )
        _remove_internal_directory(staging / ".npm-cache", staging)
        user_config.unlink(missing_ok=True)
        global_config.unlink(missing_ok=True)

        package_dir = staging / "node_modules" / Path(*spec.package_path)
        version = _installed_package_version(package_dir, spec.package)
        executable = _installed_native_executable(backend, spec, staging, package_dir)
        runtime_paths = _backend_runtime_path_entries(backend, executable, root=staging)
        _verify_backend_executable(
            backend,
            executable,
            root=staging,
            env=_backend_command_environment(
                toolchain,
                base_env,
                runtime_paths=runtime_paths,
            ),
        )

        releases_root = backend_root / "releases"
        releases_root.mkdir(mode=0o700, exist_ok=True)
        release_root = releases_root / uuid.uuid4().hex
        os.replace(staging, release_root)
        published_executable = release_root / executable.relative_to(staging)

        relative_executable = published_executable.relative_to(toolchain.backends_root).as_posix()
        descriptor = {
            "schema_version": CURRENT_DESCRIPTOR_SCHEMA_VERSION,
            "backend": backend,
            "package": spec.package,
            "version": version,
            "executable": relative_executable,
        }
        descriptor_path = backend_root / "current.json"
        previous_descriptor = _read_descriptor_bytes(descriptor_path)
        try:
            # The descriptor is the publication boundary. Configuration may
            # point at a release only after this atomic replacement succeeds,
            # so a crash can never activate an unpublished executable.
            _write_current_descriptor(descriptor_path, descriptor)
            if activate is not None:
                activate(str(published_executable))
        except Exception:
            _restore_descriptor_bytes(descriptor_path, previous_descriptor)
            # Once staging has been renamed, the outer cleanup can no longer
            # reach it. Remove the release only after restoring the descriptor;
            # if restoration itself fails, retaining the files is safer than a
            # descriptor that points at a deleted executable.
            _remove_staging_directory(release_root, backend_root)
            raise

        return DesktopBackendInstallResult(
            backend=backend,
            package=spec.package,
            version=version,
            path=str(published_executable),
            output=output,
        )
    except DesktopBackendError as exc:
        installer_undrained = exc.code == "install_drain_failed"
        raise
    except Exception as exc:
        raise DesktopBackendError(
            f"Desktop backend install failed: {exc}",
            code="desktop_install_failed",
        ) from exc
    finally:
        if installer_undrained:
            logger.error(
                "Keeping %s held: a %s process tree could not be shown to have exited",
                lock.lock_path,
                DESKTOP_BACKEND_INSTALL_LABEL,
            )
        else:
            # The record goes only with its staging, and both before the lock.
            if _remove_staging_directory(staging, backend_root):
                _forget_installer_record(_installer_record_path(staging))
            lock.release()


def _prepare_backend_root(root: Path, backend_root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DesktopBackendError("Invalid desktop backend root.", code="invalid_backend_root")
    backend_root.mkdir(mode=0o700, exist_ok=True)
    if backend_root.is_symlink() or not backend_root.is_dir():
        raise DesktopBackendError("Invalid desktop backend directory.", code="invalid_backend_root")
    if os.name != "nt":
        root.chmod(0o700)
        backend_root.chmod(0o700)


def _npm_environment(
    toolchain: DesktopBackendToolchain,
    staging: Path,
    user_config: Path,
    global_config: Path,
    base_env: Mapping[str, str] | None,
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = _safe_process_environment(source)
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    node_dir = str(toolchain.node.parent)
    env["PATH"] = os.pathsep.join([node_dir, *(entry for entry in path_entries if entry != node_dir)])
    env.update(
        {
            "NPM_CONFIG_PREFIX": str(staging),
            "NPM_CONFIG_CACHE": str(staging / ".npm-cache"),
            "NPM_CONFIG_USERCONFIG": str(user_config),
            "NPM_CONFIG_GLOBALCONFIG": str(global_config),
            "NPM_CONFIG_REGISTRY": NPM_REGISTRY,
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        }
    )
    return env


def _backend_command_environment(
    toolchain: DesktopBackendToolchain,
    base_env: Mapping[str, str] | None,
    *,
    runtime_paths: tuple[Path, ...] = (),
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = _safe_process_environment(source)
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    node_dir = str(toolchain.node.parent)
    env["PATH"] = os.pathsep.join([node_dir, *(entry for entry in entries if entry != node_dir)])
    _prepend_path_entries(env, runtime_paths)
    return env


def _prepend_path_entries(environment: dict[str, str], paths: tuple[Path, ...]) -> None:
    entries = [entry for entry in environment.get("PATH", "").split(os.pathsep) if entry]
    prefixes = [str(path) for path in paths]
    environment["PATH"] = os.pathsep.join(
        [*prefixes, *(entry for entry in entries if entry not in prefixes)]
    )


def _safe_process_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LOCALAPPDATA",
        "NODE_EXTRA_CA_CERTS",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    return {
        key: value
        for key, value in source.items()
        if key in allowed or key.startswith("LC_")
    }


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    owned = _spawn_owned_installer(command, cwd=cwd, env=env)
    process = owned.process
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            signal_process_tree(process, KILL_SIGNAL, logger, DESKTOP_BACKEND_INSTALL_LABEL)
            stdout, stderr = process.communicate(
                timeout=DESKTOP_BACKEND_PROCESS_DRAIN_TIMEOUT_SECONDS,
            )
            raise DesktopBackendError(
                "Desktop backend install timed out.",
                code="install_timeout",
                output=_bounded_output(stdout, stderr),
            ) from exc
    finally:
        # npm exiting is not its tree exiting: anything it started is still
        # in its process group, or at least still carries the marker.
        if not _settle_owned_installer(owned):
            raise DesktopBackendError(
                "Desktop backend installer processes did not exit.",
                code="install_drain_failed",
            )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


@dataclass
class _OwnedInstaller:
    """An installer tree this process started, and so must see gone."""

    process: subprocess.Popen[str]
    worker_fingerprint: str
    identity: PersistedProcessIdentity | None
    settled: bool = False


# The owner-side registry. Its lock is held across spawn and registration, so
# a drain that starts meanwhile waits and then sees the new tree too.
_OWNED_INSTALLERS: dict[str, _OwnedInstaller] = {}
_OWNED_INSTALLERS_LOCK = threading.RLock()
_INSTALLERS_CLOSED = False


_STAGING_NAME = re.compile(r"\.staging-[0-9a-f]{32}")


def _installer_records_dir() -> Path:
    return paths.get_runtime_dir() / "desktop-backend-installs"


def _installer_record_path(staging: Path) -> Path:
    return _installer_records_dir() / f"{staging.name.removeprefix('.staging-')}.json"


def _write_installer_record(
    record_path: Path,
    *,
    worker_fingerprint: str,
    identity: PersistedProcessIdentity | None,
    staging: Path,
) -> None:
    payload = {
        "schema_version": INSTALL_RECORD_SCHEMA_VERSION,
        "worker_fingerprint": worker_fingerprint,
        "identity": None if identity is None else serialize_process_identity(identity),
        "staging": str(staging),
    }
    write_atomic(record_path, json.dumps(payload, separators=(",", ":")))


def _forget_installer_record(record_path: Path) -> None:
    try:
        record_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove %s record %s", DESKTOP_BACKEND_INSTALL_LABEL, record_path, exc_info=True)


def _spawn_owned_installer(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> _OwnedInstaller:
    # ``cwd`` is the install's staging directory. Its record names it, and goes
    # only with it: the install removes both, or a claim of the root does.
    marker = new_process_identity_marker()
    worker_fingerprint = fingerprint_process_marker(marker)
    record_path = _installer_record_path(cwd)
    child_env = dict(env)
    child_env[PROCESS_IDENTITY_ENV] = marker
    # Every member inherits these, so a process scan finds the whole tree and
    # its owner without its record: the stops and the claim decide by them.
    child_env[DESKTOP_ROLE_ENV] = DESKTOP_INSTALLER_ROLE
    child_env[DESKTOP_INSTALLER_OWNER_ENV] = _installer_owner()
    runtime_id = desktop_runtime_id()
    if runtime_id is not None:
        child_env[DESKTOP_RUNTIME_ID_ENV] = runtime_id
    with _OWNED_INSTALLERS_LOCK:
        if _INSTALLERS_CLOSED:
            raise DesktopBackendError(
                "Avibe is shutting down; the desktop backend install was not started.",
                code="install_shutting_down",
            )
        # Recorded before the spawn, so a stopper can find the tree by its
        # marker even if this process dies before it learns the pid.
        _write_installer_record(record_path, worker_fingerprint=worker_fingerprint, identity=None, staging=cwd)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **isolated_subprocess_kwargs(),
        )
        identity = capture_spawned_process_identity(process.pid, marker)
        owned = _OwnedInstaller(process, worker_fingerprint, identity)
        _OWNED_INSTALLERS[worker_fingerprint] = owned
        if identity is not None:
            try:
                _write_installer_record(
                    record_path, worker_fingerprint=worker_fingerprint, identity=identity, staging=cwd
                )
            except OSError:
                # The marker alone still finds the tree; only the pid is lost.
                logger.warning("Could not record the %s pid=%s", DESKTOP_BACKEND_INSTALL_LABEL, process.pid, exc_info=True)
    return owned


def _stop_owned_installer_leader(process: subprocess.Popen[str]) -> bool:
    """Stop and reap the installer this process spawned, through its own handle.

    The handle is what makes this exact: ``wait`` reaps the leader, so the reap
    that follows never mistakes an unreaped zombie for a live member.
    """

    for sig in (signal.SIGTERM, KILL_SIGNAL):
        if process.poll() is not None:
            return True
        signal_process_tree(process, sig, logger, DESKTOP_BACKEND_INSTALL_LABEL)
        try:
            process.wait(timeout=DESKTOP_BACKEND_TERMINATE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            continue
        return True
    return process.poll() is not None


def _await_installer_group_exit(identity: PersistedProcessIdentity | None) -> None:
    """Give members the group signal reached their phase to exit and be reaped.

    Until its reaper collects it, an exited member still occupies the group
    and no longer shows its marker, which would leave the reap unable to
    confirm the group is ours or gone.
    """

    if identity is None or os.name == "nt":
        return
    deadline = time.monotonic() + DESKTOP_BACKEND_TERMINATE_TIMEOUT_SECONDS
    while process_group_exists(identity.pid, logger, DESKTOP_BACKEND_INSTALL_LABEL):
        if time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _reap_installer_tree(
    worker_fingerprint: str,
    identity: PersistedProcessIdentity | None,
) -> bool:
    """Stop whatever is left of an installer tree; True once it is shown gone."""

    outcomes = []
    if identity is not None:
        outcomes.append(
            reap_orphaned_process_tree(logger, DESKTOP_BACKEND_INSTALL_LABEL, expected_identity=identity)
        )
    # The marker is the authority: it also finds members that left the group.
    outcomes.append(
        reap_marked_processes(
            logger,
            DESKTOP_BACKEND_INSTALL_LABEL,
            worker_fingerprint=worker_fingerprint,
            terminate_timeout=DESKTOP_BACKEND_TERMINATE_TIMEOUT_SECONDS,
        )
    )
    return "unconfirmed" not in outcomes


def _settle_owned_installer(owned: _OwnedInstaller) -> bool:
    with _OWNED_INSTALLERS_LOCK:
        if owned.settled:
            return True
        try:
            gone = _stop_owned_installer_leader(owned.process)
            if gone:
                _await_installer_group_exit(owned.identity)
                gone = _reap_installer_tree(owned.worker_fingerprint, owned.identity)
        except Exception:
            logger.exception("Unexpected error stopping the %s tree", DESKTOP_BACKEND_INSTALL_LABEL)
            gone = False
        if not gone:
            logger.error(
                "Could not show the %s tree pid=%s exited; keeping its record",
                DESKTOP_BACKEND_INSTALL_LABEL,
                owned.process.pid,
            )
            return False
        owned.settled = True
        _OWNED_INSTALLERS.pop(owned.worker_fingerprint, None)
        return True


def drain_desktop_backend_installs() -> bool:
    """Stop every installer tree this process started, before it exits.

    For the owning process's shutdown: after this call it starts no new
    installer. Returns False when a tree could not be shown gone. Its durable
    record stays either way until the install removes its staging; one left
    behind is cleared by the next claim of its backend root.
    """

    global _INSTALLERS_CLOSED
    with _OWNED_INSTALLERS_LOCK:
        _INSTALLERS_CLOSED = True
        owned = list(_OWNED_INSTALLERS.values())
    drained = True
    for installer in owned:
        drained = _settle_owned_installer(installer) and drained
    return drained


@dataclass(frozen=True)
class _InstallerRecord:
    path: Path
    worker_fingerprint: str
    identity: PersistedProcessIdentity | None
    staging: Path

    @property
    def backend_root(self) -> Path:
        return self.staging.parent


def _recorded_staging(value: Any) -> Path | None:
    """The staging directory a record names, if an install could have made it.

    Only ``<backends root>/<backend>/.staging-<hex>`` qualifies, so removing a
    recorded staging directory can never reach a release, a descriptor or
    anything outside a backend root.
    """

    if not isinstance(value, str) or os.path.normpath(value) != value:
        return None
    staging = Path(value)
    if not staging.is_absolute() or not _STAGING_NAME.fullmatch(staging.name):
        return None
    return staging if staging.parent.name in BACKEND_SPECS else None


def _read_installer_record(record_path: Path) -> _InstallerRecord | None:
    """The record at ``record_path``, or ``None`` when what it holds is invalid.

    Raises ``OSError`` when it cannot be read: such a record is unknown, not
    invalid, and must not be discarded.
    """

    text = record_path.read_bytes()
    try:
        payload = json.loads(text.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != INSTALL_RECORD_SCHEMA_VERSION:
        return None
    worker_fingerprint = payload.get("worker_fingerprint")
    raw_identity = payload.get("identity")
    staging = _recorded_staging(payload.get("staging"))
    if not is_valid_worker_fingerprint(worker_fingerprint) or staging is None:
        return None
    identity = None
    if raw_identity is not None:
        identity = process_identity_from_payload(
            raw_identity,
            raw_identity.get("pid") if isinstance(raw_identity, dict) else None,
        )
        if identity is None or identity.worker_fingerprint != worker_fingerprint:
            return None
    return _InstallerRecord(record_path, worker_fingerprint, identity, staging)


def _installer_records() -> list[_InstallerRecord]:
    """Every valid installer record, for a claim to clear the staging it names.

    Raises ``OSError`` when the directory or a record in it cannot be read:
    the listing is then unknown and nothing is discarded. A record proven
    invalid is discarded only once every record has been read.
    """

    try:
        with os.scandir(_installer_records_dir()) as entries:
            record_paths = sorted(Path(entry.path) for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        # No install has recorded anything yet.
        return []
    records: list[_InstallerRecord] = []
    invalid: list[Path] = []
    for record_path in record_paths:
        try:
            record = _read_installer_record(record_path)
        except FileNotFoundError:
            # Its install finished after the listing.
            continue
        if record is None:
            invalid.append(record_path)
        else:
            records.append(record)
    for record_path in invalid:
        # Nothing in it can identify a process, so it authorises nothing.
        logger.warning("Discarding invalid %s record %s", DESKTOP_BACKEND_INSTALL_LABEL, record_path)
        _forget_installer_record(record_path)
    return records


def _clear_abandoned_install(record: _InstallerRecord) -> bool:
    """Reap a recorded installer tree, then remove its staging and its record.

    True once the tree is shown gone. The record stays until its staging is.
    """

    try:
        gone = _reap_installer_tree(record.worker_fingerprint, record.identity)
    except Exception:
        logger.exception("Unexpected error stopping the %s recorded in %s", DESKTOP_BACKEND_INSTALL_LABEL, record.path)
        gone = False
    if not gone:
        logger.error("Could not stop the %s recorded in %s", DESKTOP_BACKEND_INSTALL_LABEL, record.path)
        return False
    if _remove_staging_directory(record.staging, record.backend_root):
        _forget_installer_record(record.path)
    return True


def _installer_owner() -> str:
    from vibe import runtime

    started_at = runtime.process_create_time(os.getpid())
    if started_at is None:
        raise DesktopBackendError(
            "Avibe could not identify itself as the owner of a desktop backend install.",
            code="desktop_install_failed",
        )
    return f"{os.getpid()}:{started_at!r}"


def _installer_owner_gone(owner: str, runtime_id: str | None) -> bool | None:
    """Whether the process named by the owner value of a ``runtime_id`` tree has exited.

    The owner is gone when its pid is not alive, and alive while that pid
    keeps its create time. macOS can shift the create time psutil shows for a
    process that keeps running, so another one alone proves nothing: the pid
    was reused only when the process now holding it is readable and is not a
    UI of that Runtime, as every owner is. ``None`` when the value is
    malformed, or the create time differs and the process cannot be read.
    """

    from vibe import runtime

    pid_text, _, started_text = owner.partition(":")
    try:
        pid, started_at = int(pid_text), float(started_text)
    except ValueError:
        return None
    if pid <= 0 or not math.isfinite(started_at):
        return None
    if not runtime.pid_alive(pid):
        return True
    if runtime.process_create_time(pid) == started_at:
        return False
    is_ui = runtime.is_desktop_ui(pid, runtime_id)
    return None if is_ui is None else not is_ui


def reap_abandoned_desktop_backend_installs(runtime_id: str | None = None) -> bool:
    """Stop every installer tree of a Runtime whose owner has exited.

    ``runtime_id`` defaults to this process's. Installs run only in the UI,
    which drains its own trees as it exits; one killed first leaves its tree
    running. A process scan finds such trees by the role and Runtime id every
    member inherits, and each tree names its owner, so no record or pidfile
    decides, whichever process asks. Staging and records stay for the next
    claim of their backend root. Returns False when a tree could not be shown
    gone, or its owner could not be told alive or gone.
    """

    if runtime_id is None:
        runtime_id = desktop_runtime_id()
    abandoned: set[str] = set()
    for process in processes_carrying_marker(
        fingerprint_process_marker(DESKTOP_INSTALLER_ROLE),
        marker_env=DESKTOP_ROLE_ENV,
    ):
        try:
            env = process.environ()
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            logger.error("Could not inspect the %s pid=%s", DESKTOP_BACKEND_INSTALL_LABEL, process.pid, exc_info=True)
            return False
        if desktop_runtime_id(env) != runtime_id:
            continue
        marker = env.get(PROCESS_IDENTITY_ENV, "")
        if not marker or not marker.isascii():
            logger.error("The %s pid=%s carries no identity marker", DESKTOP_BACKEND_INSTALL_LABEL, process.pid)
            return False
        gone = _installer_owner_gone(env.get(DESKTOP_INSTALLER_OWNER_ENV, ""), runtime_id)
        if gone is None:
            logger.error("Could not tell whether the owner of the %s pid=%s is alive", DESKTOP_BACKEND_INSTALL_LABEL, process.pid)
            return False
        if gone:
            abandoned.add(fingerprint_process_marker(marker))
    # By its marker, which also finds members started since the scan.
    return all([_reap_installer_tree(fingerprint, None) for fingerprint in sorted(abandoned)])


def _claim_backend_root(backend_root: Path, *, timeout_seconds: float) -> MigrationFileLock:
    """Take ``backend_root``'s install lock, with no abandoned installer left in it.

    Every acquisition of ``.install.lock`` goes through here. Liveness comes
    from the process scan: every abandoned installer tree of this Runtime is
    reaped first. An install holds the lock from before its record exists
    until after it has removed its staging and then its record, so a record
    for this root seen under the lock names an install whose owner died
    first; its tree is reaped by its marker and its staging removed. When the
    records cannot be listed or read, nothing is removed; then, or when a
    tree cannot be shown gone, the lock is released and ``install_locked``
    raised. So is it when removal deleted the lock file while this waited.
    """

    lock_path = backend_root / ".install.lock"
    removed: list[bool] = []

    def names_the_lock_path(handle) -> bool:
        # Removal deletes a lock file while it holds it, so a waiter that gets
        # the lock then holds a file no other install would lock.
        try:
            same = os.path.samestat(os.fstat(handle.fileno()), os.stat(lock_path))
        except FileNotFoundError:
            same = False
        if not same:
            removed.append(True)
        return same

    lock = MigrationFileLock(lock_path, timeout_seconds=timeout_seconds, _handle_validator=names_the_lock_path)
    try:
        lock.acquire()
    except MigrationLockTimeout as exc:
        raise DesktopBackendError(
            f"Another {backend_root.name} install is already running.",
            code="install_locked",
        ) from exc
    except OSError as exc:
        if not removed:
            raise
        raise DesktopBackendError(
            f"The {backend_root.name} backend directory is being removed.",
            code="install_locked",
        ) from exc
    try:
        records = [record for record in _installer_records() if record.backend_root == backend_root]
        settled = reap_abandoned_desktop_backend_installs() and all(
            [_clear_abandoned_install(record) for record in records]
        )
    except OSError as exc:
        lock.release()
        raise DesktopBackendError(
            f"The {backend_root.name} install records could not be read.",
            code="install_locked",
        ) from exc
    except BaseException:
        lock.release()
        raise
    if not settled:
        lock.release()
        raise DesktopBackendError(
            f"A previous {backend_root.name} install could not be shown to have stopped.",
            code="install_locked",
        )
    return lock


def remove_desktop_backends(base_env: Mapping[str, str] | None = None) -> None:
    """Delete the app-private backend root once no installer can be writing into it.

    Every backend directory is claimed the way an install claims it, which
    reaps the installer trees abandoned in it, and nothing is deleted until all
    of them are held. Nothing is deleted recursively once a lock is released,
    so an install that begins meanwhile keeps its files, and fails the
    removal. Raises ``DesktopBackendError``: ``install_locked`` with nothing
    deleted, and any other code when part of the root may remain.
    """

    root = private_desktop_backends_root(base_env)
    if root is None:
        raise DesktopBackendError("The desktop backend root is unavailable.", code="invalid_backend_root")
    if not os.path.lexists(root):
        return
    if root.is_symlink() or not root.is_dir():
        raise DesktopBackendError("Invalid desktop backend root.", code="invalid_backend_root")
    claimed: list[tuple[Path, MigrationFileLock]] = []
    try:
        try:
            entries = sorted(root.iterdir())
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    lock = _claim_backend_root(entry, timeout_seconds=DESKTOP_BACKEND_REMOVAL_LOCK_TIMEOUT_SECONDS)
                    claimed.append((entry, lock))
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    for member in entry.iterdir():
                        if member.name != ".install.lock":
                            _remove_path(member)
                else:
                    _remove_path(entry)
            for entry, lock in claimed:
                # A waiter that then gets the lock finds its file gone, and
                # refuses. Windows deletes no open file, so the lock file goes
                # once released, and stays while any installer has it open.
                if os.name == "nt":
                    lock.release()
                (entry / ".install.lock").unlink(missing_ok=True)
                entry.rmdir()
            # A directory an install created since the listing keeps the root.
            root.rmdir()
        finally:
            for _entry, lock in reversed(claimed):
                lock.release()
    except OSError as exc:
        raise DesktopBackendError(
            f"The desktop backend root could not be removed: {exc}",
            code="backend_removal_failed",
        ) from exc


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _installed_package_version(package_dir: Path, expected_package: str) -> str:
    manifest = package_dir / "package.json"
    try:
        if manifest.is_symlink() or manifest.stat().st_size > MAX_DESCRIPTOR_BYTES:
            raise ValueError("invalid package manifest")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DesktopBackendError(
            "Installed package metadata is missing or invalid.",
            code="invalid_package",
        ) from exc
    name = payload.get("name") if isinstance(payload, dict) else None
    version = payload.get("version") if isinstance(payload, dict) else None
    if name != expected_package or not isinstance(version, str) or not version or len(version) > 128:
        raise DesktopBackendError(
            "Installed package identity does not match the requested backend.",
            code="invalid_package",
        )
    return version


def _installed_native_executable(
    backend: str,
    spec: DesktopBackendSpec,
    staging: Path,
    package_dir: Path,
) -> Path:
    os_name, arch = _native_target()
    if backend == "codex":
        executable_name = "codex.exe" if os_name == "win32" else "codex"
        target_package = staging / "node_modules" / "@openai" / f"codex-{os_name}-{arch}"
        target_roots = [target_package, package_dir / "node_modules" / "@openai" / target_package.name]
        candidates = [
            candidate
            for target_root in target_roots
            for candidate in target_root.glob(f"vendor/*/bin/{executable_name}")
        ]
    elif backend == "claude":
        executable_name = "claude.exe" if os_name == "win32" else "claude"
        target_package = staging / "node_modules" / "@anthropic-ai" / f"claude-code-{os_name}-{arch}"
        target_roots = [target_package, package_dir / "node_modules" / "@anthropic-ai" / target_package.name]
        candidates = [target_root / executable_name for target_root in target_roots]
    elif backend == "opencode":
        target_os = "windows" if os_name == "win32" else os_name
        executable_name = "opencode.exe" if os_name == "win32" else "opencode"
        target_package = staging / "node_modules" / f"opencode-{target_os}-{arch}"
        target_roots = [target_package, package_dir / "node_modules" / target_package.name]
        candidates = [target_root / "bin" / executable_name for target_root in target_roots]
    else:  # BACKEND_SPECS and callers keep this unreachable.
        candidates = []

    unique: dict[Path, Path] = {}
    for candidate in candidates:
        try:
            unique[candidate.resolve(strict=True)] = candidate
        except OSError:
            continue
    if len(unique) != 1:
        raise DesktopBackendError(
            f"Installed {backend} package contains no unique target-native executable.",
            code="native_executable_missing",
        )
    executable = next(iter(unique))
    if not _verified_direct_executable(executable, root=staging, require_native=True):
        raise DesktopBackendError(
            f"Installed {backend} executable failed validation.",
            code="invalid_executable",
        )
    return executable


def _backend_runtime_path_entries(
    backend: str,
    executable: Path,
    *,
    root: Path,
) -> tuple[Path, ...]:
    if backend != "codex":
        return ()

    windows = os.name == "nt"
    target_root = executable.parent.parent
    executable_paths = [
        target_root
        / "bin"
        / ("codex-code-mode-host.exe" if windows else "codex-code-mode-host"),
        target_root / "codex-path" / ("rg.exe" if windows else "rg"),
    ]
    if windows:
        executable_paths.extend(
            [
                target_root / "codex-resources" / "codex-command-runner.exe",
                target_root / "codex-resources" / "codex-windows-sandbox-setup.exe",
            ]
        )
    else:
        executable_paths.append(
            target_root / "codex-resources" / "zsh" / "bin" / "zsh"
        )
        if platform.system().lower() == "linux":
            executable_paths.append(target_root / "codex-resources" / "bwrap")

    package_manifest = target_root / "codex-package.json"
    if not _verified_direct_regular_file(package_manifest, root=root) or not all(
        _verified_direct_executable(path, root=root, require_native=True)
        for path in executable_paths
    ):
        raise DesktopBackendError(
            "Installed Codex package is missing a required native runtime helper.",
            code="incomplete_backend_runtime",
        )
    return (executable_paths[1].parent.resolve(strict=True),)


def _verify_backend_executable(
    backend: str,
    executable: Path,
    *,
    root: Path,
    env: Mapping[str, str],
) -> None:
    if not _verified_direct_executable(executable, root=root, require_native=True):
        raise DesktopBackendError(
            f"Installed {backend} executable failed validation.",
            code="invalid_executable",
        )
    # Spawned as an installer, so the probe is owned, recorded and scanned
    # like the npm tree before it: no stop or drain can miss it.
    try:
        result = _run_command(
            [str(executable), "--version"],
            cwd=root,
            env=env,
            timeout_seconds=DESKTOP_BACKEND_PROBE_TIMEOUT_SECONDS,
        )
    except DesktopBackendError as exc:
        if exc.code != "install_timeout":
            raise
        raise DesktopBackendError(
            f"Installed {backend} executable did not report a version in time.",
            code="executable_probe_failed",
            output=exc.output,
        ) from exc
    except OSError as exc:
        raise DesktopBackendError(
            f"Installed {backend} executable could not be started.",
            code="executable_probe_failed",
        ) from exc
    if result.returncode != 0 or not _version_from_output(result.stdout, result.stderr):
        raise DesktopBackendError(
            f"Installed {backend} executable did not report a valid version.",
            code="executable_probe_failed",
            output=_bounded_output(result.stdout, result.stderr),
        )


def _verified_direct_executable(path: Path, *, root: Path, require_native: bool) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        relative = path.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        if path.suffix.lower() in {".cmd", ".ps1", ".js"}:
            return False
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            return False
        return not require_native or _has_native_magic(resolved)
    except (OSError, ValueError):
        return False


def _verified_direct_regular_file(path: Path, *, root: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        relative = path.relative_to(root)
        cursor = root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                return False
        return True
    except (OSError, ValueError):
        return False


def _has_native_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic.startswith(b"MZ") or magic == b"\x7fELF" or magic in {
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }


def _native_target() -> tuple[str, str]:
    if os.name == "nt":
        os_name = "win32"
    elif platform.system().lower() == "darwin":
        os_name = "darwin"
    elif platform.system().lower() == "linux":
        os_name = "linux"
    else:
        raise DesktopBackendError("Unsupported desktop platform.", code="unsupported_platform")
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise DesktopBackendError("Unsupported desktop architecture.", code="unsupported_platform")
    return os_name, arch


def _descriptor_relative_path(value: Any) -> Path | None:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        return None
    if any(not part or ":" in part for part in relative.parts):
        return None
    return Path(*relative.parts)


def _write_current_descriptor(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("desktop backend descriptor exceeds size limit")
    _write_descriptor_bytes(path, encoded)


def _read_descriptor_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DESCRIPTOR_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _restore_descriptor_bytes(path: Path, previous: bytes | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        return
    _write_descriptor_bytes(path, previous)


def _write_descriptor_bytes(path: Path, encoded: bytes) -> None:
    if len(encoded) > MAX_DESCRIPTOR_BYTES:
        raise ValueError("desktop backend descriptor exceeds size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".current-", suffix=".json", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_staging_directory(staging: Path, backend_root: Path) -> bool:
    """Remove ``staging`` if it lies under ``backend_root``; True once it is gone."""

    try:
        staging.relative_to(backend_root)
    except ValueError:
        return False
    try:
        if staging.is_symlink():
            staging.unlink(missing_ok=True)
        elif staging.exists():
            shutil.rmtree(staging)
    except OSError:
        logger.warning("Failed to remove desktop backend staging directory %s", staging)
    return not os.path.lexists(staging)


def _remove_internal_directory(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError:
        return
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _version_from_output(stdout: str | None, stderr: str | None) -> str | None:
    import re

    match = re.search(r"\d+(?:\.\d+){1,3}(?:[-+][\w.-]+)?", f"{stdout or ''} {stderr or ''}")
    return match.group(0) if match else None


def _bounded_output(stdout: str | None, stderr: str | None) -> str | None:
    output = (stdout or "") + ("\n" + stderr if stderr else "")
    output = output.strip()
    if len(output) > MAX_INSTALL_OUTPUT_CHARS:
        output = "...(truncated)\n" + output[-MAX_INSTALL_OUTPUT_CHARS:]
    return output or None
