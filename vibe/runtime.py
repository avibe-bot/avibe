import ipaddress
import json
import logging
import os
import signal
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from config import paths
from config.v2_config import (
    AgentsConfig,
    ClaudeConfig,
    CodexConfig,
    OpenCodeConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)


logger = logging.getLogger(__name__)
SHUTDOWN_INTENT_TTL_SECONDS = 30
SHUTDOWN_INTENT_ENV = "VIBE_REQUIRE_SHUTDOWN_INTENT"
SERVICE_LOCK_READY_TIMEOUT_SECONDS = 5.0
SERVICE_SLOW_START_TIMEOUT_SECONDS = 120.0


def get_package_root() -> Path:
    """Get the root directory of the vibe package."""
    return Path(__file__).resolve().parent


def get_project_root() -> Path:
    """Get the project root directory (for development mode)."""
    return Path(__file__).resolve().parents[1]


def get_ui_dist_path() -> Path:
    """Get the path to UI dist directory."""
    # First check if we're in development mode (ui/dist exists at project root)
    project_root = get_project_root()
    dev_ui_path = project_root / "ui" / "dist"
    if dev_ui_path.exists():
        return dev_ui_path

    # Then check if UI is bundled with the package
    package_ui_path = get_package_root() / "ui" / "dist"
    if package_ui_path.exists():
        return package_ui_path

    # Fallback to development path
    return dev_ui_path


def get_service_main_path() -> Path:
    """Get the path to the main service entry point."""
    # First check if we're in development mode (main.py exists at project root)
    project_root = get_project_root()
    dev_main_path = project_root / "main.py"
    if dev_main_path.exists():
        return dev_main_path

    # Then check if service_main.py is bundled with the package
    package_main_path = get_package_root() / "service_main.py"
    if package_main_path.exists():
        return package_main_path

    # Fallback to development path
    return dev_main_path


def get_working_dir() -> Path:
    """Get the working directory for subprocess execution."""
    # In development mode, use project root
    project_root = get_project_root()
    if (project_root / "main.py").exists():
        return project_root

    # In installed mode, use package root
    return get_package_root()


ROOT_DIR = get_project_root()  # For backward compatibility
MAIN_PATH = get_service_main_path()
_SERVICE_LOCK = threading.Lock()
_SERVICE_INSTANCE_LOCK_HANDLE = None
_SERVICE_START_PROCESSES: dict[int, subprocess.Popen] = {}


def _rounded_seconds(seconds: float) -> float:
    return round(max(0.0, seconds), 3)


class ServiceAlreadyRunningError(RuntimeError):
    def __init__(self, *, lock_path: Path, holder_pid: int | None = None):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        detail = f"Vibe service is already running for this data directory: {lock_path}"
        if holder_pid:
            detail = f"{detail} (pid={holder_pid})"
        super().__init__(detail)


def ensure_dirs():
    paths.ensure_data_dirs()


def default_config():
    work_dir = Path.home() / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return V2Config(
        mode="self_host",
        version="v2",
        slack=SlackConfig(bot_token="", app_token=""),
        runtime=RuntimeConfig(default_cwd=str(work_dir)),
        agents=AgentsConfig(
            opencode=OpenCodeConfig(enabled=True, cli_path="opencode"),
            claude=ClaudeConfig(enabled=True, cli_path="claude"),
            codex=CodexConfig(enabled=False, cli_path="codex"),
        ),
    )


def ensure_config():
    config_path = paths.get_config_path()
    if not config_path.exists():
        default = default_config()
        default.save(config_path)
    return V2Config.load(config_path)


def write_json(path, payload):
    # Write atomically (unique temp file in the same dir + os.replace) so a
    # concurrent reader never sees a half-written file. The regression supervisor
    # polls status files (e.g. restart_status.json) while restart jobs rewrite
    # them, and a partial read would otherwise surface as None and be misread as
    # "no restart in progress". The temp name must be unique *per call* — several
    # threads in this process can write the same status path at once (e.g.
    # overlapping FastAPI control requests dispatched through a threadpool), and a
    # shared temp name would let one writer's os.replace yank the file from under
    # another.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Status files are best-effort: a partially written or corrupted
        # payload should not break write_status() or read_status().
        return None


