"""OpenCode caller-context bridge.

OpenCode runs a shared ``opencode serve`` process, so per-Agent Avibe context
cannot live in the server process environment. Instead Avibe installs a tiny
OpenCode plugin that resolves each shell call's OpenCode session id through an
Avibe-managed binding file and injects the AVIBE_* env vars for that call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping

from config import paths
from core.caller_context import caller_context_from_platform_payload

PLUGIN_FILENAME = "avibe-caller-context.js"
BINDINGS_FILENAME = "opencode_caller_context.json"
BINDING_TTL_HOURS = 24


PLUGIN_SOURCE = r"""
import { readFileSync } from "node:fs"

const bindingPath = process.env.AVIBE_OPENCODE_CALLER_CONTEXT_PATH

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

export const AvibeCallerContextPlugin = async () => ({
  "shell.env": async (input, output) => {
    const sessionID = input && typeof input.sessionID === "string" ? input.sessionID : ""
    if (!sessionID) return
    const binding = readBindings()[sessionID]
    if (!binding || typeof binding !== "object") return
    const expiresAt = typeof binding.expires_at === "string" ? Date.parse(binding.expires_at) : 0
    if (expiresAt && Date.now() > expiresAt) return
    applyEnv(output, binding.env)
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


def bind_session(
    opencode_session_id: str,
    platform_payload: Mapping[str, object] | None,
    *,
    ttl_hours: int = BINDING_TTL_HOURS,
) -> bool:
    session_id = str(opencode_session_id or "").strip()
    if not session_id:
        return False
    caller = caller_context_from_platform_payload(platform_payload)
    if caller is None:
        return False

    path = binding_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _utc_now()
    expires_at = now + timedelta(hours=max(1, int(ttl_hours)))
    data = _load_bindings(path)
    sessions = _prune_sessions(data.get("sessions", {}), now)
    sessions[session_id] = {
        "env": caller.to_env(),
        "caller_context": caller.to_metadata(),
        "updated_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    data["sessions"] = sessions
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)
    return True
