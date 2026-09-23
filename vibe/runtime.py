import calendar
import getpass
import ipaddress
import json
import logging
import os
import signal
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

import psutil

from config import paths
from config.atomic_io import write_atomic
from config.v2_config import (
    AgentsConfig,
    ClaudeConfig,
    CodexConfig,
    OpenCodeConfig,
    RuntimeConfig,
    SlackConfig,
    V2Config,
)
from core.process_isolation import isolated_subprocess_kwargs
from vibe.log_sink import RUNTIME_LOG_MAX_BYTES, RUNTIME_LOG_RETAIN_BYTES


logger = logging.getLogger(__name__)
SHUTDOWN_INTENT_TTL_SECONDS = 30
SHUTDOWN_INTENT_ENV = "VIBE_REQUIRE_SHUTDOWN_INTENT"
SERVICE_LOCK_READY_TIMEOUT_SECONDS = 5.0
SERVICE_SLOW_START_TIMEOUT_SECONDS = 120.0

#: The service takes the lock before it migrates the database and builds its
#: controller, because both of those have to be done under the exclusion the lock
#: provides. So the lock marks the START of startup, not the end of it, and the
#: record it writes says which of the two the holder has reached. Anything asking
#: "is this instance up" has to wait for the second one: a migration that fails
#: kills the holder, and a watcher that stopped at the lock has already recorded
#: a success by then.
SERVICE_PHASE_STARTING = "starting"
SERVICE_PHASE_RUNNING = "running"


@dataclass
class ProcessStartInfo:
    pid: int | None = None
    create_unix_ms: float | None = None
    reused: bool = False

    def capture(self, pid: int, *, reused: bool) -> int:
        if self.pid != pid:
            created = process_create_time(pid)
            self.pid = pid
            self.create_unix_ms = created * 1000 if created is not None else None
        self.reused = reused
        return pid


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


class ServiceLauncher(NamedTuple):
    """The install that a service or UI process should be started from.

    One value rather than an ambient fact, because there is exactly one case
    where the answer is not "this process" and it is the case that matters: a
    rollback that crosses the `vibe-remote` -> `avibe-os` tool rename reinstalls
    into a different directory than the one the rolling-back process is running
    out of. Every spawn site read `sys.executable` for itself, so the reinstall
    succeeded and the failed release was started again -- twice over, since the
    service and the UI each read it separately and would otherwise have to be
    fixed separately, and a third spawn site would have to be found and fixed
    again.

    The interpreter and the entry point travel together because they are one
    install. Pairing an interpreter with another install's entry point runs the
    replaced release's code under the replacement's imports, which is neither
    generation and is the failure this exists to avoid.
    """

    python: str
    main: str


def current_service_launcher() -> ServiceLauncher:
    """The install THIS process is running from.

    The default for every spawn, so nothing changes for the ordinary restart:
    the interpreter that is running is the one that should run the next
    generation. Only a rollback overrides it, and only because by then this
    process is no longer the install being started.
    """

    return ServiceLauncher(python=sys.executable, main=str(get_service_main_path()))


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
# /ready can spend up to two seconds in the Controller health probe.
UI_ADOPTION_PROBE_TIMEOUT_SECONDS = 3.0


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
        model_hub=ModelHubConfig(),
    )


def ensure_config():
    from storage.migrations import guard_source_checkout_default_state_bootstrap

    guard_source_checkout_default_state_bootstrap()
    config_path = paths.get_config_path()
    from config.v2_config import config_file_lock

    # Create-if-absent under the cross-process file lock (#1458 stage
    # ③): the existence check and the seeding save are one atomic step,
    # so a first-run default snapshot cannot overwrite an initial
    # settings save another process completed between a lock-free check
    # and the write.
    with config_file_lock(config_path):
        if not config_path.exists():
            default = default_config()
            default.save(config_path)
    return V2Config.load(config_path)


def write_json(path, payload):
    # ``write_atomic`` owns the swap so a concurrent reader never sees a
    # half-written file. The regression supervisor polls status files (e.g.
    # restart_status.json) while restart jobs rewrite them, and a partial read
    # would otherwise surface as None and be misread as "no restart in
    # progress". Its per-call unique temp name is what makes that safe here:
    # several threads in this process can write the same status path at once
    # (e.g. overlapping FastAPI control requests dispatched through a
    # threadpool), and a shared temp name would let one writer's os.replace
    # yank the file from under another.
    write_atomic(Path(path), json.dumps(payload, indent=2))


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


# The byte this file's lock lives on, on Windows only.
#
# Windows byte-range locks are mandatory and scoped to a HANDLE, not a process:
# an exclusive lock denies every other handle -- including a second handle this
# same process opens -- both read and write access to the locked range. This
# lock file is also the record naming its holder, so locking the bytes the
# record occupies hid that record for exactly as long as the lock meant
# anything, and every reader below treats an unreadable record as "nobody holds
# this". Locking past end-of-file is legal, costs no disk and does not extend
# the file, so a byte out here still names this file while leaving every byte
# anyone reads unlocked. It sits far beyond any record this writes.
#
# POSIX keeps its whole-file advisory ``flock``: it never denied a read, so it
# has no defect to fix and an installed base that must keep excluding today's
# releases.
_WINDOWS_LOCK_BYTE_OFFSET = 1 << 30


def _windows_lock_byte(lock_file, mode: int) -> None:
    """Take or drop the Windows lock byte, leaving the handle where it was.

    ``msvcrt.locking`` locks ``nbytes`` from the descriptor's CURRENT position,
    so the offset has to be applied to the raw descriptor -- a text handle
    cannot seek to an arbitrary byte. Restoring position 0 afterwards keeps the
    buffered handle coherent for the record write and read that follow.
    """

    import msvcrt

    os.lseek(lock_file.fileno(), _WINDOWS_LOCK_BYTE_OFFSET, os.SEEK_SET)
    try:
        msvcrt.locking(lock_file.fileno(), mode, 1)
    finally:
        lock_file.seek(0)