def get_restart_status_path() -> Path:
    return paths.get_runtime_restart_status_path()


def get_service_lock_path() -> Path:
    return paths.get_runtime_service_lock_path()


def _lock_file_pid(lock_file) -> int | None:
    try:
        lock_file.seek(0)
        payload = json.loads(lock_file.read() or "{}")
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    pid = payload.get("pid") if isinstance(payload, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _try_lock_file(lock_file) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_file(lock_file) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.debug("Failed to unlock service instance lock", exc_info=True)
        return

    import fcntl

    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        logger.debug("Failed to unlock service instance lock", exc_info=True)


def acquire_service_instance_lock() -> None:
    """Acquire the data-dir scoped service runtime lock for this process lifetime."""
    global _SERVICE_INSTANCE_LOCK_HANDLE
    if _SERVICE_INSTANCE_LOCK_HANDLE is not None:
        return
    paths.ensure_data_dirs()
    lock_path = get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    if not _try_lock_file(lock_file):
        holder_pid = _lock_file_pid(lock_file)
        lock_file.close()
        raise ServiceAlreadyRunningError(lock_path=lock_path, holder_pid=holder_pid)
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "command": get_process_command(os.getpid()),
            },
            indent=2,
        )
    )
    lock_file.flush()
    try:
        os.fsync(lock_file.fileno())
    except OSError:
        logger.debug("Failed to fsync service instance lock", exc_info=True)
    paths.get_runtime_pid_path().write_text(str(os.getpid()), encoding="utf-8")
    _SERVICE_INSTANCE_LOCK_HANDLE = lock_file


def release_service_instance_lock() -> None:
    global _SERVICE_INSTANCE_LOCK_HANDLE
    lock_file = _SERVICE_INSTANCE_LOCK_HANDLE
    if lock_file is None:
        return
    _SERVICE_INSTANCE_LOCK_HANDLE = None
    try:
        try:
            paths.get_runtime_pid_path().unlink(missing_ok=True)
        except OSError:
            logger.debug("Failed to remove service pid file while releasing lock", exc_info=True)
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
        except OSError:
            logger.debug("Failed to truncate service instance lock", exc_info=True)
        _unlock_file(lock_file)
    finally:
        lock_file.close()


def service_instance_lock_available() -> tuple[bool, int | None]:
    """Return whether the data-dir scoped service lock can be acquired."""
    paths.ensure_data_dirs()
    lock_path = get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        if _try_lock_file(lock_file):
            _unlock_file(lock_file)
            return True, None
        return False, _lock_file_pid(lock_file)
    finally:
        lock_file.close()


def get_shutdown_intent_path() -> Path:
    return paths.get_runtime_dir() / "shutdown_intent.json"


def write_shutdown_intent(
    target_pid: int,
    *,
    signum: int = signal.SIGTERM,
    reason: str = "managed-stop",
) -> None:
    """Record a short-lived intent before sending a managed shutdown signal."""
    if not isinstance(target_pid, int) or target_pid <= 0:
        return
    payload = {
        "target_pid": target_pid,
        "signum": int(signum),
        "reason": reason,
        "created_at": time.time(),
        "sender_pid": os.getpid(),
        "sender_command": get_process_command(os.getpid()),
        "target_command": get_process_command(target_pid),
    }
    try:
        write_json(get_shutdown_intent_path(), payload)
        logger.info("Recorded managed shutdown intent: %s", payload)
    except OSError:
        logger.warning("Failed to write shutdown intent for pid=%s", target_pid, exc_info=True)


