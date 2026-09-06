from __future__ import annotations

import contextlib
import filecmp
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import cast
from uuid import uuid4

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from vibe.package_shape import (
    CORE_PACKAGE_NAME,
    LEGACY_CORE_PACKAGE_NAME,
    MEMORY_PACKAGE_NAME as SHAPE_MEMORY_PACKAGE_NAME,
)

from config import paths as config_paths
from core.install_integrity import (
    IntegrityResult,
    isolated_probe_environment,
    run_isolated_probe,
    verify_python_environment,
)
from storage.lock import MigrationFileLock
from vibe import runtime as runtime_mod


logger = logging.getLogger(__name__)

PACKAGE_NAME = CORE_PACKAGE_NAME
LEGACY_PACKAGE_NAME = LEGACY_CORE_PACKAGE_NAME
MEMORY_PACKAGE_NAME = SHAPE_MEMORY_PACKAGE_NAME
MEMORY_EXTRA_NAME = "memory"
PIP_DOWNLOAD_DEST_PLACEHOLDER = "{avibe-pip-download-destination}"
# A GitHub-only pre-release publishes its wheels as release assets and nothing
# to PyPI, so an install taken from one can only be repaired from that same
# release. Which release that is comes from the installer's own PEP 610 record,
# never from the version string: `publish.yml` accepts official `vX.Y.ZrcN`
# tags and publishes them to PyPI, so a pre-release version says nothing about
# where its wheels live.
RELEASE_DOWNLOAD_BASE_URL = "https://github.com/avibe-bot/avibe/releases/download"
DEFAULT_UPDATE_METADATA_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
CURRENT_VIBE_EXECUTABLE_ENV = "VIBE_CURRENT_EXECUTABLE"
SHOW_RUNTIME_SKIP_ENV = "VIBE_INSTALL_SKIP_SHOW_RUNTIME"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
UV_FALLBACK_BIN_DIRS = (".local/bin", ".cargo/bin")
UPGRADE_INSTALL_TIMEOUT_SECONDS = 30 * 60
RESTART_PENDING_GRACE_SECONDS = 5 * 60
DEFERRED_ACTIVATION_TIMEOUT_SECONDS = 5 * 60
# A spec that names nothing but a package, so appending ``==<version>`` to it
# yields a requirement rather than a broken string. PEP 508 names only:
# anything with a path separator, a URL scheme, extras, a marker, or a version
# of its own is deliberately excluded. See `pinned_package_spec`.
_BARE_PACKAGE_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# PEP 440-ish parser: release + optional pre-release (a/b/rc) + optional
# post + optional dev, plus a local version segment (``+local``) that we
# accept but ignore for ordering. Word forms (alpha/beta/preview) are listed
# before their single-letter aliases so the alternation does not match a bare
# leading letter (e.g. the "a" in "alpha").
_VERSION_RE = re.compile(
    r"^\s*v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[._-]?(?P<pre>alpha|beta|preview|pre|rc|a|b|c)[._-]?(?P<pre_num>\d+)?)?"
    r"(?:[._-]?(?P<post>post)[._-]?(?P<post_num>\d+)?)?"
    r"(?:[._-]?(?P<dev>dev)[._-]?(?P<dev_num>\d+)?)?"
    r"(?:\+(?P<local>[a-z0-9._-]+))?\s*$",
    re.IGNORECASE,
)
# Relative ordering of pre-release stages: a/alpha < b/beta < c/rc/pre/preview.
_PRE_ORDER = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}


class MemoryRequirementUnreadableError(ValueError):
    """Persisted configuration cannot safely decide Memory package shape."""


class RestartState(str, Enum):
    """Closed restart-state vocabulary and its retention policy."""

    def __new__(cls, value: str, retention: str | None):
        member = str.__new__(cls, value)
        member._value_ = value
        member.retention = retention
        return member

    SCHEDULED = ("scheduled", "seed")
    RUNNING = ("running", "seed")
    SUCCEEDED = ("succeeded", "result")
    FAILED = ("failed", "result")
    ERROR = ("error", "result")
    CANCELLED = ("cancelled", "result")
    SKIPPED = ("skipped", "result")
    UNKNOWN = ("unknown", None)


@dataclass(frozen=True)
class UpgradePlan:
    """A validated package-install command and its execution metadata."""

    command: list[str]
    env: dict[str, str] | None
    method: str
    preflight_command: list[str] | None = None
    preflight_fallback_command: list[str] | None = None
    activation: "AtomicActivation | None" = None
    preflight_error: str | None = None


