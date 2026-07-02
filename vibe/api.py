import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import ssl
import stat
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy import select

from config import paths
from config.v2_config import CONFIG_LOCK, V2Config
from config.v2_settings import (
    SettingsStore,
    ChannelSettings,
    GuildSettings,
    UserSettings,
    RoutingSettings,
    normalize_show_message_types,
    _parse_routing,
    _routing_to_dict,
    routing_to_compat_dict,
)
from config.v2_sessions import SessionsStore
from config.platform_registry import get_platform_descriptor
from vibe.opencode_config import (
    get_opencode_config_paths,
    load_first_opencode_user_config,
    set_jsonc_top_level_string_property,
)
from vibe.upgrade import (
    build_upgrade_plan,
    get_latest_version_info,
    get_running_vibe_path,
    get_safe_cwd,
    should_skip_show_runtime_prepare,
)
from vibe.restart_supervisor import schedule_restart
from vibe.claude_model_catalog import DEFAULT_CLAUDE_MODEL_ALIASES, load_catalog_models
from vibe.i18n import t as backend_t
from modules.agents.catalog import (
    agent_backend_catalog_payload,
    agent_backend_descriptors,
    is_agent_backend,
    latest_probe_for_backend,
    runtime_refresh_success_message,
    supports_runtime_refresh,
    supports_web_oauth,
)
from modules.agents.subagent_router import list_codex_subagents
from core.vibe_agents import (
    VibeAgentStore,
    iter_global_agent_files,
    parse_agent_file,
    validate_agent_backend,
)
from core.process_isolation import isolated_subprocess_kwargs, signal_process_tree, KILL_SIGNAL


logger = logging.getLogger(__name__)

# Cache per cwd: { cwd: { "data": ..., "updated_at": ... } }
_OPENCODE_OPTIONS_CACHE: dict[str, dict] = {}
_OPENCODE_OPTIONS_TTL_SECONDS = 30.0


_PLATFORM_SECRET_FIELDS: dict[str, tuple[str, ...]] = {
    "slack": ("bot_token", "app_token", "signing_secret"),
    "discord": ("bot_token",),
    "telegram": ("bot_token", "webhook_secret_token"),
    "lark": ("app_secret",),
    "wechat": ("bot_token",),
}
_GATEWAY_SECRET_FIELDS = ("workspace_token", "client_secret")


def _parse_agent_import_file(path: Path, *, backend: str):
    try:
        return parse_agent_file(path, backend=backend)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to read or parse agent import file: {exc}") from exc


def _validate_direct_agent_import_path(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise ValueError(f"Unable to validate agent import file: {exc}") from exc
    if resolved.suffix.lower() != ".md":
        raise ValueError("Agent import file must be a Markdown (.md) file")
    try:
        raw = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Unable to read or parse agent import file: {exc}") from exc
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Agent import file must include YAML frontmatter with a name field")
    end_idx = next((idx for idx, line in enumerate(lines[1:], start=1) if line.strip() == "---"), -1)
    if end_idx < 0:
        raise ValueError("Agent import file must include YAML frontmatter with a name field")
    try:
        header = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Unable to read or parse agent import file: {exc}") from exc
    if not isinstance(header, dict) or not str(header.get("name") or "").strip():
        raise ValueError("Agent import file must include YAML frontmatter with a name field")
    return resolved


def _enabled_agent_backends_from_config(config: Optional[V2Config] = None) -> list[str]:
    try:
        cfg = config or load_config()
    except FileNotFoundError:
        return [descriptor.id for descriptor in agent_backend_descriptors() if descriptor.default_enabled]
    result: list[str] = []
    agents = getattr(cfg, "agents", None)
    if agents is not None:
        for backend in ("opencode", "claude", "codex"):
            backend_cfg = getattr(agents, backend, None)
            if backend_cfg is not None and bool(getattr(backend_cfg, "enabled", True)):
                result.append(backend)
    return result


def _ensure_builtin_default_agents(config: Optional[V2Config] = None) -> None:
    backends = _enabled_agent_backends_from_config(config)
    store = VibeAgentStore()
    try:
        store.ensure_builtin_default_agents(backends)
    finally:
        store.close()


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

    npm_paths: list[Path] = []
    for candidate in _candidate_cli_paths("npm"):
        if _is_executable_file(candidate) and candidate not in npm_paths:
            npm_paths.append(candidate)

    which_npm = shutil.which("npm")
    if which_npm:
        npm_candidate = Path(which_npm)
        if npm_candidate not in npm_paths:
            npm_paths.append(npm_candidate)

    candidates: list[Path] = []
    for npm_path in npm_paths:
        prefix_path = _npm_prefix_for(npm_path)
        if prefix_path is None:
            continue

        for candidate in _npm_binary_candidates_for_prefix(prefix_path, binary):
            if candidate not in candidates:
                candidates.append(candidate)

    return candidates


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


def resolve_cli_path(binary: str) -> str | None:
    for candidate in _candidate_cli_paths(binary):
        if _is_executable_file(candidate):
            return str(candidate)

    path = shutil.which(os.path.expanduser(binary)) if binary else None
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
    if expanded.is_absolute() or has_path_separator:
        basename = expanded.name
        if basename and basename != binary:
            for candidate in _candidate_cli_paths(basename):
                if _is_executable_file(candidate):
                    logger.info(
                        "resolve_cli_path: stored path %s missing; falling back to %s",
                        binary,
                        candidate,
                    )
                    return str(candidate)
    return None


def _command_env_for(binary_path: str | None) -> dict[str, str]:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    if not binary_path:
        return env

    binary_dir = str(Path(binary_path).expanduser().resolve().parent)
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != binary_dir]
    env["PATH"] = os.pathsep.join([binary_dir, *path_entries])
    return env


def _codex_npm_install_env(npm_path: str, *, prefix: str | Path | None = None) -> dict[str, str]:
    env = _command_env_for(npm_path)
    if os.name == "nt":
        return env

    install_prefix = Path(os.path.expanduser(str(prefix))) if prefix is not None else Path.home() / ".local"
    prefix = str(install_prefix)
    prefix_bin = str(install_prefix / "bin")
    path_entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry and entry != prefix_bin]
    env["PATH"] = os.pathsep.join([prefix_bin, *path_entries])
    env["NPM_CONFIG_PREFIX"] = prefix
    return env


def _npm_prefix_from_node_modules_path(codex_path: str) -> Path | None:
    try:
        paths = [Path(codex_path).expanduser(), Path(codex_path).expanduser().resolve()]
    except OSError:
        return None

    for path in paths:
        parts = path.parts
        for index in range(len(parts) - 2):
            if parts[index : index + 3] != ("node_modules", "@openai", "codex"):
                continue
            if index == 0 or parts[index - 1] != "lib":
                continue
            prefix_parts = parts[: index - 1]
            if prefix_parts:
                return Path(*prefix_parts)
    return None


def _npm_prefix_for_existing_codex_install(npm_path: str, codex_path: str) -> Path | None:
    try:
        original = Path(codex_path).expanduser()
        resolved = original.resolve()
    except OSError:
        resolved = None

    npm_prefix = _npm_prefix_for(npm_path)
    if npm_prefix is None:
        return None

    inferred_prefix = _npm_prefix_from_node_modules_path(codex_path)
    if inferred_prefix is not None:
        try:
            if inferred_prefix.resolve() == npm_prefix.resolve():
                return npm_prefix
        except OSError:
            if inferred_prefix == npm_prefix:
                return npm_prefix

    for candidate in _npm_binary_candidates_for_prefix(npm_prefix, "codex"):
        try:
            candidate_resolved = candidate.resolve()
        except OSError:
            candidate_resolved = None
        if original == candidate or (resolved is not None and candidate_resolved == resolved):
            return npm_prefix

    return None


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_npm_codex_install(codex_path: str) -> bool:
    try:
        resolved = Path(codex_path).expanduser().resolve()
    except OSError:
        return False
    parts = resolved.parts
    if "node_modules" in parts and "@openai" in parts and "codex" in parts:
        return True

    try:
        original = Path(codex_path).expanduser()
    except OSError:
        return False
    for candidate in _npm_global_binary_candidates("codex"):
        if original == candidate or resolved == candidate.resolve():
            return True
    return False


def _is_homebrew_codex_install(codex_path: str) -> bool:
    brew_path = resolve_cli_path("brew")
    if not brew_path:
        return False

    try:
        result = subprocess.run(
            [brew_path, "list", "--cask", "codex"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_command_env_for(brew_path),
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False

    try:
        resolved = Path(codex_path).expanduser().resolve()
    except OSError:
        return False

    brew_prefixes: list[Path] = []
    for command in ([brew_path, "--prefix"], [brew_path, "--prefix", "--cask"]):
        try:
            prefix_result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=10,
                env=_command_env_for(brew_path),
            )
        except Exception:
            continue
        if prefix_result.returncode != 0:
            continue
        prefix = (prefix_result.stdout or "").strip().splitlines()
        if prefix:
            brew_prefixes.append(Path(os.path.expanduser(prefix[-1])))

    brew_prefixes.extend([Path("/opt/homebrew"), Path("/usr/local"), Path("/home/linuxbrew/.linuxbrew")])
    return any(_path_is_relative_to(resolved, prefix) for prefix in brew_prefixes)


def _codex_cli_supports_update(codex_path: str) -> bool:
    try:
        result = subprocess.run(
            [codex_path, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_command_env_for(codex_path),
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return re.search(r"(?m)^\s*update\s+", output) is not None


def _codex_upgrade_command(existing_path: str) -> tuple[list[str], dict[str, str] | None] | dict:
    if _is_homebrew_codex_install(existing_path):
        brew_path = resolve_cli_path("brew")
        if not brew_path:
            return {
                "ok": False,
                "message": "Codex appears to be installed via Homebrew, but brew was not found. Please upgrade Codex manually.",
                "output": None,
            }
        return [brew_path, "upgrade", "--cask", "codex"], None

    if _is_npm_codex_install(existing_path):
        npm_path = resolve_cli_path("npm")
        if not npm_path:
            return {
                "ok": False,
                "message": "Codex appears to be installed via npm, but npm was not found. Please install Node.js or upgrade Codex manually.",
                "output": None,
            }
        prefix = _npm_prefix_for_existing_codex_install(npm_path, existing_path)
        return [npm_path, "install", "-g", "@openai/codex"], _codex_npm_install_env(npm_path, prefix=prefix)

    if _codex_cli_supports_update(existing_path):
        return [existing_path, "update"], None

    return {
        "ok": False,
        "message": "Could not determine how Codex was installed, and this Codex CLI does not expose an update command. Please upgrade Codex with the installer you originally used.",
        "output": None,
    }


def browse_directory(path: str, show_hidden: bool = False) -> dict:
    """List sub-directories of *path* for the directory browser UI.

    Symlinks are not followed when scanning entries.

    Returns ``{"ok": True, "path": <abs>, "parent": <abs|None>, "dirs": [...]}``
    where each entry in *dirs* is ``{"name": ..., "path": ...}``.
    """
    try:
        target = Path(os.path.expanduser(path or "~")).resolve()

        if not target.is_dir():
            return {"ok": False, "error": f"Not a directory: {target}"}

        abs_path = str(target)
        parent = str(target.parent) if target.parent != target else None

        entries: list[dict[str, str]] = []
        try:
            for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
                if not show_hidden and entry.name.startswith("."):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    entries.append({"name": entry.name, "path": str(target / entry.name)})
        except PermissionError:
            return {"ok": False, "error": "permission_denied"}

        return {"ok": True, "path": abs_path, "parent": parent, "dirs": entries}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def browse_favorites() -> dict:
    """Return OS-appropriate quick-access directories for the directory picker.

    Each entry is ``{"key": <stable id>, "path": <abs>}``; only directories that
    actually exist are returned, so the UI never renders a dead shortcut. The
    ``key`` lets the UI localize the well-known shortcuts (home/desktop/...),
    while OS-specific roots (``/tmp``, ``/data``, Windows drive letters) are
    shown by their path. Home is always first. Mirrors how Finder/Explorer/most
    Linux file managers seed their sidebar per platform.
    """
    import platform

    home = Path(os.path.expanduser("~"))
    system = platform.system().lower()  # 'darwin' | 'linux' | 'windows'

    # (key, path) candidates in display order. Existence is verified below, so
    # OS-specific entries absent on this machine simply drop out of the list.
    candidates: list[tuple[str, Path]] = [("home", home)]
    if system == "darwin":
        candidates += [
            ("desktop", home / "Desktop"),
            ("documents", home / "Documents"),
            ("downloads", home / "Downloads"),
            ("applications", Path("/Applications")),
            ("tmp", Path("/tmp")),
        ]
    elif system == "windows":
        candidates += [
            ("desktop", home / "Desktop"),
            ("documents", home / "Documents"),
            ("downloads", home / "Downloads"),
        ]
        # Drive roots (C:\, D:\, …) as quick jumps to a volume's top level.
        candidates += [(f"drive_{letter.lower()}", Path(f"{letter}:\\")) for letter in "CDEFG"]
    else:  # linux / other unix
        candidates += [
            ("desktop", home / "Desktop"),
            ("documents", home / "Documents"),
            ("downloads", home / "Downloads"),
            ("root", Path("/")),
            ("tmp", Path("/tmp")),
            ("data", Path("/data")),
            ("mnt", Path("/mnt")),
            ("media", Path("/media")),
        ]

    seen: set[str] = set()
    favorites: list[dict] = []
    for key, candidate in candidates:
        try:
            abs_path = str(candidate)
            if abs_path in seen or not candidate.is_dir():
                continue
        except OSError:
            continue
        seen.add(abs_path)
        favorites.append({"key": key, "path": abs_path})

    return {"ok": True, "system": system, "favorites": favorites}


def load_config() -> V2Config:
    return V2Config.load()


def _deep_merge_dicts(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


_AGENT_AUTH_FIELDS = ("auth_mode", "api_key", "base_url")


def _strip_agent_auth_fields(payload: dict) -> dict:
    """Drop auth fields from a generic settings patch.

    The UI's Settings → Backends page round-trips the masked agent config
    on save (api_key arrives as ``None`` after masking), and naive
    deep-merge would clobber the real key. Auth state changes must go
    through ``/backend/<name>/auth`` exclusively; this helper enforces
    that contract on the generic settings POST.
    """
    if not isinstance(payload, dict):
        return payload
    agents = payload.get("agents")
    if not isinstance(agents, dict):
        return payload
    cleaned_agents = dict(agents)
    for backend in ("claude", "codex"):
        backend_payload = cleaned_agents.get(backend)
        if isinstance(backend_payload, dict):
            cleaned_backend = {
                k: v for k, v in backend_payload.items() if k not in _AGENT_AUTH_FIELDS
            }
            cleaned_agents[backend] = cleaned_backend
    return {**payload, "agents": cleaned_agents}


def _strip_preserved_config_secrets(payload: dict) -> dict:
    """Drop redacted secret placeholders from generic config saves.

    ``GET /api/config`` now returns ``has_<field>`` metadata instead of
    plaintext platform and gateway secrets. Older UI paths may still round-trip
    an empty string for such a field; when the metadata says the secret is
    already configured, an empty submitted value means "unchanged", not "clear".
    """
    if not isinstance(payload, dict):
        return payload
    cleaned = dict(payload)
    for config_key, fields in _PLATFORM_SECRET_FIELDS.items():
        section = cleaned.get(config_key)
        if not isinstance(section, dict):
            continue
        next_section = dict(section)
        for field in fields:
            if next_section.get(f"has_{field}") is True and not next_section.get(field):
                next_section.pop(field, None)
        cleaned[config_key] = next_section
    gateway = cleaned.get("gateway")
    if isinstance(gateway, dict):
        next_gateway = dict(gateway)
        for field in _GATEWAY_SECRET_FIELDS:
            if next_gateway.get(f"has_{field}") is True and not next_gateway.get(field):
                next_gateway.pop(field, None)
        cleaned["gateway"] = next_gateway
    return cleaned


def _mark_explicit_audio_asr_enabled(payload: dict) -> dict:
    """Record explicit ASR enablement changes from config API payloads.

    Persisted configs from older versions may contain ``enabled: false`` only
    because that was the old default. A current API request that includes the
    same field is different: it is the user's requested value and must survive
    the migration default.
    """
    if not isinstance(payload, dict):
        return payload
    audio_asr = payload.get("audio_asr")
    if not isinstance(audio_asr, dict) or "enabled" not in audio_asr or "enabled_configured" in audio_asr:
        return payload
    return {**payload, "audio_asr": {**audio_asr, "enabled_configured": True}}


def _runtime_credential_fields_for_platform(platform: str) -> tuple[str, ...]:
    if platform == "wechat":
        return ()
    return get_platform_descriptor(platform).credential_fields


def _payload_edits_runtime_credential(section: dict, field: str, base_value: object) -> bool:
    if field not in section:
        return False
    value = section.get(field)
    if value == base_value:
        return False
    if not value and section.get(f"has_{field}") is True:
        return False
    return True


def _platforms_requiring_runtime_credential_validation(
    config: V2Config,
    payload: dict,
    base_config: Optional[V2Config],
) -> set[str]:
    enabled = set(config.platforms.enabled)
    previous_enabled = set(base_config.platforms.enabled) if base_config is not None else set()
    platforms = enabled - previous_enabled if "platforms" in payload or "platform" in payload else set()

    # Finishing setup promotes the saved config to a runnable runtime config, so
    # every enabled adapter must be bootable at that boundary.
    if payload.get("setup_completed") is True:
        platforms.update(enabled)

    # If a save edits credential fields for an already-enabled platform, reject
    # partial clears or mismatched edits before they are persisted.
    for platform in enabled:
        required_fields = _runtime_credential_fields_for_platform(platform)
        if not required_fields:
            continue
        descriptor = get_platform_descriptor(platform)
        section = payload.get(descriptor.config_key)
        base_platform_config = descriptor.get_config(base_config) if base_config is not None else None
        if isinstance(section, dict) and any(
            _payload_edits_runtime_credential(
                section,
                field,
                getattr(base_platform_config, field, None),
            )
            for field in required_fields
        ):
            platforms.add(platform)
    return platforms


def _validate_enabled_platform_runtime_credentials(
    config: V2Config,
    payload: dict,
    base_config: Optional[V2Config],
) -> None:
    """Reject enabled IM transports that cannot start after config save.

    ``V2Config.from_payload`` intentionally allows empty credential fields so
    users can save setup drafts for disabled platforms. Once a platform enters
    ``platforms.enabled``, however, the running adapter must be able to boot.
    WeChat is the existing exception: its runtime idles without a token while
    the QR-login flow completes.
    """
    for platform in _platforms_requiring_runtime_credential_validation(config, payload, base_config):
        required_fields = _runtime_credential_fields_for_platform(platform)
        if not required_fields:
            continue
        descriptor = get_platform_descriptor(platform)
        platform_config = descriptor.get_config(config)
        missing = [field for field in required_fields if not getattr(platform_config, field, None)]
        if missing:
            fields_text = "', '".join(f"{descriptor.config_key}.{field}" for field in missing)
            raise ValueError(f"Config '{fields_text}' must be provided when {platform} is enabled")


def save_config(payload: dict) -> V2Config:
    if not isinstance(payload, dict):
        raise ValueError("Config payload must be an object")

    payload = _strip_agent_auth_fields(payload)
    payload = _strip_preserved_config_secrets(payload)
    payload = _mark_explicit_audio_asr_enabled(payload)

    with CONFIG_LOCK:
        base_payload: dict = {}
        base_config: Optional[V2Config] = None
        try:
            base_config = load_config()
            base_payload = config_to_payload(base_config, include_secrets=True)
        except FileNotFoundError:
            # Fresh install: no config file yet. Seed the same workbench-only
            # default the read side (GET /api/config) serves, so a partial
            # first-run save — e.g. the wizard's reused provider-config modal
            # POSTing just ``{"agents": ...}`` — merges onto a valid base
            # instead of feeding a partial payload straight into
            # ``V2Config.from_payload`` (which requires ``mode``/``runtime`` and
            # would raise). ``base_config`` stays ``None`` so the Discord-scope
            # preservation below (which keys off a real prior config) is skipped.
            from core.services.settings import default_config

            base_payload = config_to_payload(default_config(), include_secrets=True)
            # Don't let the seed's workbench-only ``platforms`` shadow
            # from_payload's legacy ``platform`` -> ``platforms`` migration: when
            # the request is a legacy single-platform update (``platform`` set,
            # ``platforms`` absent), drop the seed's platform fields so the
            # request's own platform still derives ``platforms.enabled``. The
            # wizard always sends ``platforms`` and is unaffected; a bare partial
            # save (neither key) keeps the workbench-only seed.
            if "platform" in payload and "platforms" not in payload:
                base_payload.pop("platforms", None)
                base_payload.pop("platform", None)

        merged_payload = _deep_merge_dicts(base_payload, payload) if base_payload else payload
        merged_payload = _merge_legacy_discord_guild_scope_fields(merged_payload, payload, base_config)
        sanitized_payload, guild_scope_update = _extract_settings_scopes_from_config_payload(merged_payload)
        config = V2Config.from_payload(sanitized_payload)
        _validate_enabled_platform_runtime_credentials(config, payload, base_config)
        if guild_scope_update is not None:
            _save_discord_guild_scope_update(*guild_scope_update)
        elif base_config is not None:
            store = SettingsStore.get_instance()
            if not store.has_guild_scope_for_platform("discord"):
                existing_update = _discord_guild_scope_from_config(base_config)
                if existing_update is not None:
                    _save_discord_guild_scope_update(*existing_update, store=store)
        config.save()
        _ensure_builtin_default_agents(config)
        return config


def _vibe_cloud_payload(config: V2Config, include_secrets: bool) -> dict:
    payload = config.remote_access.vibe_cloud.__dict__.copy()
    if not include_secrets:
        for key in ("tunnel_token", "instance_secret", "session_secret"):
            payload.pop(key, None)
    return payload


def _agent_payload(raw: dict, *, include_secrets: bool) -> dict:
    """Project a Claude/Codex config dict for the UI, masking the api_key.

    The UI surfaces *whether* a key is configured (and its length, so the
    user can see ``****6c1f``-style hints), never the plaintext. Only the
    secrets-included path (used by the setup wizard's "load existing
    config" flow) sees the raw value.
    """
    payload = dict(raw)
    api_key = payload.get("api_key")
    if isinstance(api_key, str):
        payload["api_key_length"] = len(api_key)
        payload["has_api_key"] = bool(api_key)
    else:
        payload["api_key_length"] = 0
        payload["has_api_key"] = False
    if not include_secrets:
        payload["api_key"] = None
    return payload


def _project_secret_fields(payload: dict | None, fields: tuple[str, ...], *, include_secrets: bool) -> dict | None:
    if payload is None:
        return None
    projected = dict(payload)
    for field in fields:
        value = projected.get(field)
        if isinstance(value, str):
            projected[f"has_{field}"] = bool(value)
            projected[f"{field}_length"] = len(value)
        else:
            projected[f"has_{field}"] = False
            projected[f"{field}_length"] = 0
        if not include_secrets:
            projected.pop(field, None)
    return projected


def config_to_payload(config: V2Config, *, include_secrets: bool = False) -> dict:
    from config.platform_registry import platform_descriptors
    from modules.agents.catalog import agent_backend_catalog_payload

    platform_payload = {}
    for descriptor in platform_descriptors():
        descriptor_config = descriptor.get_config(config)
        raw = descriptor_config.__dict__.copy() if descriptor_config else None
        platform_payload[descriptor.config_key] = _project_secret_fields(
            raw,
            _PLATFORM_SECRET_FIELDS.get(descriptor.config_key, ()),
            include_secrets=include_secrets,
        )
    if isinstance(platform_payload.get("discord"), dict):
        platform_payload["discord"].pop("guild_allowlist", None)
        platform_payload["discord"].pop("guild_denylist", None)
    payload = {
        "platform": config.platform,
        "platforms": {
            "enabled": config.platforms.enabled,
            "primary": config.platforms.primary,
        },
        "platform_catalog": config.platform_catalog(),
        "agent_backend_catalog": agent_backend_catalog_payload(),
        "setup_state": config.setup_state(),
        "mode": config.mode,
        "version": config.version,
        **platform_payload,
        "runtime": {
            "default_cwd": config.runtime.default_cwd,
            "log_level": config.runtime.log_level,
            "resource_governance": config.runtime.resource_governance,
        },
        "agents": {
            "opencode": config.agents.opencode.__dict__,
            "claude": _agent_payload(config.agents.claude.__dict__, include_secrets=include_secrets),
            "codex": _agent_payload(config.agents.codex.__dict__, include_secrets=include_secrets),
        },
        "gateway": _project_secret_fields(
            config.gateway.__dict__ if config.gateway else None,
            _GATEWAY_SECRET_FIELDS,
            include_secrets=include_secrets,
        ),
        "ui": config.ui.__dict__,
        "remote_access": {
            "provider": config.remote_access.provider,
            "vibe_cloud": _vibe_cloud_payload(config, include_secrets),
        },
        "audio_asr": config.audio_asr.__dict__,
        "update": config.update.__dict__,
        "ack_mode": config.ack_mode,
        "language": config.language,
        "show_duration": config.show_duration,
        "include_time_info": config.include_time_info,
        "include_user_info": config.include_user_info,
        "reply_enhancements": config.reply_enhancements,
        "show_pages_prompt": config.show_pages_prompt,
        "agent_progress_style": config.agent_progress_style,
        "agent_status_heartbeat_ms": config.agent_status_heartbeat_ms,
        "agent_status_no_output_ms": config.agent_status_no_output_ms,
        "setup_completed": config.setup_completed,
    }
    return payload


def _merge_legacy_discord_guild_scope_fields(
    merged_payload: dict,
    request_payload: dict,
    base_config: Optional[V2Config],
) -> dict:
    """Complete partial legacy Discord guild updates before migration."""
    request_discord = request_payload.get("discord")
    if not isinstance(request_discord, dict):
        return merged_payload
    if "guild_allowlist" not in request_discord and "guild_denylist" not in request_discord:
        return merged_payload

    next_payload = dict(merged_payload)
    merged_discord = dict(next_payload.get("discord") or {})
    base_discord = getattr(base_config, "discord", None) if base_config is not None else None

    if "guild_allowlist" not in request_discord and base_discord is not None:
        merged_discord["guild_allowlist"] = getattr(base_discord, "guild_allowlist", None) or []
    if "guild_denylist" not in request_discord and base_discord is not None:
        merged_discord["guild_denylist"] = getattr(base_discord, "guild_denylist", None) or []

    next_payload["discord"] = merged_discord
    return next_payload


def get_platform_catalog() -> dict:
    from config.platform_registry import platform_catalog_payload

    return {"platforms": platform_catalog_payload()}


def get_agent_backend_catalog() -> dict:
    return {"backends": agent_backend_catalog_payload()}


def _apply_session_meta(payloads: list[dict]) -> list[dict]:
    """Attach title / platform / agent to each Show Page payload from agent_sessions."""
    from storage.sessions_service import read_session_display_meta

    meta = read_session_display_meta([payload["session_id"] for payload in payloads])
    for payload in payloads:
        info = meta.get(payload["session_id"], {})
        payload["title"] = info.get("title")
        payload["platform"] = info.get("platform")
        payload["agent"] = info.get("agent")
    return payloads


def list_show_pages() -> dict:
    """All Show Pages, newest-first, each enriched with the session title.

    Reuses ``ShowPageStore.list_page`` (already ordered by ``updated_at`` desc)
    and joins ``agent_sessions.title`` so the UI can label rows by title and
    fall back to the session id when no title is set.
    """
    from core.avibe_cloud import avibe_cloud_connect_guidance, avibe_cloud_url_available
    from core.show_pages import ShowPageStore, show_page_payload

    config = V2Config.load()
    store = ShowPageStore()
    try:
        result = store.list_page(page_request=None)
        pages = [show_page_payload(page, config=config) for page in result.items]
    finally:
        store.close()
    _apply_session_meta(pages)
    return {
        "ok": True,
        "count": len(pages),
        "pages": pages,
        "url_available": avibe_cloud_url_available(config),
        "url_guidance": avibe_cloud_connect_guidance(config),
    }


def set_show_page_visibility(session_id: str, visibility: str) -> dict:
    """Switch a Show Page between private / public / offline.

    Raises ``ShowPageError`` (a ``ValueError``) for invalid input, which the
    route layer maps to a 4xx response.
    """
    from core.show_pages import ShowPageStore, show_page_payload

    config = V2Config.load()
    store = ShowPageStore()
    try:
        updated = store.update_visibility(session_id, visibility)
        payload = show_page_payload(updated, config=config)
    finally:
        store.close()
    return {"ok": True, **_apply_session_meta([payload])[0]}


def ensure_show_page(session_id: str) -> dict:
    """Create the session's Show Page if it doesn't exist yet; report which.

    ``existed`` tells the caller whether the page was already initialized, so the
    workbench can ALSO send the "visualize this session" prompt only on first
    creation. Mirrors the CLI ``vibe show path`` (ensure + return).
    """
    from core.show_pages import ShowPageStore, show_page_payload

    config = V2Config.load()
    store = ShowPageStore()
    try:
        # Atomic: refuses to create a page for an archived session and reports
        # whether IT created the row (so the UI only prompts the agent on a real
        # first creation, not a concurrent ensure). Raises ShowPageError for an
        # archived session — the route maps it to a 4xx.
        page, created = store.ensure_active(session_id)
        payload = show_page_payload(page, config=config)
    finally:
        store.close()
    return {"ok": True, "existed": not created, **_apply_session_meta([payload])[0]}


def rotate_show_page_share(session_id: str) -> dict:
    """Revoke the current public link and issue a new one (public pages only)."""
    from core.show_pages import ShowPageStore, show_page_payload

    config = V2Config.load()
    store = ShowPageStore()
    try:
        updated, previous_share_id = store.rotate_share(session_id)
        payload = show_page_payload(updated, config=config)
    finally:
        store.close()
    return {"ok": True, "previous_share_id": previous_share_id, **_apply_session_meta([payload])[0]}


def set_show_page_share_id(session_id: str, share_id: str) -> dict:
    """Set a custom public link suffix (public pages only).

    Like ``rotate_show_page_share`` but with a caller-chosen value; setting it
    revokes the previous public URL. Raises ``ShowPageError`` for an invalid /
    taken suffix or a non-public page, which the route layer maps to a 4xx/409.
    """
    from core.show_pages import ShowPageStore, show_page_payload

    config = V2Config.load()
    store = ShowPageStore()
    try:
        updated, previous_share_id = store.set_share_id(session_id, share_id)
        payload = show_page_payload(updated, config=config)
    finally:
        store.close()
    return {"ok": True, "previous_share_id": previous_share_id, **_apply_session_meta([payload])[0]}


def _vibe_agent_payload(agent, *, brief: bool = False) -> dict:
    payload = agent.to_dict()
    if brief:
        return {
            "id": payload["id"],
            "name": payload["name"],
            "description": payload["description"],
            "backend": payload["backend"],
            "model": payload["model"],
            "reasoning_effort": payload["reasoning_effort"],
            "enabled": payload["enabled"],
            "source": payload["source"],
            "updated_at": payload["updated_at"],
        }
    return payload


def _parse_agent_enabled_field(payload: dict, *, default: Optional[bool] = None) -> Optional[bool]:
    if "enabled" not in payload:
        return default
    value = payload.get("enabled")
    if isinstance(value, bool):
        return value
    raise ValueError("Agent enabled must be a JSON boolean")


def get_vibe_agents(*, backend: Optional[str] = None, include_disabled: bool = False) -> dict:
    _ensure_builtin_default_agents()
    store = VibeAgentStore()
    try:
        normalized_backend = validate_agent_backend(backend) if backend else None
        agents = store.list_agents(include_disabled=include_disabled)
        if normalized_backend:
            agents = [agent for agent in agents if agent.backend == normalized_backend]
        default_agent = store.get_default_agent()
        return {
            "ok": True,
            "agents": [_vibe_agent_payload(agent, brief=True) for agent in agents],
            "default_agent_name": default_agent.name if default_agent else None,
        }
    finally:
        store.close()


def get_vibe_agent(name: str) -> dict:
    store = VibeAgentStore()
    try:
        agent = store.require(name)
        default_agent = store.get_default_agent()
        return {
            "ok": True,
            "agent": _vibe_agent_payload(agent),
            "default_agent_name": default_agent.name if default_agent else None,
        }
    finally:
        store.close()


def create_vibe_agent(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Agent payload must be an object")
    metadata = payload.get("metadata") or payload.get("metadata_json") or {}
    if not isinstance(metadata, dict):
        raise ValueError("Agent metadata must be an object")
    store = VibeAgentStore()
    try:
        agent = store.create(
            name=str(payload.get("name") or "").strip(),
            backend=validate_agent_backend(str(payload.get("backend") or "")),
            description=payload.get("description"),
            model=payload.get("model"),
            reasoning_effort=payload.get("reasoning_effort") or payload.get("effort"),
            system_prompt=payload.get("system_prompt"),
            metadata=metadata,
            enabled=_parse_agent_enabled_field(payload, default=True),
        )
        return {"ok": True, "agent": _vibe_agent_payload(agent)}
    finally:
        store.close()


def update_vibe_agent(name: str, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Agent payload must be an object")
    if "name" in payload and str(payload.get("name") or "").strip() and str(payload.get("name")).strip() != name:
        raise ValueError("Agent name is immutable")
    if "backend" in payload:
        raise ValueError("Agent backend is immutable")

    allowed_fields = {
        "description",
        "model",
        "reasoning_effort",
        "effort",
        "system_prompt",
        "metadata",
        "metadata_json",
        "enabled",
    }
    kwargs: dict[str, object] = {}
    if "description" in payload:
        kwargs["description"] = payload.get("description")
    if "model" in payload:
        kwargs["model"] = payload.get("model")
    if "reasoning_effort" in payload or "effort" in payload:
        kwargs["reasoning_effort"] = payload.get("reasoning_effort") or payload.get("effort")
    if "system_prompt" in payload:
        kwargs["system_prompt"] = payload.get("system_prompt")
    if "metadata" in payload or "metadata_json" in payload:
        metadata = payload.get("metadata") if "metadata" in payload else payload.get("metadata_json")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise ValueError("Agent metadata must be an object")
        kwargs["metadata"] = metadata
    if "enabled" in payload:
        kwargs["enabled"] = _parse_agent_enabled_field(payload)
    unknown = sorted(set(payload) - allowed_fields - {"name"})
    if unknown:
        raise ValueError(f"Unsupported Agent fields: {', '.join(unknown)}")
    if not kwargs:
        raise ValueError("No editable Agent fields were provided")

    store = VibeAgentStore()
    try:
        agent = store.update(name, **kwargs)
        return {"ok": True, "agent": _vibe_agent_payload(agent)}
    finally:
        store.close()


def remove_vibe_agent(name: str) -> dict:
    store = VibeAgentStore()
    try:
        counts = store.reference_counts(name)
        if any(counts.values()):
            return {
                "ok": False,
                "code": "agent_in_use",
                "message": f"agent '{name}' is still referenced",
                "references": counts,
            }
        try:
            removed = store.remove(name)
        except ValueError as exc:
            return {
                "ok": False,
                "code": "agent_builtin",
                "message": str(exc),
            }
        if not removed:
            return {"ok": False, "code": "agent_not_found", "message": f"agent '{name}' not found"}
        return {"ok": True, "removed_agent": name}
    finally:
        store.close()


def set_default_vibe_agent(name: str) -> dict:
    store = VibeAgentStore()
    try:
        store.set_default_agent_name(name)
        agent = store.require(name)
        return {"ok": True, "default_agent_name": agent.name, "agent": _vibe_agent_payload(agent, brief=True)}
    finally:
        store.close()


# ----- Vaults (secret management; design: docs/plans/vaults.md) -----
# Thin web-facing wrappers over storage.vault_service. Reads are masked (no values);
# values are only ever delivered to agents via the CLI (vibe vault run/fetch/...).


class VaultApiError(ValueError):
    """A vault REST error carrying a stable code + HTTP status for the route layer."""

    def __init__(self, message: str, *, code: str = "vault_error", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _vault_api_error_from_avault(exc: "AvaultError", *, prefix: str) -> VaultApiError:
    message = str(exc)
    if "requires avault >=" in message:
        return VaultApiError(f"{prefix}: {message}", code="avault_upgrade_required", status=409)
    return VaultApiError(f"{prefix}: {message}", code="avault_failed")


def _vault_engine():
    from storage.db import create_sqlite_engine
    from storage.importer import ensure_sqlite_state, resolve_primary_platform_from_config

    # Resolve the platform from config like the other CLI/data paths — if the Vaults
    # route is the first to initialize SQLite on an upgraded install with legacy
    # sessions, ``ensure_sqlite_state`` requires a platform and would otherwise raise.
    ensure_sqlite_state(primary_platform=resolve_primary_platform_from_config(paths.get_state_dir()))
    return create_sqlite_engine(paths.get_sqlite_state_path())


def get_vault_secrets(*, group: Optional[str] = None) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    with engine.connect() as conn:
        secrets = vault_service.list_secrets(conn, group=group)
    return {"ok": True, "secrets": secrets}


def get_vault_pubkey() -> dict:
    try:
        return {"ok": True, **avault_pubkey()}
    except AvaultError as exc:
        raise VaultApiError(f"avault pubkey failed: {exc}", code="avault_failed") from exc


def get_vault_agent_pubkey() -> dict:
    try:
        return {"ok": True, **avault_agent_pubkey()}
    except AvaultError as exc:
        raise _vault_api_error_from_avault(exc, prefix="avault agent pubkey failed") from exc


def get_vault_vmk() -> dict:
    from storage import vault_service

    engine = _vault_engine()
    with engine.connect() as conn:
        wrap_meta = vault_service.latest_protected_vmk_wrap_meta(conn)
    return {"ok": True, "exists": wrap_meta is not None, "wrap_meta": wrap_meta}


def _reject_plaintext_value_fields(payload: object) -> None:
    if isinstance(payload, dict):
        if "value" in payload:
            raise VaultApiError(
                "vault create does not accept plaintext value fields",
                code="plaintext_value_rejected",
            )
        for item in payload.values():
            _reject_plaintext_value_fields(item)
    elif isinstance(payload, list):
        for item in payload:
            _reject_plaintext_value_fields(item)


def _sealed_from_payload(payload: dict):
    from storage.vault_crypto import Sealed

    if not isinstance(payload, dict):
        raise VaultApiError("sealed envelope must be an object", code="invalid_envelope")
    try:
        ciphertext = payload["ciphertext"]
        nonce = payload["nonce"]
        wrap_meta = payload["wrap_meta"]
    except KeyError as exc:
        raise VaultApiError("sealed envelope requires ciphertext, nonce, and wrap_meta", code="invalid_envelope") from exc
    if not isinstance(ciphertext, str) or not ciphertext.strip():
        raise VaultApiError("sealed envelope fields must be non-empty strings", code="invalid_envelope")
    if not isinstance(nonce, str) or not nonce.strip():
        raise VaultApiError("sealed envelope fields must be non-empty strings", code="invalid_envelope")
    if isinstance(wrap_meta, dict):
        wrap_meta = json.dumps(wrap_meta)
    if not isinstance(wrap_meta, str) or not wrap_meta.strip():
        raise VaultApiError("sealed envelope fields must be non-empty strings", code="invalid_envelope")
    return Sealed(ciphertext=ciphertext, nonce=nonce, wrap_meta=wrap_meta)


def create_vault_secret(payload: dict) -> dict:
    from storage import vault_crypto, vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    _reject_plaintext_value_fields(payload)
    name = str(payload.get("name") or "").strip()
    if not vault_crypto.is_valid_secret_name(name):
        raise VaultApiError("invalid secret name (use ^[A-Z][A-Z0-9_]*$)", code="invalid_name")
    tags = payload.get("tags") if isinstance(payload.get("tags"), list) else None
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else None
    public_meta = payload.get("public_meta") if isinstance(payload.get("public_meta"), dict) else None
    protection = str(payload.get("protection") or "standard").strip().lower()
    establishing_vmk = bool(payload.get("establishing_vmk"))
    kind = str(payload.get("kind") or "static").strip().lower()
    signer_kind = payload.get("signer_kind")
    if signer_kind is not None:
        signer_kind = str(signer_kind)
    if protection == "standard":
        blind_box = payload.get("blind_box")
        if isinstance(blind_box, dict):
            try:
                sealed = avault_seal_blind_box(name, blind_box)
            except AvaultError as exc:
                raise VaultApiError(f"avault blind-box seal failed: {exc}", code="avault_failed") from exc
        else:
            raise VaultApiError(
                "standard create requires a browser blind_box",
                code="blind_box_required",
            )
    elif protection == "protected":
        # Protected-tier encryption is browser-side. Python stores the opaque envelope
        # and wrap_meta only; no avault open/decrypt happens in this process.
        sealed = _sealed_from_payload(payload.get("sealed") or payload.get("envelope") or payload)
    else:
        raise VaultApiError("invalid protection tier", code="invalid_protection")
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            meta = vault_service.create_secret(
                conn,
                name=name,
                sealed=sealed,
                group=str(payload.get("group") or vault_service.DEFAULT_GROUP),
                tags=tags,
                protection=protection,
                kind=kind,
                signer_kind=signer_kind,
                description=payload.get("description"),
                policy=policy,
                public_meta=public_meta,
                establishing_vmk=establishing_vmk,
            )
    except vault_service.InvalidSecretNameError as exc:
        raise VaultApiError("invalid secret name (use ^[A-Z][A-Z0-9_]*$)", code="invalid_name") from exc
    except vault_service.SecretExistsError as exc:
        raise VaultApiError(f"secret '{name}' already exists", code="secret_exists", status=409) from exc
    except vault_service.VaultAlreadyInitializedError as exc:
        raise VaultApiError(str(exc), code="vault_already_initialized", status=409) from exc
    except vault_service.VaultServiceError as exc:
        raise VaultApiError(str(exc), code="vault_error") from exc
    return {"ok": True, "secret": meta}


def delete_vault_secret(name: str) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    release_scopes: list[dict[str, str]] = []
    try:
        with engine.begin() as conn:
            grant_rows = vault_service.active_grant_rows_for_secret(conn, name)
            vault_service.delete_secret(conn, name)
            release_scopes = vault_service.agent_release_scopes_after_rows(conn, grant_rows)
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{name}' not found", code="secret_not_found", status=404) from exc
    release_vault_agent_scopes(release_scopes, reason="delete_vault_secret")
    return {"ok": True, "removed": True, "name": name}


def get_vault_audit(*, secret_name: Optional[str] = None, limit: int = 100) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    with engine.connect() as conn:
        events = vault_service.list_audit(conn, secret_name=secret_name, limit=limit)
    return {"ok": True, "events": events}


def get_vault_requests(*, status: Optional[str] = "pending", request_type: Optional[str] = None, limit: int = 100) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    with engine.begin() as conn:
        requests = vault_service.list_requests(conn, status=status, request_type=request_type, limit=limit)
    return {"ok": True, "requests": requests}


def _vault_request_result(conn, request: dict) -> dict | None:
    from storage import vault_service

    request_type = request.get("request_type")
    if request.get("status") != "approved":
        return None
    if request_type == "access":
        grant = vault_service.get_grant_created_by_request(conn, str(request["id"]))
        if grant is None:
            requester = request.get("requester") if isinstance(request.get("requester"), dict) else {}
            delivery = request.get("delivery") if isinstance(request.get("delivery"), dict) else {}
            session_id = str(requester.get("session_id") or delivery.get("session_id") or "").strip() or None
            grant = vault_service.find_active_grant_for_secret(
                conn,
                str(request["secret_name"]),
                session_id=session_id,
            )
        if grant is None:
            return None
        return {"type": "grant", "grant": grant}
    if request_type == "sign":
        delivery = request.get("delivery") if isinstance(request.get("delivery"), dict) else {}
        signature = delivery.get("signature") if isinstance(delivery, dict) else None
        if isinstance(signature, dict):
            return {"type": "signature", "signature": signature}
    return None


def get_vault_request(request_id: str, *, audience: str | None = None) -> dict:
    from storage import vault_service

    request_audience = audience or vault_service.REQUEST_AUDIENCE_AGENT
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            request = vault_service.get_request(conn, request_id, audience=request_audience)
            result = _vault_request_result(conn, request)
    except vault_service.RequestNotFoundError as exc:
        raise VaultApiError(f"request '{request_id}' not found", code="request_not_found", status=404) from exc
    payload = {"ok": True, "request": request}
    if result is not None:
        payload["result"] = result
    return payload


def _vault_requester_payload(payload: dict) -> dict:
    requester = payload.get("requester") if isinstance(payload.get("requester"), dict) else {}
    session_id = str(payload.get("session_id") or requester.get("session_id") or "").strip()
    out = {"source": "agent-cli"}
    if session_id:
        out["session_id"] = session_id
    if payload.get("run_id"):
        out["run_id"] = str(payload["run_id"])
    if payload.get("skill"):
        out["skill"] = str(payload["skill"])
    if isinstance(requester, dict):
        for key in ("backend", "native_session_id"):
            if requester.get(key):
                out[key] = str(requester[key])
    return out


def _vault_delivery_payload(payload: dict) -> dict:
    delivery = dict(payload.get("delivery") if isinstance(payload.get("delivery"), dict) else {})
    for key in ("session_id", "skill", "command", "egress", "mode"):
        if payload.get(key) is not None:
            delivery[key] = payload[key]
    return delivery


def _expires_at_from_ttl(payload: dict) -> str | None:
    ttl = payload.get("request_ttl_seconds")
    if ttl is None:
        return None
    try:
        ttl_seconds = int(ttl)
    except (TypeError, ValueError) as exc:
        raise VaultApiError("request_ttl_seconds must be an integer", code="invalid_request") from exc
    if ttl_seconds <= 0:
        raise VaultApiError("request_ttl_seconds must be positive", code="invalid_request")
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()


def request_vault_access(payload: dict) -> dict:
    from storage import vault_crypto, vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    name = str(payload.get("name") or "").strip()
    if not vault_crypto.is_valid_secret_name(name):
        raise VaultApiError("invalid secret name (use ^[A-Z][A-Z0-9_]*$)", code="invalid_name")
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            vault_service.get_secret_meta(conn, name)
            request = vault_service.create_access_request(
                conn,
                name,
                requester=_vault_requester_payload(payload),
                delivery=_vault_delivery_payload(payload),
                expires_at=_expires_at_from_ttl(payload),
                audience=vault_service.REQUEST_AUDIENCE_AGENT,
            )
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{name}' not found", code="secret_not_found", status=404) from exc
    except vault_service.NotGrantableError as exc:
        raise VaultApiError(str(exc), code="not_grantable", status=409) from exc
    except vault_service.InvalidRequestError as exc:
        raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
    return {"ok": True, "request": request}


def request_vault_sign(payload: dict) -> dict:
    from storage import vault_crypto, vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    name = str(payload.get("name") or "").strip()
    digest = _sign_digest_from_payload(payload.get("digest"))
    scheme = str(payload.get("scheme") or "ecdsa-secp256k1-recoverable").strip()
    if scheme not in vault_service.SUPPORTED_SIGNATURE_SCHEMES:
        raise VaultApiError(f"unsupported signature scheme: {scheme}", code="invalid_request", status=409)
    if not vault_crypto.is_valid_secret_name(name):
        raise VaultApiError("invalid secret name (use ^[A-Z][A-Z0-9_]*$)", code="invalid_name")
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            meta = vault_service.get_secret_meta(conn, name)
            if meta.get("kind") != "keypair":
                raise VaultApiError(f"secret '{name}' is not a signing key", code="not_signing_key", status=409)
            if (meta.get("signer_kind") or "local") != "local":
                raise VaultApiError(
                    f"secret '{name}' uses signer_kind '{meta.get('signer_kind')}', which is not locally signable",
                    code="unsupported_signer_kind",
                    status=409,
                )
            request = vault_service.create_sign_request(
                conn,
                name,
                digest=digest,
                scheme=scheme,
                requester=_vault_requester_payload(payload),
                delivery=_vault_delivery_payload(payload),
                expires_at=_expires_at_from_ttl(payload),
            )
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{name}' not found", code="secret_not_found", status=404) from exc
    except vault_service.InvalidRequestError as exc:
        raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
    except vault_service.VaultServiceError as exc:
        raise VaultApiError(str(exc), code="vault_error") from exc
    return {"ok": True, "request": request}


def deny_vault_request(request_id: str, payload: dict | None = None) -> dict:
    from storage import vault_service

    body = payload if isinstance(payload, dict) else {}
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            request = vault_service.deny_request(
                conn,
                str(request_id),
                requester=body.get("requester") if isinstance(body.get("requester"), dict) else None,
                reason=str(body.get("reason") or "").strip() or None,
            )
    except vault_service.RequestNotFoundError as exc:
        raise VaultApiError(f"request '{request_id}' not found", code="request_not_found", status=404) from exc
    except vault_service.InvalidRequestError as exc:
        raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
    return {"ok": True, "request": request}


def get_vault_grants(*, status: Optional[str] = "active", session_id: Optional[str] = None) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    with engine.begin() as conn:
        grants = vault_service.list_grants(conn, status=status, session_id=session_id)
    return {"ok": True, "grants": grants}


def _parse_iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _grant_ttl_seconds(grant: dict) -> int:
    created_at = _parse_iso_utc(grant.get("created_at"))
    expires_at = _parse_iso_utc(grant.get("expires_at"))
    if expires_at is None:
        return 1
    if created_at is None:
        return 1
    approved_lifetime = (expires_at - created_at).total_seconds()
    return max(1, int(round(approved_lifetime)))


def _release_one_shot_agent_grant(grant: dict | None, *, reason: str) -> None:
    if not isinstance(grant, dict) or grant.get("one_shot") is not True:
        return
    release_vault_agent_scopes(_grant_scope_payload(grant), reason=reason)


def consume_one_shot_grants(grants: list[dict] | tuple[dict, ...] | None, *, reason: str) -> None:
    """Expire one-shot grants after the caller has committed value delivery."""
    from storage import vault_service

    if not grants:
        return
    seen: set[str] = set()
    release_scopes: list[dict[str, str]] = []
    engine = _vault_engine()
    with engine.begin() as conn:
        for grant in grants:
            if not isinstance(grant, dict) or grant.get("one_shot") is not True:
                continue
            grant_id = str(grant.get("id") or "")
            if not grant_id or grant_id in seen:
                continue
            seen.add(grant_id)
            with contextlib.suppress(vault_service.GrantNotActiveError, vault_service.GrantNotFoundError):
                release_scopes.extend(vault_service.consume_one_shot_grant(conn, grant_id))
    release_vault_agent_scopes(release_scopes, reason=reason)


def _agent_grant_cached_all(result: dict, expected_count: int) -> bool:
    granted = result.get("granted")
    try:
        return int(granted) == expected_count
    except (TypeError, ValueError):
        return False


def _restore_access_request_after_failed_agent_grant(
    *,
    engine: Any,
    request_id: str,
    member_names: list[str] | set[str] | tuple[str, ...] | None,
    session_id: str | None,
) -> None:
    from storage import vault_service

    with contextlib.suppress(Exception), engine.begin() as conn:
        vault_service.restore_access_request_after_failed_grant(
            conn,
            created_by_request_id=str(request_id),
            member_names=list(member_names or []),
            session_id=session_id,
        )


def _grant_dek_entries_from_payload(payload: dict) -> object:
    deks = payload.get("deks")
    if deks is None:
        deks = payload.get("agent_deks")
    if deks is None and isinstance(payload.get("deks_by_secret"), dict):
        deks = []
        for name, dek in payload["deks_by_secret"].items():
            if isinstance(dek, dict):
                deks.append({"name": name, **dek})
            else:
                deks.append({"name": name, "dek_blindbox": dek, "approval": None})
    return deks


def _validate_resident_agent_dek_blindbox(blindbox: dict[str, Any]) -> dict[str, str]:
    allowed_keys = {"scheme", "enc", "ct"}
    if set(blindbox) != allowed_keys:
        raise VaultApiError("resident agent grant DEKs must contain only opaque blind-box metadata", code="invalid_grant")
    normalized: dict[str, str] = {}
    for key in allowed_keys:
        value = blindbox.get(key)
        if not isinstance(value, str) or not value:
            raise VaultApiError("resident agent grant DEK blind boxes require non-empty scheme, enc, and ct", code="invalid_grant")
        normalized[key] = value
    return normalized


def _validate_resident_agent_dek_approval(approval: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"nonce", "expires_at_unix"}
    if set(approval) != allowed_keys:
        raise VaultApiError("resident agent grant DEKs must contain only opaque blind-box metadata", code="invalid_grant")
    nonce = approval.get("nonce")
    expires_at_unix = approval.get("expires_at_unix")
    if not isinstance(nonce, str) or not nonce:
        raise VaultApiError("resident agent grant DEK approvals require a non-empty nonce", code="invalid_grant")
    if type(expires_at_unix) is not int:
        raise VaultApiError("resident agent grant DEK approvals require expires_at_unix", code="invalid_grant")
    return {"nonce": nonce, "expires_at_unix": expires_at_unix}


def _resident_agent_deks_from_payload(payload: dict, *, needs_agent_deks: bool) -> list[dict[str, Any]]:
    deks = _grant_dek_entries_from_payload(payload)
    if deks is None and not needs_agent_deks:
        return []
    if not isinstance(deks, list) or (needs_agent_deks and not deks):
        raise VaultApiError("resident agent grant requires sealed DEKs", code="invalid_grant")
    agent_deks: list[dict[str, Any]] = []
    allowed_keys = {"name", "dek_blindbox", "approval"}
    for item in deks:
        if not isinstance(item, dict):
            raise VaultApiError("resident agent grant DEKs must be objects", code="invalid_grant")
        extra_keys = set(item) - allowed_keys
        if extra_keys:
            raise VaultApiError("resident agent grant DEKs must contain only opaque blind-box metadata", code="invalid_grant")
        name = item.get("name")
        dek_blindbox = item.get("dek_blindbox")
        approval = item.get("approval")
        if not isinstance(name, str) or not name or not isinstance(dek_blindbox, dict) or not isinstance(approval, dict):
            raise VaultApiError("resident agent grant DEKs require name, dek_blindbox, and approval", code="invalid_grant")
        agent_deks.append(
            {
                "name": name,
                "dek_blindbox": _validate_resident_agent_dek_blindbox(dek_blindbox),
                "approval": _validate_resident_agent_dek_approval(approval),
            }
        )
    return agent_deks


def _access_request_secret_name(request_id: str) -> str:
    from storage import vault_service

    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            request = vault_service.get_request(conn, request_id, audience=vault_service.REQUEST_AUDIENCE_AGENT)
    except vault_service.RequestNotFoundError as exc:
        raise VaultApiError(f"request '{request_id}' not found", code="request_not_found", status=404) from exc
    if request.get("request_type") != "access":
        raise VaultApiError("access fulfillment must target an access request", code="invalid_request", status=409)
    name = str(request.get("secret_name") or "").strip()
    if not name:
        raise VaultApiError("access request is missing a secret name", code="invalid_request", status=409)
    return name


def _grant_scope_payload(grant: dict) -> list[dict[str, str]]:
    scope_type = str(grant.get("scope_type") or "")
    scope_ref = str(grant.get("scope_ref") or "")
    if not scope_type or not scope_ref:
        return []
    return [{"scope_type": scope_type, "scope_ref": scope_ref}]


def _cleanup_failed_agent_grant(
    *,
    engine: Any,
    grant: dict,
    reason: str,
    force_release_on_cleanup_failure: bool = False,
    force_release_scope: bool = False,
) -> None:
    from storage import vault_service

    release_scopes: list[dict[str, str]] = []
    try:
        with engine.begin() as conn:
            grant_rows = [
                dict(row)
                for row in conn.execute(
                    select(vault_service.vault_grants).where(
                        vault_service.vault_grants.c.id == str(grant["id"])
                    )
                ).mappings()
            ]
            with contextlib.suppress(Exception):
                vault_service.expire_grant(conn, str(grant["id"]), reason="grant-expired-agent-relay-failed")
            release_scopes = (
                vault_service.agent_release_scopes_after_rows(conn, grant_rows)
                if grant_rows
                else _grant_scope_payload(grant)
            )
            if force_release_scope:
                forced_scopes = _grant_scope_payload(grant)
                for scope in forced_scopes:
                    raw_scope_members = grant.get("member_snapshot") or []
                    scope_members = set(raw_scope_members if isinstance(raw_scope_members, list) else [])
                    active_scope_members: set[str] = set()
                    for active in conn.execute(
                        select(vault_service.vault_grants).where(
                            vault_service.vault_grants.c.status.in_(vault_service.ACTIVE_GRANT_STATES),
                            vault_service.vault_grants.c.scope_type == scope["scope_type"],
                            vault_service.vault_grants.c.scope_ref == scope["scope_ref"],
                        )
                    ).mappings():
                        active_row = dict(active)
                        if vault_service.grant_row_has_resident_agent_ready(active_row):
                            active_members = json.loads(active_row.get("member_snapshot") or "[]")
                            if isinstance(active_members, list):
                                active_scope_members.update(str(member) for member in active_members if isinstance(member, str))
                    if not scope_members.issubset(active_scope_members) and scope not in release_scopes:
                        release_scopes.append(scope)
    except Exception:
        if not force_release_on_cleanup_failure:
            raise
        logger.warning("%s: failed to update grant DB state; releasing resident scope", reason, exc_info=True)
        release_scopes = _grant_scope_payload(grant)
    release_vault_agent_scopes(release_scopes, reason=reason)


def create_vault_grant(payload: dict) -> dict:
    from storage import vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    try:
        scope_type = str(payload["scope_type"])
        scope_ref = str(payload["scope_ref"])
    except KeyError as exc:
        raise VaultApiError("scope_type and scope_ref are required", code="invalid_grant") from exc
    ttl = payload.get("ttl_seconds")
    try:
        ttl_seconds = int(ttl) if ttl is not None else None
    except (TypeError, ValueError) as exc:
        raise VaultApiError("ttl_seconds must be an integer", code="invalid_grant") from exc
    request_id = payload.get("request_id") or payload.get("created_by_request_id")
    if not request_id:
        raise VaultApiError("request_id is required to create a grant", code="missing_request_id")
    engine = _vault_engine()
    preflight_error: Exception | None = None
    with engine.begin() as conn:
        try:
            grantable_members = vault_service.request_grantable_member_metas(
                conn,
                scope_type,
                scope_ref,
                str(request_id),
            )
        except (
            vault_service.SecretNotFoundError,
            vault_service.RequestNotFoundError,
            vault_service.InvalidRequestError,
            vault_service.InvalidGrantError,
        ) as exc:
            preflight_error = exc
            grantable_members = []
    if isinstance(preflight_error, vault_service.SecretNotFoundError):
        raise VaultApiError(f"secret '{preflight_error}' not found", code="secret_not_found", status=404) from preflight_error
    if isinstance(preflight_error, vault_service.RequestNotFoundError):
        raise VaultApiError(f"request '{preflight_error}' not found", code="request_not_found", status=404) from preflight_error
    if isinstance(preflight_error, vault_service.InvalidRequestError):
        raise VaultApiError(str(preflight_error), code="invalid_request", status=409) from preflight_error
    if isinstance(preflight_error, vault_service.InvalidGrantError):
        raise VaultApiError(str(preflight_error), code="invalid_grant") from preflight_error
    if not grantable_members:
        raise VaultApiError(f"{scope_type}:{scope_ref} has no grantable static secrets", code="not_grantable", status=409)
    protected_member_names = {str(member["name"]) for member in grantable_members if member.get("protection") == "protected"}
    needs_agent_deks = bool(protected_member_names)

    agent_deks = _resident_agent_deks_from_payload(payload, needs_agent_deks=needs_agent_deks)
    provided_names = {item["name"] for item in agent_deks}
    if needs_agent_deks and provided_names != protected_member_names:
        raise VaultApiError("resident agent DEKs must match the protected grant members", code="invalid_grant")
    expected_member_names = {str(member["name"]) for member in grantable_members} if needs_agent_deks else None
    expected_pubkey = payload.get("agent_pubkey") if isinstance(payload.get("agent_pubkey"), dict) else None
    if needs_agent_deks:
        try:
            _require_avault_p2_surface("resident agent grant")
            validate_avault_agent_pubkey(expected_pubkey)
        except AvaultError as exc:
            raise _vault_api_error_from_avault(exc, prefix="avault agent grant failed") from exc
    session_id = payload.get("session_id")
    inherit_request_session = payload.get("this_session_only") is not False
    if not inherit_request_session:
        session_id = None
    try:
        with engine.begin() as conn:
            grant = vault_service.create_grant(
                conn,
                scope_type=scope_type,
                scope_ref=scope_ref,
                session_id=str(session_id) if session_id else None,
                ttl_seconds=ttl_seconds,
                created_by_request_id=str(request_id),
                inherit_request_session=inherit_request_session,
                expected_member_names=expected_member_names,
                cache_ready=False,
            )
    except vault_service.NotGrantableError as exc:
        raise VaultApiError(str(exc), code="not_grantable", status=409) from exc
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{exc}' not found", code="secret_not_found", status=404) from exc
    except vault_service.RequestNotFoundError as exc:
        raise VaultApiError(f"request '{exc}' not found", code="request_not_found", status=404) from exc
    except vault_service.InvalidRequestError as exc:
        raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
    except vault_service.InvalidGrantError as exc:
        raise VaultApiError(str(exc), code="invalid_grant") from exc
    if not needs_agent_deks:
        return {"ok": True, "grant": grant}
    agent_relayed = False
    try:
        relay_ttl = _grant_ttl_seconds(grant)
        agent_result = avault_agent_grant(
            scope_type=str(grant["scope_type"]),
            scope_ref=str(grant["scope_ref"]),
            ttl_secs=relay_ttl,
            deks=agent_deks,
            expected_pubkey=expected_pubkey,
        )
        if not _agent_grant_cached_all(agent_result, len(agent_deks)):
            raise AvaultError("avault agent grant cached fewer DEKs than requested")
        agent_relayed = True
        with engine.begin() as conn:
            grant = vault_service.mark_grant_agent_ready(conn, str(grant["id"]), ttl_seconds=relay_ttl)
    except (vault_service.InvalidGrantError, vault_service.GrantNotActiveError) as exc:
        if agent_relayed:
            _cleanup_failed_agent_grant(
                engine=engine,
                grant=grant,
                reason=f"create_vault_grant:{grant['id']}:mark-ready-failed",
                force_release_on_cleanup_failure=True,
                force_release_scope=True,
            )
            _restore_access_request_after_failed_agent_grant(
                engine=engine,
                request_id=str(request_id),
                member_names=expected_member_names,
                session_id=str(grant.get("session_id") or "") or None,
            )
        raise VaultApiError(str(exc), code="invalid_grant") from exc
    except AvaultError as exc:
        _cleanup_failed_agent_grant(
            engine=engine,
            grant=grant,
            reason=f"create_vault_grant:{grant['id']}",
            force_release_on_cleanup_failure=True,
            force_release_scope=True,
        )
        _restore_access_request_after_failed_agent_grant(
            engine=engine,
            request_id=str(request_id),
            member_names=expected_member_names,
            session_id=str(grant.get("session_id") or "") or None,
        )
        raise _vault_api_error_from_avault(exc, prefix="avault agent grant failed") from exc
    except Exception:
        if agent_relayed:
            _cleanup_failed_agent_grant(
                engine=engine,
                grant=grant,
                reason=f"create_vault_grant:{grant['id']}:mark-ready-failed",
                force_release_on_cleanup_failure=True,
                force_release_scope=True,
            )
        raise
    return {"ok": True, "grant": grant}


def fulfill_vault_access_request(request_id: str, payload: dict | None = None) -> dict:
    """Approve an access request by relaying browser blind-boxed DEKs to avault.

    The submitted DEKs are HPKE blind boxes addressed to the resident avault
    agent. Python validates only metadata and forwards the opaque boxes; it never
    opens a protected DEK or protected plaintext.
    """

    body = dict(payload) if isinstance(payload, dict) else {}
    request_id = str(request_id or "").strip()
    if not request_id:
        raise VaultApiError("request_id is required to fulfill protected access", code="missing_request_id")
    if not body.get("scope_type") or not body.get("scope_ref"):
        body.setdefault("scope_type", "secret")
        body.setdefault("scope_ref", _access_request_secret_name(request_id))
    body["request_id"] = request_id
    created = create_vault_grant(body)
    return {
        "ok": True,
        "request_id": request_id,
        "grant": created["grant"],
        "result": {"type": "grant", "grant": created["grant"]},
    }


def _sign_digest_from_payload(value: object) -> str:
    if not isinstance(value, str):
        raise VaultApiError("digest must be a 32-byte hex string", code="invalid_digest")
    digest = value.strip()
    try:
        decoded = bytes.fromhex(digest)
    except ValueError as exc:
        raise VaultApiError("digest must be a 32-byte hex string", code="invalid_digest") from exc
    if len(digest) != 64 or len(decoded) != 32:
        raise VaultApiError("digest must be a 32-byte hex string", code="invalid_digest")
    return digest.lower()


def _protected_sign_invalid(message: str) -> VaultApiError:
    return VaultApiError(message, code="invalid_request", status=409)


def _protected_signing_public_key_bytes(meta: dict) -> bytes:
    signing_key = meta.get("signing_public_key")
    if not isinstance(signing_key, dict):
        raise _protected_sign_invalid("protected signing key is missing signing_public_key")
    if signing_key.get("curve") != "secp256k1":
        raise _protected_sign_invalid("protected signing key must use secp256k1")
    public_key = signing_key.get("public_key")
    if not isinstance(public_key, str) or not public_key:
        raise _protected_sign_invalid("protected signing key is missing a public key")
    try:
        public_key_bytes = bytes.fromhex(public_key)
    except ValueError as exc:
        raise _protected_sign_invalid("protected signing public key must be hex-encoded") from exc
    return public_key_bytes


def _signature_bytes_for_verification(signature: dict[str, Any]) -> bytes:
    raw_signature = signature.get("signature")
    if not isinstance(raw_signature, str) or not raw_signature:
        raise _protected_sign_invalid("signature payload requires a non-empty signature")
    try:
        return bytes.fromhex(raw_signature)
    except ValueError as exc:
        raise _protected_sign_invalid("signature must be hex-encoded bytes") from exc


def _normalized_protected_signature(signature: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"signature", "recovery_id"}
    extra_keys = set(signature) - allowed_keys
    if extra_keys:
        raise _protected_sign_invalid(f"signature payload contains unsupported fields: {', '.join(sorted(extra_keys))}")
    normalized = {"signature": signature.get("signature")}
    if signature.get("recovery_id") is not None:
        normalized["recovery_id"] = signature.get("recovery_id")
    return normalized


def _verify_protected_ecdsa_signature(
    *,
    public_key_bytes: bytes,
    digest_bytes: bytes,
    scheme: str,
    signature: dict[str, Any],
) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    sig_bytes = _signature_bytes_for_verification(signature)
    recovery_id = signature.get("recovery_id")
    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), public_key_bytes)
    except ValueError as exc:
        raise _protected_sign_invalid("protected signing public key is not a valid secp256k1 point") from exc
    if scheme == "ecdsa-secp256k1-recoverable":
        if len(sig_bytes) != 64:
            raise _protected_sign_invalid("recoverable secp256k1 signatures must be 64 bytes")
        if type(recovery_id) is not int or recovery_id not in {0, 1, 2, 3}:
            raise _protected_sign_invalid("recoverable secp256k1 signatures require recovery_id 0..3")
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        sig_bytes = utils.encode_dss_signature(r, s)
    else:
        if len(sig_bytes) < 8 or sig_bytes[0] != 0x30:
            raise _protected_sign_invalid("DER secp256k1 signatures must be DER-encoded")
        if recovery_id is not None:
            raise _protected_sign_invalid("DER secp256k1 signatures must not include recovery_id")
    try:
        public_key.verify(sig_bytes, digest_bytes, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
    except InvalidSignature as exc:
        raise _protected_sign_invalid("browser signature does not verify against signing_public_key") from exc
    if scheme == "ecdsa-secp256k1-recoverable":
        _verify_recoverable_signature_recovery_id(
            public_key_bytes=public_key_bytes,
            digest_bytes=digest_bytes,
            r=r,
            s=s,
            recovery_id=recovery_id,
        )


_SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_SECP256K1_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_Secp256k1Point = tuple[int, int] | None


def _secp256k1_mod_sqrt(value: int) -> int | None:
    root = pow(value, (_SECP256K1_P + 1) // 4, _SECP256K1_P)
    return root if pow(root, 2, _SECP256K1_P) == value % _SECP256K1_P else None


def _secp256k1_decompress_public_key(public_key_bytes: bytes) -> _Secp256k1Point:
    if len(public_key_bytes) == 33 and public_key_bytes[0] in {2, 3}:
        x = int.from_bytes(public_key_bytes[1:], "big")
        if x >= _SECP256K1_P:
            return None
        y = _secp256k1_mod_sqrt((pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P)
        if y is None:
            return None
        if (y % 2) != (public_key_bytes[0] & 1):
            y = _SECP256K1_P - y
        return (x, y)
    if len(public_key_bytes) == 65 and public_key_bytes[0] == 4:
        x = int.from_bytes(public_key_bytes[1:33], "big")
        y = int.from_bytes(public_key_bytes[33:], "big")
        if x >= _SECP256K1_P or y >= _SECP256K1_P or (pow(y, 2, _SECP256K1_P) - pow(x, 3, _SECP256K1_P) - 7) % _SECP256K1_P:
            return None
        return (x, y)
    return None


def _secp256k1_lift_x(x: int) -> _Secp256k1Point:
    if x >= _SECP256K1_P:
        return None
    y_sq = (pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P
    y = _secp256k1_mod_sqrt(y_sq)
    if y is None:
        return None
    return (x, y if y % 2 == 0 else _SECP256K1_P - y)


def _secp256k1_add(point_a: _Secp256k1Point, point_b: _Secp256k1Point) -> _Secp256k1Point:
    if point_a is None:
        return point_b
    if point_b is None:
        return point_a
    x1, y1 = point_a
    x2, y2 = point_b
    if x1 == x2 and (y1 + y2) % _SECP256K1_P == 0:
        return None
    if point_a == point_b:
        slope = (3 * x1 * x1 * pow(2 * y1, -1, _SECP256K1_P)) % _SECP256K1_P
    else:
        slope = ((y2 - y1) * pow(x2 - x1, -1, _SECP256K1_P)) % _SECP256K1_P
    x3 = (slope * slope - x1 - x2) % _SECP256K1_P
    y3 = (slope * (x1 - x3) - y1) % _SECP256K1_P
    return (x3, y3)


def _secp256k1_mul(scalar: int, point: _Secp256k1Point) -> _Secp256k1Point:
    result: _Secp256k1Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _secp256k1_add(result, addend)
        addend = _secp256k1_add(addend, addend)
        scalar >>= 1
    return result


def _secp256k1_recover_ecdsa_public_key(r: int, s: int, digest_bytes: bytes, recovery_id: int) -> _Secp256k1Point:
    if not (1 <= r < _SECP256K1_N and 1 <= s < _SECP256K1_N):
        return None
    x = r + (recovery_id >> 1) * _SECP256K1_N
    if x >= _SECP256K1_P:
        return None
    y = _secp256k1_mod_sqrt((pow(x, 3, _SECP256K1_P) + 7) % _SECP256K1_P)
    if y is None:
        return None
    if (y % 2) != (recovery_id & 1):
        y = _SECP256K1_P - y
    point_r = (x, y)
    if _secp256k1_mul(_SECP256K1_N, point_r) is not None:
        return None
    z = int.from_bytes(digest_bytes, "big") % _SECP256K1_N
    r_inv = pow(r, -1, _SECP256K1_N)
    return _secp256k1_mul(
        r_inv,
        _secp256k1_add(
            _secp256k1_mul(s, point_r),
            _secp256k1_mul(z, (_SECP256K1_G[0], (-_SECP256K1_G[1]) % _SECP256K1_P)),
        ),
    )


def _verify_recoverable_signature_recovery_id(
    *,
    public_key_bytes: bytes,
    digest_bytes: bytes,
    r: int,
    s: int,
    recovery_id: int,
) -> None:
    expected = _secp256k1_decompress_public_key(public_key_bytes)
    if expected is None:
        raise _protected_sign_invalid("protected signing public key is not a valid secp256k1 point")
    recovered = _secp256k1_recover_ecdsa_public_key(r, s, digest_bytes, recovery_id)
    if recovered != expected:
        raise _protected_sign_invalid("recoverable signature recovery_id does not match signing_public_key")


def _bip340_tagged_hash(tag: str, payload: bytes) -> bytes:
    import hashlib

    tag_hash = hashlib.sha256(tag.encode("utf-8")).digest()
    return hashlib.sha256(tag_hash + tag_hash + payload).digest()


def _xonly_public_key(public_key_bytes: bytes) -> bytes:
    if len(public_key_bytes) == 32:
        x_only = public_key_bytes
    elif len(public_key_bytes) == 33 and public_key_bytes[0] in {2, 3}:
        x_only = public_key_bytes[1:]
    elif len(public_key_bytes) == 65 and public_key_bytes[0] == 4:
        x_only = public_key_bytes[1:33]
        x = int.from_bytes(x_only, "big")
        y = int.from_bytes(public_key_bytes[33:], "big")
        if x >= _SECP256K1_P or y >= _SECP256K1_P or (pow(y, 2, _SECP256K1_P) - pow(x, 3, _SECP256K1_P) - 7) % _SECP256K1_P:
            raise _protected_sign_invalid("protected signing public key is not a valid secp256k1 point")
    else:
        raise _protected_sign_invalid("protected signing public key must be compressed, uncompressed, or x-only secp256k1")
    if _secp256k1_lift_x(int.from_bytes(x_only, "big")) is None:
        raise _protected_sign_invalid("protected signing public key is not a valid secp256k1 point")
    return x_only


def _verify_protected_schnorr_signature(
    *,
    public_key_bytes: bytes,
    digest_bytes: bytes,
    signature: dict[str, Any],
) -> None:
    sig_bytes = _signature_bytes_for_verification(signature)
    if len(sig_bytes) != 64:
        raise _protected_sign_invalid("BIP340 Schnorr signatures must be 64 bytes")
    if signature.get("recovery_id") is not None:
        raise _protected_sign_invalid("BIP340 Schnorr signatures must not include recovery_id")
    public_key_xonly = _xonly_public_key(public_key_bytes)
    r = int.from_bytes(sig_bytes[:32], "big")
    s = int.from_bytes(sig_bytes[32:], "big")
    if r >= _SECP256K1_P or s >= _SECP256K1_N:
        raise _protected_sign_invalid("browser signature does not verify against signing_public_key")
    point_p = _secp256k1_lift_x(int.from_bytes(public_key_xonly, "big"))
    if point_p is None:
        raise _protected_sign_invalid("protected signing public key is not a valid secp256k1 point")
    challenge = int.from_bytes(_bip340_tagged_hash("BIP0340/challenge", sig_bytes[:32] + public_key_xonly + digest_bytes), "big") % _SECP256K1_N
    point_r = _secp256k1_add(
        _secp256k1_mul(s, _SECP256K1_G),
        _secp256k1_mul(challenge, (point_p[0], (-point_p[1]) % _SECP256K1_P)),
    )
    if point_r is None or point_r[1] % 2 != 0 or point_r[0] != r:
        raise _protected_sign_invalid("browser signature does not verify against signing_public_key")


def _verify_protected_browser_signature(meta: dict, *, digest: str, scheme: str, signature: dict[str, Any]) -> None:
    public_key_bytes = _protected_signing_public_key_bytes(meta)
    digest_bytes = bytes.fromhex(digest)
    if scheme in {"ecdsa-secp256k1-recoverable", "ecdsa-secp256k1-der"}:
        _verify_protected_ecdsa_signature(
            public_key_bytes=public_key_bytes,
            digest_bytes=digest_bytes,
            scheme=scheme,
            signature=signature,
        )
        return
    if scheme == "schnorr-secp256k1-bip340":
        _verify_protected_schnorr_signature(public_key_bytes=public_key_bytes, digest_bytes=digest_bytes, signature=signature)
        return
    raise _protected_sign_invalid(f"unsupported signature scheme: {scheme}")


def revoke_vault_grant(grant_id: str) -> dict:
    from storage import vault_service

    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            grant_row = conn.execute(select(vault_service.vault_grants).where(vault_service.vault_grants.c.id == grant_id)).mappings().first()
            grant_rows = [dict(grant_row)] if grant_row is not None else []
            grant = vault_service.revoke_grant(conn, grant_id)
            release_scopes = vault_service.agent_release_scopes_after_rows(conn, grant_rows)
    except vault_service.GrantNotFoundError as exc:
        raise VaultApiError(f"grant '{grant_id}' not found", code="grant_not_found", status=404) from exc
    except vault_service.GrantNotActiveError as exc:
        raise VaultApiError(f"grant '{grant_id}' is not active", code="grant_not_active", status=409) from exc
    release_vault_agent_scopes(release_scopes, reason=f"revoke_vault_grant:{grant_id}")
    return {"ok": True, "grant": grant}


def vault_sign(payload: dict) -> dict:
    """Sign a digest without returning private key material to Python.

    Standard-tier local keypairs are relayed to ``avault sign``. Protected-tier keypairs
    are browser-signed; the daemon accepts and audits the public signature object only.
    """
    from storage import vault_crypto, vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    name = str(payload.get("name") or "").strip()
    digest = _sign_digest_from_payload(payload.get("digest"))
    scheme = str(payload.get("scheme") or "ecdsa-secp256k1-recoverable").strip()
    if scheme not in vault_service.SUPPORTED_SIGNATURE_SCHEMES:
        raise VaultApiError(f"unsupported signature scheme: {scheme}", code="invalid_request", status=409)
    if not vault_crypto.is_valid_secret_name(name):
        raise VaultApiError("invalid secret name (use ^[A-Z][A-Z0-9_]*$)", code="invalid_name")
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            meta = vault_service.get_secret_meta(conn, name)
            if meta.get("kind") != "keypair":
                raise VaultApiError(f"secret '{name}' is not a signing key", code="not_signing_key", status=409)
            signer_kind = meta.get("signer_kind") or "local"
            if signer_kind != "local":
                raise VaultApiError(
                    f"secret '{name}' uses signer_kind '{signer_kind}', which is not locally signable",
                    code="unsupported_signer_kind",
                    status=409,
                )
            if meta.get("protection") == "protected":
                signature = payload.get("signature")
                if not isinstance(signature, dict):
                    request = vault_service.create_sign_request(
                        conn,
                        name,
                        digest=digest,
                        scheme=scheme,
                        requester=payload.get("requester") if isinstance(payload.get("requester"), dict) else None,
                        delivery=payload.get("delivery") if isinstance(payload.get("delivery"), dict) else None,
                    )
                    return {"ok": False, "code": "browser_signature_required", "request": request}
                request_id = str(payload.get("request_id") or "")
                if not request_id:
                    raise VaultApiError("request_id is required to complete protected signing", code="missing_request_id")
                vault_service.validate_sign_request(conn, request_id, name=name, digest=digest, scheme=scheme)
                signature = _normalized_protected_signature(signature)
                _verify_protected_browser_signature(meta, digest=digest, scheme=scheme, signature=signature)
                request = vault_service.complete_sign_request(
                    conn,
                    request_id,
                    name=name,
                    digest=digest,
                    scheme=scheme,
                    signature=signature,
                    requester=payload.get("requester") if isinstance(payload.get("requester"), dict) else None,
                )
                return {"ok": True, "signature": signature, "request": request}
            request_id = str(payload.get("request_id") or "")
            if request_id:
                vault_service.claim_sign_request(conn, request_id, name=name, digest=digest, scheme=scheme)
            key_envelope = vault_service.get_key_envelope(conn, name)
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{name}' not found", code="secret_not_found", status=404) from exc
    except vault_service.RequestNotFoundError as exc:
        raise VaultApiError(f"request '{exc}' not found", code="request_not_found", status=404) from exc
    except vault_service.InvalidRequestError as exc:
        raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
    except vault_service.VaultServiceError as exc:
        raise VaultApiError(str(exc), code="vault_error") from exc

    def _fail_claimed_request(reason: str) -> None:
        request_id = str(payload.get("request_id") or "")
        if not request_id:
            return
        with contextlib.suppress(Exception):
            with engine.begin() as conn:
                vault_service.fail_sign_request(conn, request_id, reason=reason)

    try:
        signature = avault_sign(key_envelope, digest, scheme, name=name)
    except AvaultError as exc:
        _fail_claimed_request("avault_failed")
        raise VaultApiError(f"avault sign failed: {exc}", code="avault_failed") from exc
    request_id = str(payload.get("request_id") or "")
    if request_id:
        try:
            with engine.begin() as conn:
                request = vault_service.complete_sign_request(
                    conn,
                    request_id,
                    name=name,
                    digest=digest,
                    scheme=scheme,
                    signature=signature,
                    requester=payload.get("requester") if isinstance(payload.get("requester"), dict) else None,
                )
        except vault_service.RequestNotFoundError as exc:
            raise VaultApiError(f"request '{exc}' not found", code="request_not_found", status=404) from exc
        except vault_service.InvalidRequestError as exc:
            _fail_claimed_request("signature_rejected")
            raise VaultApiError(str(exc), code="invalid_request", status=409) from exc
        return {"ok": True, "signature": signature, "request": request}
    try:
        with engine.begin() as conn:
            vault_service.record_signing_use(
                conn,
                name,
                requester=payload.get("requester") if isinstance(payload.get("requester"), dict) else None,
                delivery={"scheme": scheme, "digest": digest},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("vault_sign: signature produced but usage audit failed for %s: %s", name, exc)
    return {"ok": True, "signature": signature}


def store_vault_pubkey_pin(payload: dict) -> dict:
    """Store value-free pubkey pin/attestation metadata on a vault secret."""
    from storage import vault_service

    if not isinstance(payload, dict):
        raise VaultApiError("payload must be an object", code="invalid_payload")
    name = str(payload.get("name") or "").strip()
    pin = payload.get("pin") if isinstance(payload.get("pin"), dict) else None
    if not name or not pin:
        raise VaultApiError("name and pin are required", code="invalid_pin")
    engine = _vault_engine()
    try:
        with engine.begin() as conn:
            meta = vault_service.store_pubkey_pin(conn, name, pin)
    except vault_service.SecretNotFoundError as exc:
        raise VaultApiError(f"secret '{name}' not found", code="secret_not_found", status=404) from exc
    return {"ok": True, "secret": meta}


def import_vibe_agents(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Import payload must be an object")
    candidates = []
    skipped = []
    file_path = payload.get("file")
    source = payload.get("from") or payload.get("from_source")
    name = str(payload.get("name") or "").strip()
    import_all = bool(payload.get("all"))

    if file_path:
        if source:
            raise ValueError("Use either file or from, not both")
        if name or import_all:
            raise ValueError("name and all are only valid with from")
        backend = validate_agent_backend(str(payload.get("backend") or ""))
        path = _validate_direct_agent_import_path(Path(file_path).expanduser())
        candidates.append(_parse_agent_import_file(path, backend=backend))
    else:
        if source not in {"claude", "codex", "opencode"}:
            raise ValueError("from must be one of: claude, codex, opencode")
        if name and import_all:
            raise ValueError("Use either name or all, not both")
        for path, backend in iter_global_agent_files(str(source)):
            try:
                candidate = _parse_agent_import_file(path, backend=backend)
            except ValueError as exc:
                skipped.append({"source_ref": str(path), "reason": "invalid", "error": str(exc)})
                continue
            if name and candidate.name != name:
                continue
            candidates.append(candidate)
        if name and not candidates:
            return {
                "ok": False,
                "code": "agent_import_source_not_found",
                "message": f"agent '{name}' was not found in {source} global agents",
            }

    store = VibeAgentStore()
    try:
        result = store.import_candidates(candidates)
        return {
            "ok": True,
            "imported": [_vibe_agent_payload(agent, brief=True) for agent in result.imported],
            "skipped": skipped + result.skipped,
        }
    finally:
        store.close()


def get_settings(platform: Optional[str] = None) -> dict:
    store = SettingsStore.get_instance()
    target_platform = platform or _current_platform()
    if target_platform == "discord":
        _migrate_discord_guild_scope_from_config(store)
    payload = _settings_to_payload(store, platform=target_platform)
    payload["agent_catalog"] = get_vibe_agents()
    return payload


def _normalize_backend_routing_payload(routing_payload: dict) -> dict:
    from modules.agents.opencode.utils import normalize_claude_reasoning_effort

    routing = _parse_routing(routing_payload or {})
    if _backend_for_routing_agent(routing.agent_name) == "claude":
        routing.reasoning_effort = normalize_claude_reasoning_effort(
            routing.model,
            routing.reasoning_effort,
        )
    return _routing_to_dict(routing)


def _backend_for_routing_agent(agent_name: Optional[str]) -> Optional[str]:
    name = str(agent_name or "").strip()
    if not name:
        return None
    if is_agent_backend(name):
        return name
    store = VibeAgentStore()
    try:
        agent = store.get(name)
        return agent.backend if agent is not None else None
    except Exception:
        logger.debug("Failed to resolve Agent backend while normalizing routing payload", exc_info=True)
        return None
    finally:
        store.close()


def _normalize_show_message_types_for_platform(show_message_types: Optional[list], platform: str) -> list[str]:
    normalized = normalize_show_message_types(show_message_types)
    if not get_platform_descriptor(platform).capabilities.supports_toolcall_delivery:
        return [msg_type for msg_type in normalized if msg_type != "toolcall"]
    return normalized


def save_settings(payload: dict) -> dict:
    store = SettingsStore.get_instance()
    platform = payload.get("platform") or _current_platform()

    if "channels" in payload:
        channels = {}
        for channel_id, channel_payload in (payload.get("channels") or {}).items():
            channels[channel_id] = ChannelSettings(
                enabled=channel_payload.get("enabled", True),
                show_message_types=_normalize_show_message_types_for_platform(
                    channel_payload.get("show_message_types"), platform
                ),
                custom_cwd=channel_payload.get("custom_cwd"),
                routing=_parse_routing(_normalize_backend_routing_payload(channel_payload.get("routing") or {})),
                require_mention=channel_payload.get("require_mention"),
                require_bind=channel_payload.get("require_bind"),
            )
        store.set_channels_for_platform(platform, channels)
    if "guilds" in payload or "guild_allowlist" in payload:
        guilds, default_enabled = _guild_scope_update_from_settings_payload(store, platform, payload)
        store.set_guilds_for_platform(platform, guilds, default_enabled=default_enabled)
    store.save()
    return _settings_to_payload(store, platform=platform)


def _guild_scope_update_from_settings_payload(
    store: SettingsStore,
    platform: str,
    payload: dict,
) -> tuple[dict[str, GuildSettings], bool]:
    next_guilds = _guild_settings_from_payload(payload)
    if "guild_default_enabled" in payload:
        return next_guilds, bool(payload.get("guild_default_enabled", False))

    default_enabled = store.get_guild_default_enabled_for_platform(platform)
    if default_enabled:
        for guild_id, settings in store.get_guilds_for_platform(platform).items():
            if not settings.enabled and guild_id not in next_guilds:
                next_guilds[guild_id] = settings
    return next_guilds, default_enabled


def _guild_settings_from_payload(payload: dict) -> dict[str, GuildSettings]:
    if "guilds" in payload:
        guild_payload = payload.get("guilds") or {}
        if not isinstance(guild_payload, dict):
            return {}
        return {
            str(guild_id): GuildSettings(enabled=(settings or {}).get("enabled", True))
            for guild_id, settings in guild_payload.items()
            if isinstance(settings, dict)
        }

    allowlist = payload.get("guild_allowlist") or []
    if not isinstance(allowlist, list):
        return {}
    return {str(guild_id): GuildSettings(enabled=True) for guild_id in allowlist if str(guild_id)}


def _migrate_discord_guild_scope_from_config(store: SettingsStore, config: Optional[V2Config] = None) -> None:
    if store.has_guild_scope_for_platform("discord"):
        return
    try:
        cfg = config or load_config()
    except FileNotFoundError:
        return
    discord_config = getattr(cfg, "discord", None)
    if not discord_config:
        return
    allowlist = getattr(discord_config, "guild_allowlist", None) or []
    denylist = getattr(discord_config, "guild_denylist", None) or []
    if not allowlist and not denylist:
        return
    _save_discord_guild_scope_update(*_discord_guild_scope_from_legacy_payload(allowlist, denylist), store=store)


def _discord_guild_scope_from_legacy_payload(
    allowlist: list | None,
    denylist: list | None,
) -> tuple[dict[str, GuildSettings], bool]:
    default_enabled = not bool(allowlist)
    guilds = {
        str(guild_id): GuildSettings(enabled=True)
        for guild_id in (allowlist or [])
        if str(guild_id)
    }
    for guild_id in denylist or []:
        guilds[str(guild_id)] = GuildSettings(enabled=False)
    return guilds, default_enabled


def _discord_guild_scope_from_config(config: V2Config) -> Optional[tuple[dict[str, GuildSettings], bool]]:
    discord_config = getattr(config, "discord", None)
    if not discord_config:
        return None
    allowlist = getattr(discord_config, "guild_allowlist", None) or []
    denylist = getattr(discord_config, "guild_denylist", None) or []
    if not allowlist and not denylist:
        return None
    return _discord_guild_scope_from_legacy_payload(allowlist, denylist)


def _save_discord_guild_scope_update(
    guilds: dict[str, GuildSettings],
    default_enabled: bool,
    store: Optional[SettingsStore] = None,
) -> None:
    target_store = store or SettingsStore.get_instance()
    target_store.set_guilds_for_platform("discord", guilds, default_enabled=default_enabled)
    target_store.save()


def _extract_settings_scopes_from_config_payload(
    payload: dict,
) -> tuple[dict, Optional[tuple[dict[str, GuildSettings], bool]]]:
    """Move legacy Discord server access fields from config updates to settings."""
    if not isinstance(payload, dict):
        return payload, None
    next_payload = dict(payload)
    discord_payload = next_payload.get("discord")
    if not isinstance(discord_payload, dict):
        return next_payload, None

    discord_next = dict(discord_payload)
    has_guild_scope = "guild_allowlist" in discord_next or "guild_denylist" in discord_next
    allowlist = discord_next.pop("guild_allowlist", None)
    denylist = discord_next.pop("guild_denylist", None)
    next_payload["discord"] = discord_next

    if has_guild_scope:
        return next_payload, _discord_guild_scope_from_legacy_payload(allowlist, denylist)

    return next_payload, None


def init_sessions() -> None:
    store = SessionsStore()
    if store.sessions_path.exists():
        return
    store.save()


def detect_cli(binary: str) -> dict:
    path = resolve_cli_path(binary)
    if not path:
        return {"found": False, "path": None}
    return {"found": True, "path": path}


def check_cli_exec(path: str) -> dict:
    if not path:
        return {"ok": False, "error": "path is empty"}
    if not os.path.exists(path):
        return {"ok": False, "error": "path does not exist"}
    if not os.access(path, os.X_OK):
        return {"ok": False, "error": "path is not executable"}
    return {"ok": True}


def _stored_platform_config(platform: str) -> Any | None:
    try:
        config = load_config()
    except FileNotFoundError:
        return None
    return getattr(config, platform, None)


def _stored_platform_secret(platform: str, field: str) -> str:
    platform_config = _stored_platform_config(platform)
    value = getattr(platform_config, field, "") if platform_config else ""
    return value if isinstance(value, str) else ""


def _stored_platform_field(platform: str, field: str, default: str = "") -> str:
    platform_config = _stored_platform_config(platform)
    value = getattr(platform_config, field, default) if platform_config else default
    return value if isinstance(value, str) else default


def slack_auth_test(bot_token: str, proxy_url: str | None = None) -> dict:
    bot_token = bot_token or _stored_platform_secret("slack", "bot_token")
    try:
        from slack_sdk.web import WebClient
        from vibe.proxy import resolve_proxy

        proxy = resolve_proxy(proxy_url)
        client = WebClient(token=bot_token, proxy=proxy)
        response = client.auth_test()
        return {"ok": True, "response": response.data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def list_channels(
    bot_token: str,
    browse_all: bool = False,
    force: bool = False,
    include_not_returned: bool = False,
) -> dict:
    from core import chat_discovery

    bot_token = bot_token or _stored_platform_secret("slack", "bot_token")
    return chat_discovery.channels_response(
        "slack",
        bot_token=bot_token,
        browse_all=browse_all,
        require_member=not browse_all,
        force=force,
        include_not_returned=include_not_returned,
    )


def list_channels_live(bot_token: str, browse_all: bool = False) -> dict:
    """List Slack channels.

    When *browse_all* is False (default), only channels the bot has joined are
    returned via ``users_conversations``.  This is very fast and avoids hitting
    Slack rate-limits even in large workspaces.

    When *browse_all* is True, all visible channels in the workspace are
    returned via ``conversations_list``.  Rate-limit retries with exponential
    back-off are applied automatically.
    """
    import time

    from slack_sdk.errors import SlackApiError
    from slack_sdk.web import WebClient

    client = WebClient(token=bot_token)
    channels: list[dict] = []
    cursor = None

    try:
        while True:
            for attempt in range(5):
                try:
                    if browse_all:
                        response = client.conversations_list(
                            types="public_channel,private_channel",
                            exclude_archived=True,
                            limit=200,
                            cursor=cursor,
                        )
                    else:
                        response = client.users_conversations(
                            types="public_channel,private_channel",
                            exclude_archived=True,
                            limit=200,
                            cursor=cursor,
                        )
                    break  # success
                except SlackApiError as e:
                    if e.response.status_code == 429:
                        retry_after = int(e.response.headers.get("Retry-After", 1))
                        wait = max(retry_after, 2**attempt)
                        logger.warning(
                            "Slack rate-limited (429), retrying after %ds (attempt %d/5)",
                            wait,
                            attempt + 1,
                        )
                        time.sleep(wait)
                    else:
                        raise
            else:
                # Exhausted retries
                return {
                    "ok": False,
                    "error": "Slack rate-limit exceeded after 5 retries",
                }

            for channel in response.get("channels", []):
                channels.append(
                    {
                        "id": channel.get("id"),
                        "name": channel.get("name"),
                        "is_private": channel.get("is_private", False),
                        "is_member": channel.get("is_member"),
                    }
                )
            cursor = response.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return {"ok": True, "channels": channels, "is_member_only": not browse_all}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def discord_auth_test(bot_token: str, proxy_url: str | None = None) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(discord_auth_test_async(bot_token, proxy_url=proxy_url))


async def discord_auth_test_async(bot_token: str, proxy_url: str | None = None) -> dict:
    bot_token = bot_token or _stored_platform_secret("discord", "bot_token")
    try:
        data = await _discord_api_get_async(bot_token, "users/@me", proxy_url=proxy_url)
        return {"ok": True, "response": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def telegram_auth_test(bot_token: str, proxy_url: str | None = None) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(telegram_auth_test_async(bot_token, proxy_url=proxy_url))


async def telegram_auth_test_async(bot_token: str, proxy_url: str | None = None) -> dict:
    bot_token = bot_token or _stored_platform_secret("telegram", "bot_token")
    try:
        from vibe.proxy import resolve_proxy

        proxy = resolve_proxy(proxy_url)
        return {"ok": True, "response": await _telegram_get_me(bot_token, proxy)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def delete_channel_scope(platform: str, native_id: str, scope_type: str = "channel") -> dict:
    """Permanently remove a discovered channel/chat scope and its settings.

    Restricted to ``channel`` scopes: this endpoint exists only to clear stale
    discovered chats. Deleting other scope types (e.g. ``project``) would bypass
    their dedicated lifecycle (project archival preserves the scope for sessions),
    so any non-channel scope type is rejected.
    """
    from core import chat_discovery

    platform = str(platform or "").strip()
    native_id = str(native_id or "").strip()
    scope_type = str(scope_type or "channel").strip()
    if not platform or not native_id:
        return {"ok": False, "error": "platform and id are required"}
    if scope_type != "channel":
        return {"ok": False, "error": "only channel scopes can be removed here"}
    try:
        outcome = chat_discovery.delete_scope(platform, native_id, scope_type="channel")
    except Exception as exc:
        logger.warning("Failed to delete %s scope %s: %s", platform, native_id, exc, exc_info=True)
        return {"ok": False, "error": str(exc)}
    # ``dismissed`` means history was preserved and the entry was hidden instead
    # of physically deleted; ``removed`` means the scope row was deleted outright.
    return {"ok": True, **outcome}


def telegram_list_chats(include_private: bool = False, include_not_returned: bool = False) -> dict:
    from core import chat_discovery

    return chat_discovery.channels_response(
        "telegram",
        include_private=include_private,
        include_not_returned=include_not_returned,
    )


def discord_list_guilds(bot_token: str) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(discord_list_guilds_async(bot_token))


async def discord_list_guilds_async(bot_token: str) -> dict:
    bot_token = bot_token or _stored_platform_secret("discord", "bot_token")
    try:
        data = await _discord_api_get_async(bot_token, "users/@me/guilds")
        return {"ok": True, "guilds": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def discord_list_channels(
    bot_token: str, guild_id: str, force: bool = False, include_not_returned: bool = False
) -> dict:
    guild_id = str(guild_id or "").strip()
    if not guild_id:
        return {
            "ok": False,
            "channels": [],
            "chats": [],
            "refreshing": False,
            "last_attempt_at": None,
            "last_success_at": None,
            "error": "Discord guild_id is required",
            "summary": {"discovered_count": 0, "visible_count": 0, "hidden_private_count": 0, "forum_count": 0},
        }

    from core import chat_discovery

    from storage.settings_service import make_scope_id

    bot_token = bot_token or _stored_platform_secret("discord", "bot_token")
    parent_scope_id = make_scope_id("discord", "guild", guild_id)
    return chat_discovery.channels_response(
        "discord",
        bot_token=bot_token,
        guild_id=guild_id,
        parent_scope_id=parent_scope_id,
        force=force,
        include_not_returned=include_not_returned,
    )


def discord_list_channels_live(bot_token: str, guild_id: str) -> dict:
    try:
        data = _discord_api_get(bot_token, f"guilds/{guild_id}/channels")
        channels = []
        for channel in data:
            channels.append(
                {
                    "id": channel.get("id"),
                    "name": channel.get("name"),
                    "type": channel.get("type"),
                    "position": channel.get("position"),
                    "parent_id": channel.get("parent_id"),
                }
            )
        return {"ok": True, "channels": channels}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def opencode_options(cwd: str) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    try:
        return run_coroutine_blocking(opencode_options_async(cwd))
    except Exception as exc:
        logger.warning("OpenCode options fetch failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc)}


def _discord_retry_wait(exc: "urllib.error.HTTPError", attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return max(float(retry_after), float(2**attempt)) if retry_after else float(2**attempt)
    except (TypeError, ValueError):
        return float(2**attempt)


def _discord_api_get(bot_token: str, path: str, proxy_url: str | None = None) -> dict:
    import urllib.error
    import urllib.request

    from vibe.proxy import is_socks_proxy, resolve_proxy

    if not bot_token:
        raise ValueError("bot_token is required")
    url = f"https://discord.com/api/v10/{path.lstrip('/')}"
    headers = {"Authorization": f"Bot {bot_token}", "User-Agent": "avibe"}

    proxy = resolve_proxy(proxy_url)

    def _request() -> dict:
        if proxy and is_socks_proxy(proxy):
            return _https_json_request_via_socks(proxy, url, headers=headers)
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=10) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)

    attempts = 5
    for attempt in range(attempts):
        try:
            return _request()
        except urllib.error.HTTPError as http_exc:
            if http_exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = _discord_retry_wait(http_exc, attempt)
                logger.warning(
                    "Discord rate-limited/transient (%d), retrying after %ss (attempt %d/%d)",
                    http_exc.code,
                    wait,
                    attempt + 1,
                    attempts,
                )
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("Discord request exhausted retries")  # pragma: no cover


async def _discord_api_get_async(bot_token: str, path: str, proxy_url: str | None = None) -> dict:
    import urllib.error
    import urllib.request

    from vibe.proxy import is_socks_proxy, resolve_proxy

    if not bot_token:
        raise ValueError("bot_token is required")
    url = f"https://discord.com/api/v10/{path.lstrip('/')}"
    headers = {"Authorization": f"Bot {bot_token}", "User-Agent": "avibe"}

    proxy = resolve_proxy(proxy_url)
    use_socks = bool(proxy and is_socks_proxy(proxy))

    if proxy and not use_socks:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    else:
        opener = urllib.request.build_opener()

    def _request() -> dict:
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=10) as resp:
            payload = resp.read().decode("utf-8")
            return json.loads(payload)

    attempts = 5
    for attempt in range(attempts):
        try:
            if use_socks:
                # urllib has no native SOCKS support; route via aiohttp + aiohttp_socks.
                return await _discord_api_get_via_aiohttp(url, headers, proxy)
            return await asyncio.to_thread(_request)
        except urllib.error.HTTPError as http_exc:
            if http_exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = _discord_retry_wait(http_exc, attempt)
                logger.warning(
                    "Discord rate-limited/transient (%d), retrying after %ss (attempt %d/%d)",
                    http_exc.code,
                    wait,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as exc:
            # aiohttp (SOCKS path) raises ClientResponseError with a `.status`.
            status = getattr(exc, "status", None)
            if status in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                wait = float(2**attempt)
                logger.warning(
                    "Discord rate-limited/transient (%s) via proxy, retrying after %ss (attempt %d/%d)",
                    status,
                    wait,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(wait)
                continue
            raise
    raise RuntimeError("Discord request exhausted retries")  # pragma: no cover


def _https_json_request_via_socks(
    proxy_url: str,
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> dict:
    from python_socks.sync import Proxy

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS URLs are supported")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    sock = Proxy.from_url(proxy_url).connect(parsed.hostname, port, timeout=timeout)
    try:
        tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
    except Exception:
        sock.close()
        raise
    conn = HTTPSConnection(parsed.hostname, port, timeout=timeout)
    conn.sock = tls_sock
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        if resp.status < 200 or resp.status >= 300:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)
        return json.loads(payload)
    finally:
        conn.close()


async def _discord_api_get_via_aiohttp(url: str, headers: dict, proxy: str) -> dict:
    import aiohttp
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy, rdns=True)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            # urllib.urlopen raises HTTPError on non-2xx; mirror that here so
            # callers like discord_auth_test correctly treat 401 as a failure
            # instead of returning Discord's error JSON as a successful payload.
            resp.raise_for_status()
            return await resp.json()


async def _telegram_get_me(bot_token: str, proxy_url: str | None = None) -> dict:
    from modules.im import telegram_api

    result = await telegram_api.get_me(bot_token, proxy_url=proxy_url)
    return result.get("result") or {}


async def opencode_options_async(cwd: str) -> dict:
    # Expand ~ to user home directory
    request_loop = asyncio.get_running_loop()
    expanded_cwd = os.path.expanduser(cwd)
    cache_entry = _OPENCODE_OPTIONS_CACHE.get(expanded_cwd, {})
    cache_data = cache_entry.get("data")
    updated_at = cache_entry.get("updated_at", 0.0)
    cache_age = time.monotonic() - updated_at
    if cache_data and cache_age < _OPENCODE_OPTIONS_TTL_SECONDS:
        return {"ok": True, "data": cache_data, "cached": True}

    server = None
    try:
        from config.v2_compat import to_app_config
        from core.resource_governance import AgentResourceGovernor, config_from_runtime
        from modules.agents.opencode import (
            OpenCodeServerManager,
            build_reasoning_effort_options,
        )

        v2_config = V2Config.load()
        config = to_app_config(v2_config)
        if not config.opencode:
            return {"ok": False, "error": "opencode disabled"}
        opencode_config = config.opencode
        timeout_seconds = min(10.0, float(opencode_config.request_timeout_seconds or 10))

        def _build_reasoning_options(
            models: dict,
            builder,
        ) -> dict:
            options: dict = {}
            for provider in models.get("providers", []):
                provider_id = provider.get("id") or provider.get("provider_id") or provider.get("name")
                if not provider_id:
                    continue
                model_ids = []
                provider_models = provider.get("models", {})
                if isinstance(provider_models, dict):
                    model_ids = list(provider_models.keys())
                elif isinstance(provider_models, list):
                    model_ids = [
                        model.get("id") for model in provider_models if isinstance(model, dict) and model.get("id")
                    ]
                for model_id in model_ids:
                    model_key = f"{provider_id}/{model_id}"
                    options[model_key] = builder(models, model_key)
            return options

        server = await OpenCodeServerManager.get_instance(
            binary=opencode_config.binary,
            port=opencode_config.port,
            request_timeout_seconds=opencode_config.request_timeout_seconds,
            resource_governor=AgentResourceGovernor(config_from_runtime(v2_config)),
        )
        await asyncio.wait_for(server.ensure_running(), timeout=timeout_seconds)
        agents = await asyncio.wait_for(server.get_available_agents(expanded_cwd), timeout=timeout_seconds)
        models = await asyncio.wait_for(server.get_available_models(expanded_cwd), timeout=timeout_seconds)
        provider_catalog_available = True
        try:
            providers_raw = await asyncio.wait_for(server.get_providers(), timeout=timeout_seconds)
        except Exception as exc:
            logger.debug("OpenCode provider auth filter skipped: provider list failed: %s", exc)
            providers_raw = {}
            provider_catalog_available = False
        try:
            from vibe.opencode_config import read_opencode_provider_auth_entries

            auth_entries = await asyncio.to_thread(
                read_opencode_provider_auth_entries, logger_instance=logger
            )
        except Exception as exc:
            logger.debug("OpenCode provider auth filter skipped: auth read failed: %s", exc)
            auth_entries = {}
        allowed_provider_ids: set[str] | None = None
        if provider_catalog_available:
            config_api_key_provider_ids = await _read_opencode_config_api_key_provider_ids()
            custom_config_provider_ids = await _read_opencode_custom_provider_ids()
            allowed_provider_ids = _configured_opencode_provider_ids(
                providers_raw=providers_raw,
                auth_entries=auth_entries,
                config_api_key_provider_ids=config_api_key_provider_ids,
                custom_config_provider_ids=custom_config_provider_ids,
            )
            models = _filter_opencode_models_to_configured_providers(
                models,
                providers_raw=providers_raw,
                auth_entries=auth_entries,
                config_api_key_provider_ids=config_api_key_provider_ids,
                custom_config_provider_ids=custom_config_provider_ids,
            )
        user_model_index = await _read_opencode_user_model_index()
        models = _merge_opencode_user_models(
            models,
            user_model_index,
            allowed_provider_ids=allowed_provider_ids,
        )
        defaults = await asyncio.wait_for(server.get_default_config(expanded_cwd), timeout=timeout_seconds)
        reasoning_options = _build_reasoning_options(models, build_reasoning_effort_options)
        data = {
            "agents": agents,
            "models": models,
            "defaults": defaults,
            "reasoning_options": reasoning_options,
        }
        _OPENCODE_OPTIONS_CACHE[expanded_cwd] = {
            "data": data,
            "updated_at": time.monotonic(),
        }
        return {"ok": True, "data": data}
    except Exception as exc:
        logger.warning("OpenCode options fetch failed: %s", exc, exc_info=True)
        if cache_data:
            return {"ok": True, "data": cache_data, "cached": True, "warning": str(exc)}
        return {"ok": False, "error": str(exc)}
    finally:
        if server is not None:
            await server.close_http_session(loop=request_loop)


def _current_platform() -> str:
    return load_config().platform


def _settings_to_payload(store: SettingsStore, platform: str) -> dict:
    payload: dict = {
        "channels": {},
        "guilds": {},
        "guild_allowlist": [],
        "guild_scope_configured": False,
        "guild_default_enabled": False,
        "users": {},
        "bind_codes": [],
    }
    for channel_id, settings in store.get_channels_for_platform(platform).items():
        payload["channels"][channel_id] = {
            "enabled": settings.enabled,
            "show_message_types": _normalize_show_message_types_for_platform(settings.show_message_types, platform),
            "custom_cwd": settings.custom_cwd,
            "require_mention": settings.require_mention,
            "require_bind": settings.require_bind,
            "routing": routing_to_compat_dict(settings.routing),
        }
    payload["guild_scope_configured"] = store.has_guild_scope_for_platform(platform)
    payload["guild_default_enabled"] = store.get_guild_default_enabled_for_platform(platform)
    for guild_id, settings in store.get_guilds_for_platform(platform).items():
        payload["guilds"][guild_id] = {
            "enabled": settings.enabled,
        }
    payload["guild_allowlist"] = [
        guild_id for guild_id, settings in payload["guilds"].items() if settings.get("enabled")
    ]
    for user_id, u in store.get_users_for_platform(platform).items():
        payload["users"][user_id] = {
            "display_name": u.display_name,
            "is_admin": u.is_admin,
            "bound_at": u.bound_at,
            "enabled": u.enabled,
            "show_message_types": _normalize_show_message_types_for_platform(u.show_message_types, platform),
            "custom_cwd": u.custom_cwd,
            "routing": routing_to_compat_dict(u.routing),
        }
    for bc in store.settings.bind_codes:
        payload["bind_codes"].append(
            {
                "code": bc.code,
                "type": bc.type,
                "created_at": bc.created_at,
                "expires_at": bc.expires_at,
                "is_active": bc.is_active,
                "used_by": bc.used_by,
            }
        )
    return payload


def get_slack_manifest() -> dict:
    """Get Slack App Manifest template for self-host mode.

    Loads manifest from vibe/templates/slack_manifest.json.

    Returns:
        {"ok": True, "manifest": str, "manifest_compact": str} on success
        {"ok": False, "error": str} on failure
    """
    import json
    import importlib.resources

    try:
        manifest = None

        # Try to load from package resources (installed via pip/uv)
        try:
            if hasattr(importlib.resources, "files"):
                package_files = importlib.resources.files("vibe")
                template_path = package_files / "templates" / "slack_manifest.json"
                if hasattr(template_path, "read_text"):
                    manifest = json.loads(template_path.read_text(encoding="utf-8"))
        except (TypeError, FileNotFoundError, AttributeError, json.JSONDecodeError):
            pass

        # Fallback: load from file system (development mode)
        if manifest is None:
            this_dir = Path(__file__).parent
            template_file = this_dir / "templates" / "slack_manifest.json"
            if template_file.exists():
                manifest = json.loads(template_file.read_text(encoding="utf-8"))

        if manifest is None:
            return {"ok": False, "error": "Manifest template file not found"}

        # Pretty JSON for display, compact JSON for URL
        manifest_pretty = json.dumps(manifest, indent=2)
        manifest_compact = json.dumps(manifest, separators=(",", ":"))
        return {
            "ok": True,
            "manifest": manifest_pretty,
            "manifest_compact": manifest_compact,
        }
    except Exception as exc:
        logger.error("Failed to load Slack manifest: %s", exc)
        return {"ok": False, "error": str(exc)}


def _pid_file_points_to_live_process(pid_path: Path) -> bool:
    from vibe import runtime

    if pid_path == paths.get_runtime_pid_path():
        return runtime.service_pid_file_points_to_running_service(pid_path)
    if pid_path == paths.get_runtime_ui_pid_path():
        return runtime.ui_pid_file_points_to_running_ui(pid_path)
    return False


def _runtime_process_was_running() -> bool:
    return _pid_file_points_to_live_process(paths.get_runtime_pid_path()) or _pid_file_points_to_live_process(
        paths.get_runtime_ui_pid_path()
    )


def get_version_info() -> dict:
    """Get current version and check for updates.

    Returns:
        {
            "current": str,
            "latest": str | None,
            "has_update": bool,
            "error": str | None
        }
    """
    from vibe import __version__

    return get_latest_version_info(__version__)


def do_upgrade(auto_restart: bool = True) -> dict:
    """Perform upgrade to latest version.

    Args:
        auto_restart: If True, restart vibe after successful upgrade

    Returns:
        {"ok": bool, "message": str, "output": str | None, "restarting": bool}
    """
    current_vibe_path = get_running_vibe_path()
    plan = build_upgrade_plan(vibe_path=current_vibe_path)
    runtime_was_running = _runtime_process_was_running()

    # Use a stable directory as cwd to avoid "Current directory does not exist"
    # errors.  The vibe service process cwd may be inside the uv tool venv
    # directory, which uv deletes and recreates during upgrade.
    safe_cwd = get_safe_cwd()

    try:
        result = subprocess.run(
            plan.command,
            capture_output=True,
            text=True,
            timeout=120,
            env=plan.env,
            cwd=safe_cwd,
        )
        if result.returncode == 0:
            restarting = False
            restart_failed = False
            runtime_output = None
            if auto_restart and runtime_was_running:
                try:
                    schedule_restart(
                        delay_seconds=2.0,
                        vibe_path=current_vibe_path,
                        trigger="upgrade",
                        prepare_show_runtime=not should_skip_show_runtime_prepare(),
                    )
                    restarting = True
                except Exception as exc:
                    restart_failed = True
                    runtime_output = f"Restart scheduling failed; run `vibe restart` to use the new version.\n{exc}"
            else:
                runtime_output = _prepare_show_runtime_after_upgrade(current_vibe_path, safe_cwd)
            if restarting:
                message = "Upgrade successful. Restarting..."
            elif restart_failed:
                message = "Upgrade successful, but restart scheduling failed. Please restart vibe."
            else:
                message = "Upgrade successful. Please restart vibe."

            return {
                "ok": True,
                "message": message,
                "output": _append_upgrade_output(result.stdout, runtime_output),
                "restarting": restarting,
            }
        else:
            return {
                "ok": False,
                "message": "Upgrade failed",
                "output": result.stderr or result.stdout,
                "restarting": False,
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "message": "Upgrade timed out",
            "output": None,
            "restarting": False,
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "output": None, "restarting": False}


def _append_upgrade_output(output: str | None, runtime_output: str | None) -> str | None:
    parts = [part for part in ((output or "").strip(), (runtime_output or "").strip()) if part]
    if not parts:
        return None
    return "\n\n".join(parts)


def _prepare_show_runtime_after_upgrade(vibe_path: str | None, cwd: str) -> str | None:
    if should_skip_show_runtime_prepare():
        return "Show Runtime preparation skipped because VIBE_INSTALL_SKIP_SHOW_RUNTIME is set."
    if not vibe_path:
        return "Show Runtime was not prepared because the vibe executable path was not available."
    try:
        result = subprocess.run(
            [vibe_path, "runtime", "prepare", "--strict"],
            capture_output=True,
            text=True,
            # 600s (not 300s): prepare now refreshes both the Show Runtime AND
            # askill, so budget for two installers nested in this one call.
            timeout=600,
            cwd=cwd,
            check=False,
        )
    except Exception as exc:
        return f"Show Runtime preparation skipped: {exc}"
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode == 0:
        return output or "Show Runtime prepared."
    return "Show Runtime preparation failed; avibe upgrade is still installed." + (f"\n{output}" if output else "")


def _opencode_permission_grants_all(node) -> bool:
    """Recursively decide whether an OpenCode permission node allows every call.

    OpenCode resolves the last matching rule, with ``"*"`` as the wildcard at
    each level and tool entries that are themselves either a string or a nested
    rule object (https://opencode.ai/docs/permissions). A node grants all tool
    calls iff it is the string ``"allow"``, or a dict whose ``"*"`` wildcard
    grants all AND every nested rule grants all — so a single ``"bash": "ask"``
    (or a nested ``ask``/``deny``) keeps it ungranted, while a fully-``allow``
    tree (e.g. ``{"*": "allow", "bash": {"*": "allow"}}``) passes. Conservative:
    a dict without a ``"*"`` can't guarantee otherwise-unmatched keys, so it is
    treated as not-granted — erring toward keeping the write-allow button rather
    than letting a tool call silently stall on a prompt avibe can't answer.
    """
    if node == "allow":
        return True
    if isinstance(node, dict):
        return _opencode_permission_grants_all(node.get("*")) and all(
            _opencode_permission_grants_all(value) for value in node.values()
        )
    return False


def opencode_permission_allowed(probe) -> bool:
    """Whether an OpenCode config probe already grants full tool-call permission.

    Single source of truth for the "Allow tool calls" state. Without a config
    that allows every call the OpenCode daemon prompts on tool calls and Vibe
    Remote can't answer the prompt, so both the Settings provider page and the
    setup wizard hide the write-allow affordance once this returns True.

    Accepts both forms OpenCode documents: the ``"permission": "allow"`` string
    shorthand and the granular object form — evaluated recursively via
    :func:`_opencode_permission_grants_all` so any config that already avoids all
    approval prompts (including nested ``allow`` rules) isn't gated or nagged to
    overwrite, while a partial config that can still prompt keeps the button.
    """
    config = getattr(probe, "config", None)
    if not isinstance(config, dict):
        return False
    return _opencode_permission_grants_all(config.get("permission"))


def opencode_permission_status() -> dict:
    """Cheaply report whether ``opencode.json`` already grants ``permission: "allow"``.

    Reads (and JSONC-parses) the user's OpenCode config only — it never starts
    the OpenCode server — so the setup wizard can decide whether to surface the
    write-allow affordance without paying for a full provider probe (which the
    Settings page already does via ``get_opencode_providers``).

    Returns ``{"ok": bool, "permission_allowed": bool, "config_path": str}``.
    """
    config_paths = get_opencode_config_paths(Path.home())
    probe = load_first_opencode_user_config(home=Path.home(), logger_instance=logger)
    # A malformed existing config can't be auto-fixed — ``setup_opencode_permission``
    # refuses to overwrite invalid files — so report unknown (``ok: False``) here.
    # The wizard gate keys off a successful status read, so this makes it FAIL
    # OPEN instead of trapping the user behind a Continue the only available
    # action can't satisfy. (A missing config — no existing paths — is the normal
    # first-run case and stays ``ok: True`` so setup can create it.)
    if probe.config is None and probe.existing_paths:
        error_path, error_message = (
            probe.errors[0] if probe.errors else (probe.existing_paths[0], "unknown parse error")
        )
        return {
            "ok": False,
            "permission_allowed": False,
            "config_path": str(error_path),
            "message": f"Existing OpenCode config could not be parsed: {error_message}",
        }
    config_path = probe.path if probe.path is not None else (config_paths[0] if config_paths else None)
    return {
        "ok": True,
        "permission_allowed": opencode_permission_allowed(probe),
        "config_path": str(config_path) if config_path is not None else "",
    }


def setup_opencode_permission() -> dict:
    """Set OpenCode permission to 'allow' in config file.

    Detection priority (aligned with _load_opencode_user_config):
    1. ~/.config/opencode/opencode.json - if exists and valid JSON/JSONC, update it
    2. ~/.opencode/opencode.json - if exists and valid JSON/JSONC, update it
    3. Create new file at ~/.config/opencode/opencode.json (XDG standard)

    Mirrors _load_opencode_user_config behavior: skips invalid files and tries next.
    If config files exist but none can be parsed, returns an error instead of
    overwriting the existing file contents.

    Returns:
        {"ok": bool, "message": str, "config_path": str}
    """
    config_paths = get_opencode_config_paths(Path.home())
    probe = load_first_opencode_user_config(home=Path.home(), logger_instance=logger)

    if probe.config is not None and probe.path is not None:
        if opencode_permission_allowed(probe):
            return {
                "ok": True,
                "message": "Permission already set",
                "config_path": str(probe.path),
            }

        try:
            original_content = probe.content
            if original_content is None:
                original_content = probe.path.read_text(encoding="utf-8")

            updated_content = set_jsonc_top_level_string_property(original_content, "permission", "allow")
            probe.path.write_text(updated_content, encoding="utf-8")
            return {
                "ok": True,
                "message": "Permission set to 'allow'",
                "config_path": str(probe.path),
            }
        except Exception as e:
            logger.error(f"Failed to update OpenCode config at {probe.path}: {e}")
            return {"ok": False, "message": str(e), "config_path": str(probe.path)}

    if probe.existing_paths:
        error_path, error_message = (
            probe.errors[0] if probe.errors else (probe.existing_paths[0], "unknown parse error")
        )
        logger.error(f"Refusing to overwrite invalid OpenCode config at {error_path}: {error_message}")
        return {
            "ok": False,
            "message": f"Existing OpenCode config could not be parsed: {error_message}. File left unchanged.",
            "config_path": str(error_path),
        }

    # No existing valid config found, create at XDG path (first in list)
    config_path = config_paths[0]
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"permission": "allow"}, indent=2) + "\n", encoding="utf-8")
        return {
            "ok": True,
            "message": "Permission set to 'allow'",
            "config_path": str(config_path),
        }
    except Exception as e:
        logger.error(f"Failed to create OpenCode config: {e}")
        return {"ok": False, "message": str(e), "config_path": str(config_path)}


def parse_claude_agent_file(agent_path: str) -> Optional[dict]:
    """Parse a Claude agent markdown file and extract metadata.

    Agent files have YAML frontmatter and a markdown body:
    ---
    name: agent-name
    description: When to invoke this agent
    tools: Read, Bash, Edit  # Optional
    model: sonnet  # Optional: sonnet, opus, haiku, inherit
    ---
    System prompt content here...

    Returns:
        {
            "name": str,
            "description": str,
            "prompt": str,       # The markdown body (system prompt)
            "tools": list[str],  # Optional
            "model": str,        # Optional
        }
        or None on parse failure
    """
    try:
        content = Path(agent_path).read_text(encoding="utf-8")

        # Check for YAML frontmatter
        if not content.startswith("---"):
            # No frontmatter, use entire content as prompt
            return {
                "name": Path(agent_path).stem,
                "description": f"Agent from {Path(agent_path).name}",
                "prompt": content.strip(),
                "tools": None,
                "model": None,
            }

        # Find the closing ---
        lines = content.split("\n")
        end_idx = -1
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = i
                break

        if end_idx == -1:
            # Malformed frontmatter, use entire content
            return {
                "name": Path(agent_path).stem,
                "description": f"Agent from {Path(agent_path).name}",
                "prompt": content.strip(),
                "tools": None,
                "model": None,
            }

        # Parse YAML frontmatter
        frontmatter_lines = lines[1:end_idx]
        frontmatter_text = "\n".join(frontmatter_lines)

        # Use yaml.safe_load for proper YAML parsing (handles lists, etc.)
        metadata: dict = {}
        try:
            import yaml

            parsed = yaml.safe_load(frontmatter_text)
            if isinstance(parsed, dict):
                metadata = parsed
        except Exception as yaml_err:
            logger.debug(f"YAML parse failed, falling back to simple parsing: {yaml_err}")
            # Fallback to simple key: value parsing
            for line in frontmatter_lines:
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if key and value:
                        metadata[key] = value

        # Extract body (system prompt)
        body_lines = lines[end_idx + 1 :]
        body = "\n".join(body_lines).strip()

        # Parse tools if present
        tools = None
        if "tools" in metadata:
            tools_val = metadata["tools"]
            if isinstance(tools_val, list):
                # YAML list format: tools:\n  - Read\n  - Bash
                tools = [str(t).strip() for t in tools_val if t]
            elif isinstance(tools_val, str):
                # Inline format: tools: Read, Bash, Edit
                if "," in tools_val:
                    tools = [t.strip() for t in tools_val.split(",") if t.strip()]
                else:
                    tools = [t.strip() for t in tools_val.split() if t.strip()]

        return {
            "name": metadata.get("name", Path(agent_path).stem),
            "description": metadata.get("description", f"Agent from {Path(agent_path).name}"),
            "prompt": body,
            "tools": tools,
            "model": metadata.get("model"),
        }
    except Exception as e:
        logger.warning(f"Failed to parse agent file {agent_path}: {e}")
        return None


def claude_agents(cwd: Optional[str] = None) -> dict:
    """List available Claude Code agents (global + project).

    Claude supports both:
    - Global agents: ~/.claude/agents/*.md
    - Project agents: <cwd>/.claude/agents/*.md (if cwd provided)

    Returns:
        {
            "ok": True,
            "agents": [
                {"id": "reviewer", "name": "reviewer", "path": "/path/to/reviewer.md"},
                ...
            ]
        }
        or {"ok": False, "error": str} on failure
    """
    global_dir = Path.home() / ".claude" / "agents"
    project_dir: Optional[Path] = None
    if cwd:
        try:
            project_dir = Path(cwd).expanduser().resolve() / ".claude" / "agents"
        except Exception:
            project_dir = None

    def _scan_agents(directory: Path, source: str) -> dict[str, dict]:
        if not directory.exists():
            return {}
        if not directory.is_dir():
            return {}
        found: dict[str, dict] = {}
        for agent_file in sorted(directory.glob("*.md")):
            if not agent_file.is_file():
                continue
            agent_id = agent_file.stem
            found[agent_id] = {
                "id": agent_id,
                "name": agent_id,
                "path": str(agent_file),
                "source": source,
            }
        return found

    try:
        # Project overrides global on name collision.
        merged = _scan_agents(global_dir, "global")
        if project_dir is not None:
            merged.update(_scan_agents(project_dir, "project"))
        agents = list(merged.values())
        agents.sort(key=lambda x: (0 if x.get("source") == "project" else 1, x.get("id", "")))
        return {"ok": True, "agents": agents}
    except Exception as e:
        logger.error(f"Failed to scan Claude agents directory: {e}")
        return {"ok": False, "error": str(e)}


def codex_agents(cwd: Optional[str] = None) -> dict:
    """List available Codex custom agents (global + project)."""
    try:
        project_root: Optional[Path] = None
        if cwd:
            try:
                project_root = Path(cwd).expanduser().resolve()
            except Exception:
                project_root = None

        definitions = list_codex_subagents(project_root=project_root)
        agents = [
            {
                "id": definition.name,
                "name": definition.name,
                "path": str(definition.path) if definition.path else "",
                "source": definition.source,
                "description": definition.description,
            }
            for definition in definitions.values()
        ]
        agents.sort(key=lambda item: (0 if item.get("source") == "project" else 1, item.get("id", "")))
        return {"ok": True, "agents": agents}
    except Exception as e:
        logger.error("Failed to scan Codex agents directory: %s", e)
        return {"ok": False, "error": str(e)}


def claude_models() -> dict:
    """Best-effort merged list of Claude Code model options.

    Claude Code does not expose a stable `list models` CLI subcommand.
    We merge suggestions from:
    - The repository-owned Claude model catalog
    - ~/.claude/settings.json model/env values
    """

    def _append_unique(options: list[str], seen: set[str], value: object) -> None:
        if not isinstance(value, str):
            return
        model = value.strip()
        if not model or model in seen:
            return
        seen.add(model)
        options.append(model)

    options: list[str] = []
    seen: set[str] = set()

    for model in load_catalog_models():
        _append_unique(options, seen, model)

    for model in DEFAULT_CLAUDE_MODEL_ALIASES:
        _append_unique(options, seen, model)

    settings_path = Path.home() / ".claude" / "settings.json"
    try:
        if settings_path.exists() and settings_path.is_file():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _append_unique(options, seen, data.get("model"))
                env = data.get("env")
                if isinstance(env, dict):
                    for key in (
                        "ANTHROPIC_MODEL",
                        "ANTHROPIC_SMALL_FAST_MODEL",
                    ):
                        _append_unique(options, seen, env.get(key))
    except Exception as exc:
        logger.warning("Failed to read Claude settings.json: %s", exc, exc_info=True)

    from modules.agents.opencode.utils import build_claude_reasoning_options, format_claude_model_label

    reasoning_options = {"": build_claude_reasoning_options(None)}
    model_labels = {}
    for model in options:
        reasoning_options[model] = build_claude_reasoning_options(model)
        label = format_claude_model_label(model)
        if label != model:
            model_labels[model] = label
    return {
        "ok": True,
        "models": options,
        "reasoning_options": reasoning_options,
        "model_labels": model_labels,
    }


def _effort_values(reasoning_entries: object) -> list[str]:
    """Extract settable reasoning-effort values, dropping the UI ``__default__`` sentinel."""
    out: list[str] = []
    if not isinstance(reasoning_entries, list):
        return out
    for entry in reasoning_entries:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value and value != "__default__" and value not in out:
            out.append(value)
    return out


def _backend_default_model(config: Optional[V2Config], backend: str) -> Optional[str]:
    if config is None:
        return None
    agents = getattr(config, "agents", None)
    backend_cfg = getattr(agents, backend, None) if agents is not None else None
    value = getattr(backend_cfg, "default_model", None) if backend_cfg is not None else None
    return value or None


def _flat_catalog_models(catalog: dict, default_model: Optional[str]) -> list[dict]:
    """Shape claude_models()/codex_models() output into the unified models list."""
    reasoning_map = catalog.get("reasoning_options") or {}
    models: list[dict] = []
    for model_id in catalog.get("models") or []:
        if not isinstance(model_id, str) or not model_id:
            continue
        models.append(
            {
                "value": model_id,
                "label": (catalog.get("model_labels") or {}).get(model_id, model_id),
                "default": bool(default_model) and model_id == default_model,
                "reasoning_efforts": _effort_values(reasoning_map.get(model_id)),
            }
        )
    return models


def _opencode_model_options(
    *,
    provider: Optional[str],
    cwd: Optional[str],
    config: Optional[V2Config],
) -> dict:
    """OpenCode model options, including custom providers and user-added models.

    Reuses ``opencode_options`` whose async path already overlays custom
    providers (``_read_opencode_custom_provider_ids``) and user models
    (``_merge_opencode_user_models``), so this only normalizes the shape and
    applies the optional ``provider`` filter.
    """

    resolved_cwd = cwd
    if not resolved_cwd:
        runtime_cfg = getattr(config, "runtime", None) if config is not None else None
        resolved_cwd = getattr(runtime_cfg, "default_cwd", None) if runtime_cfg is not None else None
    if not resolved_cwd:
        resolved_cwd = "."

    raw = opencode_options(resolved_cwd)
    if not raw.get("ok"):
        return {"ok": False, "backend": "opencode", "error": raw.get("error") or "opencode options unavailable"}

    data = raw.get("data") or {}
    models_block = data.get("models") if isinstance(data.get("models"), dict) else {}
    reasoning_map = data.get("reasoning_options") or {}
    default_block = models_block.get("default") if isinstance(models_block.get("default"), dict) else {}
    providers_raw = models_block.get("providers") if isinstance(models_block.get("providers"), list) else []

    try:
        from vibe.opencode_config import _is_vibe_user_model as _oc_is_user_model
        from vibe.opencode_config import read_opencode_custom_providers

        custom_ids = set(read_opencode_custom_providers().keys())
    except Exception:
        _oc_is_user_model = None
        custom_ids = set()

    provider_filter = (provider or "").strip().lower() or None
    providers_out: list[dict] = []
    models_out: list[dict] = []
    for prov in providers_raw:
        if not isinstance(prov, dict):
            continue
        pid = prov.get("id") or prov.get("provider_id") or prov.get("name")
        if not isinstance(pid, str) or not pid:
            continue
        if provider_filter and pid.lower() != provider_filter:
            continue
        providers_out.append({"id": pid, "name": prov.get("name") or pid, "custom": pid in custom_ids})
        prov_models = prov.get("models")
        if isinstance(prov_models, dict):
            model_items = list(prov_models.items())
        elif isinstance(prov_models, list):
            model_items = [(m.get("id"), m) for m in prov_models if isinstance(m, dict) and m.get("id")]
        else:
            model_items = []
        for model_id, model_info in model_items:
            if not isinstance(model_id, str) or not model_id:
                continue
            value = f"{pid}/{model_id}"
            source = "catalog"
            if _oc_is_user_model is not None and isinstance(model_info, dict) and _oc_is_user_model(model_id, model_info):
                source = "user"
            models_out.append(
                {
                    "value": value,
                    "provider": pid,
                    "default": default_block.get(pid) == model_id,
                    "source": source,
                    "reasoning_efforts": _effort_values(reasoning_map.get(value)),
                }
            )

    opencode_cfg = getattr(getattr(config, "agents", None), "opencode", None) if config is not None else None
    default_provider = getattr(opencode_cfg, "default_provider", None) if opencode_cfg is not None else None

    notes: list[str] = []
    if provider_filter and not providers_out:
        notes.append(f"no configured OpenCode provider matches '{provider}'")
    return {
        "ok": True,
        "backend": "opencode",
        "default_model": _backend_default_model(config, "opencode"),
        "default_provider": default_provider or None,
        "providers": providers_out,
        "models": models_out,
        "source": "opencode server (live) + user config overlay",
        "live": True,
        "notes": notes or None,
    }


def agent_model_options(
    backend: str,
    *,
    provider: Optional[str] = None,
    cwd: Optional[str] = None,
) -> dict:
    """Backend-agnostic available models + per-model reasoning efforts.

    Single entry point wrapping ``claude_models`` / ``codex_models`` /
    ``opencode_options`` into one shape so the CLI and the Web UI can share it::

        {ok, backend, default_model, models: [{value, default, reasoning_efforts,
         provider?, source?}], providers?: [{id, name, custom}], source, live, notes}

    ``provider`` filters OpenCode results (ignored for other backends). Narrowing
    to a single model is a presentation concern left to the caller.
    """

    normalized_backend = (backend or "").strip().lower()
    if normalized_backend not in ("claude", "codex", "opencode"):
        return {"ok": False, "error": f"unknown backend '{backend}'", "backend": normalized_backend}

    try:
        config: Optional[V2Config] = V2Config.load()
    except Exception:
        config = None

    if normalized_backend == "claude":
        data = claude_models()
        if not data.get("ok"):
            return {"ok": False, "backend": normalized_backend, "error": data.get("error") or "claude model lookup failed"}
        default_model = _backend_default_model(config, "claude")
        result = {
            "ok": True,
            "backend": "claude",
            "default_model": default_model,
            "models": _flat_catalog_models(data, default_model),
            "source": "claude model catalog + ~/.claude/settings.json",
            "live": False,
            "notes": ["Claude Code has no stable list-models command; this is a best-effort merged list."],
        }
    elif normalized_backend == "codex":
        data = codex_models()
        if not data.get("ok"):
            return {"ok": False, "backend": normalized_backend, "error": data.get("error") or "codex model lookup failed"}
        default_model = _backend_default_model(config, "codex")
        result = {
            "ok": True,
            "backend": "codex",
            "default_model": default_model,
            "models": _flat_catalog_models(data, default_model),
            "source": "codex built-in list + ~/.codex caches",
            "live": False,
            "notes": ["Codex CLI has no stable list-models command; this is a best-effort merged list."],
        }
    else:
        result = _opencode_model_options(provider=provider, cwd=cwd, config=config)

    return result


_AGENT_INSTALL_JOB_LOCK = threading.Lock()
_AGENT_INSTALL_JOBS: dict[str, dict] = {}
_AGENT_INSTALL_LATEST_BY_BACKEND: dict[str, str] = {}
_AGENT_INSTALL_JOB_TTL_SECONDS = 3600.0


def _prune_agent_install_jobs(now: float | None = None) -> None:
    timestamp = now or time.time()
    stale = [
        job_id
        for job_id, job in _AGENT_INSTALL_JOBS.items()
        if job.get("finished_at") and timestamp - float(job.get("finished_at") or 0) > _AGENT_INSTALL_JOB_TTL_SECONDS
    ]
    for job_id in stale:
        job = _AGENT_INSTALL_JOBS.pop(job_id, None)
        backend = job.get("backend") if isinstance(job, dict) else None
        if isinstance(backend, str) and _AGENT_INSTALL_LATEST_BY_BACKEND.get(backend) == job_id:
            _AGENT_INSTALL_LATEST_BY_BACKEND.pop(backend, None)


def _agent_install_job_succeeded(result: dict, name: str) -> bool:
    if not bool(result.get("ok")):
        return False
    if name == "claude" or not supports_runtime_refresh(name):
        return True
    restart = result.get("restart")
    return isinstance(restart, dict) and bool(restart.get("ok"))


def start_agent_install_job(name: str) -> dict:
    """Start backend CLI install/upgrade in a background job.

    The UI request path must not run package-manager subprocesses directly:
    npm/curl/brew can hang or take minutes, and backend CLI failures should
    not affect the avibe main service. The worker still uses the same
    install/upgrade implementation as the CLI card used before; only the
    execution boundary changes.
    """
    if not is_agent_backend(name):
        return {"ok": False, "message": f"Unknown agent: {name}"}

    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        "ok": True,
        "job_id": job_id,
        "backend": name,
        "status": "running",
        "message": "Upgrade started",
        "output": "",
        "path": None,
        "started_at": now,
        "finished_at": None,
    }
    with _AGENT_INSTALL_JOB_LOCK:
        _prune_agent_install_jobs(now)
        latest_job_id = _AGENT_INSTALL_LATEST_BY_BACKEND.get(name)
        latest_job = _AGENT_INSTALL_JOBS.get(latest_job_id or "")
        if isinstance(latest_job, dict) and latest_job.get("status") == "running":
            return dict(latest_job)
        _AGENT_INSTALL_JOBS[job_id] = job
        _AGENT_INSTALL_LATEST_BY_BACKEND[name] = job_id

    def _worker() -> None:
        try:
            result = install_agent(name)
            if result.get("ok") and name != "claude" and supports_runtime_refresh(name):
                try:
                    result["restart"] = restart_backend(
                        name,
                        metadata={"reason": "agent_install_job", "source": "ui_api"},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Backend refresh after %s install job failed: %s",
                        name,
                        exc,
                    )
                    result["restart"] = {"ok": False, "message": str(exc)}
            ok = _agent_install_job_succeeded(result, name)
            status = "succeeded" if ok else "failed"
            if not ok:
                restart = result.get("restart")
                if isinstance(restart, dict):
                    restart_message = restart.get("message")
                    if isinstance(restart_message, str) and restart_message.strip():
                        result["message"] = restart_message.strip()
            with _AGENT_INSTALL_JOB_LOCK:
                current = _AGENT_INSTALL_JOBS.get(job_id)
                if current is not None:
                    current.update(result)
                    current["ok"] = ok
                    current["status"] = status
                    current["finished_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent install job failed for %s: %s", name, exc, exc_info=True)
            with _AGENT_INSTALL_JOB_LOCK:
                current = _AGENT_INSTALL_JOBS.get(job_id)
                if current is not None:
                    current.update(
                        {
                            "ok": False,
                            "status": "failed",
                            "message": str(exc),
                            "output": None,
                            "finished_at": time.time(),
                        }
                    )

    threading.Thread(
        target=_worker,
        daemon=True,
        name=f"vibe-agent-install-{name}-{job_id[:8]}",
    ).start()
    return dict(job)


def get_agent_install_job(job_id: str | None = None, *, backend: str | None = None) -> dict:
    """Return the latest state for a background backend install job."""
    with _AGENT_INSTALL_JOB_LOCK:
        _prune_agent_install_jobs()
        resolved_job_id = (job_id or "").strip()
        if not resolved_job_id and backend:
            resolved_job_id = _AGENT_INSTALL_LATEST_BY_BACKEND.get(backend.strip()) or ""
        job = _AGENT_INSTALL_JOBS.get(resolved_job_id)
        if not job:
            return {"ok": False, "error": "job_not_found"}
        return dict(job)


def install_agent(name: str) -> dict:
    """Install (or upgrade) an agent CLI tool.

    Upgrade path (binary already on disk): keep the user's install method
    stable. Without this, our previous flow bricked Claude installs whenever
    the user's bootstrap method differed from our hard-coded installer URL —
    e.g. the Dockerfile bootstraps via ``npm install -g
    @anthropic-ai/claude-code`` but our upgrade ran ``curl
    https://claude.ai/install.sh | bash``, which migrated the binary to
    ``~/.local/bin/claude`` and left V2Config pointing at the now-empty
    ``/usr/local/bin/claude``.

    Upgrade commands:
      - ``claude update``   — auto-detects npm-global vs native install
      - ``opencode upgrade`` — auto-detects curl/npm/pnpm/bun/brew/choco/scoop
      - Codex npm installs use ``npm install -g @openai/codex`` with a
        user-owned npm prefix.
      - Codex Homebrew installs use ``brew upgrade --cask codex``.
      - Unknown Codex installs fall back to ``codex update``.

    Fresh-install path (binary missing) keeps the bootstrap commands
    below — the user has no install yet, so we have no install method
    to defer to.

    Returns:
        {"ok": bool, "message": str, "output": str | None, "path": str | None}
    """
    import platform

    system = platform.system().lower()

    # Max output size to prevent UI slowdown (last N characters)
    MAX_OUTPUT_CHARS = 8192

    def _check_binary(binary: str) -> str | None:
        """Check if a binary exists in PATH. Returns error message if not found."""
        if resolve_cli_path(binary) is None:
            return f"{binary} is required but not found. Please install it first."
        return None

    def _truncate_output(output: str) -> str:
        """Truncate output to last MAX_OUTPUT_CHARS characters."""
        if len(output) <= MAX_OUTPUT_CHARS:
            return output
        return "...(truncated)\n" + output[-MAX_OUTPUT_CHARS:]

    # Upgrade branch: if the binary is already on disk, keep the install
    # source stable. Some CLIs own a reliable self-update command; Codex has
    # multiple install sources, so choose npm/brew/self-update by source.
    existing_path = resolve_cli_path(name)
    if existing_path:
        if name == "claude":
            cmd = [existing_path, "update"]
            command_env = None
        elif name == "opencode":
            # ``opencode upgrade`` auto-detects the install method; we
            # don't pass ``--method`` so the user's bootstrap choice
            # wins (curl on our Dockerfile, brew/npm/bun on user machines).
            cmd = [existing_path, "upgrade"]
            command_env = None
        elif name == "codex":
            upgrade = _codex_upgrade_command(existing_path)
            if isinstance(upgrade, dict):
                return upgrade
            cmd, command_env = upgrade
        else:
            cmd = None
        if cmd is not None:
            return _run_install_command(name, cmd, _truncate_output, mode="upgrade", env=command_env)

    command_env: dict[str, str] | None = None

    if name == "opencode":
        # OpenCode: use curl installer (not supported on Windows)
        if system == "windows":
            return {
                "ok": False,
                "message": "OpenCode installer is not supported on Windows. Please use the manual installation method.",
                "output": None,
            }
        # Check prerequisites
        for binary in ["curl", "bash"]:
            error = _check_binary(binary)
            if error:
                return {"ok": False, "message": error, "output": None}
        # Use pipefail to ensure curl failures are detected
        cmd = ["bash", "-c", "set -euo pipefail; curl -fsSL https://opencode.ai/install | bash"]
    elif name == "claude":
        # Claude Code: platform-specific installer
        if system == "windows":
            # Windows: use PowerShell with error handling
            error = _check_binary("powershell")
            if error:
                return {"ok": False, "message": error, "output": None}
            cmd = ["powershell", "-NoProfile", "-Command", "irm https://claude.ai/install.ps1 -ErrorAction Stop | iex"]
        else:
            # macOS/Linux: use bash with pipefail
            for binary in ["curl", "bash"]:
                error = _check_binary(binary)
                if error:
                    return {"ok": False, "message": error, "output": None}
            cmd = ["bash", "-c", "set -euo pipefail; curl -fsSL https://claude.ai/install.sh | bash"]
    elif name == "codex":
        # Fresh installs use npm because it is the documented cross-platform
        # package source that does not require OS package-manager privileges.
        npm_path = resolve_cli_path("npm")
        if npm_path:
            cmd = [npm_path, "install", "-g", "@openai/codex"]
            command_env = _codex_npm_install_env(npm_path)
        else:
            return {
                "ok": False,
                "message": "npm not found. Please install Node.js first.",
                "output": None,
            }
    else:
        return {"ok": False, "message": f"Unknown agent: {name}", "output": None}

    return _run_install_command(name, cmd, _truncate_output, mode="install", env=command_env)


def _run_install_command(
    name: str,
    cmd: list[str],
    truncate_output,
    *,
    mode: str = "install",
    env: dict[str, str] | None = None,
) -> dict:
    """Shared subprocess + post-success bookkeeping for install / upgrade.

    Factored out so the self-update branch (existing binary) and the
    fresh-install branch (curl/npm bootstrap) share identical post-run
    handling: log the result, invalidate the version cache, capture the
    new install path, and persist it to V2Config when it changed.
    """
    label = "Upgrading" if mode == "upgrade" else "Installing"
    try:
        logger.info("%s agent %s with command: %s", label, name, cmd)
        command_env = env or _command_env_for(cmd[0] if cmd and os.path.isabs(cmd[0]) else None)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=command_env,
            **isolated_subprocess_kwargs(),
        )
        try:
            stdout, stderr = process.communicate(timeout=300)  # 5 minute timeout
        except subprocess.TimeoutExpired:
            logger.error("Agent %s %s timed out", name, mode)
            signal_process_tree(process, KILL_SIGNAL, logger, f"{name} {mode}")
            stdout, stderr = process.communicate(timeout=10)
            output = (stdout or "") + ("\n" + stderr if stderr else "")
            output = truncate_output(output.strip())
            return {"ok": False, "message": f"{mode.capitalize()} timed out", "output": output or None}
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
        output = result.stdout + ("\n" + result.stderr if result.stderr else "")
        output = truncate_output(output.strip())
        if result.returncode == 0:
            installed_path = resolve_cli_path(name)
            if installed_path:
                logger.info("Agent %s %s succeeded at %s", name, mode, installed_path)
            else:
                logger.warning(
                    "Agent %s %s command succeeded but CLI path was not detected",
                    name,
                    mode,
                )
            # The chip refreshes runtime immediately after upgrade; drop the
            # 30s version cache so it reads the new `--version` instead of the
            # pre-upgrade value.
            _invalidate_version_cache(name)

            # Persist real Agent backend CLI paths to V2Config so the next
            # ``get_backend_runtime`` reads them directly instead of relying
            # on the resolver's stale-path fallback. Local dependencies such
            # as askill reuse this runner but do not have ``agents.<name>``
            # config entries, so they must not touch V2Config bookkeeping.
            if installed_path and is_agent_backend(name):
                try:
                    with CONFIG_LOCK:
                        try:
                            cfg = load_config()
                        except FileNotFoundError:
                            logger.debug(
                                "install_agent: config is not initialized; skipping cli_path persistence for %s",
                                name,
                            )
                        else:
                            target = getattr(getattr(cfg, "agents", None), name, None)
                            if target is not None:
                                previous = getattr(target, "cli_path", "") or ""
                                if previous != installed_path:
                                    target.cli_path = installed_path
                                    cfg.save()
                                    logger.info(
                                        "install_agent: updated V2Config cli_path for %s: %s -> %s",
                                        name,
                                        previous or "<unset>",
                                        installed_path,
                                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "install_agent: failed to persist cli_path for %s: %s",
                        name,
                        exc,
                    )

            return {
                "ok": True,
                "message": f"{name} {mode}d successfully" if mode == "install" else f"{name} upgraded successfully",
                "path": installed_path,
                "output": output,
            }
        logger.warning("Agent %s %s failed: %s", name, mode, output)
        return {
            "ok": False,
            "message": f"{mode.capitalize()} failed (exit code {result.returncode})",
            "output": output,
        }
    except Exception as e:
        logger.error("Agent %s %s error: %s", name, mode, e)
        return {"ok": False, "message": str(e), "output": None}


# =============================================================================
# Local dependencies (askill / avault) — required tools avibe installs for the user
# =============================================================================


_ASKILL_INSTALL_LOCK = threading.Lock()
_AVAULT_INSTALL_LOCK = threading.Lock()
_AVAULT_AGENT_MANAGER_LOCK = threading.Lock()
_AVAULT_AGENT_MANAGER = None
AVAULT_P2_MIN_VERSION = "0.1.3"
# Installer pin must reference a published manifest-pinned release. It may lag
# the P2 surface; standard sealing remains usable while P2-only entry points
# gate on AVAULT_P2_MIN_VERSION below.
AVAULT_VERSION = "0.1.3"
_AVAULT_RELEASE_BASE_URL = f"https://github.com/avibe-bot/avault/releases/download/v{AVAULT_VERSION}/"


def _truncate_install_output(output: str, limit: int = 8192) -> str:
    return output if len(output) <= limit else "...(truncated)\n" + output[-limit:]


def _managed_avault_release_satisfies_p2() -> bool:
    return _version_at_least(AVAULT_VERSION, AVAULT_P2_MIN_VERSION)


def _avault_p2_release_unavailable_result(*, existing: str | None = None, existing_version: str | None = None) -> dict:
    return {
        "ok": False,
        "installed": bool(existing),
        "changed": False,
        "path": existing,
        "version": existing_version,
        "status": "upgrade_required",
        "reason": "avault_p2_release_unavailable",
        "message": backend_t(
            "dependencies.avault.p2ReleaseUnavailable",
            pinned=AVAULT_VERSION,
            required=AVAULT_P2_MIN_VERSION,
        ),
    }


def install_askill() -> dict:
    """Install (or refresh) the askill CLI — a required local dependency for Skills.

    Uses the official one-line installer (same shape as the OpenCode bootstrap
    in ``install_agent``). Runs through ``_run_install_command``, whose
    V2Config cli_path bookkeeping is limited to real Agent backends, so it is
    safe for a standalone local dependency.
    """
    import platform

    system = platform.system().lower()
    if system != "windows" and resolve_cli_path("curl") and resolve_cli_path("bash"):
        cmd = ["bash", "-c", "set -euo pipefail; curl -fsSL https://askill.sh | sh"]
        return _run_install_command("askill", cmd, _truncate_install_output, mode="install")
    # No npm fallback: askill is distributed via the askill.sh installer, not a
    # public npm package, so a curl/bash-less host (e.g. Windows) must install
    # it manually rather than hit a guaranteed-failing `npm i -g`.
    return {
        "ok": False,
        "message": "askill auto-install needs curl + bash (macOS/Linux). Install it manually from https://askill.sh.",
        "output": None,
    }


def ensure_askill_installed(force: bool = False) -> dict:
    """Ensure askill is present. Idempotent — installs only when missing or forced.

    Reports success only when the binary is actually resolvable afterward: an
    installer can exit 0 while leaving the binary on a PATH this service does not
    inherit, and we must not claim "installed" while ``/api/skills`` still
    answers ``askill_not_found``.
    """
    if not _ASKILL_INSTALL_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "skipped": True,
            "reason": "askill_install_already_running",
            "message": "askill install or update is already running; try again shortly.",
        }
    try:
        existing = resolve_cli_path("askill")
        if existing and not force:
            return {"ok": True, "installed": True, "changed": False, "path": existing}
        result = install_askill()
        resolved = resolve_cli_path("askill")
        installed = bool(resolved)
        result["installed"] = installed
        result["changed"] = installed and bool(result.get("ok"))
        result["path"] = resolved
        if result.get("ok") and not installed:
            result["ok"] = False
            result["message"] = (
                result.get("message") or "askill installed but was not found on PATH; restart the service or check PATH."
            )
        return result
    finally:
        _ASKILL_INSTALL_LOCK.release()


def askill_status() -> dict:
    """Report whether askill is installed and its version (best-effort)."""
    path = resolve_cli_path("askill")
    if not path:
        return {"id": "askill", "installed": False, "version": None, "status": "missing", "path": None}
    version: str | None = None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_command_env_for(path),
            **isolated_subprocess_kwargs(),
        )
        text = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0 and text:
            version = text.split()[-1]
    except Exception:  # noqa: BLE001
        version = None
    return {"id": "askill", "installed": True, "version": version, "status": "ready", "path": path}


def _configured_avault_cli_path() -> str:
    try:
        return str(V2Config.load().agents.avault.cli_path or "avault")
    except Exception:  # noqa: BLE001
        return "avault"


def _resolve_avault_cli_path() -> str | None:
    configured = _configured_avault_cli_path()
    resolved = resolve_cli_path(configured)
    if resolved:
        return resolved
    if configured != "avault":
        return resolve_cli_path("avault")
    return None


def _avault_target() -> tuple[str | None, str]:
    import platform

    system = platform.system()
    machine = platform.machine()
    normalized_system = system.lower()
    normalized_machine = machine.lower()
    platform_label = f"{system or 'unknown'}-{machine or 'unknown'}"

    if normalized_system == "darwin" and normalized_machine == "arm64":
        return "macos-arm64", platform_label
    if normalized_system == "darwin" and normalized_machine in {"x86_64", "amd64"}:
        return "macos-x64", platform_label
    if normalized_system == "linux" and normalized_machine in {"x86_64", "amd64"}:
        return "linux-x64", platform_label
    if normalized_system == "linux" and normalized_machine in {"aarch64", "arm64"}:
        return "linux-arm64", platform_label
    if normalized_system == "windows" and normalized_machine in {"amd64", "x86_64"}:
        return "windows-x64", platform_label
    if normalized_system == "windows" and normalized_machine == "arm64":
        return "windows-arm64", platform_label
    return None, platform_label


def _avault_binary_name_for_target(target: str) -> str:
    return "avault.exe" if target.startswith("windows-") else "avault"


def _avault_managed_bin_path(target: str | None = None) -> Path:
    target = target or (_avault_target()[0] or "")
    # Windows uses the same Avibe-managed bin directory, with the .exe name
    # that _candidate_cli_paths("avault") checks on Windows.
    return Path.home() / ".local" / "bin" / _avault_binary_name_for_target(target)


def _persist_avault_cli_path(path: str) -> None:
    try:
        try:
            load_config()
        except FileNotFoundError:
            save_config({})
        with CONFIG_LOCK:
            cfg = load_config()
            previous = getattr(cfg.agents.avault, "cli_path", "") or ""
            if previous != path:
                cfg.agents.avault.cli_path = path
                cfg.save()
                logger.info(
                    "install_avault: updated V2Config cli_path: %s -> %s",
                    previous or "<unset>",
                    path,
                )
    except Exception as exc:
        logger.warning("install_avault: failed to persist cli_path: %s", exc)
        raise


def _download_avault_release_file(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "avibe/avault-installer",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - public GitHub release asset
        return response.read()


def _extract_avault_binary(archive_bytes: bytes, output_path: Path, member_name: str = "avault") -> None:
    import io
    import tarfile

    output_real = output_path.parent.resolve()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        member = next((item for item in archive.getmembers() if item.name == member_name), None)
        if member is None or not member.isfile():
            raise ValueError("archive does not contain the avault executable")
        target = (output_path.parent / member.name).resolve()
        if target.parent != output_real or target.name != member_name:
            raise ValueError("archive contains an unsafe avault path")
        source = archive.extractfile(member)
        if source is None:
            raise ValueError("archive does not contain readable avault data")
        with output_path.open("wb") as out:
            shutil.copyfileobj(source, out)


def _clear_macos_quarantine(path: Path) -> None:
    if os.sys.platform != "darwin":
        return
    try:
        os.removexattr(path, "com.apple.quarantine")
    except (AttributeError, FileNotFoundError, OSError):
        pass


def install_avault(force: bool = False) -> dict:
    """Install (or refresh) avault, the required local custody-core dependency.

    Downloads the Avibe-pinned public avault release manifest and tarball,
    verifies the manifest sha256, safely extracts the single ``avault`` member,
    and atomically installs it into Avibe's managed CLI location.
    """
    path = _resolve_avault_cli_path()
    if path and not force:
        existing_version = _probe_avault_version(path)
        return {
            "ok": True,
            "message": backend_t("dependencies.avault.ready"),
            "output": None,
            "path": path,
            "version": existing_version,
        }

    import hashlib
    import tempfile

    target, platform_label = _avault_target()
    if target is None:
        if path:
            existing_version = _probe_avault_version(path)
            return {
                "ok": True,
                "message": backend_t("dependencies.avault.ready"),
                "output": None,
                "path": path,
                "version": existing_version,
                "changed": False,
            }
        return {
            "ok": False,
            "message": backend_t("dependencies.avault.noBuild", platform=platform_label),
            "output": None,
            "path": None,
        }

    try:
        manifest_url = urllib.parse.urljoin(_AVAULT_RELEASE_BASE_URL, "manifest.json")
        manifest = json.loads(_download_avault_release_file(manifest_url).decode("utf-8"))
        entry = manifest["versions"][AVAULT_VERSION][target]
        asset = entry["asset"]
        expected_sha256 = str(entry["sha256"]).strip().lower()
        if not isinstance(asset, str) or not asset.endswith(".tar.gz") or "/" in asset:
            raise ValueError("manifest contains an invalid avault asset name")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("manifest contains an invalid avault sha256")

        archive_url = urllib.parse.urljoin(_AVAULT_RELEASE_BASE_URL, asset)
        archive_bytes = _download_avault_release_file(archive_url)
        actual_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        if actual_sha256 != expected_sha256:
            return {
                "ok": False,
                "message": backend_t("dependencies.avault.checksumMismatch"),
                "output": f"expected {expected_sha256}, got {actual_sha256}",
                "path": None,
            }

        member_name = _avault_binary_name_for_target(target)
        install_path = _avault_managed_bin_path(target)
        install_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="avault-install-", dir=install_path.parent) as tmp_dir:
            tmp_path = Path(tmp_dir) / member_name
            _extract_avault_binary(archive_bytes, tmp_path, member_name)
            if not target.startswith("windows-"):
                tmp_path.chmod(0o755)
            os.replace(tmp_path, install_path)
        if not target.startswith("windows-"):
            install_path.chmod(0o755)
            _clear_macos_quarantine(install_path)
        _persist_avault_cli_path(str(install_path))

        return {
            "ok": True,
            "message": backend_t("dependencies.avault.installed", version=AVAULT_VERSION),
            "output": f"Installed avault {AVAULT_VERSION} for {target}",
            "path": str(install_path),
            "changed": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("avault install failed: %s", exc, exc_info=True)
        return {
            "ok": False,
            "message": backend_t("dependencies.avault.installFailed", error=str(exc)),
            "output": None,
            "path": None,
        }


def ensure_avault_installed(force: bool = False) -> dict:
    """Ensure avault is present.

    The managed pin can be older than the P2 surface while still supporting the
    standard seal path. P2-only commands call ``_require_avault_p2_surface`` at
    their own boundary instead of making dependency install fail.
    """
    if not _AVAULT_INSTALL_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "skipped": True,
            "reason": "avault_install_already_running",
            "message": backend_t("dependencies.avault.alreadyRunning"),
        }
    try:
        existing = _resolve_avault_cli_path()
        existing_version = _probe_avault_version(existing) if existing else None
        existing_is_p2 = _version_at_least(existing_version, AVAULT_P2_MIN_VERSION)
        can_managed_install_p2 = _managed_avault_release_satisfies_p2()
        # Never downgrade: keep an existing binary that is strictly newer than the
        # managed pin, even under ``force``. ``force`` may still reinstall an equal
        # or older managed version to repair a binary, but it must not replace a
        # newer user/custom avault (e.g. 0.1.4) with the older managed release. A
        # genuinely broken binary can't report a version, so it isn't matched here
        # and still gets repaired below.
        existing_newer_than_pin = (
            existing_is_p2
            and _version_at_least(existing_version, AVAULT_VERSION)
            and not _version_at_least(AVAULT_VERSION, existing_version)
        )
        if existing and existing_newer_than_pin:
            return {
                "ok": True,
                "installed": True,
                "changed": False,
                "path": existing,
                "version": existing_version,
            }
        if existing and not existing_is_p2 and force and not can_managed_install_p2:
            return _avault_p2_release_unavailable_result(existing=existing, existing_version=existing_version)
        if existing and (
            (existing_is_p2 and (not force or not can_managed_install_p2))
            or (not existing_is_p2 and not force and not can_managed_install_p2)
        ):
            return {
                "ok": True,
                "installed": True,
                "changed": False,
                "path": existing,
                "version": existing_version,
            }
        needs_upgrade = bool(existing and not existing_is_p2 and can_managed_install_p2)
        result = install_avault(force=force or needs_upgrade)
        resolved = _resolve_avault_cli_path()
        installed = bool(resolved)
        resolved_version = _probe_avault_version(resolved) if resolved else None
        result["installed"] = installed
        result["version"] = resolved_version
        if "changed" not in result:
            result["changed"] = installed and bool(result.get("ok")) and (not existing or force or needs_upgrade)
        result["path"] = resolved
        if result.get("ok") and not installed:
            result["ok"] = False
            result["message"] = (
                result.get("message") or backend_t("dependencies.avault.installedNotFound")
            )
        elif result.get("ok") and not _version_at_least(resolved_version, AVAULT_P2_MIN_VERSION):
            result["status"] = "upgrade_required"
        return result
    finally:
        _AVAULT_INSTALL_LOCK.release()


def _probe_avault_version(path: str | None) -> str | None:
    if not path:
        return None
    try:
        proc = subprocess.run(
            [path, "version"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_command_env_for(path),
            **isolated_subprocess_kwargs(),
        )
        text = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0 and text:
            return text.split()[-1]
    except Exception:  # noqa: BLE001
        return None
    return None


def avault_status() -> dict:
    """Report whether avault is installed and its version (best-effort)."""
    path = _resolve_avault_cli_path()
    if not path:
        return {"id": "avault", "installed": False, "version": None, "status": "missing", "path": None}
    version = _probe_avault_version(path)
    status = "ready" if _version_at_least(version, AVAULT_P2_MIN_VERSION) else "upgrade_required"
    return {"id": "avault", "installed": True, "version": version, "status": status, "path": path}


def _version_at_least(current: str | None, minimum: str) -> bool:
    if not current:
        return False
    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(current) >= Version(minimum)
        except InvalidVersion:
            pass
    except Exception:  # pragma: no cover - packaging is a transitive dep
        pass

    def _parts(value: str) -> tuple[int, ...] | None:
        core = value.split("+", 1)[0].split("-", 1)[0]
        try:
            return tuple(int(part) for part in core.split("."))
        except ValueError:
            return None

    cur_parts = _parts(current)
    min_parts = _parts(minimum)
    if cur_parts is None or min_parts is None:
        return False
    width = max(len(cur_parts), len(min_parts))
    return cur_parts + (0,) * (width - len(cur_parts)) >= min_parts + (0,) * (width - len(min_parts))


def _require_avault_p2_surface(feature: str) -> None:
    status = avault_status()
    if not status.get("installed"):
        raise AvaultPreHandoffError(f"avault is required for {feature}")
    version = status.get("version")
    if not _version_at_least(version, AVAULT_P2_MIN_VERSION):
        detail = f"{feature} requires avault >= {AVAULT_P2_MIN_VERSION}; installed {version or 'unknown'}"
        if not _managed_avault_release_satisfies_p2():
            detail = f"{detail}; managed avault install is pinned to {AVAULT_VERSION}"
        raise AvaultPreHandoffError(detail)


# ---------------------------------------------------------------------------
# avault client — Avibe's only path to value cryptography.
#
# Every Vaults value operation (seal on create, deliver on use, key backup) is a
# one-shot ``avault`` subprocess. Plaintext/keys flow only INTO avault via stdin;
# avault returns ciphertext, a delivery exit code, an HTTP response, or a written
# file — never plaintext. The daemon never holds the master key and never decrypts.
# (Design: avibe docs/plans/avault-custody-core.md §18; verbs: avault/docs/DESIGN.md.)
# ---------------------------------------------------------------------------

# avibe's wait must outlast avault's own fetch timeout (10s connect + 30s total).
_AVAULT_TIMEOUT_SECONDS = 20.0
_AVAULT_FETCH_TIMEOUT_SECONDS = 60.0
class AvaultError(Exception):
    """An ``avault`` invocation failed. Messages never carry secret material —
    avault is designed to keep secrets out of its stdout/stderr and errors."""


class AvaultPreHandoffError(AvaultError):
    """avault failed before receiving an envelope or performing delivery."""


def _avault_detail(proc: "subprocess.CompletedProcess") -> str:
    """Best-effort, secret-free detail from a failed avault run (its stderr)."""
    raw = proc.stderr
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return (raw or "").strip()


def _require_avault_path() -> str:
    path = _resolve_avault_cli_path()
    if not path:
        raise AvaultPreHandoffError(backend_t("dependencies.avault.missing"))
    return path


def _avault_agent_client():
    return _avault_agent_manager().client()


def _avault_agent_manager():
    from vibe.avault_agent import AvaultAgentManager

    global _AVAULT_AGENT_MANAGER
    with _AVAULT_AGENT_MANAGER_LOCK:
        if _AVAULT_AGENT_MANAGER is None:
            _AVAULT_AGENT_MANAGER = AvaultAgentManager(
                binary_resolver=_require_avault_path,
                command_env=_command_env_for,
            )
        manager = _AVAULT_AGENT_MANAGER
    return manager


def _run_avault(
    args: list[str],
    *,
    stdin: bytes | None = None,
    timeout: float = _AVAULT_TIMEOUT_SECONDS,
) -> "subprocess.CompletedProcess":
    """Run a capturing one-shot avault command. Bulk blobs go via ``stdin``."""
    path = _require_avault_path()
    try:
        return subprocess.run(
            [path, *args],
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=_command_env_for(path),
            **isolated_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        raise AvaultPreHandoffError("avault binary not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise AvaultError("avault timed out") from exc


def _envelope_payload(sealed) -> dict:
    """Serialize a stored envelope for an avault stdin request (no plaintext)."""
    return {"ciphertext": sealed.ciphertext, "nonce": sealed.nonce, "wrap_meta": sealed.wrap_meta}


def avault_pubkey() -> dict:
    """Return avault's blind-box public key + fingerprint."""
    _require_avault_p2_surface("blind-box pubkey")
    proc = _run_avault(["pubkey"])
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault pubkey failed")
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise AvaultError("avault pubkey returned malformed output") from exc
    public_key = payload.get("public_key")
    fingerprint = payload.get("fingerprint")
    if not isinstance(public_key, str) or not isinstance(fingerprint, str) or not public_key or not fingerprint:
        raise AvaultError("avault pubkey returned malformed output")
    return {"public_key": public_key, "fingerprint": fingerprint}


def avault_agent_pubkey() -> dict:
    """Return the resident agent's ephemeral blind-box public key."""
    from vibe.avault_agent import AvaultAgentError

    _require_avault_p2_surface("resident agent pubkey")
    try:
        payload = _avault_agent_client().pubkey()
    except AvaultAgentError as exc:
        raise AvaultError(str(exc)) from exc
    public_key = payload.get("public_key")
    fingerprint = payload.get("fingerprint")
    if not isinstance(public_key, str) or not isinstance(fingerprint, str) or not public_key or not fingerprint:
        raise AvaultError("avault agent pubkey returned malformed output")
    return {"public_key": public_key, "fingerprint": fingerprint}


def avault_seal_blind_box(name_or_blind_box, blind_box: dict | None = None):
    """Relay a browser HPKE blind box to ``avault seal --blind-box``.

    ``avault`` requires the secret name for AAD binding. For convenience, callers may
    pass ``(name, blind_box)`` or a single object containing ``name`` and ``blind_box``.
    """
    from storage.vault_crypto import Sealed

    if blind_box is None:
        if not isinstance(name_or_blind_box, dict):
            raise AvaultError("blind-box request must be an object")
        request = name_or_blind_box
        name = str(request.get("name") or "")
        if isinstance(request.get("blind_box"), dict):
            blind_box = request["blind_box"]
        else:
            blind_box = {key: request[key] for key in ("scheme", "enc", "ct") if key in request}
    else:
        name = str(name_or_blind_box or "")
    if not name:
        raise AvaultError("secret name is required for blind-box seal")
    _require_avault_p2_surface("blind-box seal")
    body = json.dumps(blind_box).encode("utf-8")
    proc = _run_avault(["seal", "--name", name, "--blind-box"], stdin=body)
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault blind-box seal failed")
    try:
        payload = json.loads(proc.stdout)
        return Sealed(
            ciphertext=payload["ciphertext"],
            nonce=payload["nonce"],
            wrap_meta=payload["wrap_meta"],
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise AvaultError("avault blind-box seal returned malformed output") from exc


def avault_sign(
    key_envelope,
    digest: str,
    scheme: str,
    *,
    name: str | None = None,
) -> dict:
    """Ask avault to sign a caller-computed 32-byte digest with a local key envelope."""
    if not name:
        raise AvaultError("secret name is required for avault sign")
    _require_avault_p2_surface("vault signing")
    body = json.dumps(
        {
            "name": name,
            "key_envelope": _envelope_payload(key_envelope),
            "digest": digest,
            "scheme": scheme,
        }
    ).encode("utf-8")
    proc = _run_avault(["sign"], stdin=body)
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault sign failed")
    try:
        payload = json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise AvaultError("avault sign returned malformed output") from exc
    signature = payload.get("signature")
    if not isinstance(signature, str) or not signature:
        raise AvaultError("avault sign returned malformed output")
    return {"signature": signature, "recovery_id": payload.get("recovery_id")}


def avault_deliver_run(secrets: list[dict], command: list[str]) -> dict:
    """Run ``command`` with the secrets injected as env vars, inside avault.

    ``secrets`` is ``[{"name": <secret name>, "env": <env var>, "envelope": <Sealed>}]``.
    avault decrypts, spawns the child with that env, waits, and zeroizes — the
    plaintext never returns here. The child inherits this process's stdio so its
    output passes through; the run-secrets JSON (envelopes only, no plaintext)
    goes on avault's stdin to stay out of ``ps``. Returns the child's exit code
    (``128 + signal`` if signalled). Delivery is fail-closed: once avault returns
    an exit code, callers must treat the secret as handed to the child unless a
    future avault protocol provides a distinct pre-handoff failure signal.
    """
    path = _require_avault_path()
    payload = json.dumps(
        [
            {"name": s["name"], "env": s["env"], "envelope": _envelope_payload(s["envelope"])}
            for s in secrets
        ]
    ).encode("utf-8")
    try:
        proc = subprocess.Popen(
            [path, "deliver", "run", "--", *command],
            stdin=subprocess.PIPE,
            env=_command_env_for(path),
            **isolated_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        raise AvaultError("avault binary not found") from exc
    try:
        assert proc.stdin is not None
        proc.stdin.write(payload)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        # avault exited before reading stdin (e.g. bad request); fall through to wait()
        # which surfaces its exit code.
        pass
    exit_code = proc.wait()
    return {"exit_code": exit_code, "delivered": True}


def _agent_secret_payload(secret: dict, *, target_field: str) -> dict:
    return {
        "name": secret["name"],
        target_field: secret[target_field],
        "envelope": _envelope_payload(secret["envelope"]),
    }


def avault_agent_grant(
    *,
    scope_type: str,
    scope_ref: str,
    ttl_secs: int,
    deks: list[dict],
    expected_pubkey: dict | None = None,
) -> dict:
    """Cache browser-released protected DEKs in the resident agent."""
    from vibe.avault_agent import AvaultAgentError

    _require_avault_p2_surface("resident agent grant")
    validate_avault_agent_pubkey(expected_pubkey)
    try:
        return _avault_agent_client().grant(
            scope_type=scope_type,
            scope_ref=scope_ref,
            ttl_secs=ttl_secs,
            deks=deks,
        )
    except AvaultAgentError as exc:
        raise AvaultError(str(exc)) from exc


def validate_avault_agent_pubkey(expected_pubkey: dict | None) -> None:
    """Fail before DB approval if the browser sealed to a stale resident key."""
    if expected_pubkey is None:
        return
    current = avault_agent_pubkey()
    expected_fingerprint = expected_pubkey.get("fingerprint")
    if expected_fingerprint and expected_fingerprint != current.get("fingerprint"):
        raise AvaultError("avault agent pubkey fingerprint mismatch")
    expected_public_key = expected_pubkey.get("public_key")
    if expected_public_key and expected_public_key != current.get("public_key"):
        raise AvaultError("avault agent public key mismatch")


def avault_agent_release(*, scope_type: str, scope_ref: str) -> dict:
    """Drop a resident-agent protected grant if present."""
    from vibe.avault_agent import AvaultAgentClient, AvaultAgentError

    _require_avault_p2_surface("resident agent release")
    try:
        return AvaultAgentClient(_avault_agent_manager().socket_path, timeout=1.0).release(scope_type=scope_type, scope_ref=scope_ref)
    except AvaultAgentError as exc:
        raise AvaultError(str(exc)) from exc


def _agent_release_failure_is_absent(exc: AvaultError) -> bool:
    detail = str(exc).lower()
    return _avault_agent_error_is_absent(detail)


def _avault_agent_error_is_absent(detail: str) -> bool:
    text = detail.lower()
    return (
        "failed to connect to avault agent" in text
        and (
            "no such file" in text
            or "connection refused" in text
            or "errno 2" in text
            or "errno 61" in text
            or "errno 111" in text
        )
    )


def _quarantine_resident_agent_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(mode):
        path.unlink(missing_ok=True)


def _fail_closed_resident_agent_after_release_failure() -> None:
    from storage import vault_service

    manager = _avault_agent_manager()
    manager.reset()
    _quarantine_resident_agent_socket(manager.socket_path)
    engine = _vault_engine()
    with engine.begin() as conn:
        vault_service.expire_active_grants(
            conn,
            reason="grant-expired-agent-cache-reset",
        )


def _release_failed_grant_scope(scope_type: str, scope_ref: str, *, reason: str) -> None:
    try:
        release_vault_agent_scopes([{"scope_type": scope_type, "scope_ref": scope_ref}], reason=reason)
    except AvaultError:
        logger.warning(
            "%s: failed to clear resident agent scope after failed grant relay",
            reason,
            exc_info=True,
        )


def release_vault_agent_scopes(scopes: list[dict[str, str]], *, reason: str) -> None:
    seen: set[tuple[str, str]] = set()
    for scope in scopes:
        scope_type = str(scope.get("scope_type") or "")
        scope_ref = str(scope.get("scope_ref") or "")
        if not scope_type or not scope_ref:
            continue
        key = (scope_type, scope_ref)
        if key in seen:
            continue
        seen.add(key)
        try:
            avault_agent_release(scope_type=scope_type, scope_ref=scope_ref)
        except AvaultError as exc:
            if _agent_release_failure_is_absent(exc):
                logger.debug("%s: resident agent absent while releasing grant %s:%s", reason, scope_type, scope_ref)
                continue
            try:
                _fail_closed_resident_agent_after_release_failure()
            except OSError as reset_exc:
                raise AvaultError("failed to clear resident agent after release failure") from reset_exc
            logger.warning(
                "%s: release failed for resident agent grant %s:%s; reset resident agent cache",
                reason,
                scope_type,
                scope_ref,
                exc_info=True,
            )


def avault_agent_deliver_run(
    *,
    scope_type: str,
    scope_ref: str,
    secrets: list[dict],
    command: list[str],
) -> dict:
    """Run a child under a protected grant. Plaintext stays inside avault."""
    from vibe.avault_agent import AvaultAgentError

    _require_avault_p2_surface("resident agent deliver run")
    try:
        result = _avault_agent_manager().client(timeout=None).deliver_run(
            scope_type=scope_type,
            scope_ref=scope_ref,
            command=command,
            secrets=[_agent_secret_payload(secret, target_field="env") for secret in secrets],
        )
    except AvaultAgentError as exc:
        if _avault_agent_error_is_absent(str(exc)):
            raise AvaultPreHandoffError(str(exc)) from exc
        raise AvaultError(str(exc)) from exc
    try:
        exit_code = int(result["exit_code"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AvaultError("avault agent deliver run returned malformed output") from exc
    return {"exit_code": exit_code}


def avault_agent_deliver_fetch(
    *,
    scope_type: str,
    scope_ref: str,
    name: str,
    sealed,
    request: dict,
) -> dict:
    """Broker an HTTP request under a protected grant."""
    from vibe.avault_agent import AvaultAgentError

    _require_avault_p2_surface("resident agent deliver fetch")
    try:
        return _avault_agent_manager().client(timeout=_AVAULT_FETCH_TIMEOUT_SECONDS).deliver_fetch(
            scope_type=scope_type,
            scope_ref=scope_ref,
            name=name,
            envelope=_envelope_payload(sealed),
            request=request,
        )
    except AvaultAgentError as exc:
        if _avault_agent_error_is_absent(str(exc)):
            raise AvaultPreHandoffError(str(exc)) from exc
        raise AvaultError(str(exc)) from exc


def avault_agent_deliver_inject(
    *,
    scope_type: str,
    scope_ref: str,
    path: str,
    fmt: str,
    secrets: list[dict],
) -> None:
    """Render a protected-grant secret file inside avault."""
    from vibe.avault_agent import AvaultAgentError

    _require_avault_p2_surface("resident agent deliver inject")
    try:
        result = _avault_agent_client().deliver_inject(
            scope_type=scope_type,
            scope_ref=scope_ref,
            path=str(path),
            fmt=fmt,
            secrets=[_agent_secret_payload(secret, target_field="key") for secret in secrets],
        )
    except AvaultAgentError as exc:
        if _avault_agent_error_is_absent(str(exc)):
            raise AvaultPreHandoffError(str(exc)) from exc
        raise AvaultError(str(exc)) from exc
    if result.get("ok") is not True:
        raise AvaultError("avault agent inject returned malformed output")


def avault_deliver_fetch(name: str, sealed, request: dict) -> dict:
    """Broker an HTTP request inside avault with the secret injected at egress.

    ``request`` must include ``allowed_hosts`` (avault rejects the target otherwise).
    Returns ``{"status", "headers", "body"}`` — the response only, never the secret.
    """
    body = json.dumps(
        {"name": name, "envelope": _envelope_payload(sealed), "request": request}
    ).encode("utf-8")
    proc = _run_avault(["deliver", "fetch"], stdin=body, timeout=_AVAULT_FETCH_TIMEOUT_SECONDS)
    # avault exits 0 for 2xx and 1 for a non-2xx HTTP status; both still emit the
    # response JSON. Any higher code is an avault-level failure (no response).
    if proc.returncode not in (0, 1):
        raise AvaultError(_avault_detail(proc) or "avault fetch failed")
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise AvaultError("avault fetch returned malformed output") from exc


def avault_deliver_inject(path: str, fmt: str, secrets: list[dict]) -> None:
    """Render the secrets to a 0600 file at ``path`` (dotenv/json) inside avault.

    ``secrets`` is ``[{"name": <secret name>, "key": <file key>, "envelope": <Sealed>}]``.
    """
    body = json.dumps(
        {
            "path": str(path),
            "format": fmt,
            "secrets": [
                {"name": s["name"], "key": s["key"], "envelope": _envelope_payload(s["envelope"])}
                for s in secrets
            ],
        }
    ).encode("utf-8")
    proc = _run_avault(["deliver", "inject"], stdin=body)
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault inject failed")


def avault_key_export(passphrase: str) -> dict:
    """Export the machine key as a passphrase-wrapped backup blob via avault."""
    proc = _run_avault(["key", "export"], stdin=passphrase.encode("utf-8"))
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault key export failed")
    try:
        return json.loads(proc.stdout)
    except (ValueError, TypeError) as exc:
        raise AvaultError("avault key export returned malformed output") from exc


def avault_key_import(blob: dict, passphrase: str, *, force: bool = False) -> None:
    """Restore the machine key from an :func:`avault_key_export` blob via avault."""
    body = json.dumps({"passphrase": passphrase, "blob": blob}).encode("utf-8")
    args = ["key", "import"] + (["--force"] if force else [])
    proc = _run_avault(args, stdin=body)
    if proc.returncode != 0:
        raise AvaultError(_avault_detail(proc) or "avault key import failed")


def _askill_auto_update_disabled() -> bool:
    """Return whether the managed askill reconcile loop is explicitly disabled."""
    skip_value = os.environ.get(_ASKILL_SKIP_ENV, "").strip().lower()
    if skip_value in _TRUTHY_ENV_VALUES:
        return True
    value = os.environ.get(_ASKILL_AUTO_UPDATE_ENV, "").strip().lower()
    return value in _FALSY_ENV_VALUES


def _fetch_latest_askill_version() -> str | None:
    return _fetch_github_latest_release_version(_ASKILL_RELEASE_REPOSITORY, user_agent="avibe/askill-dependency")


def _cached_latest_askill() -> str | None:
    # Share the backend lifecycle cache so every local tool latest probe obeys the
    # same one-hour success TTL and short failure TTL.
    key = "askill"
    with _BACKEND_CACHE_LOCK:
        cached = _BACKEND_LATEST_CACHE.get(key)
    if cached:
        ttl = _BACKEND_LATEST_TTL_SECONDS if cached[1] else _BACKEND_LATEST_FAILURE_TTL_SECONDS
        if time.time() - cached[0] < ttl:
            return cached[1]
    latest = _fetch_latest_askill_version()
    with _BACKEND_CACHE_LOCK:
        _BACKEND_LATEST_CACHE[key] = (time.time(), latest)
    return latest


def askill_update_status(*, include_latest: bool = True) -> dict:
    """Return local askill version plus best-effort upstream update state."""
    current = askill_status()
    latest = _cached_latest_askill() if include_latest else None
    current_version = current.get("version") if current.get("installed") else None
    has_update = bool(include_latest and current.get("installed") and _compare_versions(current_version, latest))
    out = {
        **current,
        "latest_version": latest,
        "has_update": has_update,
        "auto_update": not _askill_auto_update_disabled(),
    }
    if current.get("installed") and current_version is None:
        out["status"] = "unknown"
    return out


def reconcile_askill_auto_update() -> dict:
    """Install or refresh askill as a required managed dependency.

    This runs from the shared update checker cadence but is intentionally
    independent of ``update.auto_update``. Avibe owns askill as a local runtime
    dependency for Skills; disabling product self-upgrades must not strand that
    dependency on an incompatible CLI contract. Operators can still disable this
    reconcile loop with ``VIBE_ASKILL_AUTO_UPDATE=0`` or ``VIBE_INSTALL_SKIP_ASKILL``.
    """
    if _askill_auto_update_disabled():
        return {"ok": True, "skipped": True, "reason": "askill_auto_update_disabled"}

    status = askill_update_status()
    if not status.get("installed"):
        result = ensure_askill_installed(force=False)
        result["action"] = "install"
        return result

    latest = status.get("latest_version")
    if latest is None:
        return {"ok": True, "skipped": True, "reason": "latest_unavailable", "status": status}

    if status.get("version") is None:
        logger.info("askill local version is unknown; refreshing managed dependency")
        result = ensure_askill_installed(force=True)
        result["action"] = "refresh_unknown_version"
        result["latest_version"] = latest
        return result

    if not status.get("has_update"):
        return {"ok": True, "skipped": True, "reason": "up_to_date", "status": status}

    logger.info("askill update available: %s -> %s", status.get("version"), latest)
    result = ensure_askill_installed(force=True)
    result["action"] = "update"
    result["from_version"] = status.get("version")
    result["latest_version"] = latest
    with _BACKEND_CACHE_LOCK:
        _BACKEND_LATEST_CACHE.pop("askill", None)
    return result


# =============================================================================
# Dependencies aggregate + manual install jobs (askill / show runtime)
# =============================================================================

_ALLOWED_DEP_INSTALLS = {"askill", "avault", "show-runtime", "tmux"}
_STARTUP_DEPENDENCY_RECONCILE_LOCK = threading.Lock()
_DEFAULT_STARTUP_SHOW_PAGE_PREWARM_LIMIT = 3
_MAX_STARTUP_SHOW_PAGE_PREWARM_LIMIT = 10


def dependencies_status() -> dict:
    """Status of the required local runtime dependencies for the Dependencies
    settings page: askill, the Show Page runtime, and the shared Node.js
    prerequisite. (Agent backend CLIs are managed on the Backends tab.)

    Returns stable ids + machine-readable status only — display copy (label /
    detail) is localized in the React page, not sent from here.
    """
    deps: list[dict] = []

    a = askill_update_status(include_latest=False)
    deps.append(
        {
            "id": "askill",
            "kind": "tool",
            "required": True,
            "installed": a["installed"],
            "version": a.get("version"),
            "latest_version": a.get("latest_version"),
            "has_update": a.get("has_update", False),
            "status": a["status"],
        }
    )

    av = avault_status()
    deps.append(
        {
            "id": "avault",
            "kind": "tool",
            "required": True,
            "installed": av["installed"],
            "version": av.get("version"),
            "latest_version": None,
            "has_update": False,
            "status": av["status"],
        }
    )

    try:
        from core.show_runtime import get_show_runtime_manager

        srt = get_show_runtime_manager().status()
    except Exception as exc:  # noqa: BLE001
        srt = {"installed": False, "node_available": None, "node_version": None, "reason": str(exc)}
    manifest = srt.get("manifest") if isinstance(srt.get("manifest"), dict) else {}
    srt_installed = bool(srt.get("installed"))
    deps.append(
        {
            "id": "show-runtime",
            "kind": "runtime",
            "required": True,
            "installed": srt_installed,
            "version": manifest.get("runtime_version"),
            "status": "ready" if srt_installed else "missing",
        }
    )

    try:
        from core.tmux_runtime import tmux_status

        tmux = tmux_status()
    except Exception as exc:  # noqa: BLE001
        tmux = {"installed": False, "version": None, "status": "missing", "reason": str(exc)}
    deps.append(
        {
            "id": "tmux",
            "kind": "tool",
            "required": False,
            "installed": bool(tmux.get("installed")),
            "version": tmux.get("version"),
            "status": "ready" if tmux.get("installed") else "missing",
        }
    )

    # Node present but below the Show Runtime minimum (node_supported is False)
    # is not actually usable — don't show it green while runtime repair fails.
    node_ok = bool(srt.get("node_available")) and srt.get("node_supported") is not False
    deps.append(
        {
            "id": "node",
            "kind": "node",
            "required": True,
            "installed": node_ok,
            "version": srt.get("node_version"),
            "status": "ready" if node_ok else "missing",
        }
    )

    return {"ok": True, "deps": deps}


def _prepare_show_runtime_job() -> dict:
    try:
        from core.show_runtime import get_show_runtime_manager

        payload = get_show_runtime_manager().prepare(force=True)
        ok = bool(payload.get("ok"))
        return {
            "ok": ok,
            "message": "Show Runtime ready." if ok else (payload.get("reason") or "Show Runtime prepare failed"),
            "output": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "output": None}


def _prepare_tmux_job() -> dict:
    try:
        from core.tmux_runtime import ensure_tmux_installed

        payload = ensure_tmux_installed(force=True)
        ok = bool(payload.get("ok"))
        return {
            **payload,
            "ok": ok,
            "message": "tmux runtime ready." if ok else (payload.get("message") or payload.get("reason") or "tmux install failed"),
            "output": payload.get("output"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc), "output": None}


def _startup_dependency_reconcile_enabled() -> bool:
    value = os.environ.get("VIBE_STARTUP_DEPENDENCY_RECONCILE")
    return value is None or value.strip().lower() not in {"0", "false", "no", "off"}


def startup_show_page_prewarm_limit() -> int:
    raw = os.environ.get("VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT")
    if raw is None or not raw.strip():
        return _DEFAULT_STARTUP_SHOW_PAGE_PREWARM_LIMIT
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid VIBE_STARTUP_SHOW_PAGE_PREWARM_LIMIT=%r; using default", raw)
        return _DEFAULT_STARTUP_SHOW_PAGE_PREWARM_LIMIT
    return max(0, min(value, _MAX_STARTUP_SHOW_PAGE_PREWARM_LIMIT))


def startup_show_page_prewarm_targets(limit: int | None = None) -> dict:
    """Recent non-offline Show Pages to warm after the runtime sidecar starts."""
    resolved_limit = startup_show_page_prewarm_limit() if limit is None else max(0, min(limit, _MAX_STARTUP_SHOW_PAGE_PREWARM_LIMIT))
    if resolved_limit <= 0:
        return {"ok": True, "limit": resolved_limit, "pages": []}

    from core.show_pages import ShowPageStore, VISIBILITY_PRIVATE, VISIBILITY_PUBLIC
    from storage.pagination import PageRequest

    store = ShowPageStore()
    try:
        candidates = [
            *store.list_page(visibility=VISIBILITY_PRIVATE, page_request=PageRequest(limit=resolved_limit)).items,
            *store.list_page(visibility=VISIBILITY_PUBLIC, page_request=PageRequest(limit=resolved_limit)).items,
        ]
    finally:
        store.close()

    candidates.sort(key=lambda page: (page.updated_at, page.session_id), reverse=True)
    pages = []
    for page in candidates[:resolved_limit]:
        base_path = f"/p/{page.share_id}/" if page.visibility == VISIBILITY_PUBLIC and page.share_id else None
        pages.append(
            {
                "session_id": page.session_id,
                "visibility": page.visibility,
                "updated_at": page.updated_at,
                "base_path": base_path,
            }
        )
    return {"ok": True, "limit": resolved_limit, "pages": pages}


def reconcile_startup_dependencies() -> dict:
    """Best-effort startup repair for local capabilities.

    The main service must stay available even when these dependencies are slow
    or broken, so callers should run this in a background thread/task. Node is
    only detected here; installing system-level Node remains an explicit
    installer/settings action.
    """
    if not _startup_dependency_reconcile_enabled():
        return {"ok": True, "skipped": True, "reason": "disabled"}
    if not _STARTUP_DEPENDENCY_RECONCILE_LOCK.acquire(blocking=False):
        return {"ok": True, "skipped": True, "reason": "already_running"}

    started_at = time.monotonic()
    result: dict[str, Any] = {
        "ok": True,
        "node": {"ok": False, "status": "unknown"},
        "askill": {"ok": False, "status": "unknown"},
        "avault": {"ok": False, "status": "unknown"},
        "show_runtime": {"ok": False, "status": "unknown"},
        "tmux": {"ok": False, "status": "unknown"},
    }
    try:
        try:
            askill = ensure_askill_installed(force=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup dependency reconcile failed to ensure askill: %s", exc, exc_info=True)
            askill = {"ok": False, "message": str(exc)}
        result["askill"] = askill

        try:
            avault = ensure_avault_installed(force=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup dependency reconcile failed to ensure avault: %s", exc, exc_info=True)
            avault = {"ok": False, "message": str(exc)}
        result["avault"] = avault

        try:
            from core.show_runtime import get_show_runtime_manager

            manager = get_show_runtime_manager()
            status = manager.status()
            node_available = bool(status.get("node_available"))
            node_supported = status.get("node_supported") is not False
            node_ok = node_available and node_supported
            node_status = "ready" if node_ok else "missing"
            if node_available and not node_supported:
                node_status = "unsupported"
            result["node"] = {
                "ok": node_ok,
                "status": node_status,
                "version": status.get("node_version"),
            }

            if node_ok:
                result["show_runtime"] = {
                    "ok": True,
                    "status": "pending_prewarm",
                    "reason": None,
                }
            else:
                result["show_runtime"] = {
                    "ok": False,
                    "status": "skipped",
                    "reason": "runtime_node_unsupported" if node_available else "runtime_node_missing",
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Startup dependency reconcile failed to prepare Show Runtime: %s", exc, exc_info=True)
            result["show_runtime"] = {"ok": False, "status": "failed", "reason": str(exc)}

        if os.environ.get("VIBE_UI_ENABLE_TERMINAL", "").strip().lower() in {"0", "false", "no", "off"}:
            # Terminal explicitly disabled — don't download the optional tmux runtime.
            result["tmux"] = {"ok": True, "status": "skipped", "reason": "terminal_disabled"}
        elif os.environ.get("VIBE_INSTALL_SKIP_TMUX", "").strip().lower() in _TRUTHY_ENV_VALUES:
            result["tmux"] = {"ok": True, "skipped": True, "reason": "VIBE_INSTALL_SKIP_TMUX"}
        else:
            try:
                from core.tmux_runtime import ensure_tmux_installed

                result["tmux"] = ensure_tmux_installed(force=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Startup dependency reconcile failed to ensure tmux runtime: %s", exc, exc_info=True)
                result["tmux"] = {"ok": False, "status": "failed", "reason": str(exc)}

        result["duration_ms"] = int((time.monotonic() - started_at) * 1000)
        result["ok"] = (
            bool(result["askill"].get("ok"))
            and bool(result["avault"].get("ok"))
            and bool(result["show_runtime"].get("ok"))
        )
        return result
    finally:
        _STARTUP_DEPENDENCY_RECONCILE_LOCK.release()


def start_dependency_install_job(dep: str) -> dict:
    """Install/repair a required local dependency in a background job.

    Reuses the agent install-job store (lock / prune / latest-by-key and the
    ``get_agent_install_job`` poller) keyed by the dependency id, so the UI gets
    the same non-blocking install + poll experience without running
    package-manager subprocesses on the request path.
    """
    if dep not in _ALLOWED_DEP_INSTALLS:
        return {"ok": False, "message": f"Unknown dependency: {dep}"}

    job_id = uuid.uuid4().hex
    now = time.time()
    job = {
        "ok": True,
        "job_id": job_id,
        "backend": dep,
        "status": "running",
        "message": "Install started",
        "output": "",
        "path": None,
        "started_at": now,
        "finished_at": None,
    }
    with _AGENT_INSTALL_JOB_LOCK:
        _prune_agent_install_jobs(now)
        latest_job_id = _AGENT_INSTALL_LATEST_BY_BACKEND.get(dep)
        latest_job = _AGENT_INSTALL_JOBS.get(latest_job_id or "")
        if isinstance(latest_job, dict) and latest_job.get("status") == "running":
            return dict(latest_job)
        _AGENT_INSTALL_JOBS[job_id] = job
        _AGENT_INSTALL_LATEST_BY_BACKEND[dep] = job_id

    def _worker() -> None:
        try:
            if dep == "askill":
                result = ensure_askill_installed(force=True)
            elif dep == "avault":
                result = ensure_avault_installed(force=True)
            elif dep == "show-runtime":
                result = _prepare_show_runtime_job()
            elif dep == "tmux":
                result = _prepare_tmux_job()
            else:
                result = {"ok": False, "message": f"Unknown dependency: {dep}", "output": None}
            ok = bool(result.get("ok"))
            with _AGENT_INSTALL_JOB_LOCK:
                current = _AGENT_INSTALL_JOBS.get(job_id)
                if current is not None:
                    current.update(result)
                    current["ok"] = ok
                    current["status"] = "succeeded" if ok else "failed"
                    current["finished_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Dependency install job failed for %s: %s", dep, exc, exc_info=True)
            with _AGENT_INSTALL_JOB_LOCK:
                current = _AGENT_INSTALL_JOBS.get(job_id)
                if current is not None:
                    current.update(
                        {"ok": False, "status": "failed", "message": str(exc), "output": None, "finished_at": time.time()}
                    )

    threading.Thread(target=_worker, daemon=True, name=f"vibe-dep-install-{dep}-{job_id[:8]}").start()
    return dict(job)


# =============================================================================
# Backend lifecycle (version probe, latest check, restart)
# =============================================================================

# In-memory caches keyed by (backend, cli_path) so version answers stay tied
# to the binary they came from. Trade freshness for fewer probes during rapid
# popover opens. Tuned for human pacing (seconds), not bots.
#
# The UI server handles requests on multiple threads, so reads, writes, and
# invalidation can race. A single lock serializes mutation — fast in practice
# (the cache holds at most a handful of entries), and avoids
# ``RuntimeError: dictionary changed size during iteration`` during the
# scan in ``_invalidate_version_cache``.
_BACKEND_CACHE_LOCK = __import__("threading").Lock()
_BACKEND_VERSION_CACHE: dict[tuple[str, str], tuple[float, str | None]] = {}
_BACKEND_LATEST_CACHE: dict[str, tuple[float, str | None]] = {}
_BACKEND_VERSION_TTL_SECONDS = 30.0
_BACKEND_LATEST_TTL_SECONDS = 3600.0
# Failed lookups (network down, registry hiccup) re-probe sooner so a
# transient outage doesn't pin "—" for the full hour.
_BACKEND_LATEST_FAILURE_TTL_SECONDS = 120.0
_BACKEND_RUNTIME_USER_AGENT = "avibe/backend-runtime"
_ASKILL_RELEASE_REPOSITORY = "avibe-bot/askill"
_ASKILL_AUTO_UPDATE_ENV = "VIBE_ASKILL_AUTO_UPDATE"
_ASKILL_SKIP_ENV = "VIBE_INSTALL_SKIP_ASKILL"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_FALSY_ENV_VALUES = {"0", "false", "no", "off"}

def _parse_semver(text: str) -> str | None:
    """Extract the first dotted-numeric version token from *text*.

    Handles outputs like ``opencode 1.2.3``, ``codex-cli 0.77.1 (build ...)``
    and ``v1.0.0`` uniformly. Returns ``None`` if no version is found.
    """
    if not text:
        return None
    match = re.search(r"\d+(?:\.\d+){1,3}(?:[-+][\w.\-]+)?", text)
    return match.group(0) if match else None


def _probe_cli_version(cli_path: str | None) -> str | None:
    """Run ``<cli> --version`` with a short timeout and return the parsed version."""
    if not cli_path:
        return None
    try:
        result = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_command_env_for(cli_path if os.path.isabs(cli_path) else None),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("CLI version probe failed for %s: %s", cli_path, exc)
        return None
    output = (result.stdout or "") + " " + (result.stderr or "")
    return _parse_semver(output.strip())


def _http_opener_for_best_effort_probe():
    try:
        from vibe.proxy import resolve_proxy

        proxy = resolve_proxy(None)
    except Exception:
        proxy = None

    if proxy and not proxy.lower().startswith("socks"):
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # SOCKS proxies need aiohttp_socks; latest-version probes are best-effort
    # so we silently fall back to direct urlopen rather than complicate the
    # cache path. Direct-connection failures are cached for a short TTL.
    return urllib.request.build_opener()


def _fetch_github_latest_release_version(repo: str, *, user_agent: str = _BACKEND_RUNTIME_USER_AGENT) -> str | None:
    """Best-effort GitHub release lookup. Returns a normalized version or None."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": user_agent},
    )
    try:
        with _http_opener_for_best_effort_probe().open(req, timeout=5) as resp:  # noqa: S310 - trusted registry
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network failure path
        logger.debug("Latest GitHub release probe failed for %s: %s", repo, exc)
        return None
    raw = payload.get("tag_name")
    if not isinstance(raw, str):
        return None
    return raw.lstrip("v").strip() or None


def _fetch_latest_version(name: str) -> str | None:
    """Best-effort upstream lookup. Returns ``None`` on any failure."""
    probe = latest_probe_for_backend(name)
    if not probe:
        return None
    kind, ident = probe
    if kind == "github":
        return _fetch_github_latest_release_version(ident)

    url = f"https://registry.npmjs.org/{ident}/latest"
    req = urllib.request.Request(url, headers={"User-Agent": _BACKEND_RUNTIME_USER_AGENT})

    try:
        with _http_opener_for_best_effort_probe().open(req, timeout=5) as resp:  # noqa: S310 - trusted registries
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network failure path
        logger.debug("Latest version probe failed for %s: %s", name, exc)
        return None
    raw = payload.get("version")
    if not isinstance(raw, str):
        return None
    return raw.lstrip("v").strip() or None


def _cached_version(name: str, cli_path: str | None) -> str | None:
    key = (name, cli_path or "")
    with _BACKEND_CACHE_LOCK:
        cached = _BACKEND_VERSION_CACHE.get(key)
    if cached and time.time() - cached[0] < _BACKEND_VERSION_TTL_SECONDS:
        return cached[1]
    # Probe outside the lock — CLI invocation can block on subprocess for
    # seconds, and we don't want unrelated lookups stuck behind it.
    version = _probe_cli_version(cli_path)
    with _BACKEND_CACHE_LOCK:
        _BACKEND_VERSION_CACHE[key] = (time.time(), version)
    return version


def _invalidate_version_cache(name: str) -> None:
    """Drop all cached version entries for *name* across cli paths."""
    with _BACKEND_CACHE_LOCK:
        # Snapshot keys under the lock so the subsequent ``pop`` calls can
        # never observe a partially mutated dict from a concurrent writer.
        stale = [k for k in _BACKEND_VERSION_CACHE if k[0] == name]
        for key in stale:
            _BACKEND_VERSION_CACHE.pop(key, None)


def _cached_latest(name: str) -> str | None:
    with _BACKEND_CACHE_LOCK:
        cached = _BACKEND_LATEST_CACHE.get(name)
    if cached:
        ttl = _BACKEND_LATEST_TTL_SECONDS if cached[1] else _BACKEND_LATEST_FAILURE_TTL_SECONDS
        if time.time() - cached[0] < ttl:
            return cached[1]
    # Network fetch outside the lock — same reasoning as ``_cached_version``.
    latest = _fetch_latest_version(name)
    with _BACKEND_CACHE_LOCK:
        _BACKEND_LATEST_CACHE[name] = (time.time(), latest)
    return latest


def _compare_versions(current: str | None, latest: str | None) -> bool:
    """Return True when *latest* is strictly greater than *current*.

    Honors PEP 440 / semver pre-release ordering when possible (e.g. ``0.77.1``
    is greater than ``0.77.1-beta.0``). Falls back to a conservative numeric
    tuple comparison; returns False on any parsing failure so we never nag the
    user with a phantom update.
    """
    if not current or not latest or current == latest:
        return False

    try:
        from packaging.version import InvalidVersion, Version

        try:
            return Version(latest) > Version(current)
        except InvalidVersion:
            pass
    except Exception:  # pragma: no cover - packaging is a transitive dep
        pass

    def _parts(value: str) -> tuple[tuple[int, ...], bool] | None:
        # Strip build metadata; keep pre-release tag to compare lexically.
        core, _, pre = value.split("+", 1)[0].partition("-")
        try:
            nums = tuple(int(part) for part in core.split("."))
        except ValueError:
            return None
        # A version with a pre-release suffix is "less than" the bare release.
        return nums, bool(pre)

    cur_parts = _parts(current)
    new_parts = _parts(latest)
    if cur_parts is None or new_parts is None:
        return False
    cur_nums, cur_is_pre = cur_parts
    new_nums, new_is_pre = new_parts
    if new_nums != cur_nums:
        return new_nums > cur_nums
    # Same numeric core: pre-release sorts before release.
    return cur_is_pre and not new_is_pre


def _opencode_server_pid() -> int | None:
    pid_path = paths.get_logs_dir() / "opencode_server.json"
    if not pid_path.exists():
        return None
    try:
        info = json.loads(pid_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = info.get("pid") if isinstance(info, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _opencode_process_status() -> str:
    from vibe import runtime

    pid = _opencode_server_pid()
    if not pid or not runtime.pid_alive(pid):
        return "stopped"
    cmd = runtime.get_process_command(pid) or ""
    return "running" if "opencode" in cmd and "serve" in cmd else "unknown"


def _process_matches_codex_binary(cmdline: list[str], resolved_binary: str | None) -> bool:
    """Decide whether ``cmdline`` belongs to *our* codex app-server.

    The original cmdline-substring check matched any ``codex`` mention in any
    argument (e.g. a user invoking ``codex app-server`` themselves from a
    shell, or another tool whose args happen to contain those tokens), and
    a follow-up tightening only inspected ``argv[0]`` — which missed the
    ``npm install -g @openai/codex`` shim, where the live process is
    ``node /path/.../bin/codex app-server`` (``argv[0] == "node"``).

    We now scan the first few argv tokens (``argv[0]`` and ``argv[1]``)
    looking for the codex binary itself, since the kernel preserves the
    script path as ``argv[1]`` whenever a ``#!/usr/bin/env node`` shim is
    exec'd. The match requires:

      1. one of ``argv[0]``/``argv[1]`` resolves to the same absolute path
         as the configured codex binary (or, when no resolved binary is
         known, has basename starting with ``codex``); and
      2. one of the early arguments is exactly ``app-server``.
    """
    if not cmdline:
        return False
    try:
        target = (
            str(Path(resolved_binary).expanduser().resolve())
            if resolved_binary
            else None
        )
    except Exception:
        target = resolved_binary

    target_basename = os.path.basename(target) if target else None

    def _matches(token: str) -> bool:
        try:
            resolved = str(Path(token).expanduser().resolve())
        except Exception:
            resolved = token
        token_basename = os.path.basename(token) if token else ""
        if target is not None:
            # Exact-path match (absolute argv[0] / shim path) — strongest signal.
            if resolved == target or token == target:
                return True
            # Bare-name argv[0]: ``codex`` launched via PATH lookup. The
            # kernel records argv[0] verbatim, so ``token == "codex"`` and
            # ``Path("codex").resolve()`` becomes a cwd-relative path that
            # never equals ``target``. Fall back to a basename match —
            # combined with the upstream ``app-server`` marker check this
            # still excludes random unrelated tools. The cost of matching
            # a sibling codex install (different absolute path, same
            # basename) is acceptable: the lifecycle chip reflects "a
            # codex app-server is running", which is what users want.
            if token_basename and target_basename and token_basename == target_basename:
                return True
            return False
        # No resolved binary: best-effort basename match so the chip still
        # works when the configured CLI isn't on PATH right now.
        return os.path.basename(resolved).startswith("codex")

    # Check argv[0] and argv[1] — the latter is where ``node`` shebang shims
    # land the codex script path. We deliberately stop at argv[1] so an
    # unrelated tool with ``codex`` mentioned later in its args isn't swept up.
    if not any(_matches(tok) for tok in cmdline[:2] if tok):
        return False
    # ``codex app-server`` always passes ``app-server`` as an argv token; we
    # intentionally do NOT match it inside an arbitrary substring. Widen the
    # window slightly so the node-shim layout (``node script app-server``)
    # still hits.
    return "app-server" in cmdline[1:5]


def _codex_processes(resolved_binary: str | None) -> list[int]:
    """Find live ``codex app-server`` subprocesses owned by the current user.

    The match must hit our resolved codex binary so unrelated tools that
    happen to mention ``codex`` and ``app-server`` aren't swept up.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a hard dep elsewhere
        return []

    # ``uids`` is a POSIX-only psutil attribute; requesting it on Windows
    # makes ``process_iter`` raise ``ValueError: invalid attr name 'uids'``
    # and the entire probe blows up. Gate it on ``getuid`` availability,
    # which is the same signal we use to decide whether to filter at all.
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    attrs = ["pid", "name", "cmdline"]
    if current_uid is not None:
        attrs.append("uids")
    pids: list[int] = []
    for proc in psutil.process_iter(attrs=attrs):
        try:
            info = proc.info
            cmdline = info.get("cmdline") or []
            if not _process_matches_codex_binary(cmdline, resolved_binary):
                continue
            if current_uid is not None:
                uids = info.get("uids")
                proc_uid = getattr(uids, "real", None) if uids else None
                if proc_uid is not None and proc_uid != current_uid:
                    continue
            pids.append(info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _codex_process_status(resolved_binary: str | None) -> str:
    return "running" if _codex_processes(resolved_binary) else "stopped"


def get_backend_runtime(name: str) -> dict:
    """Return live lifecycle info for one backend.

    Versions are cached for short windows so popovers and re-renders do not
    fan out into many CLI invocations or registry HTTP calls.
    """
    if not is_agent_backend(name):
        return {"ok": False, "error": f"Unknown backend: {name}"}

    try:
        config = V2Config.load()
    except Exception as exc:
        logger.debug("Failed to load config for backend runtime: %s", exc)
        config = None

    backend_cfg = getattr(getattr(config, "agents", None), name, None) if config else None
    enabled = bool(getattr(backend_cfg, "enabled", False))
    configured_path = getattr(backend_cfg, "cli_path", "") or name

    resolved_path = resolve_cli_path(configured_path)
    installed = resolved_path is not None

    current_version = _cached_version(name, resolved_path) if installed else None
    latest_version = _cached_latest(name)
    has_update = _compare_versions(current_version, latest_version)

    if name == "opencode":
        process_status = _opencode_process_status() if installed else "stopped"
    elif name == "codex":
        process_status = _codex_process_status(resolved_path) if installed else "stopped"
    elif name == "claude":
        process_status = "unknown"
    else:
        process_status = "unknown"

    return {
        "ok": True,
        "name": name,
        "enabled": enabled,
        "cli_path": configured_path,
        "resolved_path": resolved_path,
        "installed": installed,
        "current_version": current_version,
        "latest_version": latest_version,
        "has_update": has_update,
        "supports_restart": supports_runtime_refresh(name),
        "process_status": process_status,
    }


def _runtime_command_dir() -> Path:
    """Directory the controller watches for cross-process command markers."""
    base = paths.get_state_dir() / "runtime_commands"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _wait_for_controller_ack(marker: Path, timeout: float) -> tuple[bool, str | None]:
    """Poll for ``marker`` removal as a signal that the controller ran it.

    Returns ``(handled, error)``:

    - ``handled=True, error=None`` — controller picked up the marker and the
      handler returned cleanly.
    - ``handled=True, error="..."`` — controller picked up the marker but
      the handler raised; the controller wrote the message to a companion
      ``<marker>.err`` file before deleting the request marker.
    - ``handled=False, error=None`` — timed out; the controller never
      consumed the marker. Caller should fall back to a direct kill.

    The companion ``.err`` file is consumed (unlinked) before returning so
    later requests start clean.
    """
    err_marker = marker.with_name(marker.name + ".err")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not marker.exists():
            error: str | None = None
            if err_marker.exists():
                try:
                    error = err_marker.read_text(encoding="utf-8").strip() or "unknown error"
                except OSError:
                    error = "unknown error"
                try:
                    err_marker.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
            return True, error
        time.sleep(0.1)
    return False, None


def _request_controller_restart(
    backend: str,
    timeout: float = 4.0,
    *,
    metadata: Optional[dict[str, Any]] = None,
) -> tuple[bool, str | None]:
    """Ask the controller to refresh a backend via the runtime-command marker.

    The controller's ``RuntimeCommandWatcher`` (see ``core/runtime_commands.py``)
    is the in-process owner of ``CodexAgent._transports`` / OpenCode server
    state. Killing those processes from the UI server would leave that cache
    stale, so the cleanest path is to ask the controller to call its existing
    ``_refresh_backend_runtime(backend)`` for us. We drop a marker file and
    wait briefly for the controller to delete it; the caller falls back to a
    direct process kill when the controller is unreachable (e.g. running
    detached, not yet started).

    Each request gets its own marker filename (``restart-<backend>.<reqid>.cmd``)
    so we can correlate failures back to *this* request. Without the reqid,
    a stale ``.err`` from a prior request that timed out caller-side — or an
    overlapping concurrent restart — could be mistaken for *our* failure and
    surface a phantom error toast.

    Returns ``(handled, error)`` — see ``_wait_for_controller_ack`` for the
    contract. ``handled=True`` does *not* imply success; check ``error`` too
    so the UI toast doesn't claim a restart when the controller's refresh
    actually raised.
    """
    reqid = uuid.uuid4().hex[:8]
    marker = _runtime_command_dir() / f"restart-{backend}.{reqid}.cmd"
    try:
        payload = {"backend": backend, "ts": time.time(), "reqid": reqid}
        if metadata:
            payload["metadata"] = {
                str(k): v
                for k, v in metadata.items()
                if isinstance(k, str) and v is not None
            }
        marker.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("Failed to write controller restart marker for %s: %s", backend, exc)
        return False, None
    handled, error = _wait_for_controller_ack(marker, timeout)
    if handled:
        return True, error
    # Marker still present — controller didn't pick it up. Clean up the
    # request marker *and* any stray ``.err`` so the next attempt starts
    # from a clean slate.
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        marker.with_name(marker.name + ".err").unlink(missing_ok=True)
    except OSError:
        pass
    return False, None


def restart_backend(name: str, *, metadata: Optional[dict[str, Any]] = None) -> dict:
    """Refresh the backend so the next request picks up new config/env.

    Preferred path: drop a runtime-command marker that the controller
    observes and reacts to via ``_refresh_backend_runtime``. This keeps the
    controller's in-memory transport/session state consistent. If the
    controller isn't running (e.g. service not yet started), backends with a
    separate runtime can fall back to killing their OS process directly — the
    controller's recovery logic will rebuild state when it next starts.

    Claude has no separate daemon, but the controller keeps SDK sessions
    and a loaded compat config; the marker path refreshes those in memory.
    """
    if not supports_runtime_refresh(name):
        return {"ok": False, "message": f"Restart is not supported for backend: {name}"}

    controller_handled, controller_error = _request_controller_restart(name, metadata=metadata)
    _invalidate_version_cache(name)

    if controller_handled:
        if controller_error:
            # Controller saw the request and ran the handler, but the handler
            # raised. Don't lie to the user — surface the failure so they can
            # retry or look at logs. (The next runtime probe will also reflect
            # the stale state, but the toast must already say so.)
            return {
                "ok": False,
                "message": f"Backend refresh failed: {controller_error}",
            }
        return {"ok": True, "message": runtime_refresh_success_message(name)}

    if name == "opencode":
        from vibe import runtime
        from vibe.cli import _stop_opencode_server

        stopped = _stop_opencode_server()
        if stopped:
            return {"ok": True, "message": "OpenCode server stopped; it will respawn on next request."}
        pid = _opencode_server_pid()
        if not pid or not runtime.pid_alive(pid):
            return {"ok": True, "message": "OpenCode server is not running; next request will start a fresh one."}
        return {"ok": False, "message": "Failed to stop OpenCode server."}

    if name == "claude":
        return {
            "ok": False,
            "message": "Claude runtime refresh was not acknowledged by the controller; retry after the service is running.",
        }

    # codex fallback: kill app-server processes; controller recovery rebuilds.
    try:
        import psutil
    except ImportError:
        return {"ok": False, "message": "psutil unavailable; cannot manage Codex processes."}

    try:
        config = V2Config.load()
        backend_cfg = getattr(getattr(config, "agents", None), "codex", None)
        configured = getattr(backend_cfg, "cli_path", "") or "codex"
    except Exception:
        configured = "codex"
    resolved = resolve_cli_path(configured)

    pids = _codex_processes(resolved)
    if not pids:
        return {"ok": True, "message": "Codex app-server is not running; next request will start a fresh one."}

    failed: list[int] = []
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.debug("Codex restart skip pid=%s: %s", pid, exc)
        except Exception as exc:
            logger.warning("Failed to stop codex pid=%s: %s", pid, exc)
            failed.append(pid)
    if failed:
        return {"ok": False, "message": f"Failed to stop Codex process(es): {failed}"}
    return {"ok": True, "message": f"Stopped {len(pids)} Codex process(es); they will respawn on next request."}


_VALID_AUTH_MODES = {"oauth", "api_key"}


def _mask_api_key(api_key: str | None) -> str | None:
    """Return a UI-safe preview of an API key.

    Pattern: keep the prefix up to (and including) the first ``-`` block
    (e.g. ``sk-proj-``, ``sk-ant-``) so the user can still recognize
    the key type, then dots, then the last 4 characters. Short keys
    (<= 12 chars) get a uniform 6-dots-plus-last-4 pattern so we never
    accidentally render plaintext for a malformed key.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    key = api_key.strip()
    last4 = key[-4:]
    if len(key) > 12 and "-" in key:
        # Take the recognizable prefix up to and including the second dash
        # (handles both ``sk-...`` and ``sk-proj-...`` shapes).
        first_dash = key.find("-")
        second_dash = key.find("-", first_dash + 1)
        prefix_end = second_dash + 1 if second_dash != -1 else first_dash + 1
        prefix = key[:prefix_end]
        return f"{prefix}{'•' * 9}{last4}"
    return f"{'•' * 6}{last4}"


def _read_claude_cli_oauth_signed_in(
    cli_path: str | None,
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> bool | None:
    """Ask Claude Code whether a first-party OAuth account is signed in.

    This is the most accurate Settings signal because it covers both
    keychain-backed installs and Linux/Docker's on-disk credentials file.
    Keep it best-effort and quiet: if Claude is missing, slow, or emits an
    unexpected shape, callers fall back to disk inspection.
    """
    configured = (cli_path or "claude").strip() or "claude"
    binary = resolve_cli_path(configured) or configured
    try:
        result = subprocess.run(
            [binary, "auth", "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=3,
            check=False,
            env=env,
            cwd=cwd,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    text = (result.stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    logged_in = payload.get("loggedIn")
    if logged_in is False:
        return False
    if logged_in is not True:
        return None

    # ``auth status`` reports Claude Code's overall auth state. API-key
    # sources can also be "logged in", so only treat it as OAuth when the
    # CLI identifies the source as Claude.ai / first-party OAuth.
    saw_concrete_source = False
    for key in ("authMethod", "authProvider", "provider", "source"):
        raw = payload.get(key)
        if isinstance(raw, str):
            normalized = re.sub(r"[\s_]+", "-", raw.strip().lower())
            if not normalized:
                continue
            saw_concrete_source = True
            if normalized in {
                "claude.ai",
                "claude-ai",
                "oauth",
                "oauth-token",
                "setup-token",
                "claude-code-oauth-token",
                "subscription",
                "claude-subscription",
            }:
                return True
            if any(token in normalized for token in ("api-key", "apikey", "api key", "auth-token", "apihelper", "api-helper")):
                return False
    if saw_concrete_source:
        return False
    return None


def _resolve_claude_status_probe_cwd(config: Any | None) -> str | None:
    candidates = (
        getattr(getattr(config, "claude", None), "cwd", None),
        getattr(getattr(config, "runtime", None), "default_cwd", None),
    )
    for raw in candidates:
        if isinstance(raw, str) and raw.strip():
            path = os.path.abspath(os.path.expanduser(raw.strip()))
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except OSError as exc:
                logger.warning("Failed to prepare Claude auth status cwd=%s: %s", path, exc)
    return None


def _build_claude_status_probe_env(claude_env: dict[str, str] | None) -> dict[str, str] | None:
    if claude_env is None:
        return None
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("ANTHROPIC_") or key.startswith("CLAUDE_"):
            env.pop(key, None)
    env.update(claude_env)
    return env


# ---------------------------------------------------------------------------
# Web Settings → Backends OAuth flow plumbing.
#
# FastAPI hosts these flows on the UI server's persistent ASGI event loop.
# On success we drop a ``restart-<backend>.cmd`` marker so the live
# controller refreshes its in-process agent state (mirroring what
# ``_refresh_backend_runtime`` does in-process for IM-driven flows).
# ---------------------------------------------------------------------------


class _WebControllerStub:
    """Minimal ``Controller``-shaped facade for the web OAuth flow service.

    ``AgentAuthService`` only touches ``controller.config`` (for
    ``cli_path``) and gracefully no-ops when ``agent_service`` /
    ``session_handler`` are absent. The stub re-reads V2Config from disk on
    every access so a freshly-saved ``cli_path`` is picked up on the next
    flow without restarting the UI server.
    """

    @property
    def config(self):
        return load_config()

    # The following attributes are inspected via ``getattr(..., None)`` in
    # ``AgentAuthService`` and gate platform-specific paths that web flows
    # never traverse (IM message dispatch, session lookup, agent refresh).
    agent_service = None
    session_handler = None
    im_client = None


_oauth_service_lock = threading.Lock()
_oauth_service: Any = None
_oauth_loop: asyncio.AbstractEventLoop | None = None
_oauth_loop_thread: threading.Thread | None = None


def _start_oauth_event_loop() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()

    def _runner() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True, name="vibe-oauth-loop")
    thread.start()
    return loop, thread


def _on_web_auth_success(backend: str) -> None:
    """Tell the live controller to refresh its agent after web OAuth success."""
    try:
        handled, err = _request_controller_restart(
            backend,
            timeout=4.0,
            metadata={"reason": "web_auth_success", "source": "oauth_callback"},
        )
        if handled and err:
            logger.warning("Controller refresh after web auth reported error: %s", err)
        elif not handled:
            logger.info("Controller did not pick up web-auth refresh marker for %s", backend)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to notify controller after web auth: %s", exc)


def _get_oauth_service() -> Any:
    """Lazily build the (singleton) AgentAuthService for web flows."""
    global _oauth_service, _oauth_loop, _oauth_loop_thread
    with _oauth_service_lock:
        if _oauth_service is not None:
            return _oauth_service
        from core.agent_auth_service import AgentAuthService

        _oauth_loop, _oauth_loop_thread = _start_oauth_event_loop()
        controller = _WebControllerStub()
        _oauth_service = AgentAuthService(controller)
        _oauth_service._post_web_success_hook = _on_web_auth_success
        return _oauth_service


def _ensure_oauth_loop() -> asyncio.AbstractEventLoop:
    global _oauth_loop, _oauth_loop_thread
    with _oauth_service_lock:
        if _oauth_loop is None or _oauth_loop.is_closed():
            _oauth_loop, _oauth_loop_thread = _start_oauth_event_loop()
        return _oauth_loop


def _submit_oauth_coro(coro, *, timeout: float = 30.0):
    _get_oauth_service()  # ensures the persistent loop exists
    loop = _ensure_oauth_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _serialize_web_flow_status(payload: dict) -> dict:
    """Strip server-only keys before returning to the browser."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "invalid_payload"}
    return payload


def start_oauth_web(
    backend: str,
    force_reset: bool = True,
    provider_id: Optional[str] = None,
) -> dict:
    return _submit_oauth_coro(
        start_oauth_web_async(backend, force_reset=force_reset, provider_id=provider_id),
        timeout=60.0,
    )


async def start_oauth_web_async(
    backend: str,
    force_reset: bool = True,
    provider_id: Optional[str] = None,
) -> dict:
    backend = (backend or "").strip().lower()
    if not supports_web_oauth(backend):
        return {"ok": False, "error": "unsupported_backend"}
    if backend == "opencode" and not (isinstance(provider_id, str) and provider_id.strip()):
        return {"ok": False, "error": "opencode_provider_id_required"}
    service = _get_oauth_service()
    try:
        flow = await service.start_web_setup(
            backend,
            force_reset=force_reset,
            provider_id=(provider_id.strip() if isinstance(provider_id, str) else None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Web OAuth start failed for %s: %s", backend, exc, exc_info=True)
        return {"ok": False, "error": "start_failed", "detail": str(exc)}

    if flow.state == "failed":
        return {
            "ok": False,
            "error": flow.error or "start_failed",
            "flow_id": flow.flow_id,
        }
    return {
        "ok": True,
        "flow_id": flow.flow_id,
        "backend": flow.backend,
        "state": flow.state,
        "url": flow.url,
        "device_code": flow.device_code,
        "awaiting_code": flow.awaiting_code,
        "provider": flow.provider,
    }


def get_oauth_web_status(flow_id: str) -> dict:
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return {"ok": False, "error": "missing_flow_id"}
    service = _get_oauth_service()
    return _serialize_web_flow_status(service.get_web_flow_status(flow_id))


def submit_oauth_web_code(flow_id: str, code: str) -> dict:
    return _submit_oauth_coro(submit_oauth_web_code_async(flow_id, code), timeout=30.0)


async def submit_oauth_web_code_async(flow_id: str, code: str) -> dict:
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return {"ok": False, "error": "missing_flow_id"}
    service = _get_oauth_service()
    try:
        return await service.submit_web_code(flow_id, code or "")
    except Exception as exc:  # noqa: BLE001
        logger.error("Web OAuth code submit failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "submit_failed", "detail": str(exc)}


def remove_backend_auth(backend: str) -> dict:
    return _submit_oauth_coro(remove_backend_auth_async(backend), timeout=30.0)


async def remove_backend_auth_async(backend: str) -> dict:
    """Clear stored credentials for Claude or Codex (web Settings)."""
    backend = (backend or "").strip().lower()
    if not supports_web_oauth(backend):
        return {"ok": False, "error": "unsupported_backend"}
    service = _get_oauth_service()
    try:
        return await service.remove_web_auth(backend)
    except Exception as exc:  # noqa: BLE001
        logger.error("Web auth remove failed for %s: %s", backend, exc, exc_info=True)
        return {"ok": False, "error": "remove_failed", "detail": str(exc)}


async def remove_claude_oauth_credentials_async() -> dict:
    """Clear only Claude Code OAuth credentials, preserving API-key auth."""
    service = _get_oauth_service()
    try:
        return await service.clear_claude_oauth_credentials_only()
    except Exception as exc:  # noqa: BLE001
        logger.error("Claude OAuth credentials cleanup failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "remove_failed", "detail": str(exc)}


def remove_claude_oauth_credentials() -> dict:
    return _submit_oauth_coro(remove_claude_oauth_credentials_async(), timeout=30.0)


def _clear_claude_oauth_credentials_after_api_key_save(service=None) -> dict:
    service = service or _get_oauth_service()
    return _submit_oauth_coro(
        service.clear_claude_oauth_credentials_only(),
        timeout=30.0,
    )


def remove_backend_api_key(backend: str) -> dict:
    """Clear the stored API key for Claude / Codex without touching OAuth.

    Mirrors OpenCode's "Remove key" vs "Sign out" split: Claude and
    Codex can both carry ``api_key`` *and* OAuth credentials at the
    same time, and the CLI picks api_key when both are present. Without
    a way to drop just the API key, a stale or rejected key keeps
    forcing 401s even after the user signed in via OAuth.

    - **Codex**: re-applies ``apply_codex_auth(auth_mode='oauth')``
      which pops ``OPENAI_API_KEY`` from ``~/.codex/auth.json`` and
      keeps any ``tokens`` blob intact. V2Config's
      ``agents.codex.api_key`` is also cleared and ``auth_mode`` is
      flipped to ``oauth``. Triggers ``restart_backend('codex')`` so
      the persistent daemon reloads.
    - **Claude**: remove Anthropic env overrides from Claude's
      ``settings.json``, clear legacy V2Config ``api_key`` / ``base_url``
      cache fields, and flip ``auth_mode`` to ``oauth``.
      Claude Code's OAuth token store is left alone.
    """
    backend = (backend or "").strip().lower()
    if backend not in {"claude", "codex"}:
        return {"ok": False, "error": "unsupported_backend"}

    notices: list = []
    if backend == "codex":
        from vibe.codex_config import apply_codex_auth

        try:
            result = apply_codex_auth(auth_mode="oauth", api_key=None, base_url=None)
            if isinstance(result, dict):
                raw_notices = result.get("notices")
                if isinstance(raw_notices, list):
                    notices = raw_notices
        except Exception as exc:  # noqa: BLE001
            logger.error("apply_codex_auth(oauth) during remove-key failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "remove_failed", "detail": str(exc)}
    elif backend == "claude":
        from vibe.claude_config import apply_claude_auth

        try:
            apply_claude_auth(auth_mode="oauth", api_key=None, base_url=None)
        except Exception as exc:  # noqa: BLE001
            logger.error("apply_claude_auth(oauth) during remove-key failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "remove_failed", "detail": str(exc)}

    # Clear V2Config api_key for both backends.
    try:
        with CONFIG_LOCK:
            try:
                config = load_config()
            except FileNotFoundError:
                config = V2Config()
            target = getattr(getattr(config, "agents", None), backend, None)
            if target is not None:
                target.auth_mode = "oauth"
                target.api_key = None
                # Drop base_url for both backends, not just Codex: a
                # stale Claude relay URL stored in V2Config gets
                # injected into the subprocess as ``ANTHROPIC_BASE_URL``
                # on every launch via ``build_claude_subprocess_env``.
                # After removing an API key (intent: fall back to
                # OAuth), the OAuth credentials would still be routed
                # to the api-key-only relay and silently 401.
                target.base_url = None
                # User explicitly chose OAuth by clicking Remove key —
                # mark the flag so legacy env-var fallback in
                # ``build_claude_subprocess_env`` is bypassed and the
                # inherited ``ANTHROPIC_*`` env actually gets stripped.
                if backend == "claude":
                    target.auth_mode_set = True
                config.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("V2Config clear during remove-key failed for %s: %s", backend, exc)

    # Codex has a persistent daemon — refresh it so the cleared key
    # actually takes effect on the next request. Claude is one-shot per
    # request so a synthetic restart is enough.
    restart: dict
    if backend == "codex":
        try:
            restart = restart_backend(
                "codex",
                metadata={"reason": "remove_api_key", "source": "ui_api"},
            )
        except Exception as exc:  # noqa: BLE001
            restart = {"ok": False, "message": str(exc)}
    else:
        restart = {
            "ok": True,
            "message": "Claude relaunches per request; the next message uses the new auth.",
        }
    response: dict = {"ok": True, "restart": restart}
    if notices:
        response["notices"] = notices
    return response


def test_backend_auth(backend: str, model: Optional[str] = None) -> dict:
    return _submit_oauth_coro(test_backend_auth_async(backend, model=model), timeout=60.0)


async def test_backend_auth_async(backend: str, model: Optional[str] = None) -> dict:
    """Send a single-token ``Hi`` probe through the backend CLI.

    ``model`` lets the caller override the CLI's configured default —
    important for Codex users whose ``config.toml`` selects a slow
    reasoning model, where even "Hi" can blow past the test timeout.
    """
    backend = (backend or "").strip().lower()
    if not supports_web_oauth(backend):
        return {"ok": False, "error": "unsupported_backend"}
    service = _get_oauth_service()
    try:
        return await service.test_web_auth(backend, model=model)
    except Exception as exc:  # noqa: BLE001
        logger.error("Web auth test failed for %s: %s", backend, exc, exc_info=True)
        return {"ok": False, "error": "test_failed", "detail": str(exc)}


def test_opencode_provider(provider_id: str, model: Optional[str] = None) -> dict:
    return _submit_oauth_coro(test_opencode_provider_async(provider_id, model=model), timeout=90.0)


async def test_opencode_provider_async(provider_id: str, model: Optional[str] = None) -> dict:
    """Probe a single OpenCode provider over the live ``opencode serve`` HTTP API.

    OpenCode users typically wire up multiple providers (OpenAI, Poe,
    Anthropic, ...) but only a few will be active at any time. A single
    backend-wide button would either spuriously fail when one is broken
    or hide which one works. Per-provider probes echo the model's
    response so the user knows the round-trip actually returned text.
    """
    provider_id = (provider_id or "").strip()
    if not provider_id:
        return {"ok": False, "error": "missing_provider"}
    service = _get_oauth_service()
    try:
        return await service.test_opencode_provider(provider_id, model=model)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "OpenCode provider test failed for %s: %s",
            provider_id,
            exc,
            exc_info=True,
        )
        return {"ok": False, "error": "test_failed", "detail": str(exc)}


def cancel_oauth_web(flow_id: str) -> dict:
    return _submit_oauth_coro(cancel_oauth_web_async(flow_id), timeout=15.0)


async def cancel_oauth_web_async(flow_id: str) -> dict:
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return {"ok": False, "error": "missing_flow_id"}
    service = _get_oauth_service()
    try:
        return await service.cancel_web_flow(flow_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Web OAuth cancel failed: %s", exc, exc_info=True)
        return {"ok": False, "error": "cancel_failed", "detail": str(exc)}


def get_codex_auth() -> dict:
    """Return the user-facing Codex auth state for the Settings UI.

    Merges two sources of truth:
    - on-disk ``~/.codex/{config.toml,auth.json}`` (what Codex actually
      reads at launch) — authoritative for ``has_api_key`` / ``base_url`` /
      ``has_chatgpt_tokens`` so the UI never lies when the user edited the
      files by hand.
    - ``V2Config.agents.codex`` — the mode we *intend* to be in, useful as
      a tiebreaker (e.g. user clicked OAuth but hasn't run ``codex login``
      yet, so disk has no tokens but our config says oauth).

    Secrets never leave the server; only length is returned.
    """
    from vibe.codex_config import read_codex_auth_state

    disk_state = read_codex_auth_state()
    try:
        config = load_config()
        cfg = getattr(getattr(config, "agents", None), "codex", None)
        configured_mode = getattr(cfg, "auth_mode", None)
    except Exception:
        configured_mode = None

    # Disk wins when it carries unambiguous evidence of API-key auth: an
    # ``OPENAI_API_KEY`` in ``~/.codex/auth.json`` is a concrete artefact
    # the user (or a prior ``codex login`` flow) placed there. ``V2Config``
    # may still default ``auth_mode`` to ``"oauth"`` on the upgrade path
    # (older configs lacked the field entirely), so trusting the config
    # alone would make the UI render OAuth and a subsequent save would
    # then wipe ``OPENAI_API_KEY``. Configured mode remains the source of
    # truth only when disk has no key — i.e., the user's stated intent
    # before they have signed in or pasted credentials.
    if disk_state.get("has_api_key"):
        auth_mode: str | None = "api_key"
    elif configured_mode in _VALID_AUTH_MODES:
        auth_mode = configured_mode
    else:
        auth_mode = disk_state.get("auth_mode")
    # The *active* auth source the running Codex CLI uses at launch is
    # determined entirely by ``~/.codex/auth.json``: a stored API key wins;
    # else ChatGPT tokens; else "not configured". This is what the user
    # cares about ("which one is actually working"), separate from the
    # ``auth_mode`` field above (which is the *intent* we'd save next).
    has_api_key_live = bool(disk_state.get("has_api_key"))
    has_chatgpt_live = bool(disk_state.get("has_chatgpt_tokens"))
    if has_api_key_live:
        active_auth_mode = "api_key"
    elif has_chatgpt_live:
        active_auth_mode = "oauth"
    else:
        active_auth_mode = "none"

    return {
        "ok": True,
        "auth_mode": auth_mode or "oauth",
        "active_auth_mode": active_auth_mode,
        "has_api_key": has_api_key_live,
        "api_key_length": int(disk_state.get("api_key_length") or 0),
        "api_key_masked": _mask_api_key(disk_state.get("api_key_raw")),
        "base_url": disk_state.get("base_url"),
        "has_chatgpt_tokens": has_chatgpt_live,
        "chatgpt_account": disk_state.get("chatgpt_account"),
        # Forward the live Codex credentials-store status so the UI can
        # warn when the user is about to switch storage backends
        # (Codex's documented default is ``auto`` → keyring-preferred).
        # Dropping these here was the bug: the React page would treat
        # ``file_store_active`` as undefined and surface a keyring
        # warning even when the store is already ``file``.
        "credentials_store": disk_state.get("credentials_store") or "auto",
        "file_store_active": bool(disk_state.get("file_store_active")),
        # Surface "we can't read your key — it may live in the OS
        # keychain" so the UI doesn't claim "no key configured" when
        # Codex is in keyring-preferred mode and we have no disk
        # evidence. We suppress the flag when V2Config has a stored
        # ``auth_mode`` (the user already saved through our flow), since
        # we then know the mode and the next save will pin file storage.
        "auth_mode_uncertain": (
            bool(disk_state.get("auth_mode_uncertain"))
            and configured_mode not in _VALID_AUTH_MODES
        ),
    }


def save_codex_auth(payload: dict) -> dict:
    """Persist Codex auth: V2Config + ``~/.codex/{config.toml,auth.json}``.

    The on-disk write is what Codex actually reads; the V2Config write
    records the user's intent so the UI can render a coherent state after
    restart. We treat the disk write as authoritative — if it fails, we
    surface the error instead of leaving V2Config out of sync.

    After writing, we trigger ``restart_backend('codex')`` so the persistent
    app-server reloads with the new credentials. The restart failure is
    surfaced but does not roll back the config write; the user can retry.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Payload must be an object"}

    auth_mode = payload.get("auth_mode")
    if auth_mode not in _VALID_AUTH_MODES:
        return {"ok": False, "message": f"auth_mode must be one of {sorted(_VALID_AUTH_MODES)}"}

    raw_api_key = payload.get("api_key")
    if raw_api_key is not None and not isinstance(raw_api_key, str):
        return {"ok": False, "message": "api_key must be a string"}
    api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else None

    # Three-state ``base_url`` payload (matches the OpenCode provider save
    # handler and the new web Settings → OAuth flow): omitting the key
    # means "leave the stored value alone" so toggling auth_mode does not
    # accidentally clear a relay URL the user had set up in api_key mode.
    base_url_present = "base_url" in payload
    raw_base_url = payload.get("base_url") if base_url_present else None
    if base_url_present and raw_base_url is not None and not isinstance(raw_base_url, str):
        return {"ok": False, "message": "base_url must be a string"}
    base_url_change: Optional[str] = None
    if base_url_present:
        base_url_change = raw_base_url.strip() if isinstance(raw_base_url, str) else None
        if base_url_change == "":
            base_url_change = None

    if auth_mode == "api_key" and not api_key:
        # Allow callers to PATCH base_url alone by reusing the stored key.
        # ``auth.json`` is the live source Codex reads at launch, and it
        # captures keys rotated outside this flow (e.g. ``codex login
        # --with-api-key``). The V2Config cache can be stale relative to
        # disk, so trusting it first would silently revert a freshly
        # rotated key when we re-write ``auth.json`` below. Prefer disk;
        # fall back to V2Config only if disk has nothing (legacy installs
        # that never wrote ``auth.json``).
        try:
            from vibe.codex_config import read_codex_api_key

            api_key = read_codex_api_key()
        except Exception:
            api_key = None
        if not api_key:
            with CONFIG_LOCK:
                try:
                    existing = load_config()
                    stored = getattr(getattr(existing, "agents", None), "codex", None)
                    api_key = getattr(stored, "api_key", None) or None
                except Exception:
                    api_key = None
        if not api_key:
            return {"ok": False, "message": "api_key is required when auth_mode='api_key'"}

    # Resolve the effective base_url: explicit payload wins, otherwise
    # preserve whatever V2Config currently has.
    if base_url_present:
        effective_base_url = base_url_change
    else:
        with CONFIG_LOCK:
            try:
                existing_cfg = load_config()
                stored_codex = getattr(getattr(existing_cfg, "agents", None), "codex", None)
                effective_base_url = getattr(stored_codex, "base_url", None) or None
            except Exception:
                effective_base_url = None

    from vibe.codex_config import apply_codex_auth

    notices: list = []
    try:
        result = apply_codex_auth(
            auth_mode=auth_mode, api_key=api_key, base_url=effective_base_url
        )
        if isinstance(result, dict):
            raw_notices = result.get("notices")
            if isinstance(raw_notices, list):
                notices = raw_notices
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except OSError as exc:
        logger.error("Failed to write Codex auth files: %s", exc, exc_info=True)
        return {"ok": False, "message": f"Failed to write Codex config: {exc}"}

    with CONFIG_LOCK:
        try:
            config = load_config()
        except FileNotFoundError:
            config = V2Config()
        config.agents.codex.auth_mode = auth_mode
        config.agents.codex.api_key = api_key if auth_mode == "api_key" else None
        config.agents.codex.base_url = effective_base_url
        config.save()

    restart_result = restart_backend(
        "codex",
        metadata={"reason": "save_codex_auth", "source": "ui_api"},
    )
    state = get_codex_auth()
    state["restart"] = restart_result
    if notices:
        # Surface non-fatal config-rewrite notices (e.g. "we cleared a
        # custom relay pointer because OAuth tokens won't validate
        # against ai-relay.chainbot.io") so the UI can show a one-time
        # banner. Without this the user sees a green "saved" toast then
        # hits a confusing 401 on their next request.
        state["notices"] = notices
    if not restart_result.get("ok", False):
        # Config written, restart failed — tell the UI both so the toast
        # can say "saved, but you may need to restart Codex manually".
        state["ok"] = True
        state["message"] = restart_result.get("message")
    return state


def get_claude_auth() -> dict:
    """Return the user-facing Claude auth state for the Settings UI.

    Claude differs from Codex in two structural ways:

    1. ``~/.claude/settings.json`` is the source of truth for API-key
       env overrides because Claude Code layers that file on top of the
       inherited process env at launch.
    2. OAuth state is owned by Claude Code, so we first ask
       ``claude auth status --json``. If that probe is unavailable, fall
       back to the Linux/Docker credentials file signal.

    Legacy V2Config keys are read only as a migration fallback so old
    installs still render their current state before the next save moves
    the key into Claude's own settings file.
    """
    from vibe.claude_config import (
        read_claude_auth_state,
        read_claude_oauth_signed_in,
        read_claude_settings_env,
        build_claude_subprocess_env,
    )

    disk_state = read_claude_auth_state()
    disk_oauth_signed_in = read_claude_oauth_signed_in()
    settings_env = read_claude_settings_env()
    settings_key = settings_env.get("ANTHROPIC_API_KEY") or settings_env.get("ANTHROPIC_AUTH_TOKEN") or ""
    settings_base = settings_env.get("ANTHROPIC_BASE_URL") or ""

    try:
        config = load_config()
        cfg = getattr(getattr(config, "agents", None), "claude", None)
        configured_mode = getattr(cfg, "auth_mode", None)
        configured_key = getattr(cfg, "api_key", None) or ""
        configured_base = getattr(cfg, "base_url", None) or ""
        configured_cli_path = getattr(cfg, "cli_path", None)
        status_probe_cwd = _resolve_claude_status_probe_cwd(config)
    except Exception:
        configured_mode = None
        configured_key = ""
        configured_base = ""
        configured_cli_path = None
        status_probe_cwd = None

    configured_key = configured_key.strip() if isinstance(configured_key, str) else ""
    configured_base = configured_base.strip() if isinstance(configured_base, str) else ""
    try:
        claude_status_env = _build_claude_status_probe_env(
            build_claude_subprocess_env(
                cfg if "cfg" in locals() else None,
                force_oauth=True,
            )
        )
    except Exception:
        claude_status_env = None
    cli_oauth_signed_in = _read_claude_cli_oauth_signed_in(
        configured_cli_path if isinstance(configured_cli_path, str) else None,
        env=claude_status_env,
        cwd=status_probe_cwd,
    )
    oauth_signed_in = (
        cli_oauth_signed_in if cli_oauth_signed_in is not None else disk_oauth_signed_in
    )

    # settings.json wins: it is the file Claude Code itself layers on top
    # of inherited env. V2Config is a legacy fallback only.
    effective_key = settings_key or configured_key
    effective_base = settings_base or configured_base
    has_api_key = bool(effective_key)

    if configured_mode in _VALID_AUTH_MODES:
        auth_mode = configured_mode
    elif effective_key:
        auth_mode = "api_key"
    else:
        auth_mode = "oauth"

    settings_conflict = False

    # ``active_auth_mode`` reflects what the running CLI is actually using
    # at launch.
    if effective_key and auth_mode == "api_key":
        active_auth_mode = "api_key"
    elif oauth_signed_in and auth_mode == "oauth":
        active_auth_mode = "oauth"
    elif effective_key:
        active_auth_mode = "api_key"
    elif oauth_signed_in:
        active_auth_mode = "oauth"
    else:
        active_auth_mode = "none"

    # Which storage the live API key came from — helps the UI explain the
    # state ("Key configured in settings.json"). Plaintext never leaves
    # the server; only the mask is forwarded.
    if settings_key:
        api_key_source = "settings_json"
    elif configured_key:
        api_key_source = "v2config"
    else:
        api_key_source = None

    return {
        "ok": True,
        "auth_mode": auth_mode,
        "active_auth_mode": active_auth_mode,
        "has_api_key": has_api_key,
        "api_key_length": len(effective_key),
        "api_key_masked": _mask_api_key(effective_key),
        "api_key_source": api_key_source,
        "has_oauth_credentials": oauth_signed_in,
        "base_url": effective_base or None,
        "settings_path": disk_state.get("settings_path"),
        "settings_exists": bool(disk_state.get("settings_exists")),
        "settings_env_has_key": bool(disk_state.get("settings_env_has_key")),
        "settings_env_key_length": int(disk_state.get("settings_env_key_length") or 0),
        "settings_env_key_var": disk_state.get("settings_env_key_var"),
        "settings_env_base_url": disk_state.get("settings_env_base_url"),
        "settings_conflict": settings_conflict,
    }


def save_claude_auth(payload: dict) -> dict:
    """Persist Claude auth into Claude Code's own ``settings.json``.

    V2Config records only non-secret intent/legacy cleanup state. It must
    not carry the API key because Claude Code's ``settings.json`` env block
    wins at launch anyway.

    Empty ``api_key`` while in ``api_key`` mode is treated as "keep the
    stored key" — same UX promise as Codex — so callers can PATCH the
    base URL without re-typing the secret. An empty key with no stored
    fallback is rejected.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Payload must be an object"}

    auth_mode = payload.get("auth_mode")
    if auth_mode not in _VALID_AUTH_MODES:
        return {"ok": False, "message": f"auth_mode must be one of {sorted(_VALID_AUTH_MODES)}"}

    raw_api_key = payload.get("api_key")
    if raw_api_key is not None and not isinstance(raw_api_key, str):
        return {"ok": False, "message": "api_key must be a string"}
    api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else None

    # Three-state ``base_url`` payload semantics (matches Codex/OpenCode):
    # absent key → keep stored value; null/blank → clear; non-blank → set.
    base_url_present = "base_url" in payload
    raw_base_url = payload.get("base_url") if base_url_present else None
    if base_url_present and raw_base_url is not None and not isinstance(raw_base_url, str):
        return {"ok": False, "message": "base_url must be a string"}
    base_url_change: Optional[str] = None
    if base_url_present:
        base_url_change = raw_base_url.strip() if isinstance(raw_base_url, str) else None
        if base_url_change == "":
            base_url_change = None

    settings_auth_token = None
    if auth_mode == "api_key" and not api_key:
        # Reuse the live Claude settings key for base-URL-only updates.
        # Fall back to legacy V2Config only for older installs that have
        # not yet been migrated through this save path.
        try:
            from vibe.claude_config import (
                read_claude_api_key_from_settings,
                read_claude_settings_env,
            )

            api_key = read_claude_api_key_from_settings()
            if not api_key:
                env = read_claude_settings_env()
                token = env.get("ANTHROPIC_AUTH_TOKEN")
                settings_auth_token = token if isinstance(token, str) and token.strip() else None
        except Exception:
            api_key = None
            settings_auth_token = None
        if not api_key and not settings_auth_token:
            with CONFIG_LOCK:
                try:
                    existing = load_config()
                    stored = getattr(getattr(existing, "agents", None), "claude", None)
                    api_key = getattr(stored, "api_key", None) or None
                except Exception:
                    pass
        if not api_key and not settings_auth_token:
            return {"ok": False, "message": "api_key is required when auth_mode='api_key'"}

    effective_base_url = base_url_change if base_url_present else None
    if not base_url_present and auth_mode == "api_key":
        settings_env = {}
        try:
            from vibe.claude_config import read_claude_settings_env

            settings_env = read_claude_settings_env()
        except Exception:
            settings_env = {}
        existing_base = settings_env.get("ANTHROPIC_BASE_URL")
        if isinstance(existing_base, str) and existing_base.strip():
            effective_base_url = existing_base.strip()
        else:
            with CONFIG_LOCK:
                try:
                    existing = load_config()
                    stored = getattr(getattr(existing, "agents", None), "claude", None)
                    effective_base_url = getattr(stored, "base_url", None) or None
                except Exception:
                    effective_base_url = None

    oauth_cleanup_service = _get_oauth_service() if auth_mode == "api_key" else None

    from vibe.claude_config import apply_claude_auth

    try:
        apply_claude_auth(
            auth_mode=auth_mode,
            api_key=api_key if auth_mode == "api_key" else None,
            base_url=effective_base_url if auth_mode == "api_key" else None,
            auth_token=settings_auth_token if auth_mode == "api_key" else None,
        )
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    except OSError as exc:
        logger.error("Failed to write Claude settings.json: %s", exc, exc_info=True)
        return {"ok": False, "message": f"Failed to write Claude settings: {exc}"}

    with CONFIG_LOCK:
        try:
            config = load_config()
        except FileNotFoundError:
            config = V2Config()
        config.agents.claude.auth_mode = auth_mode
        # Flip the explicit marker so ``build_claude_subprocess_env``
        # honors ``auth_mode`` strictly (strip inherited env in OAuth
        # mode) for this and subsequent launches. Legacy installs that
        # have never been through this save path keep the flag at its
        # ``False`` default and continue to inherit shell env vars.
        config.agents.claude.auth_mode_set = True
        # Secrets and endpoint overrides live in Claude's own settings.json.
        # Clear legacy cache fields so future reads do not have two writers.
        config.agents.claude.api_key = None
        config.agents.claude.base_url = None
        config.save()

    oauth_cleanup_result: dict | None = None
    if auth_mode == "api_key":
        try:
            oauth_cleanup_result = _clear_claude_oauth_credentials_after_api_key_save(
                oauth_cleanup_service
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to clear Claude OAuth credentials after API-key save: %s", exc)
            oauth_cleanup_result = {
                "ok": True,
                "partial": True,
                "warning": "oauth_cleanup_failed",
                "detail": str(exc),
            }

    # Claude is one-shot per request — no daemon to restart. Return a
    # synthetic restart result so the UI handles the same response shape
    # as Codex / OpenCode and the toast wording can stay consistent.
    state = get_claude_auth()
    state["restart"] = {
        "ok": True,
        "message": "Claude relaunches per request; the next message uses the new auth.",
    }
    if isinstance(oauth_cleanup_result, dict) and oauth_cleanup_result.get("partial"):
        state["partial"] = True
        state["warning"] = oauth_cleanup_result.get("warning") or "oauth_cleanup_failed"
        state["detail"] = oauth_cleanup_result.get("detail")
    return state


# ---------------------------------------------------------------------------
# OpenCode provider configuration
# ---------------------------------------------------------------------------
#
# The OpenCode page in Settings → Backends is fully dynamic: we never ship
# a hard-coded provider list. Instead we fan out to the running OpenCode
# server (``GET /provider`` for the catalog, ``GET /provider/auth`` for
# the auth-method index, ``GET /config/providers`` for model lists) and
# merge the responses into a per-card view with ``configured`` /
# ``oauth_available`` / ``local`` flags.
#
# Provider config writes update ``opencode.json`` directly because OpenCode
# stores custom providers, base URLs, user models, and provider option API keys
# there. ``auth.json`` remains OpenCode-owned: when we need to remove a stale
# or conflicting entry, we go through OpenCode's ``DELETE /auth/<id>`` wrapper
# instead of editing the auth file ourselves. We also persist
# ``default_provider`` into ``V2Config`` so the chip and routing layers stay in
# sync across restarts.


async def _opencode_get_server():
    """Spin up a transient OpenCodeServerManager instance for HTTP calls.

    Mirrors the pattern used by ``opencode_options_async``: pull the
    OpenCode config from V2Config, request a manager instance, ensure
    the daemon is reachable, and let the caller drive its HTTP methods.
    Returns ``None`` if OpenCode is disabled — callers translate that
    into a UI-friendly error.
    """
    from config.v2_compat import to_app_config
    from core.resource_governance import AgentResourceGovernor, config_from_runtime
    from modules.agents.opencode import OpenCodeServerManager

    v2_config = V2Config.load()
    config = to_app_config(v2_config)
    if not config.opencode:
        return None
    opencode_config = config.opencode
    server = await OpenCodeServerManager.get_instance(
        binary=opencode_config.binary,
        port=opencode_config.port,
        request_timeout_seconds=opencode_config.request_timeout_seconds,
        resource_governor=AgentResourceGovernor(config_from_runtime(v2_config)),
    )
    await server.ensure_running()
    return server


_LOCAL_PROVIDER_IDS = {"ollama", "lmstudio", "lm-studio"}
_OPENCODE_PROVIDER_REFRESH_ATTEMPTS = 3
_OPENCODE_PROVIDER_REFRESH_DELAY_SECONDS = 0.5


def _opencode_provider_model_ids(provider: dict) -> set[str]:
    raw_models = provider.get("models") if isinstance(provider, dict) else None
    if isinstance(raw_models, dict):
        return {model_id for model_id in raw_models if isinstance(model_id, str)}
    if isinstance(raw_models, list):
        ids: set[str] = set()
        for model in raw_models:
            model_id = model.get("id") if isinstance(model, dict) else None
            if isinstance(model_id, str):
                ids.add(model_id)
        return ids
    return set()


def _is_opencode_user_model(model_id: str, model_info: dict) -> bool:
    meta = model_info.get("vibe_remote") if isinstance(model_info, dict) else None
    if isinstance(meta, dict) and meta.get("user_model") is True:
        return True
    return model_info.get("id") == model_id and model_info.get("name") == model_id


def _is_local_provider(provider_id: str, auth_methods: list) -> bool:
    """Whether the provider runs on localhost and needs no credentials.

    Earlier this also tagged ``no auth methods → local`` but OpenCode
    1.14's ``/provider/auth`` only enumerates providers that have OAuth
    or special prompts — bare API-key providers (minimax, openrouter,
    poe…) are simply absent from that map. Treating absence as "local"
    pushed them into a fallback that kept ``configured`` True even
    after the user removed their key. Narrow to a known-local
    whitelist; the auth-methods param is kept for symmetry / future use.
    """
    _ = auth_methods  # noqa: F841 — kept for callsite symmetry
    return isinstance(provider_id, str) and provider_id.lower() in _LOCAL_PROVIDER_IDS


def _configured_opencode_provider_ids(
    *,
    providers_raw: dict,
    auth_entries: dict,
    config_api_key_provider_ids: set[str] | None = None,
    custom_config_provider_ids: set[str] | None = None,
) -> set[str]:
    connected = providers_raw.get("connected") if isinstance(providers_raw, dict) else None
    connected_set = {pid for pid in connected if isinstance(pid, str)} if isinstance(connected, list) else set()
    all_providers = _coerce_opencode_provider_catalog(providers_raw)
    configured = {pid for pid in auth_entries.keys() if isinstance(pid, str)}
    configured.update(config_api_key_provider_ids or set())
    configured.update(custom_config_provider_ids or set())
    for pid in connected_set:
        if _is_local_provider(pid, []):
            configured.add(pid)
    for pid in all_providers:
        if _is_local_provider(pid, []) and pid in connected_set:
            configured.add(pid)
    return configured


def _filter_opencode_models_to_configured_providers(
    models: dict,
    *,
    providers_raw: dict,
    auth_entries: dict,
    config_api_key_provider_ids: set[str] | None = None,
    custom_config_provider_ids: set[str] | None = None,
) -> dict:
    """Drop unconfigured cloud providers from OpenCode model options.

    OpenCode's ``/config/providers`` returns catalog models for providers
    even after the user removes credentials. Settings/Agents model pickers
    should only offer models the runtime can actually use: providers with
    auth.json entries, provider option API keys, custom provider config,
    plus known local providers that are connected.
    """

    if not isinstance(models, dict):
        return models
    allowed = _configured_opencode_provider_ids(
        providers_raw=providers_raw,
        auth_entries=auth_entries,
        config_api_key_provider_ids=config_api_key_provider_ids,
        custom_config_provider_ids=custom_config_provider_ids,
    )
    if not allowed:
        return {**models, "providers": [], "default": {}}

    providers = []
    seen: set[str] = set()
    for provider in models.get("providers", []) or []:
        if not isinstance(provider, dict):
            continue
        pid = _opencode_provider_id(provider)
        if isinstance(pid, str) and pid in allowed:
            seen.add(pid)
            providers.append(provider)

    for pid in allowed:
        if pid in seen:
            continue
        providers.append({"id": pid, "models": {}})

    defaults = models.get("default")
    if isinstance(defaults, dict):
        defaults = {pid: model_id for pid, model_id in defaults.items() if pid in allowed}
    else:
        defaults = {}
    return {**models, "providers": providers, "default": defaults}


def _merge_opencode_user_models(
    models: dict,
    user_model_index: dict[str, dict[str, dict]],
    *,
    allowed_provider_ids: set[str] | None = None,
) -> dict:
    """Overlay user-configured ``provider.<id>.models`` onto model metadata."""

    if not isinstance(models, dict):
        return models
    providers_raw = models.get("providers")
    if not isinstance(providers_raw, list):
        return models
    if allowed_provider_ids is not None:
        filtered_providers = []
        for provider in providers_raw:
            if not isinstance(provider, dict):
                continue
            pid = _opencode_provider_id(provider)
            if isinstance(pid, str) and pid in allowed_provider_ids:
                filtered_providers.append(provider)
        raw_defaults = models.get("default")
        filtered_defaults = {}
        if isinstance(raw_defaults, dict):
            filtered_defaults = {
                pid: model_id
                for pid, model_id in raw_defaults.items()
                if pid in allowed_provider_ids
            }
        models = {
            **models,
            "providers": filtered_providers,
            "default": filtered_defaults,
        }
        providers_raw = models.get("providers", [])
    if not user_model_index:
        return models

    providers = []
    defaults = dict(models.get("default")) if isinstance(models.get("default"), dict) else {}

    def _seed_default(pid: str, user_models: dict[str, dict]) -> None:
        if pid not in defaults and user_models:
            defaults[pid] = sorted(user_models.keys())[0]

    seen: set[str] = set()
    for provider in providers_raw:
        if not isinstance(provider, dict):
            providers.append(provider)
            continue
        pid = provider.get("id") or provider.get("provider_id") or provider.get("name")
        if not isinstance(pid, str) or pid not in user_model_index:
            providers.append(provider)
            continue
        if allowed_provider_ids is not None and pid not in allowed_provider_ids:
            providers.append(provider)
            continue
        seen.add(pid)
        provider_models = provider.get("models")
        user_models = user_model_index.get(pid) or {}
        _seed_default(pid, user_models)
        if isinstance(provider_models, dict):
            merged_models = {**provider_models, **user_models}
        elif isinstance(provider_models, list):
            merged_models = {
                model.get("id"): model
                for model in provider_models
                if isinstance(model, dict) and isinstance(model.get("id"), str)
            }
            merged_models.update(user_models)
        else:
            merged_models = dict(user_models)
        providers.append({**provider, "models": merged_models})

    for pid, user_models in user_model_index.items():
        if pid in seen:
            continue
        if allowed_provider_ids is not None and pid not in allowed_provider_ids:
            continue
        _seed_default(pid, user_models)
        providers.append({"id": pid, "models": dict(user_models)})

    return {**models, "providers": providers, "default": defaults}


def _opencode_provider_id(entry: dict) -> str | None:
    pid = entry.get("id") or entry.get("provider_id") or entry.get("name")
    return pid if isinstance(pid, str) and pid else None


async def _read_opencode_user_model_index() -> dict[str, dict[str, dict]]:
    try:
        opencode_probe = await asyncio.to_thread(
            load_first_opencode_user_config, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("Could not read opencode.json for user model overlay: %s", exc)
        return {}

    if opencode_probe is None or not isinstance(opencode_probe.config, dict):
        return {}
    provider_block = opencode_probe.config.get("provider")
    if not isinstance(provider_block, dict):
        return {}

    user_model_index: dict[str, dict[str, dict]] = {}
    for pid_key, pid_config in provider_block.items():
        if not isinstance(pid_key, str) or not isinstance(pid_config, dict):
            continue
        models = pid_config.get("models")
        if not isinstance(models, dict):
            continue
        user_models = {
            model_id: model_info
            for model_id, model_info in models.items()
            if isinstance(model_id, str) and isinstance(model_info, dict)
        }
        if user_models:
            user_model_index[pid_key] = user_models
    return user_model_index


async def _read_opencode_custom_provider_ids() -> set[str]:
    try:
        from vibe.opencode_config import read_opencode_custom_providers

        custom_providers = await asyncio.to_thread(read_opencode_custom_providers, logger_instance=logger)
    except Exception as exc:
        logger.debug("Could not read opencode.json for custom providers: %s", exc)
        return set()
    if not isinstance(custom_providers, dict):
        return set()
    return {pid for pid in custom_providers if isinstance(pid, str)}


async def _read_opencode_config_api_key_provider_ids() -> set[str]:
    try:
        opencode_probe = await asyncio.to_thread(
            load_first_opencode_user_config, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("Could not read opencode.json for provider apiKey options: %s", exc)
        return set()

    if opencode_probe is None or not isinstance(opencode_probe.config, dict):
        return set()
    provider_block = opencode_probe.config.get("provider")
    if not isinstance(provider_block, dict):
        return set()

    provider_ids: set[str] = set()
    for pid_key, pid_config in provider_block.items():
        if not isinstance(pid_key, str) or not isinstance(pid_config, dict):
            continue
        options = pid_config.get("options")
        if not isinstance(options, dict):
            continue
        api_key = options.get("apiKey")
        if isinstance(api_key, str) and api_key.strip():
            provider_ids.add(pid_key)
    return provider_ids


async def _read_opencode_config_api_key(provider_id: str) -> str | None:
    """Return provider.<id>.options.apiKey from opencode.json, if present."""

    if not isinstance(provider_id, str) or not provider_id.strip():
        return None
    pid = provider_id.strip()
    try:
        opencode_probe = await asyncio.to_thread(
            load_first_opencode_user_config, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("Could not read opencode.json for provider apiKey: %s", exc)
        return None
    if opencode_probe is None or not isinstance(opencode_probe.config, dict):
        return None
    provider_block = opencode_probe.config.get("provider")
    if not isinstance(provider_block, dict):
        return None
    provider_config = provider_block.get(pid)
    if not isinstance(provider_config, dict):
        return None
    options = provider_config.get("options")
    if not isinstance(options, dict):
        return None
    api_key = options.get("apiKey")
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    return None


def _custom_opencode_provider_row(provider_id: str, provider_config: dict) -> dict:
    meta = provider_config.get("vibe_remote")
    adapter_label = ""
    if isinstance(meta, dict):
        adapter_label = str(meta.get("adapter_label") or "")
    return {
        "id": provider_id,
        "name": provider_config.get("name") or provider_id,
        "description": adapter_label,
        "models": provider_config.get("models") if isinstance(provider_config.get("models"), dict) else {},
    }


def _coerce_opencode_provider_catalog(providers_raw) -> dict:
    """Normalize OpenCode ``/provider`` payloads into an id-keyed map.

    OpenCode 1.x returns ``{all: [Provider, ...], default: {...},
    connected: [...]}`` where ``all`` is a list. A pre-1.x prototype
    returned ``{all: {pid: Provider}}`` (dict). The original legacy shape
    was ``{providers: [...]}`` under a different top-level key. Tolerate
    all three so an OpenCode upgrade-in-place or a stale client cannot
    leave the Settings grid empty.
    """
    if not isinstance(providers_raw, dict):
        return {}
    out: dict = {}
    raw_all = providers_raw.get("all")
    if isinstance(raw_all, dict):
        return raw_all
    if isinstance(raw_all, list):
        for entry in raw_all:
            if isinstance(entry, dict):
                pid = entry.get("id")
                if pid:
                    out[pid] = entry
        return out
    legacy = providers_raw.get("providers")
    if isinstance(legacy, list):
        for entry in legacy:
            pid = entry.get("id") if isinstance(entry, dict) else None
            if pid:
                out[pid] = entry
    return out


async def _get_opencode_providers_async() -> dict:
    """Build the merged provider catalog reported to the Settings UI."""
    server = await _opencode_get_server()
    if server is None:
        return {"ok": False, "message": "OpenCode is disabled in V2Config"}

    request_loop = asyncio.get_running_loop()
    try:
        providers_raw, auth_raw, config_raw = await asyncio.gather(
            server.get_providers(),
            server.get_provider_auth(),
            server.get_available_models(os.path.expanduser("~")),
            return_exceptions=False,
        )
    finally:
        await server.close_http_session(loop=request_loop)

    all_providers = _coerce_opencode_provider_catalog(providers_raw)

    connected = providers_raw.get("connected") if isinstance(providers_raw, dict) else None
    connected_set = {pid for pid in connected if isinstance(pid, str)} if isinstance(connected, list) else set()

    model_index: dict = {}
    if isinstance(config_raw, dict):
        for entry in config_raw.get("providers", []) or []:
            pid = entry.get("id") if isinstance(entry, dict) else None
            if pid:
                model_index[pid] = entry

    auth_index = auth_raw if isinstance(auth_raw, dict) else {}

    # Resolve the user-configured default provider. ``None`` means
    # the user has not picked one — the UI surfaces that as "no
    # default selected" so clicking a provider actually persists the
    # choice. Previously we fell back to ``"anthropic"`` here, which
    # made the UI render Anthropic as already-selected, and clicking
    # it became a no-op (no state change to persist), silently
    # blocking users from picking Anthropic explicitly.
    default_provider: str | None = None
    try:
        config = load_config()
        cfg = getattr(getattr(config, "agents", None), "opencode", None)
        configured_default = getattr(cfg, "default_provider", None)
        if isinstance(configured_default, str) and configured_default.strip():
            default_provider = configured_default.strip()
    except Exception:
        pass

    # Pre-load the user-config provider overrides once so we can attach
    # them to each row without re-parsing the JSON file per provider.
    try:
        from vibe.opencode_config import load_first_opencode_user_config

        opencode_probe = await asyncio.to_thread(
            load_first_opencode_user_config, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("Could not read opencode.json for baseURL pre-population: %s", exc)
        opencode_probe = None
    base_url_index: dict = {}
    user_model_index: dict[str, dict[str, dict]] = {}
    custom_provider_index: dict[str, dict] = {}
    config_api_key_mask_index: dict[str, str] = {}
    if opencode_probe is not None and isinstance(opencode_probe.config, dict):
        provider_block = opencode_probe.config.get("provider")
        if isinstance(provider_block, dict):
            for pid_key, pid_config in provider_block.items():
                if not isinstance(pid_config, dict):
                    continue
                try:
                    from vibe.opencode_config import get_opencode_custom_provider_adapter

                    custom_adapter = get_opencode_custom_provider_adapter(pid_key, pid_config)
                except Exception:
                    custom_adapter = None
                if custom_adapter is not None:
                    custom_provider_index[pid_key] = pid_config
                models = pid_config.get("models")
                if isinstance(models, dict):
                    user_model_index[pid_key] = {
                        model_id: model_info
                        for model_id, model_info in models.items()
                        if isinstance(model_id, str) and isinstance(model_info, dict)
                    }
                options = pid_config.get("options")
                if not isinstance(options, dict):
                    continue
                candidate = options.get("baseURL")
                if isinstance(candidate, str) and candidate.strip():
                    base_url_index[pid_key] = candidate.strip()
                api_key = options.get("apiKey")
                if isinstance(api_key, str) and api_key.strip():
                    masked = _mask_api_key(api_key.strip())
                    if masked:
                        config_api_key_mask_index[pid_key] = masked
    for pid_key, pid_config in custom_provider_index.items():
        all_providers.setdefault(pid_key, _custom_opencode_provider_row(pid_key, pid_config))

    # Per-provider stored credentials, masked server-side so the
    # Settings UI can show a masked preview without leaking plaintext.
    # API keys managed by avibe live in opencode.json provider
    # options; auth.json is reserved for OpenCode's own connect/OAuth
    # flows and older installs that have not been repaired yet.
    try:
        from vibe.opencode_config import read_opencode_provider_auth_entries

        auth_entries = await asyncio.to_thread(
            read_opencode_provider_auth_entries, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("Could not read OpenCode auth.json for masked keys: %s", exc)
        auth_entries = {}
    api_key_mask_index: dict = {}
    active_auth_type_index: dict = {}
    for pid_key, entry in auth_entries.items():
        entry_type = entry.get("type") if isinstance(entry, dict) else None
        if entry_type == "api":
            raw_key = entry.get("key") if isinstance(entry, dict) else None
            masked = _mask_api_key(raw_key) if raw_key else None
            if masked:
                api_key_mask_index[pid_key] = masked
            active_auth_type_index[pid_key] = "api"
        elif entry_type == "oauth":
            active_auth_type_index[pid_key] = "oauth"
        elif entry_type:
            active_auth_type_index[pid_key] = entry_type
    auth_file_provider_set: set = set(auth_entries.keys())
    config_api_key_provider_set: set = set(config_api_key_mask_index.keys())
    configured_provider_set = (
        auth_file_provider_set
        | config_api_key_provider_set
        | set(custom_provider_index.keys())
    )
    for pid_key, masked in config_api_key_mask_index.items():
        api_key_mask_index[pid_key] = masked
        active_auth_type_index[pid_key] = "api"

    out_providers = []
    for pid, entry in all_providers.items():
        if not isinstance(entry, dict):
            continue
        auth_methods = auth_index.get(pid)
        auth_methods_list = auth_methods if isinstance(auth_methods, list) else []
        oauth_available = any(
            isinstance(method, dict) and method.get("type") == "oauth"
            for method in auth_methods_list
        )
        local = _is_local_provider(pid, auth_methods_list)
        custom_config = custom_provider_index.get(pid, {})
        custom_meta = custom_config.get("vibe_remote")
        custom = pid in custom_provider_index
        if isinstance(custom_meta, dict):
            adapter = custom_meta.get("adapter")
        else:
            try:
                from vibe.opencode_config import get_opencode_custom_provider_adapter

                adapter = get_opencode_custom_provider_adapter(pid, custom_config)
            except Exception:
                adapter = None
        # Authoritative source for the "configured" badge:
        # - If auth.json carries an entry → configured (user explicitly
        #   set it up, even if OpenCode's cache hasn't caught up yet).
        # - Custom providers are configured by their opencode.json block; API
        #   keys are optional for local OpenAI-compatible endpoints.
        # - If auth.json is empty AND ``connected`` lists it → configured
        #   only when ``local`` (Ollama / LM Studio don't need keys).
        #   Otherwise treat ``connected`` as stale — the user just
        #   removed the key and the daemon hasn't restarted yet.
        if pid in configured_provider_set:
            configured = True
        elif local and pid in connected_set:
            configured = True
        else:
            configured = False
        models_for_provider = model_index.get(pid, {})
        provider_models = models_for_provider.get("models")
        user_models = user_model_index.get(pid, {})
        if isinstance(provider_models, dict):
            merged_models = {**provider_models, **user_models}
            model_ids = sorted(merged_models.keys())
        elif isinstance(provider_models, list):
            model_ids = [m.get("id") for m in provider_models if isinstance(m, dict) and m.get("id")]
            for user_model_id in user_models:
                if user_model_id not in model_ids:
                    model_ids.append(user_model_id)
            model_ids = sorted(model_ids)
        else:
            model_ids = sorted(user_models.keys())
        model_entries = []
        for model_id in model_ids:
            model_info = user_models.get(model_id)
            user_managed = (
                _is_opencode_user_model(model_id, model_info)
                if isinstance(model_id, str) and isinstance(model_info, dict)
                else False
            )
            variants = model_info.get("variants") if isinstance(model_info, dict) else None
            reasoning_efforts = sorted(variants.keys()) if isinstance(variants, dict) else []
            model_entries.append(
                {
                    "id": model_id,
                    "user_managed": user_managed,
                    "reasoning_efforts": reasoning_efforts,
                }
            )
        default_model = None
        defaults_block = config_raw.get("default") if isinstance(config_raw, dict) else None
        if isinstance(defaults_block, dict):
            raw_default = defaults_block.get(pid)
            if isinstance(raw_default, str):
                default_model = raw_default

        out_providers.append(
            {
                "id": pid,
                "name": entry.get("name") or pid,
                "description": entry.get("description") or "",
                "configured": configured,
                "has_auth": pid in auth_file_provider_set or pid in config_api_key_provider_set,
                "oauth_available": oauth_available,
                "local": local,
                "custom": custom,
                "adapter": adapter if isinstance(adapter, str) else None,
                "models": model_ids,
                "model_entries": model_entries,
                "default_model": default_model,
                "base_url": base_url_index.get(pid),
                "api_key_masked": api_key_mask_index.get(pid),
                # ``api`` / ``oauth`` / null — the type the daemon will
                # actually use at launch. Lets the UI badge the right
                # source for dual-mode providers (e.g. openai supports
                # both, but only one entry lives in auth.json at a time).
                "active_auth_type": active_auth_type_index.get(pid),
            }
        )

    out_providers.sort(key=lambda p: (not p["configured"], p["local"], p["id"]))

    # Surface the current ``permission`` setting from opencode.json so the
    # Settings page can hide the "Allow tool calls" affordance once it's
    # already ``allow`` — and strengthen the copy when it isn't, since a
    # missing/blocking setting silently makes every tool call wait for an
    # approval prompt that avibe can't reply to.
    permission_allowed = opencode_permission_allowed(opencode_probe)

    return {
        "ok": True,
        "providers": out_providers,
        "default_provider": default_provider,
        "permission_allowed": permission_allowed,
    }


def get_opencode_providers() -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(get_opencode_providers_async())


async def get_opencode_providers_async() -> dict:
    """Return the OpenCode provider catalog."""
    try:
        return await _get_opencode_providers_async()
    except Exception as exc:
        logger.warning("OpenCode providers fetch failed: %s", exc, exc_info=True)
        return {"ok": False, "message": str(exc)}


def _opencode_provider_models_loaded(catalog: dict, provider_id: str) -> bool:
    if not isinstance(catalog, dict) or catalog.get("ok") is False:
        return False
    providers = catalog.get("providers")
    if not isinstance(providers, list):
        return False
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        if provider.get("id") != provider_id:
            continue
        models = provider.get("models")
        return isinstance(models, list) and len(models) > 0
    return False


def _opencode_provider_present(catalog: dict, provider_id: str) -> bool:
    if not isinstance(catalog, dict) or catalog.get("ok") is False:
        return False
    providers = catalog.get("providers")
    if not isinstance(providers, list):
        return False
    return any(
        isinstance(provider, dict) and provider.get("id") == provider_id
        for provider in providers
    )


async def _refresh_opencode_provider_catalog_async(provider_id: str, *, require_models: bool = True) -> dict:
    """Fetch the provider catalog after an auth/config write.

    OpenCode keeps provider/auth state in the running daemon. After saving a
    key or base URL we restart/refresh the daemon and then force a fresh catalog
    read so Settings and Agents do not keep showing the pre-save empty model
    list until the generic options cache expires.
    """

    pid = provider_id.strip()
    last_catalog: dict | None = None
    for attempt in range(_OPENCODE_PROVIDER_REFRESH_ATTEMPTS):
        if attempt > 0:
            await asyncio.sleep(_OPENCODE_PROVIDER_REFRESH_DELAY_SECONDS)
        catalog = await get_opencode_providers_async()
        if isinstance(catalog, dict):
            last_catalog = catalog
        loaded = (
            _opencode_provider_models_loaded(catalog, pid)
            if require_models
            else _opencode_provider_present(catalog, pid)
        )
        if loaded:
            return {"ok": True, "provider_id": pid, "catalog": catalog}
    message = (
        "Provider saved, but model catalog has not refreshed yet"
        if require_models
        else "Provider saved, but provider catalog has not refreshed yet"
    )
    if isinstance(last_catalog, dict) and last_catalog.get("message"):
        message = str(last_catalog.get("message"))
    return {
        "ok": False,
        "provider_id": pid,
        "message": message,
        "catalog": last_catalog,
    }


def save_opencode_provider_model(provider_id: str, payload: dict) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(save_opencode_provider_model_async(provider_id, payload))


def _normalize_custom_provider_payload(payload: dict) -> tuple[str, str, str, str, str | None]:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be an object")
    provider_id = payload.get("provider_id")
    name = payload.get("name")
    adapter = payload.get("adapter")
    base_url = payload.get("base_url")
    api_key = payload.get("api_key")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id is required")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name is required")
    if not isinstance(adapter, str) or not adapter.strip():
        raise ValueError("adapter is required")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url is required")
    if api_key is not None and not isinstance(api_key, str):
        raise ValueError("api_key must be a string")
    api_key_value = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    return provider_id.strip(), name.strip(), adapter.strip(), base_url.strip(), api_key_value


async def save_opencode_custom_provider_async(payload: dict) -> dict:
    try:
        provider_id, name, adapter, base_url, api_key = _normalize_custom_provider_payload(payload)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}

    try:
        from vibe.opencode_config import is_reserved_opencode_provider_id

        if is_reserved_opencode_provider_id(provider_id):
            return {"ok": False, "message": "provider_id already exists"}
    except Exception as exc:
        logger.debug("OpenCode reserved provider check failed for %s: %s", provider_id, exc)

    try:
        providers = await _get_opencode_providers_async()
    except Exception as exc:
        logger.warning("OpenCode provider catalog fetch failed during custom provider save: %s", exc, exc_info=True)
        return {"ok": False, "message": "provider catalog is unavailable"}
    if not isinstance(providers, dict) or providers.get("ok") is False:
        message = providers.get("message") if isinstance(providers, dict) else None
        return {"ok": False, "message": message or "provider catalog is unavailable"}
    existing_providers = providers.get("providers") if isinstance(providers, dict) else []
    if not isinstance(existing_providers, list):
        return {"ok": False, "message": "provider catalog is unavailable"}
    if isinstance(existing_providers, list):
        normalized_id = provider_id.strip().lower()
        normalized_name = name.strip().lower()
        for provider in existing_providers:
            if not isinstance(provider, dict):
                continue
            existing_id = str(provider.get("id") or "").lower()
            existing_name = str(provider.get("name") or "").lower()
            if existing_id == normalized_id:
                if provider.get("custom") is not True:
                    return {"ok": False, "message": "provider_id already exists"}
            if existing_name and existing_name == normalized_name and existing_id != normalized_id:
                return {"ok": False, "message": "provider name already exists"}

    try:
        from vibe.opencode_config import upsert_opencode_custom_provider

        await asyncio.to_thread(
            upsert_opencode_custom_provider,
            provider_id,
            name,
            adapter,
            base_url,
            logger_instance=logger,
        )
    except Exception as exc:
        logger.warning("OpenCode custom provider save failed for %s: %s", provider_id, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    _OPENCODE_OPTIONS_CACHE.clear()
    try:
        restart = restart_backend("opencode")
    except Exception as exc:
        restart = {"ok": False, "message": str(exc)}
    _OPENCODE_OPTIONS_CACHE.clear()

    if api_key:
        auth_result = await _save_opencode_provider_auth_async(
            provider_id.strip().lower(),
            None,
            _BASE_URL_UNCHANGED,
            config_api_key=api_key,
        )
        _OPENCODE_OPTIONS_CACHE.clear()
        try:
            restart = restart_backend("opencode")
        except Exception as exc:
            restart = {"ok": False, "message": str(exc)}
        _OPENCODE_OPTIONS_CACHE.clear()
        if not auth_result.get("ok"):
            return {
                "ok": False,
                "provider_id": provider_id.strip().lower(),
                "message": auth_result.get("message") or "Provider saved, but API key save failed",
                "restart": restart,
            }

    catalog_refresh = await _refresh_opencode_provider_catalog_async(
        provider_id.strip().lower(),
        require_models=False,
    )
    return {
        "ok": True,
        "provider_id": provider_id.strip().lower(),
        "restart": restart,
        "catalog_refresh": catalog_refresh,
    }


def save_opencode_custom_provider(payload: dict) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(save_opencode_custom_provider_async(payload))


async def delete_opencode_custom_provider_async(provider_id: str) -> dict:
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {"ok": False, "message": "provider_id is required"}
    pid = provider_id.strip().lower()
    try:
        from vibe.opencode_config import (
            is_opencode_custom_provider,
            read_opencode_provider_auth_entries,
            remove_opencode_custom_provider,
        )

        custom = await asyncio.to_thread(
            is_opencode_custom_provider,
            pid,
            logger_instance=logger,
        )
        if not custom:
            return {"ok": False, "message": "Only custom providers can be removed"}
        auth_entries = await asyncio.to_thread(
            read_opencode_provider_auth_entries,
            logger_instance=logger,
        )
        if pid in auth_entries:
            auth_result = await _delete_opencode_provider_auth_async(pid)
            if not auth_result.get("ok"):
                return {
                    "ok": False,
                    "provider_id": pid,
                    "message": auth_result.get("message") or "Provider auth removal failed",
                }
        await asyncio.to_thread(
            remove_opencode_custom_provider,
            pid,
            logger_instance=logger,
        )
    except Exception as exc:
        logger.warning("OpenCode custom provider delete failed for %s: %s", pid, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    try:
        _clear_opencode_default_provider_if(pid)
    except Exception as exc:
        logger.warning(
            "Failed to revalidate opencode.default_provider after custom provider delete for %s: %s",
            pid,
            exc,
        )

    _OPENCODE_OPTIONS_CACHE.clear()
    try:
        restart = restart_backend("opencode")
    except Exception as exc:
        restart = {"ok": False, "message": str(exc)}
    _OPENCODE_OPTIONS_CACHE.clear()
    return {"ok": True, "provider_id": pid, "restart": restart}


def delete_opencode_custom_provider(provider_id: str) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(delete_opencode_custom_provider_async(provider_id))


async def save_opencode_provider_model_async(provider_id: str, payload: dict) -> dict:
    """Add or update a user-managed OpenCode model under one provider."""

    if not isinstance(provider_id, str) or not provider_id.strip():
        return {"ok": False, "message": "provider_id is required"}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Payload must be an object"}
    raw_model_id = payload.get("model_id")
    if not isinstance(raw_model_id, str) or not raw_model_id.strip():
        return {"ok": False, "message": "model_id is required"}
    model_id = raw_model_id.strip()
    pid = provider_id.strip()
    if model_id.lower().startswith(f"{pid.lower()}/"):
        return {"ok": False, "message": "model_id must not include a provider prefix"}

    reasoning_efforts = payload.get("reasoning_efforts", [])

    # Prevent duplicates against both OpenCode's live catalog and the
    # user-managed overlay. Updating an existing user-managed model is
    # allowed; adding a duplicate built-in model is not.
    existing_user_models: dict = {}
    try:
        from vibe.opencode_config import read_opencode_provider_user_models

        existing_user_models = await asyncio.to_thread(
            read_opencode_provider_user_models, pid, logger_instance=logger
        )
    except Exception as exc:
        logger.debug("OpenCode user-model lookup failed for %s: %s", pid, exc)

    server = await _opencode_get_server()
    request_loop = asyncio.get_running_loop()
    try:
        if server is not None:
            try:
                config_raw = await server.get_available_models(os.path.expanduser("~"))
            except Exception as exc:
                logger.warning(
                    "OpenCode provider model catalog fetch failed for %s/%s: %s",
                    pid,
                    model_id,
                    exc,
                    exc_info=True,
                )
                return {"ok": False, "message": str(exc)}
            if not isinstance(config_raw, dict):
                return {"ok": False, "message": "provider model catalog is unavailable"}
            custom_provider_ids = await _read_opencode_custom_provider_ids()
            model_index = {}
            provider_found = False
            for entry in config_raw.get("providers", []) or []:
                entry_pid = entry.get("id") if isinstance(entry, dict) else None
                if entry_pid == pid:
                    provider_found = True
                    model_index = _opencode_provider_model_ids(entry)
                    break
            if not provider_found:
                if pid not in custom_provider_ids:
                    return {"ok": False, "message": "provider model catalog is unavailable"}
            if model_id in model_index and model_id not in existing_user_models:
                return {"ok": False, "message": "model_id already exists"}
    finally:
        if server is not None:
            await server.close_http_session(loop=request_loop)

    try:
        from vibe.opencode_config import upsert_opencode_provider_model

        await asyncio.to_thread(
            upsert_opencode_provider_model,
            pid,
            model_id,
            reasoning_efforts=reasoning_efforts,
            logger_instance=logger,
        )
    except Exception as exc:
        logger.warning("OpenCode provider model save failed for %s/%s: %s", pid, model_id, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    _OPENCODE_OPTIONS_CACHE.clear()
    try:
        restart = restart_backend("opencode")
    except Exception as exc:
        restart = {"ok": False, "message": str(exc)}
    _OPENCODE_OPTIONS_CACHE.clear()
    return {"ok": True, "provider_id": pid, "model_id": model_id, "restart": restart}


def delete_opencode_provider_model(provider_id: str, model_id: str) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(delete_opencode_provider_model_async(provider_id, model_id))


async def delete_opencode_provider_model_async(provider_id: str, model_id: str) -> dict:
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {"ok": False, "message": "provider_id is required"}
    if not isinstance(model_id, str) or not model_id.strip():
        return {"ok": False, "message": "model_id is required"}
    pid = provider_id.strip()
    mid = model_id.strip()

    try:
        from vibe.opencode_config import read_opencode_provider_user_models, remove_opencode_provider_model

        user_models = await asyncio.to_thread(
            read_opencode_provider_user_models, pid, logger_instance=logger
        )
        if mid not in user_models:
            return {"ok": False, "message": "Only user-managed models can be removed"}
        await asyncio.to_thread(
            remove_opencode_provider_model,
            pid,
            mid,
            logger_instance=logger,
        )
    except Exception as exc:
        logger.warning("OpenCode provider model delete failed for %s/%s: %s", pid, mid, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    _OPENCODE_OPTIONS_CACHE.clear()
    try:
        restart = restart_backend("opencode")
    except Exception as exc:
        restart = {"ok": False, "message": str(exc)}
    _OPENCODE_OPTIONS_CACHE.clear()
    return {"ok": True, "provider_id": pid, "model_id": mid, "restart": restart}


# Sentinel used by ``save_opencode_provider_auth`` to distinguish three
# states of the optional ``base_url`` field:
#   * key absent from payload      → ``_BASE_URL_UNCHANGED`` (no-op)
#   * key present, value blank     → ``None``                (clear stored)
#   * key present, value non-blank → ``str``                 (upsert)
# Without this, a payload like ``{"api_key": "..."}`` (re-saving just
# the API key) would silently wipe the stored ``baseURL`` because the
# server cannot tell "omitted" from "explicitly empty".
_BASE_URL_UNCHANGED: object = object()


async def _save_opencode_provider_auth_async(
    provider_id: str,
    api_key: str | None,
    base_url: Any = _BASE_URL_UNCHANGED,
    *,
    config_api_key: str | None = None,
) -> dict:
    from vibe.opencode_config import (
        remove_opencode_provider_base_url,
        read_opencode_provider_auth_entries,
        upsert_opencode_provider_api_key,
        upsert_opencode_provider_base_url,
    )

    # Treat API keys entered through avibe's Settings UI as OpenCode
    # config provider options, not OpenCode daemon auth entries. The
    # daemon's /auth store is still used by OpenCode's own connect/OAuth
    # flow, but provider options are the source OpenCode-compatible
    # providers consistently use at invocation time.
    config_key = config_api_key or api_key
    if config_key:
        try:
            await asyncio.to_thread(
                upsert_opencode_provider_api_key,
                provider_id,
                config_key,
                logger_instance=logger,
            )
        except Exception as exc:
            logger.warning(
                "OpenCode provider apiKey mirror persist failed for %s: %s",
                provider_id,
                exc,
                exc_info=True,
            )
            return {
                "ok": False,
                "message": (
                    "Provider credential persistence failed: "
                    f"{exc}"
                ),
            }

        try:
            auth_entries = await asyncio.to_thread(
                read_opencode_provider_auth_entries,
                logger_instance=logger,
            )
            if provider_id in auth_entries:
                auth_cleanup = await _delete_opencode_provider_auth_async(provider_id)
                if not auth_cleanup.get("ok"):
                    return {
                        "ok": False,
                        "message": auth_cleanup.get("message")
                        or "API key saved, but stale OpenCode auth cleanup failed",
                    }
        except Exception as exc:
            logger.warning(
                "OpenCode stale auth cleanup failed for %s: %s",
                provider_id,
                exc,
                exc_info=True,
            )
            return {
                "ok": False,
                "message": (
                    "API key saved, but stale OpenCode auth cleanup failed: "
                    f"{exc}"
                ),
            }

    # ``baseURL`` is different: OpenCode's auth endpoint has no field for
    # it, so this write is the *only* place it gets persisted. A silent
    # failure would surface as "save success, value lost on reload" — the
    # exact UX bug Codex flagged. Surface those errors to the caller so
    # the UI can show a useful message.
    if base_url is _BASE_URL_UNCHANGED:
        return {"ok": True}

    try:
        if base_url:
            await asyncio.to_thread(
                upsert_opencode_provider_base_url,
                provider_id,
                base_url,
                logger_instance=logger,
            )
        else:
            await asyncio.to_thread(
                remove_opencode_provider_base_url,
                provider_id,
                logger_instance=logger,
            )
    except Exception as exc:
        logger.warning(
            "OpenCode base_url persist failed for %s: %s", provider_id, exc, exc_info=True
        )
        return {
            "ok": False,
            "message": (
                "API key saved, but base URL persistence failed: "
                f"{exc}"
            ),
        }
    return {"ok": True}


def save_opencode_provider_auth(provider_id: str, payload: dict) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(save_opencode_provider_auth_async(provider_id, payload))


async def save_opencode_provider_auth_async(provider_id: str, payload: dict) -> dict:
    """Persist a single OpenCode provider's API key (and optional base URL).

    The API key is persisted into ``opencode.json`` under
    ``provider.<id>.options.apiKey``. ``auth.json`` remains OpenCode's
    own connect/OAuth store and is touched only through the OpenCode auth
    API when we need to clear a conflicting entry. The optional
    ``base_url`` override is also persisted into ``opencode.json``.

    ``base_url`` field semantics in the payload:
      * absent              → leave the stored value untouched
      * empty / whitespace  → clear the stored value
      * non-empty string    → upsert (must start with http:// or https://)
    """
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {"ok": False, "message": "provider_id is required"}
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Payload must be an object"}
    raw_key = payload.get("api_key")
    # ``api_key`` is optional when the provider is already configured: the
    # UI's "Replace" flow hides the plaintext and only sends ``base_url``
    # for relay-URL fixes. Without this, base-URL-only edits fail unless
    # the user retypes the secret. We detect "already configured" by
    # consulting provider.<id>.options.apiKey in opencode.json first,
    # then falling back to OpenCode auth.json only to repair older
    # split-store installs. This mirrors the Settings catalog badge, so a
    # masked key shown in the UI can always save a base URL without
    # requiring replacement.
    api_key: str | None = raw_key.strip() if isinstance(raw_key, str) and raw_key.strip() else None
    has_existing_key = False
    existing_api_key: str | None = None
    if api_key is None:
        try:
            existing_api_key = await _read_opencode_config_api_key(provider_id.strip())
            if existing_api_key:
                has_existing_key = True
            if not has_existing_key:
                from vibe.opencode_config import read_opencode_provider_keys

                existing_keys = read_opencode_provider_keys(logger_instance=logger)
                maybe_existing_key = existing_keys.get(provider_id.strip())
                if isinstance(maybe_existing_key, str) and maybe_existing_key:
                    has_existing_key = True
                    existing_api_key = maybe_existing_key
            if not has_existing_key:
                config_key_ids = await _read_opencode_config_api_key_provider_ids()
                has_existing_key = provider_id.strip() in config_key_ids
            if not has_existing_key:
                custom_provider_ids = await _read_opencode_custom_provider_ids()
                has_existing_key = provider_id.strip() in custom_provider_ids
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "OpenCode auth lookup failed during base-url-only save for %s: %s",
                provider_id,
                exc,
            )
            has_existing_key = False
        if not has_existing_key:
            return {"ok": False, "message": "api_key is required"}

    base_url: Any = _BASE_URL_UNCHANGED
    if "base_url" in payload:
        raw_base_url = payload.get("base_url")
        if raw_base_url is None:
            base_url = None
        elif isinstance(raw_base_url, str):
            candidate = raw_base_url.strip()
            if not candidate:
                base_url = None
            else:
                if not candidate.lower().startswith(("http://", "https://")):
                    return {
                        "ok": False,
                        "message": "base_url must start with http:// or https://",
                    }
                base_url = candidate
        else:
            return {"ok": False, "message": "base_url must be a string"}
        if base_url is None:
            custom_provider_ids = await _read_opencode_custom_provider_ids()
            if provider_id.strip() in custom_provider_ids:
                return {"ok": False, "message": "base_url is required for custom providers"}

    try:
        result = await _save_opencode_provider_auth_async(
            provider_id.strip(),
            None,
            base_url,
            config_api_key=api_key or existing_api_key,
        )
    except Exception as exc:
        logger.warning("OpenCode set-auth failed for %s: %s", provider_id, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    if result.get("ok"):
        _OPENCODE_OPTIONS_CACHE.clear()

    # Ask the live controller to refresh the OpenCode server so the
    # daemon's in-memory ``connected`` cache picks up the new auth.
    # Without this, ``GET /provider`` keeps returning the pre-save
    # state until OpenCode restarts on its own (typically next idle
    # cleanup cycle). The restart is best-effort: we report it under a
    # separate ``restart`` key so the UI can show "saved, but daemon
    # refresh failed" when applicable.
    try:
        result["restart"] = restart_backend("opencode")
    except Exception as exc:
        logger.warning("OpenCode auto-restart after save failed for %s: %s", provider_id, exc)
        result["restart"] = {"ok": False, "message": str(exc)}
    if result.get("ok"):
        result["catalog_refresh"] = await _refresh_opencode_provider_catalog_async(provider_id.strip())
    return result


async def _delete_opencode_provider_auth_async(provider_id: str) -> dict:
    server = await _opencode_get_server()
    if server is None:
        return {"ok": False, "message": "OpenCode is disabled in V2Config"}
    request_loop = asyncio.get_running_loop()
    try:
        await server.remove_provider_auth(provider_id)
    finally:
        await server.close_http_session(loop=request_loop)
    return {"ok": True}


def _clear_opencode_default_provider_if(provider_id: str) -> None:
    """Clear the saved OpenCode default if it points at ``provider_id``."""

    with CONFIG_LOCK:
        try:
            cfg = load_config()
        except FileNotFoundError:
            return
        opencode_cfg = getattr(getattr(cfg, "agents", None), "opencode", None)
        current_default = getattr(opencode_cfg, "default_provider", None)
        if isinstance(current_default, str) and current_default.strip() == provider_id:
            opencode_cfg.default_provider = None
            cfg.save()
            logger.info(
                "clear_opencode_default_provider: cleared default_provider after removing %s",
                provider_id,
            )


def delete_opencode_provider_auth(provider_id: str) -> dict:
    from vibe.async_bridge import run_coroutine_blocking

    return run_coroutine_blocking(delete_opencode_provider_auth_async(provider_id))


async def delete_opencode_provider_auth_async(provider_id: str) -> dict:
    """Drop a single provider's stored credentials.

    Same restart pattern as save: the daemon caches ``connected`` at
    startup, so a fresh DELETE on ``/auth/<id>`` doesn't flip the
    runtime state until the daemon restarts. We trigger
    ``restart_backend("opencode")`` so the UI's next refresh reflects
    reality. The restart status comes back under ``restart`` so the
    page can warn on "removed, but daemon refresh failed".

    If the deleted provider is still recorded as
    ``agents.opencode.default_provider``, clear the saved default too.
    ``OpenCodeAgent`` injects that ``providerID`` for bare model IDs,
    so leaving a stale default behind makes every subsequent request
    target an unconfigured provider and fail.
    """
    if not isinstance(provider_id, str) or not provider_id.strip():
        return {"ok": False, "message": "provider_id is required"}
    pid = provider_id.strip()
    try:
        from vibe.opencode_config import (
            read_opencode_provider_auth_entries,
            remove_opencode_provider_api_key,
        )

        auth_entries = await asyncio.to_thread(
            read_opencode_provider_auth_entries,
            logger_instance=logger,
        )
        config_api_key_provider_ids = await _read_opencode_config_api_key_provider_ids()
        result = {"ok": True}
        removed_auth = False
        if pid in auth_entries:
            result = await _delete_opencode_provider_auth_async(pid)
            removed_auth = bool(result.get("ok"))
        if pid in config_api_key_provider_ids:
            await asyncio.to_thread(
                remove_opencode_provider_api_key,
                pid,
                logger_instance=logger,
            )
            removed_auth = True
    except Exception as exc:
        logger.warning("OpenCode delete-auth failed for %s: %s", provider_id, exc, exc_info=True)
        return {"ok": False, "message": str(exc)}

    # Revalidate the saved default. Best-effort: a V2Config write
    # failure here is non-fatal because the daemon already dropped the
    # credential, but log it so the user can investigate if the
    # default sticks around after a "Remove key" click.
    if removed_auth:
        _OPENCODE_OPTIONS_CACHE.clear()
        try:
            _clear_opencode_default_provider_if(pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to revalidate opencode.default_provider after delete for %s: %s",
                pid,
                exc,
            )

    try:
        result["restart"] = restart_backend("opencode")
    except Exception as exc:
        logger.warning("OpenCode auto-restart after delete failed for %s: %s", provider_id, exc)
        result["restart"] = {"ok": False, "message": str(exc)}
    return result


def set_opencode_default_provider(payload: dict) -> dict:
    """Persist ``V2Config.agents.opencode.default_provider``.

    No daemon contact required — OpenCode itself accepts a per-request
    ``provider`` field on messages, so the "default" is purely our
    routing concern. Storing it in V2Config keeps the chip and the
    routing layer in sync across restarts.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "message": "Payload must be an object"}
    raw = payload.get("provider_id")
    if not isinstance(raw, str) or not raw.strip():
        return {"ok": False, "message": "provider_id is required"}
    provider_id = raw.strip()

    with CONFIG_LOCK:
        try:
            config = load_config()
        except FileNotFoundError:
            config = V2Config()
        config.agents.opencode.default_provider = provider_id
        config.save()
    return {"ok": True, "default_provider": provider_id}


def codex_models() -> dict:
    """Best-effort merged list of Codex model options.

    Codex CLI does not expose a stable `list models` command.
    We merge suggestions from:
    - Built-in known model ids
    - ~/.codex/models_cache.json (maintained by Codex CLI)
    - ~/.codex/config.toml (user-selected model and migration hints)
    """

    def _append_unique(options: list[str], seen: set[str], value: object) -> None:
        if not isinstance(value, str):
            return
        model = value.strip()
        if not model or model in seen:
            return
        seen.add(model)
        options.append(model)

    def _result(model_options: list[str]) -> dict:
        # Codex reasoning-effort levels are static (model-independent). Surface
        # them per-model so the shape matches claude_models(), letting a single
        # caller (agent_model_options / the UI) treat both backends uniformly.
        from modules.agents.opencode.utils import build_codex_reasoning_options

        codex_reasoning = build_codex_reasoning_options()
        reasoning_options = {"": codex_reasoning}
        for model_id in model_options:
            reasoning_options[model_id] = codex_reasoning
        return {"ok": True, "models": model_options, "reasoning_options": reasoning_options}

    built_in_options: list[str] = [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.2-codex",
        "gpt-5.2",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.1",
        "gpt-5",
    ]

    options: list[str] = []
    seen: set[str] = set()
    codex_home = Path.home() / ".codex"
    models_cache_path = codex_home / "models_cache.json"
    config_path = codex_home / "config.toml"

    for model in built_in_options:
        _append_unique(options, seen, model)

    try:
        if models_cache_path.exists() and models_cache_path.is_file():
            cache_data = json.loads(models_cache_path.read_text(encoding="utf-8"))
            models = cache_data.get("models")
            if isinstance(models, list):
                visible_models: list[tuple[int, int, str]] = []
                for index, item in enumerate(models):
                    if not isinstance(item, dict):
                        continue
                    slug = item.get("slug")
                    if not isinstance(slug, str) or not slug.strip():
                        continue
                    priority = item.get("priority")
                    if not isinstance(priority, int):
                        priority = 10**9
                    visible_models.append((priority, index, slug.strip()))

                for _, _, slug in sorted(visible_models):
                    _append_unique(options, seen, slug)
    except Exception as exc:
        logger.warning("Failed to read Codex models_cache.json: %s", exc, exc_info=True)

    try:
        if config_path.exists() and config_path.is_file():
            try:
                import tomllib  # py3.11+
            except Exception:  # pragma: no cover
                tomllib = None

            if tomllib is None:
                return _result(options)

            data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _append_unique(options, seen, data.get("model"))
                notice = data.get("notice")
                if isinstance(notice, dict):
                    migrations = notice.get("model_migrations")
                    if isinstance(migrations, dict):
                        for k, v in migrations.items():
                            _append_unique(options, seen, k)
                            _append_unique(options, seen, v)
    except Exception as exc:
        logger.warning("Failed to read Codex config.toml: %s", exc, exc_info=True)

    return _result(options)


def _lark_api_base(domain: str = "feishu") -> str:
    """Return the API base URL for the given Lark/Feishu domain."""
    if domain == "lark":
        return "https://open.larksuite.com"
    return "https://open.feishu.cn"


def _lark_tenant_token(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
    proxy_url: str | None = None,
) -> Optional[str]:
    import urllib.request

    from vibe.proxy import is_socks_proxy

    url = f"{_lark_api_base(domain)}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    headers = {"Content-Type": "application/json"}

    if proxy_url and is_socks_proxy(proxy_url):
        result = _https_json_request_via_socks(
            proxy_url,
            url,
            method="POST",
            body=body,
            headers=headers,
        )
    else:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, data=body, headers=headers)
        with opener.open(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

    if result.get("code") == 0:
        return result.get("tenant_access_token")
    return None


async def _lark_tenant_token_async(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
    proxy_url: str | None = None,
) -> Optional[str]:
    """Get Lark tenant access token (internal helper, not exposed to frontend).

    ``proxy_url`` is honored when set: SOCKS schemes route through
    ``aiohttp_socks``, HTTP schemes use ``urllib.ProxyHandler``. The runtime
    Feishu/Lark adapter still bypasses this because ``lark-oapi`` has no
    proxy hook — that gap is surfaced by the adapter, not here.
    """
    import urllib.request

    from vibe.proxy import is_socks_proxy

    url = f"{_lark_api_base(domain)}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    headers = {"Content-Type": "application/json"}

    if proxy_url and is_socks_proxy(proxy_url):
        result = await _lark_tenant_token_via_aiohttp(url, body, headers, proxy_url)
    else:
        def _request() -> dict:
            if proxy_url:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                )
            else:
                opener = urllib.request.build_opener()
            req = urllib.request.Request(url, data=body, headers=headers)
            with opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        result = await asyncio.to_thread(_request)

    if result.get("code") == 0:
        return result.get("tenant_access_token")
    return None


async def _lark_tenant_token_via_aiohttp(url: str, body: bytes, headers: dict, proxy: str) -> dict:
    import aiohttp
    from aiohttp_socks import ProxyConnector

    connector = ProxyConnector.from_url(proxy, rdns=True)
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.post(url, data=body, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()


def lark_auth_test(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
    proxy_url: str | None = None,
) -> dict:
    app_id = app_id or _stored_platform_field("lark", "app_id")
    app_secret = app_secret or _stored_platform_secret("lark", "app_secret")
    domain = domain or _stored_platform_field("lark", "domain", "feishu")
    from vibe.proxy import resolve_proxy

    proxy = resolve_proxy(proxy_url)
    try:
        token = _lark_tenant_token(app_id, app_secret, domain, proxy_url=proxy)
        if not token:
            return {"ok": False, "error": "Invalid credentials"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


async def lark_auth_test_async(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
    proxy_url: str | None = None,
) -> dict:
    """Test Lark/Feishu app credentials. Only returns ok/error, never exposes token.

    ``proxy_url`` is honored for the auth call itself; the runtime SDK
    (``lark-oapi``) has no proxy hook and bypasses it — that limitation is
    surfaced by ``modules/im/feishu.py`` once at adapter init.
    """
    app_id = app_id or _stored_platform_field("lark", "app_id")
    app_secret = app_secret or _stored_platform_secret("lark", "app_secret")
    domain = domain or _stored_platform_field("lark", "domain", "feishu")
    from vibe.proxy import resolve_proxy

    proxy = resolve_proxy(proxy_url)
    try:
        token = await _lark_tenant_token_async(app_id, app_secret, domain, proxy_url=proxy)
        if not token:
            return {"ok": False, "error": "Invalid credentials"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def lark_list_chats(
    app_id: str,
    app_secret: str,
    domain: str = "feishu",
    force: bool = False,
    include_not_returned: bool = False,
) -> dict:
    from core import chat_discovery

    app_id = app_id or _stored_platform_field("lark", "app_id")
    app_secret = app_secret or _stored_platform_secret("lark", "app_secret")
    domain = domain or _stored_platform_field("lark", "domain", "feishu")
    return chat_discovery.channels_response(
        "lark",
        app_id=app_id,
        app_secret=app_secret,
        domain=domain,
        force=force,
        include_not_returned=include_not_returned,
    )


def lark_list_chats_live(app_id: str, app_secret: str, domain: str = "feishu") -> dict:
    """List Lark/Feishu group chats the bot has joined (with pagination)."""
    import time
    import urllib.error
    import urllib.request

    def _get_with_retry(req: "urllib.request.Request", *, attempts: int = 5) -> dict:
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as http_exc:
                # Retry on rate-limit / transient server errors with backoff.
                if http_exc.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                    retry_after = http_exc.headers.get("Retry-After") if http_exc.headers else None
                    try:
                        wait = max(int(retry_after), 2**attempt) if retry_after else 2**attempt
                    except (TypeError, ValueError):
                        wait = 2**attempt
                    logger.warning(
                        "Lark rate-limited/transient (%d), retrying after %ds (attempt %d/%d)",
                        http_exc.code,
                        wait,
                        attempt + 1,
                        attempts,
                    )
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("Lark request exhausted retries")

    try:
        token = _lark_tenant_token(app_id, app_secret, domain)
        if not token:
            return {"ok": False, "error": "Failed to get access token"}

        base = _lark_api_base(domain)
        channels = []
        page_token = ""
        seen_page_tokens: set = set()
        max_pages = 50  # safety cap to prevent infinite loop
        page = 0
        # `truncated` marks an INCOMPLETE inventory. Any early/abnormal exit while
        # the server still reports more pages must set it, so the caller never
        # marks the unseen chats not_returned from a partial list.
        truncated = False
        while True:
            if page >= max_pages:
                truncated = True
                break
            url = f"{base}/open-apis/im/v1/chats?page_size=100"
            if page_token:
                url = f"{url}&page_token={page_token}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            result = _get_with_retry(req)
            if result.get("code") != 0:
                return {"ok": False, "error": result.get("msg", "Unknown error")}
            data = result.get("data", {})
            items = data.get("items", [])
            channels.extend(
                {
                    "id": c.get("chat_id"),
                    "name": c.get("name"),
                    "chat_type": c.get("chat_type"),
                    "chat_mode": c.get("chat_mode"),
                    "is_private": c.get("chat_type") == "private",
                }
                for c in items
            )
            has_more = bool(data.get("has_more"))
            page_token = data.get("page_token") or ""
            if not has_more:
                break
            if not page_token:
                # Server claims more pages but gave no cursor — cannot continue.
                truncated = True
                break
            if page_token in seen_page_tokens:
                # Server returned a repeated cursor — avoid an infinite loop, but
                # the remaining pages are unreachable, so the list is incomplete.
                truncated = True
                break
            seen_page_tokens.add(page_token)
            page += 1
        return {"ok": True, "channels": channels, "truncated": truncated}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# User and Bind Code management (for admin permission feature)
# ---------------------------------------------------------------------------


def get_users(platform: Optional[str] = None) -> dict:
    """Get all bound users."""
    store = SettingsStore.get_instance()
    platform = platform or _current_platform()
    users = {}
    for user_id, u in store.get_users_for_platform(platform).items():
        users[user_id] = {
            "display_name": u.display_name,
            "is_admin": u.is_admin,
            "bound_at": u.bound_at,
            "enabled": u.enabled,
            "show_message_types": _normalize_show_message_types_for_platform(u.show_message_types, platform),
            "custom_cwd": u.custom_cwd,
            "routing": routing_to_compat_dict(u.routing),
        }
    return {"ok": True, "users": users}


def save_users(payload: dict) -> dict:
    """Save user settings (bulk update from UI)."""
    store = SettingsStore.get_instance()
    platform = payload.get("platform") or _current_platform()

    users = {}
    for user_id, up in (payload.get("users") or {}).items():
        if not isinstance(up, dict):
            continue
        # Preserve dm_chat_id from existing user (not editable via UI)
        existing = store.get_user(user_id, platform=platform)
        users[user_id] = UserSettings(
            display_name=up.get("display_name", ""),
            is_admin=up.get("is_admin", False),
            bound_at=up.get("bound_at", ""),
            enabled=up.get("enabled", True),
            show_message_types=_normalize_show_message_types_for_platform(up.get("show_message_types"), platform),
            custom_cwd=up.get("custom_cwd"),
            routing=_parse_routing(_normalize_backend_routing_payload(up.get("routing") or {})),
            dm_chat_id=existing.dm_chat_id if existing else "",
            pending_bind_menu_hint=existing.pending_bind_menu_hint if existing else False,
        )

    # Merge instead of replace: update existing users and add new ones,
    # but preserve users not included in the payload (e.g. concurrently bound)
    current_users = store.get_users_for_platform(platform)
    for uid, user_settings in users.items():
        current_users[uid] = user_settings
    store.set_users_for_platform(platform, current_users)
    store.save()
    return get_users(platform)


def toggle_admin(user_id: str, is_admin: bool, platform: Optional[str] = None) -> dict:
    """Toggle admin status for a user."""
    store = SettingsStore.get_instance()
    platform = platform or _current_platform()
    if not store.set_admin(user_id, is_admin, platform=platform):
        if not store.is_bound_user(user_id, platform=platform):
            return {"ok": False, "error": "User not found"}
        return {"ok": False, "error": "Failed to update admin status"}
    return {"ok": True}


def remove_user(user_id: str, platform: Optional[str] = None) -> dict:
    """Remove a bound user."""
    store = SettingsStore.get_instance()
    platform = platform or _current_platform()
    user = store.get_user(user_id, platform=platform)
    if user is None:
        return {"ok": False, "error": "User not found"}
    store.remove_user(user_id, platform=platform)
    return {"ok": True}


def get_bind_codes() -> dict:
    """Get all bind codes."""
    store = SettingsStore.get_instance()
    codes = []
    for bc in store.get_bind_codes():
        codes.append(
            {
                "code": bc.code,
                "type": bc.type,
                "created_at": bc.created_at,
                "expires_at": bc.expires_at,
                "is_active": bc.is_active,
                "used_by": bc.used_by,
            }
        )
    return {"ok": True, "bind_codes": codes}


def create_bind_code(code_type: str = "one_time", expires_at: Optional[str] = None) -> dict:
    """Create a new bind code."""
    if code_type not in ("one_time", "expiring"):
        return {"ok": False, "error": "type must be 'one_time' or 'expiring'"}
    if code_type == "expiring" and not expires_at:
        return {"ok": False, "error": "expires_at is required for expiring bind codes"}
    store = SettingsStore.get_instance()
    bc = store.create_bind_code(code_type, expires_at)
    return {
        "ok": True,
        "bind_code": {
            "code": bc.code,
            "type": bc.type,
            "created_at": bc.created_at,
            "expires_at": bc.expires_at,
            "is_active": bc.is_active,
        },
    }


def delete_bind_code(code: str) -> dict:
    """Deactivate a bind code."""
    store = SettingsStore.get_instance()
    if store.deactivate_bind_code(code):
        return {"ok": True}
    return {"ok": False, "error": "Bind code not found"}


def get_first_bind_code() -> dict:
    """Get or create the initial bind code for setup wizard."""
    store = SettingsStore.get_instance()
    # If any valid (active + not expired) code exists, return it
    for bc in store.get_bind_codes():
        if bc.is_active and store.validate_bind_code(bc.code) is not None:
            return {"ok": True, "code": bc.code, "is_new": False}
    # Otherwise create a new one-time code
    bc = store.create_bind_code("one_time")
    return {"ok": True, "code": bc.code, "is_new": True}


def auto_bind_wechat_user(user_id: str) -> dict:
    """Auto-create a UserSettings entry for the WeChat user on QR login.

    WeChat is 1:1 DM only — no channels, no bind codes needed.
    The QR scan itself is the authentication, so we auto-bind the user
    and mark the one-time menu hint to be sent on the user's next message.
    """
    from config.v2_settings import _now_iso

    store = SettingsStore.get_instance()
    platform = "wechat"

    if store.is_bound_user(user_id, platform=platform):
        existing = store.get_user(user_id, platform=platform)
        if existing is None:
            logger.warning("WeChat user %s is marked bound but missing settings", user_id)
        else:
            existing.pending_bind_menu_hint = True
            store.update_user(user_id, existing, platform=platform)
            store.save()
            logger.info("Re-armed WeChat bind menu hint for already-bound user %s", user_id)
        return {
            "ok": True,
            "already_bound": True,
            "is_admin": bool(getattr(existing, "is_admin", False)),
            "pending_bind_menu_hint": bool(getattr(existing, "pending_bind_menu_hint", True)),
        }

    config = load_config()
    is_admin = not store.has_enabled_admin(platform=platform)
    user = UserSettings(
        display_name=user_id,
        is_admin=is_admin,
        bound_at=_now_iso(),
        enabled=True,
        custom_cwd=config.runtime.default_cwd or None,
        routing=RoutingSettings(),
        pending_bind_menu_hint=True,
    )

    current_users = store.get_users_for_platform(platform)
    current_users[user_id] = user
    store.set_users_for_platform(platform, current_users)
    store.save()

    logger.info("Auto-bound WeChat user %s (admin=%s)", user_id, is_admin)
    return {"ok": True, "already_bound": False, "is_admin": is_admin, "pending_bind_menu_hint": True}


# ---------------------------------------------------------------------------
# Lark temporary WebSocket connection (for setup wizard)
# ---------------------------------------------------------------------------
# The Feishu console only shows the "Use Long Connection" option when an
# active WebSocket connection exists.  During the setup wizard we start a
# temporary WS client so the user can configure event subscriptions.

_temp_ws_lock = __import__("threading").Lock()
_temp_ws_client = None
_temp_ws_thread = None


def lark_temp_ws_start(app_id: str, app_secret: str, domain: str = "feishu") -> dict:
    """Start a temporary WebSocket connection so the Feishu console shows the long-connection option."""
    global _temp_ws_client, _temp_ws_thread

    app_id = app_id or _stored_platform_field("lark", "app_id")
    app_secret = app_secret or _stored_platform_secret("lark", "app_secret")
    domain = domain or _stored_platform_field("lark", "domain", "feishu")
    with _temp_ws_lock:
        # Stop any existing temp connection first
        _stop_temp_ws_internal()

        try:
            import lark_oapi as lark

            sdk_domain = lark.LARK_DOMAIN if domain == "lark" else lark.FEISHU_DOMAIN

            # Minimal event handler (does nothing, just keeps the connection alive)
            handler = lark.EventDispatcherHandler.builder("", "").build()

            client = lark.ws.Client(
                app_id=app_id,
                app_secret=app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
                domain=sdk_domain,
            )

            import threading

            def _run():
                try:
                    client.start()
                except Exception:
                    pass  # Thread exits silently on stop

            t = threading.Thread(target=_run, daemon=True, name="lark-temp-ws")
            t.start()

            _temp_ws_client = client
            _temp_ws_thread = t

            return {"ok": True, "message": "Temporary WebSocket connection started"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def lark_temp_ws_stop() -> dict:
    """Stop the temporary WebSocket connection."""
    with _temp_ws_lock:
        _stop_temp_ws_internal()
    return {"ok": True}


def _stop_temp_ws_internal():
    """Internal helper to stop temp WS (caller must hold _temp_ws_lock)."""
    global _temp_ws_client, _temp_ws_thread
    if _temp_ws_client is not None:
        try:
            # Prevent auto-reconnect and close the underlying connection
            _temp_ws_client._auto_reconnect = False
            from lark_oapi.ws.client import loop as ws_loop

            ws_loop.call_soon_threadsafe(ws_loop.create_task, _temp_ws_client._disconnect())
        except Exception:
            pass
        _temp_ws_client = None
        _temp_ws_thread = None


# --- Agent Skills (askill CLI shell) --------------------------------------
# Thin orchestration over core/services/skills.py: resolve the askill binary,
# call the async service, and normalize failures into the {ok: false, error}
# envelope the Web UI already reads. The resolved path is injected into the
# service so core/ never imports vibe/. See docs/plans/workbench-skills-page.md.


async def _skills_guarded(call):
    askill = resolve_cli_path("askill")
    if not askill:
        return {"ok": False, "error": {"code": "askill_not_found", "message": "askill CLI not found on PATH"}}
    from core.services import skills as skills_service

    try:
        return await call(askill, skills_service)
    except skills_service.SkillsError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
    except LookupError:
        return {"ok": False, "error": {"code": "askill_not_found", "message": "askill CLI not found on PATH"}}


async def list_skills(
    *, scope: str = "all", project_dir: Optional[str] = None, backends: Optional[List[str]] = None
) -> dict:
    return await _skills_guarded(
        lambda askill, svc: svc.list_skills(askill, scope=scope, project_dir=project_dir, backends=backends)
    )


async def preview_skill_source(source: str, *, project_dir: Optional[str] = None) -> dict:
    return await _skills_guarded(lambda askill, svc: svc.preview_source(askill, source, project_dir=project_dir))


async def add_skill(
    source: str,
    *,
    scope: str = "project",
    project_dir: Optional[str] = None,
    backends: Optional[List[str]] = None,
    all_skills: bool = False,
    skill: Optional[str] = None,
    copy: bool = False,
) -> dict:
    return await _skills_guarded(
        lambda askill, svc: svc.add_skill(
            askill,
            source,
            scope=scope,
            project_dir=project_dir,
            backends=backends,
            all_skills=all_skills,
            skill=skill,
            copy=copy,
        )
    )


async def remove_skill(
    name: str, *, scope: str = "project", project_dir: Optional[str] = None, backends: Optional[List[str]] = None
) -> dict:
    return await _skills_guarded(
        lambda askill, svc: svc.remove_skill(askill, name, scope=scope, project_dir=project_dir, backends=backends)
    )


async def find_skills(query: str = "") -> dict:
    return await _skills_guarded(lambda askill, svc: svc.find_skills(askill, query))


async def check_skills(*, scope: str = "project", project_dir: Optional[str] = None) -> dict:
    return await _skills_guarded(lambda askill, svc: svc.check(askill, scope=scope, project_dir=project_dir))


async def update_skill(name: str, *, scope: str = "project", project_dir: Optional[str] = None) -> dict:
    return await _skills_guarded(lambda askill, svc: svc.update(askill, name, scope=scope, project_dir=project_dir))


async def upload_skill_zip(payload: dict, *, project_dir: Optional[str] = None) -> dict:
    """Decode a base64 .zip, unpack it to a temp dir, and preview its skills.

    The UI then calls add_skill with ``source`` = the returned ``dir``. The
    temp dir is local-only; askill does the actual install/symlink from it.
    """
    import base64
    import binascii
    import io
    import os
    import shutil
    import tempfile
    import time
    import zipfile

    max_b64 = 24 * 1024 * 1024  # ~18 MB archive — skills are tiny; cap the body.
    max_uncompressed = 64 * 1024 * 1024
    max_entries = 2000

    # Best-effort sweep of stale unpack dirs from earlier uploads (the dir has to
    # outlive this request so add_skill can install from it, so it can't be
    # removed in a finally; sweep anything older than a couple hours instead).
    tmp_root = tempfile.gettempdir()
    try:
        cutoff = time.time() - 2 * 3600
        for name in os.listdir(tmp_root):
            if name.startswith("askill-upload-"):
                stale = os.path.join(tmp_root, name)
                try:
                    if os.path.getmtime(stale) < cutoff:
                        shutil.rmtree(stale, ignore_errors=True)
                except OSError:
                    pass
    except OSError:
        pass

    content_b64 = payload.get("content_base64") or ""
    if not content_b64:
        return {"ok": False, "error": {"code": "missing_file", "message": "no file content"}}
    if len(content_b64) > max_b64:
        return {"ok": False, "error": {"code": "file_too_large", "message": "archive exceeds the size limit"}}
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError):
        return {"ok": False, "error": {"code": "bad_file", "message": "invalid base64 content"}}

    workdir = tempfile.mkdtemp(prefix="askill-upload-")
    unpack = os.path.join(workdir, "skill")
    os.makedirs(unpack, exist_ok=True)
    unpack_root = os.path.realpath(unpack)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > max_entries:
                raise ValueError("archive has too many entries")
            if sum(info.file_size for info in infos) > max_uncompressed:
                raise ValueError("archive is too large uncompressed")
            for info in infos:
                target = os.path.realpath(os.path.join(unpack, info.filename))
                # Reject zip-slip (entries that escape the unpack dir).
                if target != unpack_root and not target.startswith(unpack_root + os.sep):
                    raise ValueError("archive contains unsafe paths")
            archive.extractall(unpack)
    except zipfile.BadZipFile:
        shutil.rmtree(workdir, ignore_errors=True)
        return {"ok": False, "error": {"code": "bad_zip", "message": "not a valid .zip archive"}}
    except ValueError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return {"ok": False, "error": {"code": "bad_zip", "message": str(exc)}}
    except (RuntimeError, NotImplementedError, OSError) as exc:
        # Encrypted entry / unsupported compression method / filesystem error —
        # extractall raises these (not BadZipFile), so catch them too.
        shutil.rmtree(workdir, ignore_errors=True)
        return {"ok": False, "error": {"code": "bad_zip", "message": f"could not extract archive: {exc}"}}

    preview = await _skills_guarded(lambda askill, svc: svc.preview_source(askill, unpack, project_dir=project_dir))
    if preview.get("ok"):
        preview["dir"] = unpack
    else:
        # Nothing will install from it — don't leak the dir.
        shutil.rmtree(workdir, ignore_errors=True)
    return preview
