from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast


PACKAGE_NAME = "avibe-os"
LEGACY_PACKAGE_NAME = "vibe-remote"
DEFAULT_UPDATE_METADATA_URL = f"https://pypi.org/pypi/{PACKAGE_NAME}/json"
CURRENT_VIBE_EXECUTABLE_ENV = "VIBE_CURRENT_EXECUTABLE"
SHOW_RUNTIME_SKIP_ENV = "VIBE_INSTALL_SKIP_SHOW_RUNTIME"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
UV_FALLBACK_BIN_DIRS = (".local/bin", ".cargo/bin")
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


@dataclass(frozen=True)
class UpgradePlan:
    command: list[str]
    env: dict[str, str] | None
    method: str


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
    return "/uv/tools/" in executable


def is_legacy_uv_tool_install(python_executable: str | None = None) -> bool:
    executable = (python_executable or sys.executable or "").replace("\\", "/")
    return f"/uv/tools/{LEGACY_PACKAGE_NAME}/" in executable


def get_current_vibe_bin_dir(vibe_path: str | None = None) -> str | None:
    current_vibe = get_running_vibe_path(vibe_path=vibe_path)
    if not current_vibe:
        return None

    return get_launcher_bin_dir(current_vibe)


def build_upgrade_plan(
    *,
    python_executable: str | None = None,
    uv_path: str | None = None,
    vibe_path: str | None = None,
    base_env: dict[str, str] | None = None,
) -> UpgradePlan:
    executable = python_executable or sys.executable
    uv_binary = find_uv_binary(uv_path=uv_path, base_env=base_env)
    package_spec = get_upgrade_package_spec()

    if is_uv_tool_install(executable) and uv_binary:
        env = dict(base_env or os.environ)
        vibe_bin_dir = get_current_vibe_bin_dir(vibe_path)
        if vibe_bin_dir:
            env["UV_TOOL_BIN_DIR"] = vibe_bin_dir
        command = [uv_binary, "tool", "install", package_spec, "--upgrade"]
        if package_spec != PACKAGE_NAME or is_legacy_uv_tool_install(executable):
            command.append("--force")
        return UpgradePlan(
            command=command,
            env=env,
            method="uv",
        )

    return UpgradePlan(
        command=[executable, "-m", "pip", "install", "--upgrade", package_spec],
        env=dict(base_env or os.environ),
        method="pip",
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