def consume_shutdown_intent(target_pid: int, signum: int = signal.SIGTERM) -> dict | None:
    """Return and remove a valid managed shutdown intent for this process."""
    path = get_shutdown_intent_path()
    payload = read_json(path)
    if not isinstance(payload, dict):
        return None
    try:
        age = time.time() - float(payload.get("created_at", 0))
        matches = (
            payload.get("target_pid") == target_pid
            and int(payload.get("signum", 0)) == int(signum)
            and 0 <= age <= SHUTDOWN_INTENT_TTL_SECONDS
        )
    except (TypeError, ValueError):
        matches = False
    if not matches:
        return None
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove consumed shutdown intent", exc_info=True)
    return payload


def shutdown_intent_required() -> bool:
    return os.environ.get(SHUTDOWN_INTENT_ENV, "").lower() in {"1", "true", "yes"}


def _pid_alive_windows(pid: int) -> bool:
    if pid <= 0:
        return False

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        synchronize = 0x00100000
        query_limited_information = 0x1000
        still_active = 259

        handle = kernel32.OpenProcess(synchronize | query_limited_information, False, pid)
        if not handle:
            last_error = ctypes.get_last_error()
            # Access denied still means the process exists.
            if last_error == 5:
                return True
            return False

        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("Windows pid_alive probe failed for pid=%s", pid, exc_info=True)
        return False


def _terminate_process_windows(pid: int, timeout: float = 5) -> bool:
    if pid <= 0:
        return False

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        synchronize = 0x00100000
        query_limited_information = 0x1000
        process_terminate = 0x0001
        wait_object_0 = 0

        handle = kernel32.OpenProcess(
            synchronize | query_limited_information | process_terminate,
            False,
            pid,
        )
        if not handle:
            return not _pid_alive_windows(pid)

        try:
            if not kernel32.TerminateProcess(handle, 1):
                return False

            timeout_ms = max(0, int(timeout * 1000))
            wait_result = kernel32.WaitForSingleObject(handle, timeout_ms)
            return wait_result == wait_object_0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("Windows process termination failed for pid=%s", pid, exc_info=True)
        return False


def _get_process_command_windows(pid: int) -> str | None:
    script = f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; if ($p) {{ $p.CommandLine }}'
    for shell in ("powershell", "pwsh"):
        try:
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            continue
        command = (result.stdout or "").strip()
        if command:
            return command
    return None


def _decode_proc_cmdline(raw: bytes) -> str | None:
    argv = [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]
    return shlex.join(argv) if argv else None


def get_process_command(pid: int) -> str | None:
    if not isinstance(pid, int) or pid <= 0:
        return None

    if os.name == "nt":
        return _get_process_command_windows(pid)

    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        command = _decode_proc_cmdline(proc_cmdline.read_bytes())
    except Exception:
        command = None
    if command:
        return command

    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    command = (result.stdout or "").strip()
    return command or None


def pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        return _pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, SystemError):
        return False
    try:
        status = psutil.Process(pid).status()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True
    except psutil.Error:
        return True
    dead_statuses = {psutil.STATUS_ZOMBIE}
    status_dead = getattr(psutil, "STATUS_DEAD", None)
    if status_dead is not None:
        dead_statuses.add(status_dead)
    return status not in dead_statuses


def process_create_time(pid: int) -> float | None:
    """Wall-clock start time of a process, or ``None`` if it can't be read.

    Used to tell a recorded pid apart from an unrelated process that later reused
    the same pid (notably across a reboot): a reused pid has a different start
    time, so ``(pid, create_time)`` identifies the original process.
    """
    try:
        return float(psutil.Process(pid).create_time())
    except (psutil.Error, ValueError, TypeError):
        return None


def stop_pid(pid: int, timeout: float = 5) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not pid_alive(pid):
        return False

    if os.name == "nt":
        return _terminate_process_windows(pid, timeout=timeout)

    write_shutdown_intent(pid, signum=signal.SIGTERM, reason="stop_pid")
    try:
        logger.info(
            "Sending managed SIGTERM to pid=%s command=%s",
            pid,
            get_process_command(pid),
        )
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        logger.warning("Sending managed SIGKILL to pid=%s command=%s", pid, get_process_command(pid))
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    logger.error("Managed SIGKILL did not terminate pid=%s command=%s", pid, get_process_command(pid))
    return False


