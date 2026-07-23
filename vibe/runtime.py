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
import uuid
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
from vibe.log_sink import RUNTIME_LOG_MAX_BYTES, RUNTIME_LOG_RETAIN_BYTES


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
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
    paths.ensure_data_dirs()


def default_config():
    from config.v2_config import ModelHubConfig

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
        model_hub=ModelHubConfig.fresh(),
    )


def ensure_config():
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
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
    ensure_dirs()
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
                "instance_id": uuid.uuid4().hex,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "started_at": process_create_time(os.getpid()),
                "phase": "running",
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
    ensure_dirs()
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


def service_lock_holder_pid() -> int | None:
    available, holder_pid = service_instance_lock_available()
    if available:
        return None
    return holder_pid


def current_process_owns_service_instance() -> bool:
    """Return whether this process is still the active service owner.

    Controller-owned background services capture whether they require this gate
    at construction time. A real service process constructs them only after
    acquiring the lock, so losing this handle later means the process must stop
    scheduling work.
    """
    if _SERVICE_INSTANCE_LOCK_HANDLE is None:
        return False
    return service_lock_held_by(os.getpid())


def service_instance_lock_attached_to_process() -> bool:
    return _SERVICE_INSTANCE_LOCK_HANDLE is not None


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
    command = (getattr(result, "stdout", "") or "").strip()
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


def _safe_resolve_path(value: str | Path) -> Path | None:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _path_is_service_entry(path: Path, current_main: Path | None) -> bool:
    resolved = _safe_resolve_path(path)
    if current_main is not None and resolved == current_main:
        return True
    if path.name == "service_main.py":
        parent = resolved.parent if resolved is not None else path.parent
        return parent.name == "vibe" and (parent / "runtime.py").exists()
    if path.name == "main.py":
        root = resolved.parent if resolved is not None else path.parent
        return (root / "vibe" / "runtime.py").exists() and (root / "core" / "controller.py").exists()
    return False


def _service_entry_arg_from_argv(args: list[str]) -> str | None:
    if not args:
        return None
    executable_name = Path(args[0].strip("\"'")).name.lower()
    if executable_name.startswith("python"):
        for arg in args[1:]:
            cleaned_arg = arg.strip("\"'")
            if cleaned_arg in {"-c", "-m"}:
                return None
            if cleaned_arg.startswith("-"):
                continue
            return cleaned_arg
        return None
    if Path(args[0].strip("\"'")).name in {"main.py", "service_main.py"}:
        return args[0].strip("\"'")
    return None


def _command_looks_like_service_entry(command: str | None, *, cwd: str | None = None) -> bool:
    if not command:
        return False
    try:
        args = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return False
    current_main = _safe_resolve_path(get_service_main_path())
    cwd_path = _safe_resolve_path(cwd) if cwd else None
    entry_arg = _service_entry_arg_from_argv(args)
    if not entry_arg:
        return False
    path = Path(entry_arg)
    if not path.is_absolute() and cwd_path is not None:
        path = cwd_path / path
    return _path_is_service_entry(path, current_main)


def _process_is_service_session_leader(pid: int) -> bool:
    if os.name == "nt":
        return True
    try:
        return os.getsid(pid) == pid
    except OSError:
        return False


def _process_command_from_info(proc) -> str | None:
    info = getattr(proc, "info", {}) or {}
    cmdline = info.get("cmdline")
    if cmdline:
        return shlex.join(str(part) for part in cmdline if str(part))
    pid = info.get("pid")
    if isinstance(pid, int):
        return get_process_command(pid)
    return None


def _process_cwd(proc) -> str | None:
    try:
        return proc.cwd()
    except (psutil.Error, OSError, AttributeError):
        return None