def _try_lock_file(lock_file) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            _windows_lock_byte(lock_file, msvcrt.LK_NBLCK)
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
            _windows_lock_byte(lock_file, msvcrt.LK_UNLCK)
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
    _write_service_instance_lock_record(lock_file, SERVICE_PHASE_STARTING)
    paths.get_runtime_pid_path().write_text(str(os.getpid()), encoding="utf-8")
    _SERVICE_INSTANCE_LOCK_HANDLE = lock_file


def _write_service_instance_lock_record(lock_file, phase: str) -> None:
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "instance_id": uuid.uuid4().hex,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "started_at": process_create_time(os.getpid()),
                "phase": phase,
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


def mark_service_instance_started() -> None:
    """Record that this service is past startup and into its normal life.

    Called once the database is migrated and the controller is built -- the last
    point at which a new release can still fail structurally, and so the first
    point at which "the upgrade worked" is a statement about anything. Whoever
    started this process is waiting on exactly this write; until it lands, a
    watcher only knows the lock was taken.

    Rewrites the record through the handle that holds the lock, so the fact and
    the lock cannot come apart: the kernel drops the lock when this process dies,
    taking the claim with it.
    """

    lock_file = _SERVICE_INSTANCE_LOCK_HANDLE
    if lock_file is None:
        return
    _write_service_instance_lock_record(lock_file, SERVICE_PHASE_RUNNING)


