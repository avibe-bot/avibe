"""Shared executable discovery for configured local CLI tools."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from vibe.desktop_backends import is_desktop_backend_path, resolve_published_desktop_backend

logger = logging.getLogger(__name__)

def _is_executable_file(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


_NVM_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")
_NVM_SUFFIX_TOKEN_RE = re.compile(r"\d+|\D+")


def _nvm_suffix_tokens(suffix: str) -> tuple[tuple[int, int, str], ...]:
    # Tokenize the prerelease suffix into (kind, num, text) triples so all
    # tokens are structurally identical and comparable. kind=0 marks numeric
    # tokens (compared by num) and kind=1 marks alphanumeric tokens (compared
    # by text). Numeric tokens compare numerically, so "-rc.10" beats
    # "-rc.2"; cross-kind tokens never compare int-vs-str, ruling out
    # TypeError for arbitrary suffix shapes.
    triples: list[tuple[int, int, str]] = []
    for tok in _NVM_SUFFIX_TOKEN_RE.findall(suffix):
        if tok.isdigit():
            triples.append((0, int(tok), ""))
        else:
            triples.append((1, 0, tok))
    return tuple(triples)


def _nvm_version_sort_key(entry: Path) -> tuple:
    # Returns (major, minor, patch, is_released, suffix_tokens). is_released
    # is True for plain "vX.Y.Z" and False for any "-suffix"; with reverse=True
    # released versions outrank pre-releases of the same triple. Within
    # pre-releases, suffix_tokens compares numerically where digits appear.
    m = _NVM_VERSION_RE.match(entry.name)
    if not m:
        return (-1, -1, -1, False, ())
    major = int(m.group(1))
    minor = int(m.group(2)) if m.group(2) else 0
    patch = int(m.group(3)) if m.group(3) else 0
    suffix = m.group(4) or ""
    return (major, minor, patch, not suffix, _nvm_suffix_tokens(suffix))


def _nvm_binary_candidates(binary: str) -> list[Path]:
    versions_dir = Path.home() / ".nvm" / "versions" / "node"
    if not versions_dir.exists():
        return []

    valid: list[Path] = []
    for entry in versions_dir.iterdir():
        # Skip non-directory entries (e.g. macOS .DS_Store) and non-version
        # dirs (e.g. nvm's "system" alias) before sorting.
        if not entry.is_dir():
            continue
        if not _NVM_VERSION_RE.match(entry.name):
            continue
        valid.append(entry)

    candidates: list[Path] = []
    for version_dir in sorted(valid, key=_nvm_version_sort_key, reverse=True):
        candidate = version_dir / "bin" / binary
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _npm_global_binary_candidates(binary: str) -> list[Path]:
    if not binary or binary == "npm":
        return []

    candidates: list[Path] = []
    for prefix_path in _npm_global_prefixes():
        for candidate in _npm_binary_candidates_for_prefix(prefix_path, binary):
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _npm_global_prefixes() -> list[Path]:
    # Global packages belong to the npm selected by the current environment.
    # Querying every historical NVM installation makes one missing CLI cost up
    # to five seconds per Node version without improving that answer.
    which_npm = shutil.which("npm")
    npm_path = Path(which_npm) if which_npm else next(
        (
            candidate
            for candidate in _candidate_cli_paths(
                "npm",
                include_npm_global=False,
            )
            if _is_executable_file(candidate)
        ),
        None,
    )
    if npm_path is None:
        return []
    prefix_path = _npm_prefix_for(npm_path)
    return [prefix_path] if prefix_path is not None else []


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
    if not prefix:
        return None

    return Path(os.path.expanduser(prefix[-1]))


def _npm_binary_candidates_for_prefix(prefix_path: Path, binary: str) -> list[Path]:
    derived_candidates = [
        prefix_path / "bin" / binary,
        prefix_path / binary,
        prefix_path / "node_modules" / ".bin" / binary,
    ]
    if os.name == "nt":
        derived_candidates.extend(
            [
                prefix_path / f"{binary}.cmd",
                prefix_path / f"{binary}.exe",
                prefix_path / "node_modules" / ".bin" / f"{binary}.cmd",
            ]
        )
    return derived_candidates


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


def _candidate_cli_paths(
    binary: str,
    *,
    include_npm_global: bool = True,
) -> list[Path]:
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
    for candidate in common_candidates + _nvm_binary_candidates(binary):
        if candidate not in candidates:
            candidates.append(candidate)
    if include_npm_global:
        for candidate in _npm_global_binary_candidates(binary):
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


def _resolve_cli_path_once(
    binary: str,
    *,
    include_npm_global: bool,
    candidate_paths=None,
    is_executable_file=None,
) -> str | None:
    candidate_paths = candidate_paths or _candidate_cli_paths
    is_executable_file = is_executable_file or _is_executable_file
    for candidate in candidate_paths(binary, include_npm_global=False):
        if is_executable_file(candidate):
            return str(candidate)

    path = shutil.which(os.path.expanduser(binary)) if binary else None
    if path:
        return path

    if include_npm_global:
        for candidate in _npm_global_binary_candidates(binary):
            if _is_executable_file(candidate):
                return str(candidate)
    return None


def resolve_cli_path(
    binary: str,
    *,
    include_npm_global: bool = True,
    candidate_paths=None,
    is_executable_file=None,
    include_desktop: bool = True,
) -> str | None:
    path = _resolve_cli_path_once(
        binary,
        include_npm_global=include_npm_global,
        candidate_paths=candidate_paths,
        is_executable_file=is_executable_file,
    )
    if path:
        return path

    # The stored cli_path was an absolute path that no longer exists. Most
    # common cause: an upstream installer moved the binary out from under us.
    # Real-world example: Claude Code's official ``install.sh`` puts the
    # native binary at ``~/.local/bin/claude`` (via ``~/.local/share/claude/
    # versions/<ver>``), while the legacy ``npm install -g
    # @anthropic-ai/claude-code`` install used ``/usr/local/bin/claude``.
    # After clicking "Upgrade" in the UI, V2Config still points at the
    # /usr/local/bin path, so the runtime probe reports ``installed=false``
    # and the chip flips to "not installed". Fall back to discovery using
    # only the basename — if a binary with that name is on any of the
    # standard candidate paths (~/.local/bin, /opt/homebrew/bin, npm/nvm/bun
    # globals, etc.) we treat that as the live install. The basename
    # restriction means custom callers passing ``"/path/to/my-claude"``
    # don't get silently redirected to the system claude.
    if not binary:
        return None
    expanded = Path(os.path.expanduser(binary))
    has_path_separator = os.sep in binary or (os.altsep is not None and os.altsep in binary)
    if expanded.is_absolute() and is_desktop_backend_path(expanded):
        lookup_name = expanded.stem if expanded.suffix.lower() == ".exe" else expanded.name
        if include_desktop and lookup_name in {"claude", "codex", "opencode"}:
            return resolve_published_desktop_backend(lookup_name)
        return None
    if expanded.is_absolute() or has_path_separator:
        basename = expanded.name
        if basename and basename != binary:
            fallback = _resolve_cli_path_once(
                basename,
                include_npm_global=include_npm_global,
                candidate_paths=candidate_paths,
                is_executable_file=is_executable_file,
            )
            if fallback:
                logger.info(
                    "resolve_cli_path: stored path %s missing; falling back to %s",
                    binary,
                    fallback,
                )
                return fallback
    if include_desktop:
        lookup_name = expanded.name if expanded.is_absolute() or has_path_separator else binary
        if lookup_name in {"claude", "codex", "opencode"}:
            return resolve_published_desktop_backend(lookup_name)
    return None


def _command_env_for(binary_path: str | None) -> dict[str, str]:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    if not binary_path:
        return env

    binary_dir = str(Path(binary_path).expanduser().resolve().parent)
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != binary_dir]
    env["PATH"] = os.pathsep.join([binary_dir, *path_entries])
    return env
