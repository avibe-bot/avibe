"""Recognize existing CLI installation provenance without executing that CLI.

Unknown sources are not invitations to migrate an installation. These plans
only cover package layouts or canonical native destinations we can identify.
The installer owner executes them under its existing lock and reads back the
exact destination with its normal (unmasked) launch environment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PACKAGES: dict[str, str | None] = {
    "claude": "@anthropic-ai/claude-code",
    "opencode": "opencode-ai",
    "codex": "@openai/codex",
    "antigravity": None,
}
BREW_NAMES: dict[str, str | None] = {
    "claude": "claude-code",
    "opencode": "opencode",
    "codex": "codex",
    "antigravity": None,
}
REPAIR_PATH_CONFLICT = "avibe: repair blocked by another resolvable opencode"


@dataclass(frozen=True)
class RepairSource:
    kind: str
    destination: str
    package: str | None = None
    prefix: Path | None = None
    cask: bool = False


def native_vendor_install_target(
    backend: str,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    resolved_home = home or Path.home()
    if backend == "opencode":
        return resolved_home / ".opencode" / "bin" / ("opencode.exe" if os.name == "nt" else "opencode")
    if backend == "claude":
        return resolved_home / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    if backend == "antigravity":
        if os.name == "nt":
            source_env = os.environ if env is None else env
            local_app_data = source_env.get("LOCALAPPDATA")
            if local_app_data:
                return Path(local_app_data) / "agy" / "bin" / "agy.exe"
            return resolved_home / ".local" / "bin" / "agy.exe"
        return resolved_home / ".local" / "bin" / "agy"
    return None


def identify_repair_source(
    backend: str,
    selected: str,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> RepairSource | None:
    if backend not in PACKAGES:
        return None
    home = home or Path.home()
    original = Path(selected).expanduser().absolute()
    try:
        target = original.resolve()
        package = PACKAGES[backend]
        if package is not None:
            package_parts = Path(package).parts
            if os.name == "nt" and original.name.lower() == backend + ".cmd":
                manifest = original.parent / "node_modules" / package / "package.json"
                if manifest.is_file() and manifest.stat().st_size < 65536 and original.stat().st_size < 16384:
                    wrapper = original.read_text(encoding="utf-8").replace("\\", "/").lower()
                    if ("%~dp0" in wrapper or "%dp0%" in wrapper) and "node_modules/" + package in wrapper:
                        if json.loads(manifest.read_text(encoding="utf-8")).get("name") == package:
                            return RepairSource("npm", str(original), package, original.parent)
            for directory in target.parents:
                if tuple(directory.parts[-len(package_parts) :]) != package_parts:
                    continue
                modules = directory.parents[len(package_parts) - 1]
                if modules.name != "node_modules":
                    continue
                manifest = directory / "package.json"
                if not manifest.is_file() or manifest.stat().st_size >= 65536:
                    return None
                if json.loads(manifest.read_text(encoding="utf-8")).get("name") != package:
                    return None
                if modules.parent.name == "lib":
                    prefix = modules.parent.parent
                elif os.name == "nt" and original.parent == modules.parent:
                    prefix = modules.parent
                else:
                    # A project-local npm install is not an npm-global prefix.
                    return None
                return RepairSource("npm", str(original), package, prefix)
        brew_name = BREW_NAMES[backend]
        if brew_name is not None:
            for root in target.parents:
                if root.name not in {"Cellar", "Caskroom"}:
                    continue
                relative = target.relative_to(root)
                if relative.parts[0] != brew_name:
                    return None
                return RepairSource("brew", str(original), brew_name, root.parent, root.name == "Caskroom")
        native = native_vendor_install_target(backend, home=home, env=env)
        if native is not None:
            native = native.expanduser().absolute()
            if backend == "claude":
                versions = home / ".local" / "share" / "claude" / "versions"
                if original == native and (not original.is_symlink() or target.is_relative_to(versions)):
                    return RepairSource("native", str(native))
            else:
                if original == native and (not original.is_symlink() or target.parent == original.parent):
                    return RepairSource("native", str(native))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return None


def native_bootstrap_env(env: dict[str, str], *, hide_directory: Path | None = None) -> dict[str, str]:
    """No shell startup hooks/functions may reintroduce the hidden old CLI."""
    result = {
        k: v
        for k, v in env.items()
        if k not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"} and not k.startswith("BASH_FUNC_")
    }
    if hide_directory is not None:
        hidden = hide_directory.resolve()
        result["PATH"] = os.pathsep.join(
            entry
            for entry in result.get("PATH", "").split(os.pathsep)
            if Path(entry or os.curdir).expanduser().resolve() != hidden
        )
    return result
