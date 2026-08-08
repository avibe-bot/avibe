"""Shared executable discovery for configured local CLI tools."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from vibe.desktop_backends import resolve_published_desktop_backend


logger = logging.getLogger(__name__)

_NVM_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
_NVM_SUFFIX_TOKEN_RE = re.compile(r"\d+|\D+")


def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def _command_env_for(binary_path: str | None) -> dict[str, str]:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    if not binary_path:
        return env

    binary_dir = str(Path(binary_path).expanduser().resolve().parent)
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != binary_dir]
    env["PATH"] = os.pathsep.join([binary_dir, *path_entries])
    return env


def _nvm_suffix_tokens(suffix: str) -> tuple[tuple[int, int, str], ...]:
    triples: list[tuple[int, int, str]] = []
    for token in _NVM_SUFFIX_TOKEN_RE.findall(suffix):
        if token.isdigit():
            triples.append((0, int(token), ""))
        else:
            triples.append((1, 0, token))
    return tuple(triples)


def _nvm_version_sort_key(entry: Path) -> tuple:
    match = _NVM_VERSION_RE.match(entry.name)
    if not match:
        return (-1, -1, -1, False, ())
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    patch = int(match.group(3)) if match.group(3) else 0
    suffix = match.group(4) or ""
    return (major, minor, patch, not suffix, _nvm_suffix_tokens(suffix))


def _nvm_binary_candidates(binary: str) -> list[Path]:
    versions_dir = Path.home() / ".nvm" / "versions" / "node"
    if not versions_dir.exists():
        return []

    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return []
    valid = [
        entry
        for entry in entries
        if entry.is_dir() and _NVM_VERSION_RE.match(entry.name)
    ]
    return [
        version_dir / "bin" / binary
        for version_dir in sorted(valid, key=_nvm_version_sort_key, reverse=True)
    ]


def _npm_prefix_for(npm_path: str | Path) -> Path | None:
    try:
        result = subprocess.run(
            [str(npm_path), "config", "get", "prefix"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_command_env_for(str(npm_path)),
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None
    prefix = (result.stdout or "").strip().splitlines()
    return Path(os.path.expanduser(prefix[-1])) if prefix else None


def _npm_binary_candidates_for_prefix(prefix_path: Path, binary: str) -> list[Path]:
    candidates = [
        prefix_path / "bin" / binary,
        prefix_path / binary,
        prefix_path / "node_modules" / ".bin" / binary,
    ]
    if os.name == "nt":
        candidates.extend(
            [
                prefix_path / f"{binary}.cmd",
                prefix_path / f"{binary}.exe",
                prefix_path / "node_modules" / ".bin" / f"{binary}.cmd",
            ]
        )
    return candidates


def _npm_global_binary_candidates(binary: str) -> list[Path]:
    if not binary or binary == "npm":
        return []

    npm_paths: list[Path] = []
    for candidate in _candidate_cli_paths("npm"):
        if _is_executable_file(candidate) and candidate not in npm_paths:
            npm_paths.append(candidate)
    which_npm = shutil.which("npm")
    if which_npm:
        candidate = Path(which_npm)
        if candidate not in npm_paths:
            npm_paths.append(candidate)

    candidates: list[Path] = []
    for npm_path in npm_paths:
        prefix_path = _npm_prefix_for(npm_path)
        if prefix_path is None:
            continue
        for candidate in _npm_binary_candidates_for_prefix(prefix_path, binary):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _windows_executable_candidates(candidates: list[Path]) -> list[Path]:
    result: list[Path] = []
    for candidate in candidates:
        result.append(candidate)
        if candidate.suffix.lower() not in {".cmd", ".exe"}:
            result.extend(
                [
                    candidate.with_name(f"{candidate.name}.exe"),
                    candidate.with_name(f"{candidate.name}.cmd"),
                ]
            )
    return result


def _candidate_cli_paths(binary: str) -> list[Path]:
    if not binary:
        return []

    expanded = Path(os.path.expanduser(binary))
    has_path_separator = os.sep in binary or (os.altsep is not None and os.altsep in binary)
    if expanded.is_absolute() or has_path_separator:
        return [expanded]

    home = Path.home()
    candidates: list[Path] = []
    if binary == "claude":
        candidates.append(home / ".claude" / "local" / "claude")
    elif binary == "opencode":
        candidates.extend(
            [
                home / ".opencode" / "bin" / "opencode",
                home / ".local" / "bin" / "opencode",
            ]
        )

    common_candidates = [
        home / ".local" / "bin" / binary,
        home / ".bun" / "bin" / binary,
        Path("/opt/homebrew/bin") / binary,
        Path("/usr/local/bin") / binary,
    ]
    if os.name == "nt":
        common_candidates = _windows_executable_candidates(common_candidates)
    for candidate in common_candidates + _nvm_binary_candidates(binary) + _npm_global_binary_candidates(binary):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def resolve_cli_path(
    binary: str,
    *,
    candidate_paths: Callable[[str], list[Path]] = _candidate_cli_paths,
    is_executable_file: Callable[[Path], bool] = _is_executable_file,
) -> str | None:
    """Resolve a configured command without relying on a GUI process PATH."""

    for candidate in candidate_paths(binary):
        if is_executable_file(candidate):
            return str(candidate)

    path = shutil.which(os.path.expanduser(binary)) if binary else None
    if path:
        return path

    if binary in {"claude", "codex", "opencode"}:
        managed_path = resolve_published_desktop_backend(binary)
        if managed_path:
            return managed_path

    if not binary:
        return None
    expanded = Path(os.path.expanduser(binary))
    has_path_separator = os.sep in binary or (os.altsep is not None and os.altsep in binary)
    if expanded.is_absolute() or has_path_separator:
        basename = expanded.name
        if basename and basename != binary:
            for candidate in candidate_paths(basename):
                if is_executable_file(candidate):
                    logger.info(
                        "resolve_cli_path: stored path %s missing; falling back to %s",
                        binary,
                        candidate,
                    )
                    return str(candidate)
            if basename in {"claude", "codex", "opencode"}:
                managed_path = resolve_published_desktop_backend(basename)
                if managed_path:
                    return managed_path
    return None