def _log_path(name: str) -> Path:
    return paths.get_runtime_dir() / name


def spawn_background(args, pid_path, stdout_name: str, stderr_name: str, env: dict[str, str] | None = None):
    stdout_path = _log_path(stdout_name)
    stderr_path = _log_path(stderr_name)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    stdin = open(os.devnull, "rb")
    try:
        process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            cwd=str(get_working_dir()),
            close_fds=True,
            env=env,
        )
    finally:
        stdin.close()
        stdout.close()
        stderr.close()
    pid_path.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def spawn_service_background_process(
    args,
    stdout_name: str,
    stderr_name: str,
    env: dict[str, str] | None = None,
) -> subprocess.Popen:
    stdout_path = _log_path(stdout_name)
    stderr_path = _log_path(stderr_name)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout = stdout_path.open("ab")
    stderr = stderr_path.open("ab")
    stdin = open(os.devnull, "rb")
    try:
        process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            cwd=str(get_working_dir()),
            close_fds=True,
            env=env,
        )
    finally:
        stdin.close()
        stdout.close()
        stderr.close()
    return process


def spawn_service_background(args, stdout_name: str, stderr_name: str, env: dict[str, str] | None = None) -> int:
    return spawn_service_background_process(args, stdout_name, stderr_name, env=env).pid


def _record_service_pid_reservation(pid: int) -> None:
    pid_path = paths.get_runtime_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def _clear_service_pid_reservation(pid: int) -> None:
    _SERVICE_START_PROCESSES.pop(pid, None)
    pid_path = paths.get_runtime_pid_path()
    try:
        recorded_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if recorded_pid == pid:
        pid_path.unlink(missing_ok=True)


