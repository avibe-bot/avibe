"""Recognize existing CLI installation provenance without executing that CLI.

Unknown sources are not invitations to migrate an installation. These plans
only cover package layouts or canonical native destinations we can identify.
The installer owner executes them under its existing lock and reads back the
exact destination with its normal (unmasked) launch environment.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

PACKAGES = {"claude": "@anthropic-ai/claude-code", "opencode": "opencode-ai", "codex": "@openai/codex"}
BREW_NAMES = {"claude": "claude-code", "opencode": "opencode", "codex": "codex"}
REPAIR_PATH_CONFLICT = "avibe: repair blocked by another resolvable opencode"


@dataclass(frozen=True)
class RepairSource:
    kind: str
    destination: str
    package: str | None = None
    prefix: Path | None = None
    cask: bool = False


def identify_repair_source(backend: str, selected: str, *, home: Path | None = None) -> RepairSource | None:
    if backend not in PACKAGES:
        return None
    home = home or Path.home()
    original = Path(selected).expanduser().absolute()
    try:
        target = original.resolve()
        package = PACKAGES[backend]
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
        for root in target.parents:
            if root.name not in {"Cellar", "Caskroom"}:
                continue
            relative = target.relative_to(root)
            if relative.parts[0] != BREW_NAMES[backend]:
                return None
            return RepairSource("brew", str(original), BREW_NAMES[backend], root.parent, root.name == "Caskroom")
        if backend == "claude":
            native = home / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
            versions = home / ".local" / "share" / "claude" / "versions"
            if original == native and (not original.is_symlink() or target.is_relative_to(versions)):
                return RepairSource("native", str(native))
        if backend == "opencode" and original == home / ".opencode" / "bin" / "opencode":
            if not original.is_symlink() or target.parent == original.parent:
                return RepairSource("native", str(original))
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
