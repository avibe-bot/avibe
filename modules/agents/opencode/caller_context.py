"""OpenCode caller-context bridge.

OpenCode runs a shared ``opencode serve`` process, so per-Agent Avibe context
cannot live in the server process environment. Instead Avibe installs a tiny
OpenCode plugin that resolves each shell call's OpenCode session id through an
Avibe-managed binding file and injects the AVIBE_* env vars for that call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from typing import Any, Mapping

from config import paths
from core.caller_context import caller_context_from_platform_payload

PLUGIN_FILENAME = "avibe-caller-context.js"
BINDINGS_FILENAME = "opencode_caller_context.json"


PLUGIN_SOURCE = r"""
import { execFileSync } from "node:child_process"
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

function ownerIdentity(pid) {
  try {
    if (process.platform === "linux") {
      const value = readFileSync(`/proc/${pid}/stat`, "utf8")
      const fields = value.slice(value.lastIndexOf(")") + 2).trim().split(/\s+/)
      return fields.length > 19 ? `linux:${fields[19]}` : ""
    }
    if (process.platform === "win32") {
      const script = `[DateTimeOffset]::new((Get-Process -Id ${pid} -ErrorAction Stop).StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()`
      const value = execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script], { encoding: "utf8" }).trim()
      return value ? `win32:${value}` : ""
    }
    const value = execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], { encoding: "utf8" }).trim().replace(/\s+/g, " ")
    return value ? `ps:${value}` : ""
  } catch {
    return ""
  }
}

function applyStaleBindingEnv(output, binding) {
  const env = binding && typeof binding.env === "object" ? binding.env : {}
  const safe = {}
  for (const [key, value] of Object.entries(env)) {
    if (key.startsWith("AVIBE_SKILL_") || key === "AVIBE_BUILTIN_SKILLS_SNAPSHOT_ID") {
      safe[key] = value
    }
  }
  if (env.AVIBE_CALLER_REMOTE === "1" && typeof env.AVIBE_SESSION_ID === "string") {
    safe.AVIBE_SESSION_ID = env.AVIBE_SESSION_ID
    safe.AVIBE_CALLER_PLATFORM = "avibe"
    safe.AVIBE_CALLER_REMOTE = "1"
  }
  applyEnv(output, safe)
}

export const AvibeCallerContextPlugin = async () => ({
  "shell.env": async (input, output) => {
    const sessionID = input && typeof input.sessionID === "string" ? input.sessionID : ""
    if (!sessionID) return
    const binding = readBindings()[sessionID]
    if (!binding || typeof binding !== "object") return
    const ownerPID = Number(binding.owner_pid)
    if (!Number.isInteger(ownerPID) || ownerPID <= 0) {
      applyStaleBindingEnv(output, binding)
      return
    }
    const expectedIdentity = typeof binding.owner_identity === "string" ? binding.owner_identity : ""
    if (!expectedIdentity || ownerIdentity(ownerPID) !== expectedIdentity) {
      applyStaleBindingEnv(output, binding)
      return
    }
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


def _owner_process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    try:
        if sys.platform.startswith("linux"):
            value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = value[value.rfind(")") + 2 :].split()
            return f"linux:{fields[19]}" if len(fields) > 19 else None
        if sys.platform == "win32":
            script = (
                "[DateTimeOffset]::new((Get-Process -Id "
                f"{pid} -ErrorAction Stop).StartTime.ToUniversalTime())"
                ".ToUnixTimeMilliseconds()"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            value = result.stdout.strip()
            return f"win32:{value}" if value else None
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = " ".join(result.stdout.split())
        return f"ps:{value}" if value else None
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def _prune_sessions(sessions: dict[str, Any]) -> dict[str, Any]:
    pruned: dict[str, Any] = {}
    for key, value in sessions.items():
        if not isinstance(value, dict):
            continue
        try:
            owner_pid = int(value.get("owner_pid"))
        except (TypeError, ValueError):
            continue
        if owner_pid <= 0:
            continue
        owner_identity = value.get("owner_identity")
        if not isinstance(owner_identity, str) or _owner_process_identity(owner_pid) != owner_identity:
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
    extra_env: Mapping[str, str] | None = None,
    binding_token: str | None = None,
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
    path = binding_path()
    now = _utc_now()
    token = binding_token or secrets.token_hex(16)
    with _binding_lock(path):
        if not env:
            if not path.is_file():
                return False
            data = _load_bindings(path)
            existing_sessions = data.get("sessions", {})
            sessions = _prune_sessions(existing_sessions)
            sessions.pop(session_id, None)
            if sessions != existing_sessions:
                data["sessions"] = sessions
                _write_bindings(path, data)
            return False

        data = _load_bindings(path)
        sessions = _prune_sessions(data.get("sessions", {}))
        owner_identity = _owner_process_identity(os.getpid())
        if owner_identity is None:
            raise RuntimeError("OpenCode caller binding cannot identify its owner process")
        entry = {
            "env": env,
            "updated_at": now.isoformat(),
            "binding_token": token,
            "owner_pid": os.getpid(),
            "owner_identity": owner_identity,
        }
        if caller is not None:
            entry["caller_context"] = caller.to_metadata()
        sessions[session_id] = entry
        data["sessions"] = sessions
        _write_bindings(path, data)
    return True


def unbind_session(opencode_session_id: str, *, binding_token: str) -> bool:
    """Remove exactly the active-Turn binding created by one caller."""

    session_id = str(opencode_session_id or "").strip()
    token = str(binding_token or "").strip()
    if not session_id or not token:
        return False
    path = binding_path()
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
