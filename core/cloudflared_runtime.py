from __future__ import annotations

import os
import subprocess
from pathlib import Path

from config import paths
from core.managed_runtime import ManagedRuntimeManager, ManagedRuntimeSpec, env_flag_enabled
from core.process_isolation import isolated_subprocess_kwargs


_CLOUDFLARED_SPEC = ManagedRuntimeSpec(
    runtime_id="cloudflared",
    manifest_resource="cloudflared_runtime_manifest.json",
    version_field="cloudflared_version",
    default_bin_path="cloudflared",
    # Cloudflare ships no windows-arm64 build; Windows 11 on ARM runs the x64 one.
    platform_aliases=(("win32-arm64", "win32-x64"),),
)


class CloudflaredRuntimeManager(ManagedRuntimeManager):
    """Install and resolve the pinned cloudflared release remote access runs."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        manifest_path: Path | str | None = None,
        manifest_url: str | None = None,
        offline: bool | None = None,
    ) -> None:
        super().__init__(
            spec=_CLOUDFLARED_SPEC,
            runtime_dir=runtime_dir or paths.get_runtime_dir() / "cloudflared",
            manifest_path=manifest_path or os.environ.get("VIBE_CLOUDFLARED_MANIFEST_PATH"),
            manifest_url=(
                manifest_url if manifest_url is not None else os.environ.get("VIBE_CLOUDFLARED_MANIFEST_URL")
            ),
            offline=env_flag_enabled("VIBE_CLOUDFLARED_OFFLINE") if offline is None else offline,
        )

    def _binary_version(self, binary: Path | None) -> str | None:
        return _probe_cloudflared_version(binary)


def _probe_cloudflared_version(binary: Path | None) -> str | None:
    if binary is None:
        return None
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            # Status polls run this probe, so it must not flash a console on Windows.
            **isolated_subprocess_kwargs(console=False),
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    # ``cloudflared version 2026.10.0 (built 2026-10-05-17:37 UTC)``
    words = (proc.stdout or "").strip().split()
    return words[2] if len(words) > 2 and words[:2] == ["cloudflared", "version"] else None
