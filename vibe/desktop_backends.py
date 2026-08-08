"""App-private agent backend installation for the self-contained desktop app."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from core.process_isolation import KILL_SIGNAL, isolated_subprocess_kwargs, signal_process_tree
from storage.lock import MigrationFileLock, MigrationLockTimeout
from vibe.desktop_runtime import (
    private_desktop_backends_root,
    private_desktop_node_bin,
    private_desktop_npm_cli,
)


logger = logging.getLogger(__name__)

CURRENT_DESCRIPTOR_SCHEMA_VERSION = 1
MAX_DESCRIPTOR_BYTES = 16 * 1024
MAX_INSTALL_OUTPUT_CHARS = 8192
NPM_REGISTRY = "https://registry.npmjs.org/"


@dataclass(frozen=True)
class DesktopBackendSpec:
    package: str
    package_path: tuple[str, ...]
    package_executable: tuple[str, ...] | None = None


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
        package_executable=("bin", "claude.exe"),
    ),
    "opencode": DesktopBackendSpec(
        package="opencode-ai",
        package_path=("opencode-ai",),
        package_executable=("bin", "opencode.exe"),
    ),
}


ActivationRollback = Callable[[], None]
ActivationCallback = Callable[[str], ActivationRollback | None]


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


def install_desktop_backend(
    backend: str,
    *,
    base_env: Mapping[str, str] | None = None,
    activate: ActivationCallback | None = None,
    timeout_seconds: float = 300,
) -> DesktopBackendInstallResult:
    """Install or update one backend and atomically publish its descriptor.

    ``activate`` lets the config owner persist the final executable while the
    cross-process install lock is still held. It may return a rollback callback,
    which is invoked if descriptor publication fails after activation.
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
    lock = MigrationFileLock(backend_root / ".install.lock", timeout_seconds=30)
    try:
        lock.acquire()
    except MigrationLockTimeout as exc:
        raise DesktopBackendError(
            f"Another {backend} install is already running.",
            code="install_locked",
        ) from exc

    staging = backend_root / f".staging-{uuid.uuid4().hex}"
    activation_rollback: ActivationRollback | None = None
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
            "--no-audit",
            "--no-fund",
            f"--registry={NPM_REGISTRY}",
            spec.package,
        ]
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
        _verify_backend_executable(
            backend,
            executable,
            root=staging,
            env=_backend_command_environment(toolchain, base_env),
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
        if activate is not None:
            activation_rollback = activate(str(published_executable))
        try:
            _write_current_descriptor(backend_root / "current.json", descriptor)
        except Exception:
            if activation_rollback is not None:
                activation_rollback()
            raise

        return DesktopBackendInstallResult(
            backend=backend,
            package=spec.package,
            version=version,
            path=str(published_executable),
            output=output,
        )
    except DesktopBackendError:
        raise
    except Exception as exc:
        raise DesktopBackendError(
            f"Desktop backend install failed: {exc}",
            code="desktop_install_failed",
        ) from exc
    finally:
        _remove_staging_directory(staging, backend_root)
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
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    env = _safe_process_environment(source)
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry]
    node_dir = str(toolchain.node.parent)
    env["PATH"] = os.pathsep.join([node_dir, *(entry for entry in entries if entry != node_dir)])
    return env


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
    logger.info("Installing desktop backend package %s", command[-1])
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **isolated_subprocess_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        signal_process_tree(process, KILL_SIGNAL, logger, "desktop backend install")
        stdout, stderr = process.communicate(timeout=10)
        raise DesktopBackendError(
            "Desktop backend install timed out.",
            code="install_timeout",
            output=_bounded_output(stdout, stderr),
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
    if spec.package_executable is not None:
        candidates = [package_dir / Path(*spec.package_executable)]
    else:
        os_name, arch = _native_target()
        executable_name = "codex.exe" if os_name == "win32" else "codex"
        target_package = staging / "node_modules" / "@openai" / f"codex-{os_name}-{arch}"
        target_roots = [target_package, package_dir / "node_modules" / "@openai" / target_package.name]
        candidates = [
            candidate
            for target_root in target_roots
            for candidate in target_root.glob(f"vendor/*/bin/{executable_name}")
        ]

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
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            cwd=root,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
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


def _remove_staging_directory(staging: Path, backend_root: Path) -> None:
    try:
        staging.relative_to(backend_root)
    except ValueError:
        return
    try:
        if staging.is_symlink():
            staging.unlink(missing_ok=True)
        elif staging.exists():
            shutil.rmtree(staging)
    except OSError:
        logger.warning("Failed to remove desktop backend staging directory %s", staging)


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