def _process_home_matches_current(proc) -> bool | None:
    current_home = _safe_resolve_path(paths.get_vibe_remote_dir())
    if current_home is None:
        return None
    try:
        env = proc.environ()
    except (psutil.Error, OSError, AttributeError):
        return None
    explicit_home = env.get(paths.AVIBE_HOME_ENV)
    if explicit_home:
        return _safe_resolve_path(explicit_home) == current_home
    has_avibe_marker = str(env.get(SHUTDOWN_INTENT_ENV) or "").lower() in {"1", "true", "yes"}
    has_avibe_marker = has_avibe_marker or str(env.get("VIBE_DISABLE_STDOUT_LOGGING") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    if not has_avibe_marker:
        return None
    home = env.get("HOME")
    if not home:
        return None
    candidates = [
        Path(home) / paths.AVIBE_HOME_DIRNAME,
        Path(home) / paths.LEGACY_HOME_DIRNAME,
    ]
    return any(_safe_resolve_path(candidate) == current_home for candidate in candidates)


def service_processes(*, include_unverified: bool = False) -> list[dict]:
    """Return Avibe service processes associated with the current data dir.

    The service lock remains the authoritative owner signal. This process scan is
    deliberately secondary: it detects extra lock-less daemons left behind by
    older lifecycle bugs, but only treats a process as actionable when its
    command looks like Avibe's service entry point and its environment maps to
    the current AVIBE_HOME. Do not require session leadership here: legacy and
    container launchers may background ``python main.py`` from a shell without
    creating a new session, and those lock-less services still need recovery.
    """
    processes: list[dict] = []
    try:
        iterator = psutil.process_iter(attrs=["pid", "cmdline"])
    except psutil.Error:
        return processes
    for proc in iterator:
        info = getattr(proc, "info", {}) or {}
        pid = info.get("pid")
        if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
            continue
        try:
            alive = pid_alive(pid)
        except Exception as exc:
            logger.warning("Failed to inspect possible service process pid=%s: %s", pid, exc)
            continue
        if not alive:
            continue
        session_leader = _process_is_service_session_leader(pid)
        command = _process_command_from_info(proc)
        if not _command_looks_like_service_entry(command, cwd=_process_cwd(proc)):
            continue
        lock_owner = service_lock_held_by(pid)
        home_match = _process_home_matches_current(proc)
        if not (lock_owner or home_match is True or (include_unverified and home_match is None)):
            continue
        processes.append(
            {
                "pid": pid,
                "command": command,
                "lock_owner": lock_owner,
                "home_match": home_match,
                "session_leader": session_leader,
            }
        )
    return processes


def extra_service_process_pids(owner_pid: int | None = None, *, include_unverified: bool = False) -> list[int]:
    pids: list[int] = []
    for process in service_processes(include_unverified=include_unverified):
        pid = process.get("pid")
        if not isinstance(pid, int) or pid == owner_pid:
            continue
        if include_unverified or process.get("home_match") is True or process.get("lock_owner") is True:
            pids.append(pid)
    return sorted(set(pids))


def service_process_running() -> bool:
    return resolve_service_owner_pid(include_starting=False) is not None or bool(extra_service_process_pids())


def _pid_reservation_is_fresh(pid_path: Path, pid: int, *, max_age: float = SERVICE_SLOW_START_TIMEOUT_SECONDS) -> bool:
    try:
        pidfile_mtime = pid_path.stat().st_mtime
    except OSError:
        return False
    create_time = process_create_time(pid)
    latest_signal = max(pidfile_mtime, create_time or 0)
    return time.time() - latest_signal <= max_age


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


def _spawn_runtime_log_sink(path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "vibe.log_sink",
            str(path),
            "--max-bytes",
            str(RUNTIME_LOG_MAX_BYTES),
            "--retain-bytes",
            str(RUNTIME_LOG_RETAIN_BYTES),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=str(get_working_dir()),
        close_fds=True,
    )


def _spawn_runtime_log_sinks(stdout_path: Path, stderr_path: Path) -> tuple[subprocess.Popen, subprocess.Popen]:
    stdout_sink = _spawn_runtime_log_sink(stdout_path)
    try:
        stderr_sink = _spawn_runtime_log_sink(stderr_path)
    except Exception:
        if stdout_sink.stdin is not None:
            stdout_sink.stdin.close()
        raise
    if stdout_sink.stdin is None or stderr_sink.stdin is None:
        if stdout_sink.stdin is not None:
            stdout_sink.stdin.close()
        if stderr_sink.stdin is not None:
            stderr_sink.stdin.close()
        raise RuntimeError("Failed to create runtime log sink pipes")
    return stdout_sink, stderr_sink


def spawn_background(args, pid_path, stdout_name: str, stderr_name: str, env: dict[str, str] | None = None):
    stdout_path = _log_path(stdout_name)
    stderr_path = _log_path(stderr_name)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_sink, stderr_sink = _spawn_runtime_log_sinks(stdout_path, stderr_path)
    stdin = open(os.devnull, "rb")
    try:
        process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout_sink.stdin,
            stderr=stderr_sink.stdin,
            start_new_session=True,
            cwd=str(get_working_dir()),
            close_fds=True,
            env=env,
        )
    finally:
        stdin.close()
        stdout_sink.stdin.close()
        stderr_sink.stdin.close()
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
    stdout_sink, stderr_sink = _spawn_runtime_log_sinks(stdout_path, stderr_path)
    stdin = open(os.devnull, "rb")
    try:
        process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout_sink.stdin,
            stderr=stderr_sink.stdin,
            start_new_session=True,
            cwd=str(get_working_dir()),
            close_fds=True,
            env=env,
        )
    finally:
        stdin.close()
        stdout_sink.stdin.close()
        stderr_sink.stdin.close()
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


def _pid_alive_for_owner_resolution(pid: int) -> bool:
    try:
        return pid_alive(pid)
    except Exception as exc:
        logger.warning("Failed to inspect service owner pid=%s: %s", pid, exc)
        return False


