"""Local, bundled-Runtime helper for a native Desktop management decision.

Discovery never initializes stores or migrates data. The native shell retains
the snapshot that was confirmed; takeover rechecks it before signaling only
those processes. Old Controllers need no new HTTP endpoint to participate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import psutil

from config import paths
from config.v2_config import UiConfig, V2Config, VibeCloudRemoteAccessConfig
from vibe import internal_client, runtime
from vibe.desktop_runtime import desktop_origin, desktop_runtime_id


class TakeoverRefused(Exception):
    """A stable reason code, rendered by the native locale catalog."""


def _lock_owner(home: Path) -> int | None:
    """Probe an existing lock without ensure_dirs, config writes, or migrations."""
    try:
        with (home / "runtime" / "service.lock").open("r+", encoding="utf-8") as stream:
            if runtime._take_lock_file(stream):
                runtime._unlock_file(stream)
                return None
            return runtime._lock_file_pid(stream)
    except FileNotFoundError:
        return None


def _process_home(process: psutil.Process) -> Path | None:
    environment = process.environ()
    if explicit := environment.get(paths.AVIBE_HOME_ENV):
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = Path(process.cwd()) / candidate
        return candidate.resolve()
    if home := environment.get("HOME") or environment.get("USERPROFILE"):
        root = Path(home)
        current, legacy = root / paths.AVIBE_HOME_DIRNAME, root / paths.LEGACY_HOME_DIRNAME
        candidates = {current.resolve(), legacy.resolve()}
        for candidate in candidates:
            if _lock_owner(candidate) == process.pid:
                return candidate
            try:
                if int((candidate / "runtime" / "vibe-ui.pid").read_text()) == process.pid:
                    return candidate
            except (OSError, ValueError):
                continue
        return None
    return None


def discover_home(selected: str | None) -> Path:
    if selected:
        home = Path(selected).expanduser().resolve()
        if not (home / "config" / "config.json").is_file():
            raise TakeoverRefused("invalid_home")
        return home
    if os.environ.get(paths.AVIBE_HOME_ENV):
        return paths.get_vibe_remote_dir().resolve()
    homes: set[Path] = set()
    username = psutil.Process().username()
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            if process.pid == os.getpid() or process.username() != username:
                continue
            command = shlex.join(process.info.get("cmdline") or [])
            if not runtime._command_looks_like_service_entry(command, cwd=process.cwd(), include_scope_wrapper=False):
                continue
            home = _process_home(process)
            if home is None:
                raise TakeoverRefused("identity_unknown")
            if (home / "config" / "config.json").is_file() and _lock_owner(home) == process.pid:
                homes.add(home)
        except (psutil.Error, OSError):
            continue
    if len(homes) > 1:
        raise TakeoverRefused("ambiguous_home")
    return next(iter(homes), paths.get_vibe_remote_dir().resolve())


def _identity(pid: int) -> dict:
    process = psutil.Process(pid)
    if process.username() != psutil.Process().username():
        raise TakeoverRefused("identity_unknown")
    if _process_home(process) != paths.get_vibe_remote_dir().resolve():
        raise TakeoverRefused("identity_unknown")
    return {"pid": pid, "created": process.create_time()}


def _supervised(pid: int) -> bool:
    process = psutil.Process(pid)
    environment = process.environ()
    if any(environment.get(key) for key in ("INVOCATION_ID", "NOTIFY_SOCKET", "LAUNCH_JOBKEY_LABEL")):
        return True
    if sys.platform == "darwin":
        # launchd-created detached processes also have ppid=1. Match the job's
        # actual PID; parentage alone cannot distinguish an ordinary daemon.
        result = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise TakeoverRefused("identity_unknown")
        if any(line.split()[:1] == [str(pid)] for line in result.stdout.splitlines()):
            return True
    parent = process.parent()
    if parent and parent.pid != 1:
        name = parent.name().lower()
        if any(manager in name for manager in ("supervisor", "nssm", "winsw", "services.exe", "svchost")):
            return True
    return False


def _idle() -> None:
    try:
        snapshot = asyncio.run(internal_client.list_running_agents())
    except (internal_client.InternalServerUnavailable, internal_client.InternalServerTimeout):
        raise TakeoverRefused("activity_unknown") from None
    body = snapshot.get("body")
    if snapshot.get("status_code") != 200 or not isinstance(body, dict) or not isinstance(body.get("agents"), list):
        raise TakeoverRefused("activity_unknown")
    if body.get("ok") is not True or body.get("ownership_available") is False:
        raise TakeoverRefused("activity_unknown")
    if any(row.get("state") != "idle" for row in body["agents"]):
        raise TakeoverRefused("busy")


def inspect_runtime() -> dict:
    home = paths.get_vibe_remote_dir().resolve()
    # V2Config.load() creates directories and may write recovery backups. Only
    # the endpoint fields are needed before consent; parse those in memory.
    config = V2Config.default()
    if paths.get_config_path().is_file():
        payload = json.loads(paths.get_config_path().read_text(encoding="utf-8"))
        ui = payload.get("ui") or {}
        cloud = (payload.get("remote_access") or {}).get("vibe_cloud") or {}
        config.ui = UiConfig(setup_host=ui.get("setup_host", "127.0.0.1"), setup_port=ui.get("setup_port", 5123))
        config.remote_access.vibe_cloud = VibeCloudRemoteAccessConfig(enabled=cloud.get("enabled", False))
    result = {"home": str(home), "origin": desktop_origin(runtime.effective_ui_bind_host(config), config.ui.setup_port),
              "external": None}
    # Do not create service.lock or a fresh data directory merely to discover it.
    if not paths.get_runtime_service_lock_path().exists():
        return result
    pid = _lock_owner(home)
    if pid is None:
        return result
    process = psutil.Process(pid)
    if not runtime._command_looks_like_service_entry(shlex.join(process.cmdline()), cwd=process.cwd(), include_scope_wrapper=False):
        raise TakeoverRefused("identity_unknown")
    if desktop_runtime_id(process.environ()) is not None:
        return result
    external = {"home": str(home), "service": None, "ui": None, "reason": None}
    result["external"] = external
    try:
        external["service"] = _identity(pid)
        if not runtime.ui_pid_file_points_to_running_ui():
            raise TakeoverRefused("identity_unknown")
        ui_pid = int(paths.get_runtime_ui_pid_path().read_text().strip())
        if runtime.is_desktop_ui(ui_pid, None) is not True:
            raise TakeoverRefused("identity_unknown")
        external["ui"] = _identity(ui_pid)
        if _supervised(pid) or _supervised(ui_pid):
            raise TakeoverRefused("supervised")
        _idle()
    except TakeoverRefused as error:
        external["reason"] = str(error)
    except (psutil.Error, OSError, ValueError):
        external["reason"] = "identity_unknown"
    return result


def take_over(confirmed: dict) -> None:
    if not isinstance(confirmed, dict) or confirmed.get("home") != str(paths.get_vibe_remote_dir().resolve()):
        raise TakeoverRefused("identity_changed")
    current = inspect_runtime()["external"]
    if current is None or current != confirmed:
        raise TakeoverRefused("identity_changed")
    if current["reason"]:
        raise TakeoverRefused(current["reason"])
    # Hold psutil identities across the stop. Its signal methods check PID reuse.
    service = psutil.Process(current["service"]["pid"])
    ui = psutil.Process(current["ui"]["pid"])
    for process, identity in ((service, current["service"]), (ui, current["ui"])):
        if process.create_time() != identity["created"]:
            raise TakeoverRefused("identity_changed")
    _idle()
    # Share the scoped-stop signal and PID-reuse rules. An independent service
    # has no Desktop Runtime id, so its confirmed identities supply authority;
    # it must never enter the id-based scan or the full CLI stop.
    runtime._stop_desktop_processes([service], timeout=30, force=False)
    if not runtime._desktop_process_gone(service):
        raise TakeoverRefused("stop_failed")
    if not runtime._desktop_process_gone(ui):
        if _identity(ui.pid) != current["ui"]:
            raise TakeoverRefused("identity_changed")
        runtime._stop_desktop_processes([ui], timeout=10, force=False)
    if not runtime._desktop_process_gone(ui):
        raise TakeoverRefused("stop_failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("inspect", "takeover"))
    parser.add_argument("--home")
    parser.add_argument("--receipt")
    args = parser.parse_args()
    try:
        home = discover_home(args.home)
        os.environ[paths.AVIBE_HOME_ENV] = str(home)
        if args.action == "takeover":
            take_over(json.loads(args.receipt or "null"))
            result = {"ok": True}
        else:
            result = inspect_runtime()
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except TakeoverRefused as error:
        print(json.dumps({"error": str(error)}))
        return 3
    except (psutil.Error, OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        print(json.dumps({"error": "identity_unknown"}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