def read_service_instance_lock_record() -> dict | None:
    """The service lock record as written by its holder, or None if unreadable."""
    try:
        payload = json.loads(get_service_lock_path().read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def service_instance_started(pid: int) -> bool:
    """Whether the service running as ``pid`` has reported it finished starting.

    Keyed on the pid, because the record is the one thing here the holder does not
    write atomically with the lock: between taking the lock and rewriting the
    record, the file still holds whatever the PREVIOUS holder last said, and that
    is a finished startup. The previous holder's pid is in it too, so requiring
    the two to agree is what keeps a departed instance's record from answering for
    this one.

    A release that predates the phase distinction wrote ``running`` when it took
    the lock, so it reads as started immediately. That is the behavior this
    replaced, kept deliberately: it is what a rollback target on an older version
    can offer, and demanding a write it will never make would turn every rollback
    into a reported failure.
    """

    record = read_service_instance_lock_record()
    if record is None:
        return False
    return record.get("pid") == pid and record.get("phase") == SERVICE_PHASE_RUNNING


def service_instance_still_starting(pid: int) -> bool:
    """Whether ``pid``'s own record says it is still on its way up.

    The mirror of :func:`service_instance_started`, and deliberately not its
    negation. That one asks whether a new generation PROVED it works, so a
    missing or unreadable record has to count as no proof. This one is read by
    everything that reports a state word to a human, where the same absence means
    only that nothing here knows -- and answering ``starting`` on no evidence
    would leave a service whose lock record was lost looking stuck forever.

    So only the holder's own record, naming this pid and this phase, moves the
    word off ``running``. Anything less says what it has always said.
    """

    record = read_service_instance_lock_record()
    if record is None:
        return False
    return record.get("pid") == pid and record.get("phase") == SERVICE_PHASE_STARTING


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


# One shell that never answers must not be able to hold a `vibe stop` open.
# This lookup names a single process for a log line or an identity check, so a
# bound that costs it an unanswered question is cheaper than an unbounded wait
# on a lifecycle path. The existing `except Exception: continue` already moves
# on to the next shell, and TimeoutExpired arrives there.
_WINDOWS_COMMAND_LOOKUP_TIMEOUT_SECONDS = 5.0


def _get_process_command_windows(pid: int) -> str | None:
    script = f'$p = Get-CimInstance Win32_Process -Filter "ProcessId = {pid}"; if ($p) {{ $p.CommandLine }}'
    for shell in ("powershell", "pwsh"):
        try:
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=_WINDOWS_COMMAND_LOOKUP_TIMEOUT_SECONDS,
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


def _systemd_scope_target_argv(args: list[str]) -> list[str] | None:
    if not args:
        return None
    executable_name = Path(args[0].strip("\"'")).name.lower()
    if executable_name != "systemd-run":
        return None
    try:
        separator = args.index("--")
    except ValueError:
        return None
    if "--scope" not in args[1:separator]:
        return None
    target = args[separator + 1 :]
    return target or None


def _service_entry_arg_from_argv(args: list[str]) -> str | None:
    if not args:
        return None
    executable_name = Path(args[0].strip("\"'")).name.lower()
    scope_target = _systemd_scope_target_argv(args)
    if scope_target is not None:
        return _service_entry_arg_from_argv(scope_target)
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


def _command_looks_like_service_entry(
    command: str | None,
    *,
    cwd: str | None = None,
    include_scope_wrapper: bool = True,
) -> bool:
    if not command:
        return False
    try:
        args = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return False
    if not include_scope_wrapper and _systemd_scope_target_argv(args) is not None:
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
    """Name a scanned process from what psutil already collected, or not at all.

    This is asked for every process on the machine, so it has to be cheap. It
    used to fall back to `get_process_command`, which on Windows launches
    `powershell -Command Get-CimInstance ...` -- and a second `pwsh` when the
    first says nothing -- once per process. A per-process external shell inside
    a whole-machine walk is invisible on Linux, where that fallback is a /proc
    read, and on Windows it is a `vibe stop` that prints nothing and never
    returns. That is how gh-v3.1.1rc10 died.

    Nothing is lost by stopping here. The scan looks for lock-less Avibe
    daemons; those are processes this install started, whose command line
    psutil can read. One it cannot read is by construction not ours, and the
    authoritative owner comes from the service lock rather than from this scan.
    """

    info = getattr(proc, "info", {}) or {}
    cmdline = info.get("cmdline")
    if cmdline:
        return shlex.join(str(part) for part in cmdline if str(part))
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
        if not _command_looks_like_service_entry(
            command,
            cwd=_process_cwd(proc),
            include_scope_wrapper=False,
        ):
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


def verified_service_running() -> bool:
    """Report whether a service holding the service lock is running.

    ``service_process_running`` answers a different question -- whether anything at
    all occupies this data dir -- and ``start_service`` is right to ask that one,
    because a half-started child must still block a second start. Deciding whether
    the instance has a working service needs the narrower fact: a process that
    reserved the pid and never acquired the lock is not a service, it is a failed
    start, and a restart record describing that failure must not be demoted to
    history because the wreckage is still running.

    That argument does not stop at the lock, and this function used to. A process
    that acquired the lock and never finished starting is not a working service
    either -- it is the same failed start one step further along, since the lock is
    taken BEFORE the database is migrated and the controller built. So the holder
    must also have published ``SERVICE_PHASE_RUNNING``. Without that, a release
    that hangs in its migration holds the lock forever and reads as healthy, which
    is precisely the state the upgrade rollback exists to end.

    A dead holder cannot keep the lock, since the kernel drops it when the process
    goes, so liveness needs no separate check beyond that.

    This is the one owner of "does this instance have a working service".
    ``service_instance_lock_available`` owns the different question ``start_service``
    asks -- whether anything at all would block a second start -- and callers must
    not mix them: a holder mid-startup blocks a start AND is not yet running, and
    both answers are correct for their own question.

    Cheap by construction: one open, one non-blocking lock attempt, and one small
    read, and unlike the broader probe it never scans the process table.
    """

    lock_available, holder_pid = service_instance_lock_available()
    if lock_available:
        return False
    return holder_pid is not None and service_instance_started(holder_pid)


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
        cwd=str(get_working_dir()),
        close_fds=True,
        **isolated_subprocess_kwargs(),
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


def independent_process_env(
    env: dict[str, str] | None,
 ) -> dict[str, str]:
    """Build the environment for a process Avibe owns, not one it runs for a caller.

    The service, the UI server, the connector and the restart supervisor all
    outlive the call that started them and act as the installation. The caller
    provenance the current process may be carrying belongs to that call, so it
    stops here — the child's own entry point is Owner work and would refuse it
    anyway. ``None`` is materialized rather than passed through to ``Popen``,
    because an implicit inherit would carry the caller across too.
    """

    from core.caller_context import environment_without_caller_context

    child_env = environment_without_caller_context(env)
    return child_env


#: How long a child that was killed because its spawn failed is waited for.
#: SIGKILL and TerminateProcess are not refusable, so this only bounds a kernel
#: that is slow to deliver them.
SPAWN_DISCARD_REAP_TIMEOUT_SECONDS = 5.0


def discard_spawned_child(process: subprocess.Popen) -> None:
    """Kill and reap a child whose handover to its caller failed.

    Called from inside an ``except`` that re-raises, so it must not replace the
    failure it is cleaning up after: anything it cannot do is logged instead.
    """

    try:
        process.kill()
    except OSError:
        logger.error("Failed to kill pid=%s after its spawn failed", process.pid, exc_info=True)
        return
    try:
        process.wait(timeout=SPAWN_DISCARD_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error(
            "Killed pid=%s after its spawn failed, but it had not exited after %.0fs",
            process.pid,
            SPAWN_DISCARD_REAP_TIMEOUT_SECONDS,
        )


def _spawn_owned_process(
    args,
    stdout_name: str,
    stderr_name: str,
    env: dict[str, str] | None,
    *,
    hand_over: Callable[[subprocess.Popen], None] | None = None,
    withdraw: Callable[[int], None] | None = None,
) -> subprocess.Popen:
    """Start a process the caller owns, or leave no process at all.

    A caller can only undo what it knows about. Every step between ``Popen``
    and the moment the caller knows -- closing the parent's copies of the
    child's handles, the pid record that lets any other process find it, the
    caller's own note that it created the child -- used to be able to raise
    with the child already running, and then no caller could see it:
    `cmd_start`'s rollback looked for a UI it had created and found none, and
    the orphan kept the listener every later start then failed to bind. So a
    failure anywhere in that span kills and reaps the child before it
    propagates, and every caller inherits the rule instead of each rolling back
    a window it cannot observe.

    ``hand_over`` is the caller's side of that span: it records the child and
    captures it as created, and it runs inside the guarded region so that
    nothing is handed back half-owned. Once it returns, the caller's own
    rollback can find the child. If the child is discarded instead,
    ``withdraw`` removes whatever part of that record already exists -- but
    only once the child is known to be gone, so a child that could not be
    reaped stays findable.
    """

    stdout_path = _log_path(stdout_name)
    stderr_path = _log_path(stderr_name)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_sink, stderr_sink = _spawn_runtime_log_sinks(stdout_path, stderr_path)
    stdin = open(os.devnull, "rb")
    # The child holds its own copies of these once it starts; the parent's must
    # still close, or a log sink never sees end-of-file.
    parent_handles = [stdin, stdout_sink.stdin, stderr_sink.stdin]
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            args,
            stdin=stdin,
            stdout=stdout_sink.stdin,
            stderr=stderr_sink.stdin,
            cwd=str(get_working_dir()),
            close_fds=True,
            env=independent_process_env(env),
            **isolated_subprocess_kwargs(),
        )
        # Closing can fail too, so it happens inside this region and before the
        # handover: once the caller owns the child, nothing here can raise.
        _close_parent_handles(parent_handles)
        if hand_over is not None:
            hand_over(process)
    except BaseException:
        if process is not None:
            discard_spawned_child(process)
            if withdraw is not None and process.returncode is not None:
                _withdraw_quietly(withdraw, process.pid)
        _close_parent_handles(parent_handles, quietly=True)
        raise
    return process


def _withdraw_quietly(withdraw: Callable[[int], None], pid: int) -> None:
    """Remove a discarded child's record without replacing the failure being raised."""

    try:
        withdraw(pid)
    except Exception:
        logger.error("Failed to remove the record of discarded pid=%s", pid, exc_info=True)


def _close_parent_handles(handles: list, *, quietly: bool = False) -> None:
    """Close every handle, then raise the first failure unless ``quietly``.

    Closing an already closed file is a no-op, so the failure path may call this
    again after a close that raised part way through the list.
    """

    first_failure: Exception | None = None
    for handle in handles:
        try:
            handle.close()
        except Exception as exc:
            if quietly:
                logger.debug("Failed to close a spawn handle after the spawn failed", exc_info=True)
            elif first_failure is None:
                first_failure = exc
    if first_failure is not None:
        raise first_failure


def spawn_background(
    args,
    pid_path,
    stdout_name: str,
    stderr_name: str,
    env: dict[str, str] | None = None,
    *,
    start_info: ProcessStartInfo | None = None,
):
    """Start a process recorded in ``pid_path``; the record exists iff the child does.

    With ``start_info`` the child is also captured as created before this
    returns, inside the same guarded region: a caller that rolls back what
    ``start_info`` names cannot miss a child this started.
    """

    def hand_over(process: subprocess.Popen) -> None:
        pid_path.write_text(str(process.pid), encoding="utf-8")
        if start_info is not None:
            start_info.capture(process.pid, reused=False)

    return _spawn_owned_process(
        args,
        stdout_name,
        stderr_name,
        env,
        hand_over=hand_over,
        withdraw=lambda pid: _forget_pid_record(pid_path, pid),
    ).pid


def spawn_service_background_process(
    args,
    stdout_name: str,
    stderr_name: str,
    env: dict[str, str] | None = None,
    *,
    hand_over: Callable[[subprocess.Popen], None] | None = None,
    withdraw: Callable[[int], None] | None = None,
) -> subprocess.Popen:
    return _spawn_owned_process(
        args,
        stdout_name,
        stderr_name,
        env,
        hand_over=hand_over,
        withdraw=withdraw,
    )


def spawn_service_background(args, stdout_name: str, stderr_name: str, env: dict[str, str] | None = None) -> int:
    return spawn_service_background_process(args, stdout_name, stderr_name, env=env).pid


def _record_service_pid_reservation(pid: int) -> None:
    pid_path = paths.get_runtime_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def _reap_service_start_process(pid: int) -> None:
    process = _SERVICE_START_PROCESSES.pop(pid, None)
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return

    def _wait() -> None:
        try:
            wait()
        except Exception:
            logger.debug("Failed to reap service launcher pid=%s", pid, exc_info=True)

    threading.Thread(
        target=_wait,
        name=f"vibe-service-launcher-reaper-{pid}",
        daemon=True,
    ).start()


def _clear_service_pid_reservation(pid: int) -> None:
    _reap_service_start_process(pid)
    _forget_pid_record(paths.get_runtime_pid_path(), pid)


def _forget_pid_record(pid_path: Path, pid: int) -> None:
    """Remove ``pid_path`` only while it still names ``pid``."""

    if _read_pid_file(pid_path) == pid:
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
            _reap_service_start_process(pid)
            return True
        if _service_start_exit_code(pid) is not None:
            return False
        if not pid_alive(pid):
            _clear_service_pid_reservation(pid)
            return False
        time.sleep(0.1)
    ready = service_pid_recorded(pid)
    if ready:
        _reap_service_start_process(pid)
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


class ServiceState(NamedTuple):
    """What this data dir's service is doing right now."""

    state: str
    detail: str
    service_pid: int | None
    owner_pid: int | None
    extra_pids: list[int]

    @property
    def running(self) -> bool:
        return self.state in {"running", "degraded"}


def resolve_service_state(*, detect_extra_processes: bool = True) -> ServiceState:
    """The one answer to what state word describes this instance's service.

    Everything that reports or records that word asks here, because the word is
    the same fact each time and every place that derived it independently got the
    same case wrong: the lock is taken BEFORE the database is migrated and the
    controller is built, so its holder occupies this instance long before it can
    serve anything. Reading that occupancy as `running` is how a release that
    hung in its migration read as healthy for eight days with nobody served.

    `starting` is therefore a state of its own rather than a shade of running:
    something is here, it holds the lock, and it is not serving. `degraded` stays
    what it was -- a service process holding no lock, which is a different fault
    with a different repair.

    Only a record that positively says so produces `starting`, never the absence
    of one saying otherwise. This function reports; it does not adjudicate. A
    machine that lost its lock record has a service on it either way, and telling
    its owner it is starting -- forever, since nothing will ever write the record
    it is waiting for -- trades a stuck instance that reads healthy for a healthy
    instance that reads stuck.
    """

    owner_pid = resolve_service_owner_pid(include_starting=False)
    extra_pids: list[int] = []
    if detect_extra_processes:
        extra_pids = extra_service_process_pids(owner_pid=owner_pid)
    if owner_pid:
        detail = f"pid={owner_pid}"
        if extra_pids:
            detail = f"{detail}; extra_service_pids={','.join(str(pid) for pid in extra_pids)}"
        if service_instance_still_starting(owner_pid):
            return ServiceState("starting", f"{detail} has not finished starting", owner_pid, owner_pid, extra_pids)
        return ServiceState("running", detail, owner_pid, owner_pid, extra_pids)
    if extra_pids:
        return ServiceState(
            "degraded",
            f"lockless service process detected pid={extra_pids[0]}",
            extra_pids[0],
            None,
            extra_pids,
        )
    return ServiceState("stopped", "process not running", None, None, [])


def _starting_record_outlived_its_start(status: dict) -> bool:
    """Whether a persisted `starting` has outlived the start it describes.

    `starting` survives a resolved `stopped` on purpose: between the spawn and
    the moment the child takes the instance lock there is a window where nothing
    resolves as running, and reporting that window as `stopped` would make every
    healthy start look like a failure. But unlike `setup` and `error`, `starting`
    is a claim about a process that is expected to arrive, so it holds only for
    as long as that arrival is still possible. `wait_for_service_ready` gives up
    after SERVICE_SLOW_START_TIMEOUT_SECONDS; past that same deadline a
    `starting` record no longer describes a machine coming up, it describes one
    that died on the way -- which is the exact failure this change exists to stop
    reporting as healthy.
    """

    updated_at = status.get("updated_at")
    if not isinstance(updated_at, str):
        return True
    try:
        written_at = calendar.timegm(time.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        # Every status write stamps `updated_at`, so a record without a readable
        # one cannot be dated at all. Treat it as expired rather than as fresh:
        # an undatable `starting` that is believed lasts forever, and the whole
        # point of the deadline is that this state must not be permanent.
        return True
    return time.time() - written_at > SERVICE_SLOW_START_TIMEOUT_SECONDS


def render_status(*, detect_extra_processes: bool = True):
    status = read_status()
    resolved = resolve_service_state(detect_extra_processes=detect_extra_processes)
    owner_pid = resolved.owner_pid
    extra_pids = resolved.extra_pids
    running = resolved.running
    # A resolved `stopped` does not overwrite a persisted `setup`, `starting` or
    # `error`: those describe a machine with nothing running on purpose, and this
    # function reports what is running, not what was meant to. `starting` is the
    # exception with a deadline -- see above -- because it is the only one of the
    # three that describes something in flight.
    overwritable = {"running", "degraded"}
    if status.get("state") == "starting" and _starting_record_outlived_its_start(status):
        overwritable.add("starting")
    if resolved.state != "stopped" or status.get("state") in overwritable:
        status["state"] = resolved.state
        status["detail"] = resolved.detail
        status["service_pid"] = resolved.service_pid
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
    internal_server_status = read_json(paths.get_internal_server_status_path())
    if internal_server_status:
        # The internal server cannot outlive its service: it runs on that
        # process's loop and owns a socket that dies with it. A SIGKILL leaves
        # no shutdown path to correct the file, so a live "ready" without a
        # service owner is stale by definition rather than something to report.
        if not running and str(internal_server_status.get("state") or "") not in {"stopped", "error"}:
            internal_server_status = {**internal_server_status, "state": "stopped", "stale": True}
        status["internal_server"] = internal_server_status
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


def _raise_service_started_but_never_ran(pid: int, *, timeout: float) -> None:
    """The service was identified and never reported itself up.

    Kept apart from :func:`_raise_service_start_not_ready` because the two are
    different failures that used to be reported as one. That one means nothing
    on this machine ever claimed to be the service. This one is the incident
    behind the rollback work: something did claim it, took the lock, and then
    died or hung inside its own startup -- which is exactly how an upgrade to a
    release that cannot start looks from outside, and saying "did not acquire
    the service lock" about it sends whoever reads the log looking for a lock
    contention that is not there.
    """

    owner_pid = resolve_service_owner_pid() or pid
    if not pid_alive(owner_pid):
        _clear_service_pid_reservation(owner_pid)
        raise RuntimeError(f"Vibe service process pid={owner_pid} exited while starting up")
    if service_instance_still_starting(owner_pid):
        raise RuntimeError(
            f"Vibe service process pid={owner_pid} acquired the service lock but did not "
            f"finish starting within {timeout:.0f} seconds"
        )
    _raise_service_start_not_ready(owner_pid, timeout=timeout)


# --- cgroup resource-governance bootstrap -----------------------------------
#
# ``core/resource_governance.py`` can only move agent subprocesses into a
# memory-capped cgroup when Avibe itself runs inside a *delegated* cgroup
# subtree. A normal login lands the service in a root-owned, non-delegated
# ``session-*.scope`` where ``mkdir`` returns EPERM, so governance is inert.
#
# The kernel's delegation-containment rule forbids a non-root process from
# migrating itself out of that session scope into the user manager's delegated
# tree (the common ancestor ``user-<uid>.slice`` is root-owned). The only way
# in is to be *launched* inside the delegated tree. ``systemd-run --user
# --scope`` does exactly that: it asks the user systemd manager to create a
# transient scope under ``user@<uid>.service/app.slice`` (qiqi-owned, memory +
# pids delegated) and then ``execve()``s into the target — so the launched
# process keeps its real pid and cmdline (no supervising parent), and Avibe's
# pid-tracking is unaffected.
#
# This is best-effort and fails open: on macOS, without systemd, without a live
# user manager, when governance is disabled, or when linger can't be confirmed,
# the prefix is empty and the service starts exactly as before.

SYSTEMD_SCOPE_PREFIX = (
    "systemd-run",
    "--user",
    "--scope",
    "-q",  # suppress the "Running scope as unit ..." banner from the log sink
    "-p",
    "Delegate=yes",
    "--",
)


def _resource_governance_mode() -> str:
    try:
        from config.v2_config import V2Config
        from core.resource_governance import config_from_runtime

        return str(config_from_runtime(V2Config.load()).get("mode") or "auto").strip().lower()
    except Exception:
        # Absent/broken config must not block startup; treat as the default.
        return "auto"


def _current_username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return str(os.getuid())


def _linger_is_enabled(user: str) -> bool:
    try:
        result = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return "Linger=yes" in (result.stdout or "")


def _ensure_linger_enabled(*, allow_enable: bool) -> bool:
    """Confirm that the user lingers, enabling it only when explicitly allowed.

    Once the service lives under ``user@<uid>.service`` instead of a login
    session scope, logind tears that manager down on last-session-end unless
    lingering is enabled — which would kill Avibe on the first SSH logout.
    ``loginctl enable-linger`` changes persistent account state, so automatic
    governance only observes it. Explicitly enabled governance may opt in to
    that change. Fail open: if we can't confirm linger, skip the scope wrap.
    """
    user = _current_username()
    if _linger_is_enabled(user):
        return True
    if not allow_enable:
        return False
    try:
        subprocess.run(
            ["loginctl", "enable-linger", user],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return _linger_is_enabled(user)


def _systemd_run_self_test_ok(timeout: float = 5.0) -> bool:
    """Bounded dry-run so DBus/user-manager flakiness surfaces here, not as an
    opaque 'service did not acquire lock' timeout 30s later."""
    try:
        result = subprocess.run(
            ["systemd-run", "--user", "--scope", "-q", "-p", "Delegate=yes", "--", "true"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False
    return result.returncode == 0


def maybe_systemd_scope_prefix() -> list[str]:
    """Return the ``systemd-run --user --scope`` argv prefix to launch the
    service inside a delegated user cgroup, or ``[]`` to start unwrapped.

    All predicates must hold; any failure fails open to today's behavior. There
    is deliberately no "already inside our own scope" guard: nested
    ``systemd-run --scope`` creates a harmless *sibling* scope (not a nested
    child), and such a guard would misfire when a governed agent shells out to
    ``vibe restart`` — landing the restarted service inside the old
    memory-capped, ``oom.group`` agent cgroup.
    """
    if not sys.platform.startswith("linux"):
        return []
    if shutil.which("systemd-run") is None:
        return []
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return []
    if not Path(f"/run/user/{getuid()}/systemd/private").is_socket():
        return []
    try:
        from core.resource_governance import detect_cgroup_root

        if detect_cgroup_root() is None:
            return []
    except Exception:
        return []
    governance_mode = _resource_governance_mode()
    if governance_mode == "disabled":
        return []
    if not _ensure_linger_enabled(allow_enable=governance_mode == "enabled"):
        logger.warning(
            "cgroup scope bootstrap: could not confirm user linger; "
            "starting service without a delegated user scope"
        )
        return []
    if not _systemd_run_self_test_ok():
        logger.warning(
            "cgroup scope bootstrap: systemd-run self-test failed; "
            "starting service without a delegated user scope"
        )
        return []
    return list(SYSTEMD_SCOPE_PREFIX)


def _adopt_scoped_service_owner(prev_pid: int) -> int | None:
    """Adopt the authoritative ``service.lock`` holder if it differs from ``prev_pid``.

    Called on every tick of ``_wait_for_scoped_service_pid``. ``--scope`` exec()s
    into the target, so ``prev_pid`` is normally already the real service pid and
    the owner matches it (no-op). This exists to stay correct on any host where a
    shim survives as a distinct parent: the flock in ``service.lock`` is the
    authoritative owner signal (see ``resolve_service_owner_pid``), so if a
    *different* live process now holds it, adopt that pid instead of the shim's.
    Returns the adopted pid, or ``None`` to leave ``prev_pid`` in place.
    """
    owner = resolve_service_owner_pid(include_starting=False)
    if owner and owner != prev_pid and pid_alive(owner):
        # The shim's Popen must not be attributed to `owner`, but a long-lived
        # caller still needs to wait() it after the service exits.
        _reap_service_start_process(prev_pid)
        _record_service_pid_reservation(owner)
        logger.info(
            "cgroup scope bootstrap: adopting lock-holder pid=%s as the service owner (shim pid=%s)",
            owner,
            prev_pid,
        )
        return owner
    return None


def _wait_for_scoped_service_pid(spawn_pid: int, timeout: float) -> int | None:
    """Poll a ``systemd-run --user --scope`` launch until it is lock-verified.

    ``--scope`` exec()s into the target, so ``spawn_pid`` is normally already the
    service pid. To stay correct even if a shim ever survives as a distinct
    parent, the ``service.lock`` flock holder is authoritative: on every tick we
    adopt a differing live lock holder, so we never settle on an unverified
    (possibly wrapper) pid. Returns the ready pid, or ``None`` if it neither
    became ready nor left a live owner within ``timeout``.
    """
    pid = spawn_pid
    deadline = time.monotonic() + timeout
    while True:
        if service_pid_recorded(pid):
            _reap_service_start_process(pid)
            return pid
        adopted = _adopt_scoped_service_owner(pid)
        if adopted is not None:
            pid = adopted
            continue
        # The spawn process is gone and nobody holds the lock -> startup failed.
        if _service_start_exit_code(spawn_pid) is not None:
            if resolve_service_owner_pid(include_starting=False) is None:
                return None
        elif not pid_alive(pid) and resolve_service_owner_pid(include_starting=False) is None:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def wait_for_service_ready(
    spawn_pid: int, timeout: float = SERVICE_SLOW_START_TIMEOUT_SECONDS
) -> int | None:
    """Wait until the service has finished starting and return its owner pid.

    Two questions, in order. WHICH process is the service: unlike
    ``wait_for_service_pid()``, which only confirms a *specific* pid, this
    resolves the real ``service.lock`` holder, so a pid handed back by
    ``start_service()`` that was a launcher/wrapper never taking the lock (e.g. a
    surviving ``systemd-run`` parent under some host configuration) is replaced by
    the live owner instead of waited on forever. Then: is that process UP --
    ``SERVICE_PHASE_RUNNING``, which it publishes after migrating the database and
    building its controller.

    The second question is the one a caller acting on the answer needs. The lock
    is taken before the migration runs, so a holder can own the lock and then die
    on it; anything that stopped at the lock reports a healthy start and leaves.
    That is the whole shape of an upgrade taking an instance dark.

    Returns the resolved owner pid, or ``None`` if none finished starting within
    ``timeout`` -- including when the owner dies while starting, which is that
    failure arriving early rather than as a timeout.
    """
    deadline = time.monotonic() + timeout
    owner_pid = _wait_for_scoped_service_pid(spawn_pid, timeout)
    if owner_pid is None:
        return None
    while True:
        if service_instance_started(owner_pid):
            return owner_pid
        if not pid_alive(owner_pid):
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)


def _start_scoped_service_result(
    spawn_pid: int,
    *,
    initial_ready_timeout: float,
    wait_for_ready: bool,
) -> int:
    """Resolve the final service pid for a scoped launch, polling+adopting the
    authoritative lock holder rather than trusting the spawn pid."""
    # A scoped launch must resolve to a lock-verified pid, so give the first
    # phase a floor even when the caller passed initial_ready_timeout=0 (e.g.
    # restart_supervisor): the spawn pid alone is not a trustworthy owner signal
    # under a scope wrapper. In the common exec() case this returns as soon as
    # the service acquires the lock, so the floor rarely costs anything.
    first_timeout = max(initial_ready_timeout, SERVICE_LOCK_READY_TIMEOUT_SECONDS)
    ready = _wait_for_scoped_service_pid(spawn_pid, first_timeout)
    if ready is not None:
        return ready
    exit_code = _service_start_exit_code(spawn_pid)
    if exit_code is not None and resolve_service_owner_pid(include_starting=False) is None:
        raise RuntimeError(
            f"Vibe service process pid={spawn_pid} exited with code {exit_code} before acquiring the service lock"
        )
    if not wait_for_ready:
        candidate = resolve_service_owner_pid() or spawn_pid
        if pid_alive(candidate):
            logger.warning(
                "Scoped Vibe service pid=%s has not acquired the service lock yet; "
                "continuing while it finishes startup",
                candidate,
            )
            return candidate
    ready = _wait_for_scoped_service_pid(spawn_pid, SERVICE_SLOW_START_TIMEOUT_SECONDS)
    if ready is not None:
        return ready
    exit_code = _service_start_exit_code(spawn_pid)
    if exit_code is not None:
        raise RuntimeError(
            f"Vibe service process pid={spawn_pid} exited with code {exit_code} before acquiring the service lock"
        )
    _raise_service_start_not_ready(spawn_pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS)
    return spawn_pid


def start_service(
    *,
    wait_for_ready: bool = True,
    initial_ready_timeout: float = SERVICE_LOCK_READY_TIMEOUT_SECONDS,
    start_info: ProcessStartInfo | None = None,
    launcher: ServiceLauncher | None = None,
) -> int:
    """Start the service and return the pid, by default only once it is up.

    Two questions, and this function exists because they were being answered by
    the same value. WHICH process is the service is settled below, by taking the
    spawn lock, reconciling the pid file against the live lock holder and
    launching if nobody holds it. WHETHER that process is up is settled here,
    once, for every way the answer can be reached.

    The split is the fix. `_resolve_service_pid()` has seven places it can hand a
    pid back -- a recorded pid file, a lock holder whose command does not match,
    a scoped launch adopting its owner, a fresh spawn taking the lock -- and only
    one of them ever waited for the running phase. Every other caller of
    `wait_for_ready=True` got "the lock was taken", which is the state an
    instance is in when the release that took it is about to die on its own
    migration. The Dashboard recorded that as a started service and the doctor
    recorded it as a successful repair, both correctly trusting the parameter.

    Waiting out here also takes the wait off the spawn lock, where a slow start
    used to block every other caller for the length of it.

    Both phases spend one deadline, not one each. `SERVICE_SLOW_START_TIMEOUT_SECONDS`
    is the budget for a service starting, and taking the lock and reaching the
    serving state are two phases of that single event -- so passing the constant
    to each in turn let a start that is slow in both cost a caller twice the
    number the configuration states, on the Dashboard and doctor paths where
    someone is waiting for the answer. Measuring from here also makes the bound
    mean what it says whichever of `_resolve_service_pid`'s several returns
    produced the pid, including the ones that did no waiting at all.
    """

    deadline = time.monotonic() + SERVICE_SLOW_START_TIMEOUT_SECONDS
    pid = _resolve_service_pid(
        wait_for_ready=wait_for_ready,
        initial_ready_timeout=initial_ready_timeout,
        launcher=launcher,
        start_info=start_info,
    )
    if not wait_for_ready:
        return pid
    ready_pid = wait_for_service_ready(pid, timeout=max(0.0, deadline - time.monotonic()))
    if ready_pid is None:
        _raise_service_started_but_never_ran(pid, timeout=SERVICE_SLOW_START_TIMEOUT_SECONDS)
    if start_info is not None:
        start_info.capture(ready_pid, reused=start_info.reused)
    return ready_pid


def _resolve_service_pid(
    *,
    wait_for_ready: bool,
    initial_ready_timeout: float,
    launcher: ServiceLauncher | None,
    start_info: ProcessStartInfo | None,
) -> int:
    """Which process is the service, starting one if nothing holds the lock.

    Answers only that. Whether the process it names has finished starting is
    `start_service()`'s to establish -- see its docstring for why that is not
    left to the individual returns below.
    """

    from storage.migrations import guard_source_checkout_default_state_bootstrap

    def result(pid: int, *, reused: bool) -> int:
        return start_info.capture(pid, reused=reused) if start_info is not None else pid

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
                        return result(existing_pid, reused=True)
                    if _pid_reservation_is_fresh(pid_path, existing_pid):
                        # A reservation this recent names the service whether or
                        # not it is up yet, so the question is answered. Waiting
                        # for the running phase used to happen here, and only
                        # here, which is precisely why every other return below
                        # answered a question it had not asked.
                        return result(existing_pid, reused=True)
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
                            return result(lock_holder_pid, reused=True)
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
                return result(lock_holder_pid, reused=True)
            raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=lock_holder_pid)

        extra_pids = extra_service_process_pids()
        if extra_pids:
            raise ServiceAlreadyRunningError(lock_path=get_service_lock_path(), holder_pid=extra_pids[0])

        launcher = launcher or current_service_launcher()
        scope_prefix = maybe_systemd_scope_prefix()
        if scope_prefix:
            logger.info("cgroup scope bootstrap: launching service inside a delegated user scope")
        # The reservation is how anything outside this frame finds a service that
        # has not taken the lock yet -- `stop_service()` included, and so every
        # rollback -- and `start_info` is how the caller knows it created one.
        # Both are written inside the spawn primitive's guarded region, so a
        # failure or a signal before the last of them kills the child instead of
        # leaving one that nothing can stop. Only a findable child is recorded as
        # created: whoever reads `start_info` may rely on the reservation too.
        def hand_over(process: subprocess.Popen) -> None:
            _SERVICE_START_PROCESSES[process.pid] = process
            _record_service_pid_reservation(process.pid)
            result(process.pid, reused=False)

        process = spawn_service_background_process(
            [*scope_prefix, launcher.python, launcher.main],
            "service_stdout.log",
            "service_stderr.log",
            env={
                **os.environ,
                "VIBE_DISABLE_STDOUT_LOGGING": "1",
                SHUTDOWN_INTENT_ENV: "1",
            },
            hand_over=hand_over,
            withdraw=_clear_service_pid_reservation,
        )
        pid = process.pid
        if scope_prefix:
            # Scoped launches resolve their pid via the authoritative lock holder
            # (poll-and-adopt), never by trusting the spawn pid alone.
            resolved_pid = _start_scoped_service_result(
                pid,
                initial_ready_timeout=initial_ready_timeout,
                wait_for_ready=wait_for_ready,
            )
            return result(resolved_pid, reused=False)
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


def _ui_health_urls(host: str, port: int) -> tuple[str, ...]:
    from vibe.desktop_runtime import desktop_origin

    primary_url = _ui_health_url(host, port)
    desktop_url = f"{desktop_origin(host, port)}/ready"
    urls = [primary_url]
    if desktop_url not in urls:
        urls.append(desktop_url)
    return tuple(urls)


def _ui_ready_identity_state(response) -> bool | None:
    try:
        payload = json.loads(response.read().decode("utf-8"))
    except (AttributeError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        return None
    if payload.get("product") != "avibe" or type(payload.get("ready")) is not bool:
        return None

    controller_runtime_id = payload.get("desktop_runtime_id")
    ui_runtime_id = payload.get("desktop_ui_runtime_id")
    expected_ready = {"schema_version", "product", "ready"}
    if controller_runtime_id is not None and ui_runtime_id is not None:
        return None
    for field, runtime_id in (
        ("desktop_runtime_id", controller_runtime_id),
        ("desktop_ui_runtime_id", ui_runtime_id),
    ):
        if runtime_id is None:
            continue
        if (
            not isinstance(runtime_id, str)
            or len(runtime_id) != 64
            or any(character not in "0123456789abcdef" for character in runtime_id)
        ):
            return None
        expected_ready.add(field)
    if response.status == 200 and set(payload) == expected_ready and payload["ready"] is True:
        return True

    code = payload.get("code")
    if (
        response.status == 503
        and isinstance(code, str)
        and bool(code)
        and payload
        == {
            "schema_version": 1,
            "product": "avibe",
            "ready": False,
            "code": code,
        }
    ):
        return None if code == "runtime_identity_invalid" else False
    return None


def _ui_server_readiness(host: str, port: int, timeout: float = 0.5) -> bool | None:
    for health_url in _ui_health_urls(host, port):
        try:
            with urllib.request.urlopen(health_url, timeout=timeout) as response:
                if health_url.endswith("/ready"):
                    return _ui_ready_identity_state(response)
                if response.status != 200:
                    return None
        except urllib.error.HTTPError as exc:
            if health_url.endswith("/ready"):
                return _ui_ready_identity_state(exc)
            return None
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            return None
    return None


def ui_server_healthy(host: str, port: int, timeout: float = 0.5) -> bool:
    return _ui_server_readiness(host, port, timeout=timeout) is True


def _ui_server_compatible(
    host: str,
    port: int,
    timeout: float = UI_ADOPTION_PROBE_TIMEOUT_SECONDS,
) -> bool:
    return _ui_server_readiness(host, port, timeout=timeout) is not None


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


def start_ui(
    host,
    port,
    *,
    wait_for_ready: bool = True,
    start_info: ProcessStartInfo | None = None,
    launcher: ServiceLauncher | None = None,
):
    from vibe.desktop_runtime import normalize_desktop_port

    port = normalize_desktop_port(port)
    pid_path = paths.get_runtime_ui_pid_path()
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
        except Exception:
            existing_pid = 0
        if existing_pid and pid_alive(existing_pid):
            if _pid_matches_ui_server(existing_pid) and _ui_server_compatible(host, port):
                if start_info is not None:
                    start_info.capture(existing_pid, reused=True)
                return existing_pid
            if _pid_matches_ui_server(existing_pid):
                logger.warning(
                    "Stopping stale UI process pid=%s because required listener or identity checks failed for %s",
                    existing_pid,
                    ", ".join(_ui_health_urls(host, port)),
                )
                if not stop_pid(existing_pid):
                    # The stale process still owns the configured listener, so a
                    # replacement would only die on bind while that process kept
                    # serving. Preserve the pid record naming the process that has
                    # to be stopped: unlinking it here would leave the incompatible
                    # UI running with nothing pointing at it.
                    logger.error(
                        "Failed to stop stale UI process pid=%s; preserving pid state and not starting a replacement",
                        existing_pid,
                    )
                    return None
            else:
                logger.warning(
                    "Ignoring stale UI pid file pid=%s because it does not match the Vibe UI server",
                    existing_pid,
                )
        pid_path.unlink(missing_ok=True)

    # Named entry point, not a body of code: whatever release the launcher's
    # interpreter belongs to supplies its own `run_ui_server`. A rollback that
    # sent source text across the generation boundary would run the replacement's
    # idea of startup inside the replaced install.
    command = "from vibe.ui_server import run_ui_server; run_ui_server('{}', {})".format(host, port)
    spawn_kwargs = {}
    # `start_info` is captured inside the spawn, not after it returns: in between,
    # a signal left a live UI with a pid record that `cmd_start`'s rollback,
    # which undoes only what `start_info` names as created, never looked at.
    if start_info is not None:
        spawn_kwargs["start_info"] = start_info
    pid = spawn_background(
        [(launcher or current_service_launcher()).python, "-c", command],
        pid_path,
        "ui_stdout.log",
        "ui_stderr.log",
        **spawn_kwargs,
    )
    if wait_for_ready and not wait_for_ui_server(host, port):
        logger.warning(
            "Started UI pid=%s but required health checks did not pass for %s",
            pid,
            ", ".join(_ui_health_urls(host, port)),
        )
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