def resolve_service_owner_pid(*, include_starting: bool = True) -> int | None:
    """Resolve the live service owner for this data dir.

    The flock in ``service.lock`` is the authoritative owner signal. The pidfile
    is still useful for fast reuse and for a just-spawned service that has not
    acquired the lock yet, but it must not be the only lifecycle source of truth.
    """
    pid_path = paths.get_runtime_pid_path()
    recorded_pid = _read_pid_file(pid_path)
    if recorded_pid and _pid_alive_for_owner_resolution(recorded_pid):
        if service_pid_recorded(recorded_pid):
            return recorded_pid

    available, lock_holder_pid = service_instance_lock_available()
    if not available and lock_holder_pid and _pid_alive_for_owner_resolution(lock_holder_pid):
        return lock_holder_pid
    if include_starting and available and recorded_pid and _pid_alive_for_owner_resolution(recorded_pid):
        if not _pid_mismatches_service(recorded_pid):
            return recorded_pid
    return None


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


def _pid_mismatches_service(pid: int) -> bool:
    command = get_process_command(pid)
    if not command:
        logger.warning(
            "Reusing existing service pid=%s because its command line could not be inspected",
            pid,
        )
        return False
    try:
        cwd = psutil.Process(pid).cwd()
    except (psutil.Error, OSError):
        cwd = None
    return not _command_looks_like_service_entry(command, cwd=cwd)


def render_status(*, detect_extra_processes: bool = True):
    status = read_status()
    owner_pid = resolve_service_owner_pid(include_starting=False)
    extra_pids: list[int] = []
    if detect_extra_processes:
        extra_pids = extra_service_process_pids(owner_pid=owner_pid)
    running = bool(owner_pid or extra_pids)
    if owner_pid:
        status["state"] = "running"
        status["service_pid"] = owner_pid
        if extra_pids:
            status["detail"] = f"pid={owner_pid}; extra_service_pids={','.join(str(pid) for pid in extra_pids)}"
        else:
            status["detail"] = f"pid={owner_pid}"
    elif extra_pids:
        status["state"] = "degraded"
        status["service_pid"] = extra_pids[0]
        status["detail"] = f"lockless service process detected pid={extra_pids[0]}"
    elif status.get("state") in {"running", "degraded"}:
        status["state"] = "stopped"
        status["detail"] = "process not running"
        status["service_pid"] = None
    if extra_pids:
        status["extra_service_pids"] = extra_pids
    else:
        status.pop("extra_service_pids", None)
    status["service_owner_pid"] = owner_pid
    status["running"] = running
    status["pid"] = owner_pid or (extra_pids[0] if extra_pids else None)
    restart_status = read_json(get_restart_status_path())
    if restart_status:
        status["restart"] = restart_status
    try:
        if owner_pid:
            from core.show_git import show_git_checkpointing_active

            checkpointing_available = show_git_checkpointing_active()
        else:
            from core.git_binary import resolve_git

            checkpointing_available = resolve_git() is not None
        if checkpointing_available:
            status.pop("show_git_checkpoints", None)
        else:
            status["show_git_checkpoints"] = "degraded: Git checkpoint service unavailable"
    except Exception:
        status["show_git_checkpoints"] = "degraded: Git checkpoint status failed"
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
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
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
                    if _pid_reservation_is_fresh(pid_path, existing_pid):
                        if not wait_for_ready:
                            return existing_pid
                        if wait_for_service_pid(existing_pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS):
                            return existing_pid
                        _raise_service_start_not_ready(existing_pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS)
                    logger.warning(
                        "Ignoring stale service pid file pid=%s because it never acquired the service lock",
                        existing_pid,
                    )
                lock_available, lock_holder_pid = service_instance_lock_available()
                if not lock_available:
                    if lock_holder_pid and pid_alive(lock_holder_pid):
                        if lock_holder_pid == existing_pid:
                            logger.warning(
                                "Reusing service pid=%s from lock even though the pid file command does not "
                                "match this CLI install",
                                existing_pid,
                            )
                            return lock_holder_pid
                        raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=lock_holder_pid)
                    raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=lock_holder_pid)
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

        extra_pids = extra_service_process_pids()
        if extra_pids:
            raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=extra_pids[0])

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
        pid_path = paths.get_runtime_pid_path()
        owner_pid = resolve_service_owner_pid()
        target_pids: list[int] = []
        if owner_pid is not None:
            target_pids.append(owner_pid)
        target_pids.extend(extra_service_process_pids(owner_pid=owner_pid))
        target_pids = sorted(set(target_pids))
        if not target_pids:
            recorded_pid = _read_pid_file(pid_path)
            if recorded_pid and not pid_alive(recorded_pid):
                pid_path.unlink(missing_ok=True)
            return False

        stopped_all = True
        for pid in target_pids:
            stopped = stop_pid(pid, timeout=5)
            if stopped:
                _clear_service_pid_reservation(pid)
                continue
            stopped_all = False
            logger.error(
                "Failed to stop resolved service process pid=%s; preserving pid and lock state",
                pid,
            )
        return stopped_all


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