def execute_upgrade_plan(
    plan: UpgradePlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **run_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    """Run a validated plan after its non-mutating preflight.

    The caller owns package mutation sequencing. This helper deliberately does
    not acquire a lifecycle lock: callers can run the plan synchronously while
    the existing restart supervisor owns any later activation.
    """

    if plan.preflight_error:
        raise ValueError(plan.preflight_error)
    preflight = preflight_upgrade_plan(plan, run=run, **run_kwargs)
    if preflight is not None:
        if preflight.returncode != 0:
            return preflight

    return run(plan.command, env=plan.env, **run_kwargs)


def preflight_upgrade_plan(
    plan: UpgradePlan,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    **run_kwargs: object,
) -> subprocess.CompletedProcess[str] | None:
    """Resolve a plan without mutating the environment."""

    if plan.preflight_command is None:
        return None

    def run_preflight(command: list[str]) -> subprocess.CompletedProcess[str]:
        scratch_dir: str | None = None
        try:
            if PIP_DOWNLOAD_DEST_PLACEHOLDER in command:
                scratch_dir = tempfile.mkdtemp(prefix="avibe-pip-download-")
                command = [
                    scratch_dir if argument == PIP_DOWNLOAD_DEST_PLACEHOLDER else argument
                    for argument in command
                ]
            return run(command, env=plan.env, **run_kwargs)
        finally:
            if scratch_dir is not None:
                shutil.rmtree(scratch_dir, ignore_errors=True)

    preflight = run_preflight(plan.preflight_command)
    if (
        preflight.returncode == 0
        or plan.preflight_fallback_command is None
        or not _pip_dry_run_is_unsupported(preflight)
    ):
        return preflight

    return run_preflight(plan.preflight_fallback_command)


def _pip_dry_run_is_unsupported(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    return "--dry-run" in output and any(
        marker in output
        for marker in ("no such option", "unknown option", "unrecognized argument")
    )


@dataclass(frozen=True)
class AtomicActivation:
    """A validated candidate and the stable launcher it will replace."""

    launcher: Path
    candidate_launcher: Path
    source_generation: Path | None = None


def atomic_uv_install_root() -> Path:
    """Return the durable root for versioned uv tool environments."""

    return config_paths.get_vibe_remote_dir() / "runtime" / "install-generations"


@contextlib.contextmanager
def atomic_upgrade_lock():
    """Serialize staged installation, launcher activation, and pruning."""

    lock_path = atomic_uv_install_root().expanduser().parent / ".install.lock"
    with MigrationFileLock(lock_path, timeout_seconds=UPGRADE_INSTALL_TIMEOUT_SECONDS):
        yield


def restart_is_pending() -> bool:
    """Whether a scheduled restart still owns the next activation boundary."""

    path = runtime_mod.get_restart_status_path()
    payload = runtime_mod.read_json(path)
    if not isinstance(payload, Mapping):
        return False
    return restart_record_is_pending(payload, path)


def restart_record_is_pending(
    payload: Mapping[str, object],
    path: Path,
    *,
    grace_seconds: float = RESTART_PENDING_GRACE_SECONDS,
) -> bool:
    """Apply the restart ownership policy to one persisted status record."""

    try:
        state = RestartState(payload.get("state"))
    except (TypeError, ValueError):
        return False
    if state.retention != "seed":
        return False
    supervisor_pid = payload.get("supervisor_pid")
    if isinstance(supervisor_pid, int) and supervisor_pid > 0 and runtime_mod.pid_alive(supervisor_pid):
        started_at = payload.get("supervisor_started_at")
        if started_at is not None:
            current_started_at = runtime_mod.process_create_time(supervisor_pid)
            if current_started_at is not None:
                return current_started_at == started_at
        # Legacy records do not carry a process identity, and process metadata
        # can be unavailable. In both cases a reused PID must not block upgrades
        # for the lifetime of an unrelated process, so fall through to age.
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= grace_seconds


def _is_stable_launcher_path(launcher: Path) -> bool:
    if launcher.name.lower() not in {"vibe", "vibe.exe"}:
        return False
    # ``vibe_path`` has already been resolved to the command that launched this
    # installation. Its role, not the directory uv happened to use when it was
    # installed, makes it the stable activation point. The only unsafe shape is
    # the entry point inside the mutable uv tool environment itself.
    logical = os.path.normcase(os.path.abspath(os.path.expanduser(str(launcher))))
    atomic_root = os.path.normcase(os.path.abspath(os.path.expanduser(str(atomic_uv_install_root()))))
    uv_layout = logical.replace("\\", "/").lower()
    return "/uv/tools/" not in uv_layout and not Path(logical).is_relative_to(Path(atomic_root))


def launcher_is_current_process(launcher: Path | str) -> bool:
    """Whether this Windows process is holding the stable launcher open."""

    if os.name != "nt":
        return False
    target = os.path.normcase(os.path.abspath(os.path.expanduser(str(launcher))))
    # ``VIBE_CURRENT_EXECUTABLE`` is inherited by the long-lived Web process
    # from the CLI that started it. It identifies a PATH entry, not the image
    # currently holding the file open, so only argv[0] is authoritative here.
    current = os.path.normcase(os.path.abspath(os.path.expanduser(sys.argv[0])))
    return current == target


def _staged_uv_environment(vibe_path: str | None) -> tuple[Path, Path, AtomicActivation | None]:
    generation = atomic_uv_install_root() / uuid4().hex
    # 3.0.13 only recognizes uv installs whose interpreter path contains
    # /uv/tools/. Generations must retain that released handoff contract so an
    # immutable 3.0.13 process can choose uv for its first product upgrade.
    tools_dir = generation / "uv" / "tools"
    bin_dir = generation / "bin"
    if not vibe_path:
        return tools_dir, bin_dir, None

    launcher = Path(vibe_path).expanduser()
    # Only replace a stable launcher link. A direct path into a virtualenv may
    # be the user's intentional installation and cannot be switched safely by
    # replacing the file the current process is executing.
    if not _is_stable_launcher_path(launcher):
        return tools_dir, bin_dir, None
    candidate = bin_dir / launcher.name
    return tools_dir, bin_dir, AtomicActivation(
        launcher=launcher,
        candidate_launcher=candidate,
        source_generation=_launcher_generation(launcher, atomic_uv_install_root()),
    )


def _candidate_python(candidate_launcher: Path) -> Path | None:
    roots = [candidate_launcher.parent]
    with contextlib.suppress(OSError, RuntimeError):
        resolved_parent = candidate_launcher.resolve().parent
        if resolved_parent not in roots:
            roots.append(resolved_parent)
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        generation = _generation_for_path(candidate_launcher, atomic_uv_install_root())
        if generation is not None:
            roots.extend((generation / "uv" / "tools", generation / "tools"))
    names = ("python.exe", "python3.exe", "python3", "python") if os.name == "nt" else ("python3", "python")
    for root in roots:
        for name in names:
            candidate = root / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        if root.name == "tools" and root.is_dir():
            for name in names:
                for candidate in sorted(root.rglob(name)):
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        return candidate
    return None


def defer_upgrade_activation(
    activation: AtomicActivation,
    *,
    parent_pid: int,
    restart_required: bool = False,
    prepare_show_runtime: bool = False,
) -> subprocess.Popen:
    """Run activation after a Windows CLI process releases its launcher."""

    candidate_python = _candidate_python(activation.candidate_launcher)
    if candidate_python is None:
        raise RuntimeError(f"candidate Python missing beside {activation.candidate_launcher}")
    command = [
        str(candidate_python),
        "-c",
        "from vibe.cli import main; main()",
        "__activate-upgrade",
        "--parent-pid",
        str(parent_pid),
        "--launcher",
        str(activation.launcher),
        "--candidate",
        str(activation.candidate_launcher),
    ]
    if activation.source_generation is not None:
        command.extend(["--source-generation", str(activation.source_generation)])
    if restart_required:
        command.append("--restart")
    if prepare_show_runtime:
        command.append("--prepare-show-runtime")
    parent_started_at = runtime_mod.process_create_time(parent_pid)
    if parent_started_at is not None:
        command.extend(["--parent-started-at", str(parent_started_at)])
    log_path = config_paths.get_logs_dir() / f"upgrade-activation-{uuid4().hex}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd=get_safe_cwd(),
            env=isolated_probe_environment(),
        )


def get_cli_launcher_path(launcher: runtime_mod.ServiceLauncher) -> Path | None:
    """Find the CLI launcher next to a saved service interpreter."""

    names = ("vibe.exe", "vibe") if os.name == "nt" else ("vibe", "vibe.exe")
    python_path = Path(launcher.python).expanduser()
    root = atomic_uv_install_root()
    generation = _launcher_generation(python_path, root)
    if generation is not None:
        generation_bin = generation / "bin"
        for name in names:
            candidate = generation_bin / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        with contextlib.suppress(OSError):
            for name in names:
                candidate = next(generation.rglob(name), None)
                if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate

    python_bin = python_path.parent
    for name in names:
        candidate = python_bin / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _generation_for_path(path: Path, root: Path) -> Path | None:
    """Return the canonical generation containing a logical install path.

    The leaf may itself be a symlink, as uv-managed Python launchers commonly
    are. Walk its logical ancestors and resolve only the candidate generation
    directory so the shared interpreter target cannot erase generation
    ownership.
    """

    try:
        root = root.expanduser().resolve()
        expanded = path.expanduser()
    except (OSError, RuntimeError):
        return None
    ancestors = [expanded, *expanded.parents]
    try:
        resolved_path = expanded.resolve()
    except (OSError, RuntimeError):
        resolved_path = None
    if resolved_path is not None:
        ancestors.extend((resolved_path, *resolved_path.parents))
    for ancestor in dict.fromkeys(ancestors):
        try:
            resolved = ancestor.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved.parent == root:
            return resolved
    return None


def _generation_for_hardlink(launcher: Path, root: Path) -> Path | None:
    """Find an atomic generation sharing the stable launcher's inode."""

    if launcher.is_symlink() or not _is_stable_launcher_path(launcher):
        return None
    try:
        launcher_stat = launcher.stat()
        identity = (launcher_stat.st_dev, launcher_stat.st_ino)
    except OSError:
        return None
    try:
        root = root.expanduser().resolve()
        if not root.is_dir():
            return None
        generations = list(root.iterdir())
    except OSError:
        return None
    for generation in generations:
        candidate = generation / "bin" / launcher.name
        try:
            candidate_stat = candidate.stat()
            if generation.is_dir() and (candidate_stat.st_dev, candidate_stat.st_ino) == identity:
                return generation
        except OSError:
            continue
    return None


def _launcher_generation(launcher: Path, root: Path) -> Path | None:
    """Return the generation currently represented by a stable launcher."""

    root = root.expanduser().resolve()
    generation = _generation_for_path(launcher, root) or _generation_for_hardlink(launcher, root)
    if generation is not None:
        return generation
    if not _is_stable_launcher_path(launcher):
        return None
    marker = launcher.parent / f".{launcher.name}.avibe-generation"
    try:
        marked = Path(marker.read_text(encoding="utf-8").lstrip("\ufeff").strip())
    except (OSError, UnicodeError):
        return None
    generation = _generation_for_path(marked, root)
    if generation is None or not generation.is_dir():
        return None
    # A marker is necessary only for the cross-volume copy fallback. Treat it
    # as a hint and prove that it still describes the live launcher before use.
    candidate = generation / "bin" / launcher.name
    try:
        return generation if filecmp.cmp(launcher, candidate, shallow=False) else None
    except OSError:
        return None


def _update_launcher_generation_marker(launcher: Path, target: Path, root: Path) -> None:
    """Record a copied launcher's generation without making cleanup mandatory."""

    marker = launcher.parent / f".{launcher.name}.avibe-generation"
    generation = _generation_for_path(target, root)
    if generation is None:
        with contextlib.suppress(OSError):
            marker.unlink()
        return
    replacement = marker.parent / f".{marker.name}.{uuid4().hex}.new"
    try:
        replacement.write_text(str(generation), encoding="utf-8")
        os.replace(replacement, marker)
    except OSError:
        with contextlib.suppress(OSError):
            replacement.unlink()
        logger.warning("failed to update launcher generation marker %s", marker, exc_info=True)


def atomic_activation_source_is_current(activation: AtomicActivation) -> bool:
    """Check that the stable launcher still points at the source we measured."""

    root = atomic_uv_install_root()
    current = _launcher_generation(activation.launcher, root)
    if activation.source_generation is None:
        return current is None
    source = _generation_for_path(activation.source_generation, root)
    if current is not None or source is not None:
        return current == source
    try:
        current_target = activation.launcher.resolve(strict=True)
        return os.path.samefile(current_target, activation.source_generation)
    except (OSError, RuntimeError, ValueError):
        return False


def activation_block_reason(activation: AtomicActivation) -> str | None:
    """Return why an activation cannot own the current install boundary."""

    if restart_is_pending():
        return "restart_pending"
    if not atomic_activation_source_is_current(activation):
        return "superseded"
    return None


def discard_atomic_uv_install_generation(path: Path | str) -> bool:
    """Remove one failed candidate generation without touching active installs."""

    root = atomic_uv_install_root().expanduser().resolve()
    generation = _generation_for_path(Path(path), root)
    if generation is None or not generation.is_dir():
        return False
    try:
        shutil.rmtree(generation)
    except OSError:
        logger.warning("failed to discard atomic Avibe install generation %s", generation, exc_info=True)
        return False
    return True


def verify_upgrade_candidate(activation: AtomicActivation) -> IntegrityResult:
    """Prove a staged tool is runnable and its package tree is complete."""

    candidate = activation.candidate_launcher
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return IntegrityResult(False, failures=(f"candidate launcher missing: {candidate}",))
    python = _candidate_python(candidate)
    if python is None:
        return IntegrityResult(False, failures=(f"candidate Python missing beside {candidate}",))
    result = verify_python_environment(python)
    if not result.ok:
        return result
    try:
        probe = run_isolated_probe([str(candidate), "--help"], timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return IntegrityResult(False, failures=(f"candidate launcher probe failed: {exc}",))
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        suffix = detail[-1] if detail else f"exit code {probe.returncode}"
        return IntegrityResult(False, failures=(f"candidate launcher probe failed: {suffix}",))
    return result


def _prepare_launcher_replacement(replacement: Path, target: Path) -> None:
    """Create a replacement launcher, using copy when links cannot cross volumes."""

    try:
        replacement.symlink_to(target)
        return
    except OSError:
        pass
    try:
        os.link(target, replacement)
        return
    except OSError:
        # A regular copy is the only portable option when the stable bin and
        # the runtime home are on different Windows volumes.
        shutil.copy2(target, replacement)


def activate_upgrade_candidate(activation: AtomicActivation) -> None:
    """Atomically switch the stable launcher to a validated candidate."""

    result = verify_upgrade_candidate(activation)
    if not result.ok:
        raise RuntimeError(f"staged Avibe install failed integrity checks: {result.detail}")
    launcher = activation.launcher
    launcher.parent.mkdir(parents=True, exist_ok=True)
    replacement = launcher.parent / f".{launcher.name}.avibe-{uuid4().hex}.new"
    root = atomic_uv_install_root().expanduser().resolve()
    try:
        _prepare_launcher_replacement(replacement, activation.candidate_launcher)
        os.replace(replacement, launcher)
    except Exception:
        with contextlib.suppress(OSError):
            replacement.unlink()
        raise
    _update_launcher_generation_marker(launcher, activation.candidate_launcher, root)


def activate_installer_candidate(activation: AtomicActivation) -> None:
    """Activate an installer candidate through the shared lifecycle owner."""

    with atomic_upgrade_lock():
        reason = activation_block_reason(activation)
        if reason == "restart_pending":
            raise RuntimeError("an Avibe restart is still in progress")
        if reason == "superseded":
            raise RuntimeError("the active Avibe generation changed while the installer was staging")
        activate_upgrade_candidate(activation)


def activate_launcher_target(launcher: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
    """Atomically point a stable launcher at an installed target."""

    launcher_path = Path(launcher).expanduser()
    target_path = Path(target).expanduser()
    if not target_path.is_file() or not os.access(target_path, os.X_OK):
        raise RuntimeError(f"launcher target is not executable: {target_path}")
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    replacement = launcher_path.parent / f".{launcher_path.name}.avibe-{uuid4().hex}.new"
    try:
        _prepare_launcher_replacement(replacement, target_path)
        os.replace(replacement, launcher_path)
    except Exception:
        with contextlib.suppress(OSError):
            replacement.unlink()
        raise
    _update_launcher_generation_marker(launcher_path, target_path, atomic_uv_install_root().expanduser().resolve())


def resolve_command_path(command: str | None, search_path: str | None = None) -> str | None:
    if not command:
        return None

    expanded = Path(command).expanduser()
    if expanded.is_absolute():
        return os.path.abspath(str(expanded))

    if any(sep in command for sep in (os.sep, "/")):
        return os.path.abspath(str(Path.cwd() / expanded))

    resolved = shutil.which(command, path=search_path)
    if not resolved:
        return None
    return os.path.abspath(os.path.expanduser(resolved))


def is_usable_command_path(path: str | None) -> bool:
    if not path:
        return False
    return os.path.exists(path) and os.access(path, os.X_OK)


def get_launcher_bin_dir(command_path: str) -> str:
    current = os.path.abspath(os.path.expanduser(command_path))

    while os.path.islink(current):
        target = os.readlink(current)
        if not os.path.isabs(target):
            target = os.path.abspath(os.path.join(os.path.dirname(current), target))
        else:
            target = os.path.abspath(os.path.expanduser(target))

        if not os.path.islink(target):
            return str(Path(current).parent)

        current = target

    return str(Path(current).parent)


def get_known_uv_paths(base_env: Mapping[str, str] | None = None) -> list[str]:
    env = base_env or os.environ
    home = env.get("HOME")
    if home is not None:
        return [os.path.join(home, bin_dir, "uv") for bin_dir in UV_FALLBACK_BIN_DIRS]
    return [os.path.expanduser(f"~/{bin_dir}/uv") for bin_dir in UV_FALLBACK_BIN_DIRS]


def should_skip_show_runtime_prepare(base_env: Mapping[str, str] | None = None) -> bool:
    env = base_env or os.environ
    return env.get(SHOW_RUNTIME_SKIP_ENV, "").strip().lower() in TRUTHY_ENV_VALUES


def find_uv_binary(uv_path: str | None = None, base_env: Mapping[str, str] | None = None) -> str | None:
    env = base_env or os.environ
    search_path = env.get("PATH")

    resolved = resolve_command_path(uv_path, search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    resolved = resolve_command_path("uv", search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    for candidate in get_known_uv_paths(base_env=env):
        resolved = resolve_command_path(candidate, search_path=search_path)
        if is_usable_command_path(resolved):
            return resolved

    return None


def get_running_vibe_path(
    *,
    vibe_path: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> str | None:
    resolved = resolve_command_path(vibe_path, search_path=search_path)
    if is_usable_command_path(resolved):
        return resolved

    env_path = resolve_command_path(os.environ.get(CURRENT_VIBE_EXECUTABLE_ENV), search_path=search_path)
    if is_usable_command_path(env_path):
        return env_path

    argv_path = resolve_command_path(argv0 or sys.argv[0], search_path=search_path)
    if is_usable_command_path(argv_path):
        argv_path_str = cast(str, argv_path)
        if Path(argv_path_str).name.startswith("vibe"):
            return argv_path_str

    fallback_path = resolve_command_path("vibe", search_path=search_path)
    if is_usable_command_path(fallback_path):
        return fallback_path
    return None


def cache_running_vibe_path(vibe_path: str | None = None) -> str | None:
    resolved = get_running_vibe_path(vibe_path=vibe_path)
    if resolved:
        os.environ[CURRENT_VIBE_EXECUTABLE_ENV] = resolved
    return resolved


def get_restart_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> list[str]:
    resolved = get_running_vibe_path(vibe_path=vibe_path, argv0=argv0, search_path=search_path)
    if resolved:
        return [resolved]
    return [python_executable or sys.executable, "-c", "from vibe.cli import main; main()"]


def get_restart_invocation_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> list[str]:
    return [
        *get_restart_command(
            vibe_path=vibe_path,
            python_executable=python_executable,
            argv0=argv0,
            search_path=search_path,
        ),
        "restart",
    ]


def _get_source_checkout_root() -> str | None:
    source_root = Path(__file__).resolve().parent.parent
    if not source_root.is_dir():
        return None
    if not (source_root / "pyproject.toml").is_file():
        return None
    if not (source_root / "vibe" / "__init__.py").is_file():
        return None
    return str(source_root)


def _normalize_pythonpath_entries(pythonpath: str) -> list[str]:
    normalized_entries: list[str] = []
    seen_entries: set[str] = set()

    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        normalized_entry = os.path.abspath(os.path.expanduser(entry))
        if normalized_entry in seen_entries:
            continue
        seen_entries.add(normalized_entry)
        normalized_entries.append(normalized_entry)

    return normalized_entries


def get_restart_environment(
    *,
    vibe_path: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    resolved = get_running_vibe_path(vibe_path=vibe_path, argv0=argv0, search_path=search_path)
    if resolved:
        return None

    source_root = _get_source_checkout_root()
    if not source_root:
        return None

    env = dict(base_env or os.environ)
    pythonpath = env.get("PYTHONPATH")
    if pythonpath:
        normalized_root = os.path.abspath(source_root)
        normalized_entries = _normalize_pythonpath_entries(pythonpath)
        if normalized_root not in normalized_entries:
            normalized_entries.insert(0, normalized_root)
        env["PYTHONPATH"] = os.pathsep.join(normalized_entries)
        return env

    env["PYTHONPATH"] = source_root
    return env


def get_restart_shell_command(
    *,
    vibe_path: str | None = None,
    python_executable: str | None = None,
    argv0: str | None = None,
    search_path: str | None = None,
) -> str:
    command = get_restart_invocation_command(
        vibe_path=vibe_path,
        python_executable=python_executable,
        argv0=argv0,
        search_path=search_path,
    )
    return shlex.join(command)


def get_update_metadata_url() -> str:
    return os.environ.get("AVIBE_UPDATE_METADATA_URL") or os.environ.get(
        "VIBE_UPDATE_METADATA_URL", DEFAULT_UPDATE_METADATA_URL
    )


def get_upgrade_package_spec() -> str:
    return os.environ.get("AVIBE_UPGRADE_PACKAGE_SPEC") or os.environ.get("VIBE_UPGRADE_PACKAGE_SPEC", PACKAGE_NAME)


def _normalize_release_parts(parts: tuple[int, ...]) -> tuple[int, ...]:
    normalized = list(parts)
    while len(normalized) > 1 and normalized[-1] == 0:
        normalized.pop()
    return tuple(normalized)


def _parse_version_parts(
    value: str,
) -> tuple[tuple[int, ...], tuple[int, int] | None, int | None, int | None] | None:
    """Split a version into (release, pre, post, dev).

    ``pre`` is ``(stage_order, num)`` for a/b/rc releases, else ``None``.
    ``post`` / ``dev`` are the numeric component, or ``None`` when absent.
    The local segment (``+...``) is matched but intentionally discarded: per
    PEP 440 it does not affect ordering.
    """
    match = _VERSION_RE.match(value)
    if not match:
        return None

    release = _normalize_release_parts(
        tuple(int(part) for part in match.group("release").split("."))
    )
    pre = None
    if match.group("pre"):
        pre = (_PRE_ORDER[match.group("pre").lower()], int(match.group("pre_num") or "0"))
    post = int(match.group("post_num") or "0") if match.group("post") else None
    dev = int(match.group("dev_num") or "0") if match.group("dev") else None
    return (release, pre, post, dev)


def _version_key(
    parts: tuple[tuple[int, ...], tuple[int, int] | None, int | None, int | None],
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Build a totally-ordered, all-int comparison key (PEP 440 ordering).

    Bands are plain int tuples so they never compare a number against a tuple.
    Within a release: ``X.devN`` < ``Xa/b/rcN`` < ``X`` (final) < ``X.postN``,
    and a trailing ``.devN`` sorts before the same version without it.
    """
    release, pre, post, dev = parts
    if pre is None and post is None and dev is not None:
        pre_band: tuple[int, ...] = (0,)  # bare X.devN sorts before any pre/final of X
    elif pre is None:
        pre_band = (2,)  # final (or post-only) sorts after pre-releases
    else:
        pre_band = (1, pre[0], pre[1])
    post_band = (0,) if post is None else (1, post)
    dev_band = (1,) if dev is None else (0, dev)  # a dev release sorts before the non-dev
    return (release, pre_band, post_band, dev_band)


def _parse_version(
    value: str,
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]] | None:
    parts = _parse_version_parts(value)
    return None if parts is None else _version_key(parts)


def _is_prerelease_version(value: str) -> bool:
    parts = _parse_version_parts(value)
    if parts is None:
        return False
    _release, pre, _post, dev = parts
    return pre is not None or dev is not None


def _is_yanked_release(files: object) -> bool:
    if not isinstance(files, list) or not files:
        return False
    yanked_flags = [bool(item.get("yanked")) for item in files if isinstance(item, dict)]
    return bool(yanked_flags) and all(yanked_flags)


def select_latest_update_version(metadata: Mapping[str, object], current_version: str) -> str:
    allow_prereleases = _is_prerelease_version(current_version)
    releases = metadata.get("releases")

    candidates: list[tuple[object, str]] = []
    if isinstance(releases, Mapping):
        for version_str, files in releases.items():
            if not isinstance(version_str, str):
                continue
            parsed = _parse_version(version_str)
            if parsed is None:
                continue
            if not allow_prereleases and _is_prerelease_version(version_str):
                continue
            if _is_yanked_release(files):
                continue
            candidates.append((parsed, version_str))

    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    latest = str((metadata.get("info") or {}).get("version") or "")
    if latest and (allow_prereleases or not _is_prerelease_version(latest)):
        return latest
    return ""


def has_newer_version(candidate: str, current: str) -> bool:
    if not candidate or candidate == current:
        return False

    latest_parsed = _parse_version(candidate)
    current_parsed = _parse_version(current)
    if latest_parsed is not None and current_parsed is not None:
        return latest_parsed > current_parsed

    # Fallback for strings the parser cannot handle: compare the leading
    # integer of each of the first three dotted components. Stop at the first
    # component without a leading digit so we never silently drop a position
    # (e.g. treating "3.0.4rc4" as [3, 0] and ranking it below "3.0.3").
    def _loose_parts(text: str) -> list[int]:
        parts: list[int] = []
        for chunk in text.split(".")[:3]:
            digits = re.match(r"\d+", chunk)
            if not digits:
                break
            parts.append(int(digits.group()))
        return parts

    try:
        return _loose_parts(candidate) > _loose_parts(current)
    except (ValueError, AttributeError):
        return candidate != current


def get_latest_version_info(current_version: str) -> dict:
    result = {"current": current_version, "latest": None, "has_update": False, "error": None}

    try:
        url = get_update_metadata_url()
        req = urllib.request.Request(url, headers={"User-Agent": PACKAGE_NAME})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        latest = select_latest_update_version(data, current_version)
        result["latest"] = latest

        if latest and latest != current_version:
            result["has_update"] = has_newer_version(latest, current_version)
    except Exception as e:
        result["error"] = str(e)

    return result


def is_uv_tool_install(python_executable: str | None = None) -> bool:
    executable = (python_executable or sys.executable or "").replace("\\", "/")
    if "/uv/tools/" in executable:
        return True
    try:
        logical = Path(os.path.abspath(os.path.expanduser(executable)))
        logical_root = Path(os.path.abspath(os.path.expanduser(str(atomic_uv_install_root()))))
        if logical.is_relative_to(logical_root):
            return True
        if _launcher_generation(logical, logical_root) is not None:
            return True
        # A logical path may contain a symlinked parent. Resolving is only the
        # fallback: uv's tool Python is itself commonly a symlink to a shared
        # interpreter outside the generation.
        return logical.resolve().is_relative_to(logical_root.resolve())
    except (OSError, ValueError, RuntimeError):
        return False


def is_legacy_uv_tool_install(python_executable: str | None = None) -> bool:
    executable = (python_executable or sys.executable or "").replace("\\", "/")
    return f"/uv/tools/{LEGACY_PACKAGE_NAME}/" in executable


def memory_package_installed() -> bool:
    """Return whether the optional Memory distribution is installed."""

    try:
        from importlib.metadata import PackageNotFoundError, distribution

        distribution(MEMORY_PACKAGE_NAME)
    except PackageNotFoundError:
        return False
    except Exception:
        logger.warning("Could not inspect %s metadata; preserving package shape", MEMORY_PACKAGE_NAME, exc_info=True)
        return True
    return True


def configured_memory_enabled() -> bool:
    """Read the persisted Memory switch for an explicit upgrade action."""

    try:
        from config.v2_config import V2Config

        required = V2Config.load().memory_required
    except FileNotFoundError:
        return False
    except Exception as exc:
        raise MemoryRequirementUnreadableError(
            "The persisted Memory requirement could not be read"
        ) from exc
    if required is None:
        raise MemoryRequirementUnreadableError(
            "The persisted Memory requirement could not be read"
        )
    return required


def _distributions_providing_this_package() -> list[str]:
    """Every installed distribution that provides the package this module is in."""

    try:
        from importlib.metadata import packages_distributions
        from packaging.utils import canonicalize_name
    except ImportError:  # pragma: no cover - importlib.metadata ships with 3.10+
        return []
    try:
        memory_name = canonicalize_name(MEMORY_PACKAGE_NAME)
        return sorted(
            {
                name
                for name in packages_distributions().get(__name__.split(".")[0], [])
                if canonicalize_name(name) != memory_name
            }
        )
    except Exception:  # pragma: no cover - a broken environment answers nothing
        logger.debug("Failed to read installed distribution metadata", exc_info=True)
        return []


def _providers_recording_a_published_release() -> list[tuple[str, str]]:
    """Every distribution providing this package, with the release it records.

    Unpublished recordings are dropped rather than reported. A `dev` or local
    version records the tree it was built from, so it can neither confirm nor
    contradict a released one, and an environment that answers nothing at all --
    no provider, unreadable metadata, nothing published -- comes back empty. Both
    callers read empty as "no evidence", never as evidence of disagreement.
    """

    distributions = _distributions_providing_this_package()
    if not distributions:
        return []
    try:
        from importlib.metadata import version as distribution_version

        recorded = [(name, distribution_version(name)) for name in distributions]
    except Exception:  # pragma: no cover - a broken environment answers nothing
        logger.debug("Failed to read the installed distribution version", exc_info=True)
        return []
    return [(name, version) for name, version in recorded if _names_a_published_release(version)]


def _providers_describing_running_code() -> list[str]:
    """Return providers whose recorded release matches the running code.

    Multiple providers can remain after the `vibe-remote` to `avibe-os` rename.
    Comparing every published record prevents a forward install from becoming a
    silent no-op when stale metadata claims the requested release is present.
    """

    from vibe import __version__

    return [
        name
        for name, recorded in _providers_recording_a_published_release()
        if recorded == __version__
    ]


def installed_metadata_describes_running_code() -> bool:
    """Whether every published provider record matches the running files.

    pip trusts installed metadata when deciding whether an upgrade has work to
    do. A mismatch therefore forces the forward reinstall. Unknown source or
    editable layouts remain unforced because they provide no reliable published
    version to compare.
    """

    from vibe import __version__

    if not _names_a_published_release(__version__):
        return True

    published = _providers_recording_a_published_release()
    if not published:
        return True
    # Metadata describes the code only if EVERY provider recording a published
    # release records this one; a single dissenter is a distribution pip would
    # read as already satisfied.
    return len(_providers_describing_running_code()) == len(published)


def get_current_vibe_bin_dir(vibe_path: str | None = None) -> str | None:
    current_vibe = get_running_vibe_path(vibe_path=vibe_path)
    if not current_vibe:
        return None

    return get_launcher_bin_dir(current_vibe)


def get_current_uv_tool_dir(python_executable: str | None = None) -> str | None:
    """Return the logical uv tool root containing the running interpreter."""

    executable = Path(python_executable or sys.executable).expanduser().absolute()
    for parent in executable.parents:
        if parent.name == "tools" and parent.parent.name == "uv":
            return str(parent)
    return None


def _names_a_published_release(version: str) -> bool:
    """Whether an index could serve `version`, as opposed to it naming this tree.

    A development build's version describes the tree it was built from rather
    than anything published: PEP 440 spells that as a `dev` segment, a local
    segment (`+<sha>`), or both, and the regression builder emits exactly that
    shape.

    Asking the property instead of listing the strings is what stops the next
    unlisted shape from costing the same attempt. Unparseable reads as
    unpublished for the same reason: a version this codebase cannot even order is
    not one it should pin an install to.
    """

    match = _VERSION_RE.match(version)
    if match is None:
        return False
    return not (match.group("dev") or match.group("local"))


def _with_memory_extra(package_spec: str) -> str:
    """Add the Memory extra without corrupting URL or local path specs."""

    if package_spec.startswith(("git+", "hg+", "svn+", "bz+", "http://", "https://", "file://")):
        return f"{PACKAGE_NAME}[{MEMORY_EXTRA_NAME}] @ {package_spec}"
    try:
        requirement = Requirement(package_spec)
    except InvalidRequirement:
        artifact_uri = Path(package_spec).expanduser().resolve().as_uri()
        return f"{PACKAGE_NAME}[{MEMORY_EXTRA_NAME}] @ {artifact_uri}"

    extras = sorted({*requirement.extras, MEMORY_EXTRA_NAME})
    rendered = f"{requirement.name}[{','.join(extras)}]"
    if requirement.url:
        rendered += f" @ {requirement.url}"
    else:
        rendered += str(requirement.specifier)
    if requirement.marker:
        rendered += f"; {requirement.marker}"
    return rendered


def _published_version(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        version = str(Version(value))
    except InvalidVersion:
        return None
    return version if _names_a_published_release(version) else None


def _memory_target_version(package_spec: str, target_version: str | None) -> str | None:
    """Choose the Memory pin from the artifact being installed or valid metadata."""

    try:
        requirement = Requirement(package_spec)
    except InvalidRequirement:
        artifact = package_spec
    else:
        if requirement.url is None:
            specifiers = list(requirement.specifier)
            if (
                len(specifiers) == 1
                and specifiers[0].operator == "=="
                and "*" not in specifiers[0].version
            ):
                return _published_version(specifiers[0].version)
            candidate = _published_version(target_version)
            if candidate is not None and requirement.specifier.contains(candidate, prereleases=True):
                return candidate
            return None
        artifact = requirement.url

    if artifact.startswith(("git+", "hg+", "svn+", "bz+")):
        return None
    try:
        parsed = urllib.parse.urlsplit(artifact)
    except ValueError:
        return None
    filename = Path(urllib.parse.unquote(parsed.path if parsed.scheme else artifact)).name
    try:
        _, version, _, _ = parse_wheel_filename(filename)
    except InvalidWheelFilename:
        try:
            _, version = parse_sdist_filename(filename)
        except InvalidSdistFilename:
            return None
    return _published_version(str(version))


def _wheel_distribution(package_name: str) -> str:
    """Spell one distribution name the way a wheel filename does."""

    return canonicalize_name(package_name).replace("-", "_")


def _recorded_install_origin(package_name: str) -> str | None:
    """The URL an installed distribution was taken from, per PEP 610.

    Installers write `direct_url.json` only when the distribution came from
    somewhere other than an index, so its absence is itself the answer: this
    copy was resolved by name and can be repaired the same way.
    """

    try:
        from importlib.metadata import PackageNotFoundError, distribution

        recorded = distribution(package_name).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    except Exception:
        logger.warning("Could not read %s install origin; assuming an index install", package_name, exc_info=True)
        return None
    if not recorded:
        return None
    try:
        url = json.loads(recorded).get("url")
    except (ValueError, AttributeError):
        return None
    return url if isinstance(url, str) else None


def release_asset_specs(version: str) -> tuple[str, str] | None:
    """The wheel pair this install came from, or `None` if an index serves it.

    A version string cannot answer this. `publish.yml` accepts official
    `vX.Y.ZrcN` tags and publishes them to PyPI, so a pre-release may well be
    on an index, while a `gh-v*` build carrying the identical version is on no
    index at all. Treating every pre-release as GitHub-only would point the
    official one at a tag that was never created.

    The installer already recorded the answer. When core came from a release
    asset of this repository, the pair is repaired from that same release —
    the one it demonstrably came from, rather than one derived from a naming
    convention. Every other origin, an index install included, returns `None`
    and keeps its index pins.

    The recorded URL must name this exact running version, so a stale or
    mismatched record cannot drive the repair to a different pair.
    """

    origin = _recorded_install_origin(PACKAGE_NAME)
    if not origin:
        return None
    try:
        normalized = str(Version(version))
    except InvalidVersion:
        return None
    prefix = f"{RELEASE_DOWNLOAD_BASE_URL}/"
    if not origin.startswith(prefix):
        return None
    # Exactly one tag segment and one filename: anything else is not an asset of
    # this repository's releases, whatever it may resemble.
    segments = origin[len(prefix) :].split("/")
    if len(segments) != 2:
        return None
    tag, asset = segments
    if not tag or tag in {".", ".."}:
        return None
    if asset != f"{_wheel_distribution(PACKAGE_NAME)}-{normalized}-py3-none-any.whl":
        return None
    # One release directory, so the companion cannot come from another release.
    return (
        origin,
        f"{prefix}{tag}/{_wheel_distribution(MEMORY_PACKAGE_NAME)}-{normalized}-py3-none-any.whl",
    )


def pinned_package_spec(
    version: str,
    *,
    package_name: str,
) -> str:
    """Pin one explicit distribution name to one exact published version.

    The explicit Memory install uses this to reinstall the running core release
    together with its matching optional package. The distribution name must be a
    bare package name; direct URLs and pre-versioned requirements are rejected.

    The message never quotes the spec. An operator-supplied spec can be an index
    URL carrying credentials.
    """

    spec = package_name
    if _BARE_PACKAGE_NAME_RE.fullmatch(spec) is None:
        raise ValueError("The configured upgrade package spec cannot carry a version pin")
    return f"{spec}=={version}"


def build_upgrade_plan(
    *,
    python_executable: str | None = None,
    uv_path: str | None = None,
    vibe_path: str | None = None,
    base_env: dict[str, str] | None = None,
    version: str | None = None,
    target_version: str | None = None,
    package_name: str | None = None,
    memory_enabled: bool = False,
    memory_package: bool | None = None,
    memory_version: str | None = None,
    package_spec: str | None = None,
    core_spec: str | None = None,
    memory_spec: str | None = None,
) -> UpgradePlan:
    """How to install avibe: the newest release, or `version` exactly.

    Exact-version plans support the explicit Memory package install action. They
    replace the ordinary upgrade request and force the installer so the matching
    optional distribution is applied even when core is already satisfied.

    `core_spec` and `memory_spec` name where to fetch that exact pair when it
    was never published to an index — a preview release's own wheel URLs. They
    replace the two pins and change nothing else, so the resulting plan is the
    same operation against a different source. A forward upgrade resolves the
    newest release rather than a known one and has no such source to name, so
    passing either without `version` is rejected instead of ignored: a spec that
    silently does nothing would read as applied.
    """

    if (core_spec or memory_spec) and not version:
        raise ValueError("Explicit install sources require an exact target version")

    executable = python_executable or sys.executable
    # A caller targeting another interpreter (for example a test-owned venv)
    # can provide an explicit package-shape measurement.  Only infer from this
    # process when the caller did not provide one; otherwise ambient metadata
    # would leak into the target plan and turn an intentional core-only install
    # into a Memory install.
    include_memory = (
        bool(memory_package)
        if version or memory_package is not None
        else bool(memory_enabled or memory_package_installed())
    )
    package_spec = (
        (
            core_spec
            or pinned_package_spec(
                version,
                package_name=package_name or PACKAGE_NAME,
            )
        )
        if version
        else (package_spec or get_upgrade_package_spec())
    )
    if not version and include_memory:
        target_version = _memory_target_version(package_spec, target_version)
        if target_version is None:
            raise ValueError("A Memory-preserving upgrade requires a target release version")
    uv_binary = find_uv_binary(uv_path=uv_path, base_env=base_env)
    if not version and include_memory and f"[{MEMORY_EXTRA_NAME}]" not in package_spec:
        package_spec = _with_memory_extra(package_spec)
    memory_target = memory_version if version else target_version
    # The URL form stays a named requirement so every installer still reads it
    # as "this distribution, from here" rather than as an anonymous artifact.
    pinned_memory_spec = None
    if include_memory:
        if memory_spec:
            pinned_memory_spec = f"{MEMORY_PACKAGE_NAME} @ {memory_spec}"
        elif memory_target:
            pinned_memory_spec = f"{MEMORY_PACKAGE_NAME}=={memory_target}"

    if is_uv_tool_install(executable) and uv_binary:
        env = dict(base_env or os.environ)
        preflight_error = None
        # Exact repairs can recreate the whole uv environment too. Stage them
        # just like forward upgrades while the service/UI still use the old one.
        tool_dir, bin_dir, atomic = _staged_uv_environment(vibe_path)
        if atomic is None:
            preflight_error = "Cannot atomically upgrade uv installation: stable vibe launcher is unavailable"
        else:
            env["UV_TOOL_DIR"] = str(tool_dir)
            env["UV_TOOL_BIN_DIR"] = str(bin_dir)
        command = [uv_binary, "tool", "install", package_spec]
        if pinned_memory_spec:
            command.extend(["--with", pinned_memory_spec])
        if not version:
            command.append("--upgrade")
        if version or package_spec != PACKAGE_NAME or is_legacy_uv_tool_install(executable):
            command.append("--force")
        preflight_command = None
        if include_memory:
            preflight_command = [uv_binary, "pip", "install", "--dry-run", "--python", executable]
            if not version:
                preflight_command.append("--upgrade")
            preflight_command.append(package_spec)
            if pinned_memory_spec:
                preflight_command.append(pinned_memory_spec)
        return UpgradePlan(
            command=command,
            env=env,
            method="uv",
            preflight_command=preflight_command,
            activation=atomic,
            preflight_error=preflight_error,
        )

    command = [executable, "-m", "pip", "install"]
    if not version:
        command.append("--upgrade")
    # pip decides whether to act from metadata. Exact installs always force so a
    # matching optional package is applied even when core is already satisfied;
    # forward installs force only when published provider metadata disagrees with
    # the running files.
    if version or not installed_metadata_describes_running_code():
        command.append("--force-reinstall")
    command.append(package_spec)
    if pinned_memory_spec:
        command.append(pinned_memory_spec)
    preflight_command = None
    # Preflight only when the optional package shape is part of the operation.
    # Core-only forward installs retain the origin/dev synchronous behavior: the
    # service is still running while pip resolves, so a second resolver pass is
    # unnecessary general-updater machinery.
    preflight_fallback_command = None
    if include_memory and not version:
        preflight_command = [
            executable,
            "-m",
            "pip",
            "install",
            "--dry-run",
            "--upgrade",
            package_spec,
        ]
        preflight_fallback_command = [
            executable,
            "-m",
            "pip",
            "download",
            "--dest",
            PIP_DOWNLOAD_DEST_PLACEHOLDER,
            package_spec,
        ]
        if pinned_memory_spec:
            preflight_command.append(pinned_memory_spec)
            preflight_fallback_command.append(pinned_memory_spec)
    elif include_memory:
        preflight_command = [
            executable,
            "-m",
            "pip",
            "download",
            "--dest",
            PIP_DOWNLOAD_DEST_PLACEHOLDER,
        ]
        preflight_command.extend(["--no-deps", package_spec])
        if pinned_memory_spec:
            preflight_command.append(pinned_memory_spec)
    return UpgradePlan(
        command=command,
        env=dict(base_env or os.environ),
        method="pip",
        preflight_command=preflight_command,
        preflight_fallback_command=preflight_fallback_command,
    )

def get_safe_cwd() -> str:
    """Return a stable, existing absolute directory for subprocess cwd.

    The vibe service process cwd may be inside the uv tool venv directory,
    which uv deletes and recreates during upgrade.  Using the home directory
    avoids 'Current directory does not exist' errors.  Falls back to the
    system temp directory or ``/`` when HOME is unset or invalid.
    """
    for candidate in (os.path.expanduser("~"), tempfile.gettempdir(), "/"):
        if os.path.isabs(candidate) and os.path.isdir(candidate):
            return candidate
    return "/"