def _read_pid_file(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def service_lock_held_by(pid: int) -> bool:
    lock_path = get_service_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+", encoding="utf-8")
    try:
        if _try_lock_file(lock_file):
            _unlock_file(lock_file)
            return False
        return _lock_file_pid(lock_file) == pid
    finally:
        lock_file.close()


def _service_start_exit_code(pid: int) -> int | None:
    process = _SERVICE_START_PROCESSES.get(pid)
    if process is None:
        return None
    exit_code = process.poll()
    if exit_code is None:
        return None
    _clear_service_pid_reservation(pid)
    return exit_code


def service_pid_recorded(pid: int) -> bool:
    pid_path = paths.get_runtime_pid_path()
    if not pid_path.exists():
        return False
    try:
        recorded_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return recorded_pid == pid and pid_alive(pid) and service_lock_held_by(pid)


def service_pid_file_points_to_running_service(pid_path: Path | None = None) -> bool:
    pid = _read_pid_file(pid_path or paths.get_runtime_pid_path())
    return bool(pid and service_pid_recorded(pid))


def wait_for_service_pid(pid: int, timeout: float = SERVICE_LOCK_READY_TIMEOUT_SECONDS) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_pid_recorded(pid):
            _SERVICE_START_PROCESSES.pop(pid, None)
            return True
        if _service_start_exit_code(pid) is not None:
            return False
        if not pid_alive(pid):
            _clear_service_pid_reservation(pid)
            return False
        time.sleep(0.1)
    ready = service_pid_recorded(pid)
    if ready:
        _SERVICE_START_PROCESSES.pop(pid, None)
    elif _service_start_exit_code(pid) is not None:
        return False
    return ready


def stop_process(pid_path, timeout=5):
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False
    if not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return False
    stopped = stop_pid(pid, timeout=timeout)
    if stopped:
        pid_path.unlink(missing_ok=True)
    else:
        logger.error(
            "Failed to stop pid=%s from %s; preserving pid file so future starts do not orphan it",
            pid,
            pid_path,
        )
    return stopped


def write_status(state, detail=None, service_pid=None, ui_pid=None):
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # Preserve started_at across consecutive "running" writes so the UI can
    # show a stable service start time. Reset it on transitions in/out of
    # running state, AND when the service PID has changed (e.g. a forced
    # restart that goes running -> running but with a new process).
    started_at = None
    if state == "running":
        previous = read_json(paths.get_runtime_status_path()) or {}
        if (
            previous.get("state") == "running"
            and previous.get("started_at")
            and previous.get("service_pid") == service_pid
        ):
            started_at = previous["started_at"]
        else:
            started_at = now_iso
    payload = {
        "state": state,
        "detail": detail,
        "service_pid": service_pid,
        "ui_pid": ui_pid,
        "updated_at": now_iso,
    }
    if started_at:
        payload["started_at"] = started_at
    write_json(paths.get_runtime_status_path(), payload)


def read_status():
    return read_json(paths.get_runtime_status_path()) or {}


def _command_references_path(command: str | None, expected_path: Path) -> bool:
    if not command:
        return False
    try:
        args = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return False
    expected_resolved = expected_path.resolve()
    for arg in args:
        cleaned_arg = arg.strip("\"'")
        try:
            if Path(cleaned_arg).resolve() == expected_resolved:
                return True
        except (OSError, RuntimeError):
            continue
    return False


def _pid_mismatches_service(pid: int) -> bool:
    command = get_process_command(pid)
    if not command:
        logger.warning(
            "Reusing existing service pid=%s because its command line could not be inspected",
            pid,
        )
        return False
    return not _command_references_path(command, get_service_main_path())


def render_status():
    status = read_status()
    pid_path = paths.get_runtime_pid_path()
    pid = pid_path.read_text(encoding="utf-8").strip() if pid_path.exists() else None
    running = bool(pid and pid.isdigit() and service_pid_recorded(int(pid)))
    status["running"] = running
    status["pid"] = int(pid) if pid and pid.isdigit() else None
    restart_status = read_json(get_restart_status_path())
    if restart_status:
        status["restart"] = restart_status
    return json.dumps(status, indent=2)


def _raise_service_start_not_ready(pid: int, *, timeout: float) -> None:
    if pid_alive(pid):
        raise RuntimeError(
            f"Vibe service process pid={pid} did not acquire the service lock within {timeout:.0f} seconds"
        )
    _clear_service_pid_reservation(pid)
    raise RuntimeError(f"Vibe service process pid={pid} did not acquire the service lock")


def start_service(
    *,
    wait_for_ready: bool = True,
    initial_ready_timeout: float = SERVICE_LOCK_READY_TIMEOUT_SECONDS,
):
    with _SERVICE_LOCK:
        pid_path = paths.get_runtime_pid_path()
        existing_pid = 0
        if pid_path.exists():
            try:
                existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
            except Exception:
                existing_pid = 0
            if existing_pid and pid_alive(existing_pid):
                if not _pid_mismatches_service(existing_pid):
                    if service_pid_recorded(existing_pid):
                        return existing_pid
                    if not wait_for_ready:
                        return existing_pid
                    if wait_for_service_pid(existing_pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS):
                        return existing_pid
                    _raise_service_start_not_ready(existing_pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS)
                logger.warning(
                    "Ignoring stale service pid file pid=%s because it does not match the Vibe service",
                    existing_pid,
                )
            pid_path.unlink(missing_ok=True)

        lock_available, lock_holder_pid = service_instance_lock_available()
        if not lock_available:
            if lock_holder_pid and lock_holder_pid == existing_pid and pid_alive(lock_holder_pid):
                return lock_holder_pid
            raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=lock_holder_pid)

        main_path = get_service_main_path()
        process = spawn_service_background_process(
            [sys.executable, str(main_path)],
            "service_stdout.log",
            "service_stderr.log",
            env={
                **os.environ,
                "VIBE_DISABLE_STDOUT_LOGGING": "1",
                SHUTDOWN_INTENT_ENV: "1",
            },
        )
        pid = process.pid
        _SERVICE_START_PROCESSES[pid] = process
        _record_service_pid_reservation(pid)
        if initial_ready_timeout > 0 and wait_for_service_pid(pid, timeout=initial_ready_timeout):
            return pid
        exit_code = _service_start_exit_code(pid)
        if exit_code is not None:
            raise RuntimeError(
                f"Vibe service process pid={pid} exited with code {exit_code} before acquiring the service lock"
            )
        if pid_alive(pid) and not wait_for_ready:
            logger.warning(
                "Vibe service process pid=%s has not acquired the service lock after %.1fs; "
                "continuing while it finishes startup",
                pid,
                initial_ready_timeout,
            )
            return pid
        if wait_for_service_pid(pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS):
            return pid
        exit_code = _service_start_exit_code(pid)
        if exit_code is not None:
            raise RuntimeError(
                f"Vibe service process pid={pid} exited with code {exit_code} before acquiring the service lock"
            )
        _raise_service_start_not_ready(pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS)
        return pid


