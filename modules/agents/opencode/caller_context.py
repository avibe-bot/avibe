"""OpenCode runtime bridge.

OpenCode runs a shared ``opencode serve`` process, so per-Agent Avibe context
cannot live in the server process environment. Instead Avibe installs a tiny
OpenCode plugin that resolves each shell call's OpenCode session id through an
Avibe-managed binding file and injects the AVIBE_* env vars for that call. The
same process-scoped plugin removes OpenCode's native Skill Catalog after its
system prompt is assembled and restores exact Hub provider definitions after
native configuration merging; Avibe owns both projections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import tempfile
from typing import Any, Mapping

from config import paths
from core.caller_context import caller_context_from_platform_payload

PLUGIN_FILENAME = "avibe-caller-context.js"
BINDINGS_FILENAME = "opencode_caller_context.json"
BINDING_TTL_HOURS = 24


PLUGIN_SOURCE = r"""
import { readFileSync } from "node:fs"

const bindingPath = process.env.AVIBE_OPENCODE_CALLER_CONTEXT_PATH
const nativeSkillIntro = "Skills provide specialized instructions and workflows for specific tasks."
const nativeSkillPrefix = "<available_skills>"
const nativeSkillSuffix = "</available_skills>"

function readBindings() {
  if (!bindingPath) return {}
  try {
    const parsed = JSON.parse(readFileSync(bindingPath, "utf8"))
    return parsed && typeof parsed === "object" && parsed.sessions && typeof parsed.sessions === "object"
      ? parsed.sessions
      : {}
  } catch {
    return {}
  }
}

function applyEnv(output, env) {
  if (!env || typeof env !== "object") return
  output.env = output.env || {}
  for (const [key, value] of Object.entries(env)) {
    if (typeof value === "string" && value.length > 0) output.env[key] = value
  }
}

function stripNativeSkillCatalog(text) {
  let current = text
  while (true) {
    const catalog = current.indexOf(nativeSkillPrefix)
    if (catalog < 0) return current
    const intro = current.lastIndexOf(nativeSkillIntro, catalog)
    const start = intro >= 0 && catalog - intro < 500 ? intro : catalog
    const close = current.indexOf(nativeSkillSuffix, catalog + nativeSkillPrefix.length)
    if (close < 0) return current
    let before = current.slice(0, start)
    let after = current.slice(close + nativeSkillSuffix.length)
    if (before.endsWith("\n") && after.startsWith("\n")) after = after.slice(1)
    current = before + after
  }
}

