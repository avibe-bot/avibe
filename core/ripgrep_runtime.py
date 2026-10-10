from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from config import paths
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeSpec, env_flag_enabled
from core.process_isolation import isolated_subprocess_kwargs


_RIPGREP_SPEC = ManagedRuntimeSpec(
    runtime_id="ripgrep",
    manifest_resource="ripgrep_runtime_manifest.json",
    version_field="ripgrep_version",
    default_bin_path="rg",
)


class RipgrepRuntimeManager(ManagedRuntimeManager):
    """Install and resolve the official ripgrep release Agent commands search with."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        super().__init__(
            spec=_RIPGREP_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "ripgrep",
            manifest_path=manifest_path or os.environ.get("VIBE_RIPGREP_MANIFEST_PATH"),
            manifest_url=manifest_url if manifest_url is not None else os.environ.get("VIBE_RIPGREP_MANIFEST_URL"),
            offline=env_flag_enabled("VIBE_RIPGREP_OFFLINE") if offline is None else offline,
        )

    def _binary_version(self, binary: Path | None) -> str | None:
        return _probe_ripgrep_version(binary)


def prepend_managed_ripgrep_to_path(
    env: MutableMapping[str, str],
    *,
    base_env: Mapping[str, str],
) -> bool:
    """Prepend the verified managed ripgrep when the effective Agent PATH has no ``rg``.

    PATH composition follows ``prepend_vendored_git_to_path``: a ``PATH`` key in
    ``env`` wins even when empty, and only an absent key falls back to
    ``base_env``. An ``rg`` the PATH already resolves is the user's and is kept.
    A missing or unverifiable managed install leaves PATH unchanged.
    """

    current_path = env["PATH"] if "PATH" in env else base_env.get("PATH", "")
    if current_path and shutil.which("rg", path=current_path):
        return False
    managed = RipgrepRuntimeManager().resolve_binary()
    if managed is None:
        return False
    bin_dir = str(managed.parent)
    env["PATH"] = bin_dir if not current_path else f"{bin_dir}{os.pathsep}{current_path}"
    return True


def _probe_ripgrep_version(binary: Path | None) -> str | None:
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
    # ``ripgrep 15.2.0 (rev e89fff89ac)``, then feature lines.
    words = (proc.stdout or "").strip().split()
    return words[1] if len(words) > 1 and words[0] == "ripgrep" else None
