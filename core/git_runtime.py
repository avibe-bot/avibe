from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from config import paths
from core.managed_runtime import (
    ManagedRuntimeManager,
    ManagedRuntimeManifest,
    ManagedRuntimeSpec,
    env_flag_enabled,
)
from core.process_isolation import isolated_subprocess_kwargs


logger = logging.getLogger(__name__)

_GIT_MANIFEST_RESOURCE = "git_runtime_manifest.json"
_MACOS_CODESIGN = Path("/usr/bin/codesign")
_MACOS_XATTR = Path("/usr/bin/xattr")
_MACOS_XCODE_SELECT = Path("/usr/bin/xcode-select")
_GIT_SPEC = ManagedRuntimeSpec(
    runtime_id="git",
    manifest_resource=_GIT_MANIFEST_RESOURCE,
    version_field="git_version",
    default_bin_path="bin/git",
)
_MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",
}


class GitRuntimeManager(ManagedRuntimeManager):
    """Install and resolve Avibe's vendored local-only Git binary."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        manifest_path_value = manifest_path or os.environ.get("VIBE_GIT_MANIFEST_PATH")
        super().__init__(
            spec=_GIT_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "git",
            manifest_path=manifest_path_value,
            manifest_url=manifest_url if manifest_url is not None else os.environ.get("VIBE_GIT_MANIFEST_URL"),
            offline=env_flag_enabled("VIBE_GIT_OFFLINE") if offline is None else offline,
        )

    def resolve_git_path(self) -> Path | None:
        """Return a verified installed vendored Git, or ``None`` without installing."""

        return self.resolve_binary()

    def _manifest_installable(self, manifest: ManagedRuntimeManifest) -> bool:
        if str(manifest.payload.get("release_state") or "published") != "published":
            self._install_reason = "git_runtime_unpublished"
            return False
        return True

    def _binary_version(self, binary: Path | None) -> str | None:
        return _probe_git_version(binary)

    def _prepare_binary(self, binary: Path) -> dict[str, Any]:
        if sys.platform != "darwin" or not _is_macho(binary):
            return {"ok": True, "skipped": True, "reason": "not_macos_macho"}
        quarantine = _strip_quarantine(binary)
        if _codesign_valid(binary):
            return {"ok": True, "changed": False, "quarantine": quarantine}
        if not _MACOS_CODESIGN.is_file():
            return {"ok": False, "reason": "git_codesign_missing", "quarantine": quarantine}
        try:
            proc = subprocess.run(
                [str(_MACOS_CODESIGN), "-f", "-s", "-", str(binary)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "git_codesign_failed", "quarantine": quarantine}
        verified = proc.returncode == 0 and _codesign_valid(binary)
        return {
            "ok": verified,
            "changed": proc.returncode == 0,
            "reason": None if verified else "git_codesign_failed",
            "quarantine": quarantine,
        }


_manager: GitRuntimeManager | None = None


def get_git_runtime_manager() -> GitRuntimeManager:
    global _manager
    if _manager is None:
        _manager = GitRuntimeManager()
    return _manager


def set_git_runtime_manager_for_tests(manager: GitRuntimeManager | None) -> None:
    global _manager
    _manager = manager


def git_runtime_status() -> dict[str, Any]:
    manager = get_git_runtime_manager()
    vendored = manager.resolve_git_path()
    managed_status = manager.status()
    system = resolve_system_git_path()
    vendored_version = str(managed_status.get("version") or "") or None
    system_version = None
    if vendored is not None:
        resolution = "vendored"
        resolved_path = vendored
        resolved_version = vendored_version
    elif system is not None:
        resolution = "system"
        resolved_path = system
        resolved_version = system_version
    else:
        resolution = "none"
        resolved_path = None
        resolved_version = None

    if system is not None:
        agent_resolution = "system"
        agent_path = system
        agent_version = system_version
    elif vendored is not None:
        agent_resolution = "vendored"
        agent_path = vendored
        agent_version = vendored_version
    else:
        agent_resolution = "none"
        agent_path = None
        agent_version = None

    return {
        "id": "git",
        # Platform-owned features follow #669's vendored -> system order.
        "resolution": resolution,
        "path": str(resolved_path) if resolved_path else None,
        "version": resolved_version,
        # Planned Agent order for post-merge wiring; this PR does not mutate child PATH.
        "agent": {
            "resolution": agent_resolution,
            "path": str(agent_path) if agent_path else None,
            "version": agent_version,
        },
        "managed": managed_status,
    }


def resolve_system_git_path(*, env: Mapping[str, str] | None = None) -> Path | None:
    """Resolve the Git selected by PATH without triggering the macOS CLT shim.

    The Git candidate intentionally follows the supplied PATH for runtime status
    reporting. When ``env`` is provided, an absent or empty PATH never falls
    back to the parent process. Supporting system probes never follow that PATH.
    """

    source = env if env is not None else os.environ
    search_path = source.get("PATH", "")
    if not search_path:
        return None
    candidate = shutil.which("git", path=search_path)
    if not candidate:
        return None
    candidate_path = Path(candidate)
    if platform.system() == "Darwin" and _is_macos_system_git(candidate_path):
        try:
            proc = subprocess.run(
                [str(_MACOS_XCODE_SELECT), "-p"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                **isolated_subprocess_kwargs(),
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
    return candidate_path


def _probe_git_version(binary: Path | None) -> str | None:
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            **isolated_subprocess_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    prefix = "git version "
    if not output.startswith(prefix):
        return None
    version = output[len(prefix) :].split()[0] if output[len(prefix) :].strip() else ""
    return version or None


def _is_macos_system_git(path: Path) -> bool:
    try:
        return path == Path("/usr/bin/git") or path.resolve() == Path("/usr/bin/git")
    except OSError:
        return path == Path("/usr/bin/git")


def _is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def _codesign_valid(binary: Path) -> bool:
    if not _MACOS_CODESIGN.is_file():
        return False
    try:
        proc = subprocess.run(
            [str(_MACOS_CODESIGN), "-v", str(binary)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **isolated_subprocess_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


def _strip_quarantine(binary: Path) -> dict[str, Any]:
    if not _MACOS_XATTR.is_file():
        return {"ok": True, "skipped": True, "reason": "xattr_missing"}
    try:
        proc = subprocess.run(
            [str(_MACOS_XATTR), "-d", "com.apple.quarantine", str(binary)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            **isolated_subprocess_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return {"ok": False, "changed": False, "reason": "xattr_failed"}
    if proc.returncode == 0:
        return {"ok": True, "changed": True}
    output = (proc.stderr or proc.stdout or "").lower()
    if "no such xattr" in output or "no such file" in output:
        return {"ok": True, "changed": False}
    return {"ok": False, "changed": False, "reason": "xattr_failed"}