export const AvibeCallerContextPlugin = async () => ({
  "config": async (config) => {
    if (process.env.AVIBE_OPENCODE_MODEL_HUB !== "1") return
    const overlay = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT || "null")
    if (!overlay || !overlay.provider || !Array.isArray(overlay.enabled_providers)) {
      throw new Error("Avibe Model Hub launch config is unavailable")
    }
    // OpenCode deep-merges native config, retaining unowned headers and model
    // transport fields. Replace each Hub provider after that merge instead.
    config.provider = { ...config.provider, ...structuredClone(overlay.provider) }
  },
  "shell.env": async (input, output) => {
    const sessionID = input && typeof input.sessionID === "string" ? input.sessionID : ""
    if (!sessionID) return
    const binding = readBindings()[sessionID]
    if (!binding || typeof binding !== "object") return
    const expiresAt = typeof binding.expires_at === "string" ? Date.parse(binding.expires_at) : 0
    if (expiresAt && Date.now() > expiresAt) return
    applyEnv(output, binding.env)
  },
  "experimental.chat.system.transform": async (_input, output) => {
    // The plugin is installed in the user's global OpenCode directory, but this
    // policy belongs only to the Avibe-managed server process.
    if (!bindingPath || !output || !Array.isArray(output.system)) return
    const transformed = output.system
      .map((text) => typeof text === "string" ? stripNativeSkillCatalog(text) : text)
      .filter((text) => typeof text !== "string" || text.length > 0)
    output.system.splice(0, output.system.length, ...transformed)
  },
})
""".lstrip()


@dataclass(frozen=True)
class PluginInstallResult:
    path: Path
    changed: bool


def binding_path() -> Path:
    return paths.get_runtime_dir() / BINDINGS_FILENAME


def _opencode_config_dir() -> Path:
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg_config_home).expanduser() if xdg_config_home else Path.home() / ".config"
    return root / "opencode"


def plugin_path() -> Path:
    return _opencode_config_dir() / "plugins" / PLUGIN_FILENAME


def ensure_plugin_installed() -> PluginInstallResult:
    path = plugin_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    changed = not path.exists() or path.read_text(encoding="utf-8") != PLUGIN_SOURCE
    if changed:
        path.write_text(PLUGIN_SOURCE, encoding="utf-8")
    return PluginInstallResult(path=path, changed=changed)


def server_environment() -> dict[str, str]:
    return {"AVIBE_OPENCODE_CALLER_CONTEXT_PATH": str(binding_path())}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_bindings(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "sessions": {}}
    if not isinstance(loaded, dict):
        return {"version": 1, "sessions": {}}
    sessions = loaded.get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    loaded["version"] = 1
    loaded["sessions"] = sessions
    return loaded


def _prune_sessions(sessions: dict[str, Any], now: datetime) -> dict[str, Any]:
    pruned: dict[str, Any] = {}
    for key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        expires_at = value.get("expires_at")
        if isinstance(expires_at, str):
            try:
                if datetime.fromisoformat(expires_at) <= now:
                    continue
            except ValueError:
                continue
        pruned[str(key)] = value
    return pruned


def _write_bindings(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    finally:
        if fd >= 0:
            os.close(fd)
        tmp_path.unlink(missing_ok=True)


def _binding_lock(path: Path):
    from storage.lock import MigrationFileLock

    return MigrationFileLock(path.with_suffix(path.suffix + ".lock"))


def bind_session(
    opencode_session_id: str,
    platform_payload: Mapping[str, object] | None,
    *,
    base_env: Mapping[str, str],
    working_dir: Path | str | None,
    ttl_hours: int = BINDING_TTL_HOURS,
    extra_env: Mapping[str, str] | None = None,
    binding_token: str | None = None,
    replace_existing: bool = True,
    path: str | Path | None = None,
    message: object | None = None,
    fallback_platform: object | None = None,
) -> bool:
    """Persist the caller env an OpenCode shell command will source.

    *message* is the turn's typed ``MessageContext``. Without it the binding carries
    caller IDENTITY only; with it the binding also carries the CREATION ORIGIN, which is
    what lets a Harness definition created by ``vibe task add`` inside this OpenCode
    session record the conversation it came from.
    """

    from core.git_runtime import prepend_vendored_git_to_path

    session_id = str(opencode_session_id or "").strip()
    if not session_id:
        return False
    caller = caller_context_from_platform_payload(
        platform_payload,
        message=message,
        fallback_platform=fallback_platform,
    )
    env = caller.to_env() if caller is not None else {}
    if extra_env:
        env.update((str(key), str(value)) for key, value in extra_env.items() if str(key) and str(value))
    prepend_vendored_git_to_path(
        env,
        base_env=base_env,
        working_dir=working_dir,
    )
    path = Path(path) if path is not None else binding_path()
    now = _utc_now()
    token = binding_token or secrets.token_hex(16)
    with _binding_lock(path):
        if not env:
            if not path.is_file():
                return False
            data = _load_bindings(path)
            existing_sessions = data.get("sessions", {})
            sessions = _prune_sessions(existing_sessions, now)
            sessions.pop(session_id, None)
            if sessions != existing_sessions:
                data["sessions"] = sessions
                _write_bindings(path, data)
            return False

        data = _load_bindings(path)
        sessions = _prune_sessions(data.get("sessions", {}), now)
        existing = sessions.get(session_id)
        if (
            not replace_existing
            and isinstance(existing, dict)
            and existing.get("binding_token") != token
        ):
            return False
        expires_at = now + timedelta(hours=max(1, int(ttl_hours)))
        entry = {
            "env": env,
            "updated_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "binding_token": token,
        }
        if caller is not None:
            entry["caller_context"] = caller.to_metadata()
        sessions[session_id] = entry
        data["sessions"] = sessions
        _write_bindings(path, data)
    return True


def unbind_session(
    opencode_session_id: str,
    *,
    binding_token: str,
    path: str | Path | None = None,
) -> bool:
    """Remove exactly the active-Turn binding created by one caller."""

    session_id = str(opencode_session_id or "").strip()
    token = str(binding_token or "").strip()
    if not session_id or not token:
        return False
    path = Path(path) if path is not None else binding_path()
    with _binding_lock(path):
        if not path.is_file():
            return False
        data = _load_bindings(path)
        sessions = data.get("sessions", {})
        entry = sessions.get(session_id)
        if not isinstance(entry, dict) or entry.get("binding_token") != token:
            return False
        sessions.pop(session_id, None)
        data["sessions"] = sessions
        _write_bindings(path, data)
    return True


def refresh_session(
    opencode_session_id: str,
    *,
    binding_token: str,
    ttl_hours: int = BINDING_TTL_HOURS,
    path: str | Path | None = None,
) -> bool:
    """Extend exactly one live binding without changing its environment."""

    session_id = str(opencode_session_id or "").strip()
    token = str(binding_token or "").strip()
    if not session_id or not token:
        return False
    path = Path(path) if path is not None else binding_path()
    now = _utc_now()
    with _binding_lock(path):
        if not path.is_file():
            return False
        data = _load_bindings(path)
        sessions = _prune_sessions(data.get("sessions", {}), now)
        entry = sessions.get(session_id)
        if not isinstance(entry, dict) or entry.get("binding_token") != token:
            if sessions != data.get("sessions", {}):
                data["sessions"] = sessions
                _write_bindings(path, data)
            return False
        entry["updated_at"] = now.isoformat()
        entry["expires_at"] = (now + timedelta(hours=max(1, int(ttl_hours)))).isoformat()
        sessions[session_id] = entry
        data["sessions"] = sessions
        _write_bindings(path, data)
    return True