def _ui_health_url(host: str, port: int) -> str:
    health_host = (host or "127.0.0.1").strip()
    if health_host in {"0.0.0.0", ""}:
        health_host = "127.0.0.1"
    elif health_host in {"::", "::0"}:
        health_host = "[::1]"
    elif health_host.startswith("[") and health_host.endswith("]"):
        pass
    elif ":" in health_host:
        health_host = f"[{health_host}]"
    return f"http://{health_host}:{port}/health"


def ui_server_healthy(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(_ui_health_url(host, port), timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return False


def wait_for_ui_server(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ui_server_healthy(host, port):
            return True
        time.sleep(0.1)
    return ui_server_healthy(host, port)


def _pid_matches_ui_server(pid: int) -> bool:
    command = get_process_command(pid)
    if not command:
        return False
    return "vibe.ui_server" in command and "run_ui_server" in command


def ui_pid_file_points_to_running_ui(pid_path: Path | None = None) -> bool:
    pid = _read_pid_file(pid_path or paths.get_runtime_ui_pid_path())
    return bool(pid and pid_alive(pid) and _pid_matches_ui_server(pid))


def resolve_localhost_family() -> str:
    """Return the loopback family ``localhost`` actually maps to on this host.

    ``"inet"`` when IPv4 loopback resolves (the common dual-stack case),
    ``"inet6"`` only when ``localhost`` is exclusively IPv6. Used by
    ``effective_ui_bind_host`` and ``_origin_host_for_pairing`` so the
    bind family and the cloudflared origin family stay aligned: forcing
    IPv4 unconditionally would regress IPv6-only hosts, while leaving
    resolution to the UI server + cloudflared independently re-creates the
    ::1 vs 127.0.0.1 race that surfaces as 502.
    """
    try:
        infos = socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return "inet"
    families = {info[0] for info in infos}
    if socket.AF_INET in families:
        return "inet"
    if socket.AF_INET6 in families:
        return "inet6"
    return "inet"


def effective_ui_bind_host(config: V2Config, requested_host: str | None = None) -> str:
    """Resolve the host the UI server should bind to.

    When the Avibe Cloud tunnel is enabled, keep loopback-only configs bound
    to loopback. For non-loopback setup hosts, bind to a wildcard so the local
    ``cloudflared`` origin (which dials ``127.0.0.1``/``[::1]``) can reach the
    UI no matter which interface IP the user typed into ``ui.setup_host``
    (Tailscale CGNAT, LAN). The host-trust middleware in ``ui_server`` still
    rejects untrusted peers, so widening the bind does not widen exposure.

    Why: If the user binds to a Tailscale or LAN IP and then enables the
    tunnel, ``cloudflared`` cannot reach the UI on its loopback origin and
    every public request returns 502.

    ``requested_host`` lets callers (e.g. the ``/ui/reload`` endpoint)
    propagate the host from the inbound request without persisting it first;
    when omitted we fall back to ``config.ui.setup_host``.
    """
    setup_host = (requested_host if requested_host is not None else config.ui.setup_host) or "127.0.0.1"
    cloud = getattr(getattr(config, "remote_access", None), "vibe_cloud", None)
    if cloud is not None and cloud.enabled:
        normalized = setup_host.strip()
        if normalized.startswith("[") and normalized.endswith("]"):
            normalized = normalized[1:-1]
        # "localhost" is ambiguous on dual-stack hosts and may even be
        # exclusively IPv6. Resolve once and bind to a literal loopback that
        # matches the family _origin_host_for_pairing will hand cloudflared,
        # so the two sides cannot disagree without widening the local socket.
        if normalized.lower() == "localhost":
            return "::1" if resolve_localhost_family() == "inet6" else "127.0.0.1"
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            address = None
        if address is not None and address.is_loopback:
            return address.compressed
        # Pick the wildcard family that matches the user's non-loopback intent
        # so IPv6 setup_host values stay reachable on v6.
        if normalized in {"::", "::0"} or ":" in normalized:
            return "::"
        return "0.0.0.0"
    return setup_host


def start_ui(host, port, *, wait_for_ready: bool = True):
    pid_path = paths.get_runtime_ui_pid_path()
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = 0
        if existing_pid and pid_alive(existing_pid):
            if _pid_matches_ui_server(existing_pid) and ui_server_healthy(host, port):
                return existing_pid
            if _pid_matches_ui_server(existing_pid):
                logger.warning(
                    "Stopping stale UI process pid=%s because health check failed for %s",
                    existing_pid,
                    _ui_health_url(host, port),
                )
                stop_pid(existing_pid)
            else:
                logger.warning(
                    "Ignoring stale UI pid file pid=%s because it does not match the Vibe UI server",
                    existing_pid,
                )
        pid_path.unlink(missing_ok=True)

    command = "from vibe.ui_server import run_ui_server; run_ui_server('{}', {})".format(host, port)
    pid = spawn_background(
        [sys.executable, "-c", command],
        pid_path,
        "ui_stdout.log",
        "ui_stderr.log",
    )
    if wait_for_ready and not wait_for_ui_server(host, port):
        logger.warning("Started UI pid=%s but health check did not pass for %s", pid, _ui_health_url(host, port))
    return pid


def stop_service():
    with _SERVICE_LOCK:
        return stop_process(paths.get_runtime_pid_path())


def stop_ui(timings: dict[str, float | bool] | None = None, *, stop_remote_access: bool = True):
    remote_access_stopped = True
    started_at = time.monotonic()
    if stop_remote_access:
        remote_access_started_at = time.monotonic()
        try:
            from vibe import remote_access

            result = remote_access.stop()
            if timings is not None:
                timings["stop_remote_access_seconds"] = _rounded_seconds(time.monotonic() - remote_access_started_at)
            if isinstance(result, dict) and result.get("ok") is False:
                logger.warning("Failed to stop remote access before UI stop: %s", result.get("error"))
                remote_access_stopped = False
        except Exception:
            if timings is not None and "stop_remote_access_seconds" not in timings:
                timings["stop_remote_access_seconds"] = _rounded_seconds(time.monotonic() - remote_access_started_at)
            logger.warning("Failed to stop remote access before UI stop", exc_info=True)
            remote_access_stopped = False
    elif timings is not None:
        timings["stop_remote_access_seconds"] = 0.0
        timings["stop_remote_access_skipped"] = True
    ui_started_at = time.monotonic()
    ui_stopped = stop_process(paths.get_runtime_ui_pid_path())
    if timings is not None:
        timings["stop_ui_process_seconds"] = _rounded_seconds(time.monotonic() - ui_started_at)
        timings["stop_ui_seconds"] = _rounded_seconds(time.monotonic() - started_at)
    return bool(ui_stopped and remote_access_stopped)
