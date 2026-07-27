"""Stable supervisor for one managed watch command."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, BinaryIO

import psutil

PROCESS_IDENTITY_ENV = "AVIBE_PROCESS_IDENTITY"
WATCH_WORKER_PROTOCOL_VERSION = 1
WATCH_WORKER_ERROR_PREFIX = "AVIBE_WATCH_WORKER_ERROR:"
_MAX_SPEC_BYTES = 16 * 1024 * 1024


class WatchWorkerProtocolError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def encode_watch_worker_spec(
    *,
    command: list[str],
    shell_command: str | None,
) -> bytes:
    payload = {
        "version": WATCH_WORKER_PROTOCOL_VERSION,
        "command": command,
        "shell_command": shell_command,
    }
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def encode_watch_worker_error(code: str, detail: str | None = None) -> str:
    payload = {"code": code}
    if detail:
        payload["detail"] = detail
    return WATCH_WORKER_ERROR_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
    )


def decode_watch_worker_error(stderr: str) -> tuple[str, str | None] | None:
    if not stderr.startswith(WATCH_WORKER_ERROR_PREFIX):
        return None
    try:
        payload = json.loads(stderr[len(WATCH_WORKER_ERROR_PREFIX) :].strip())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ("supervisorFailed", None)
    if not isinstance(payload, dict) or not isinstance(payload.get("code"), str):
        return ("supervisorFailed", None)
    detail = payload.get("detail")
    return payload["code"], detail if isinstance(detail, str) else None


def _read_watch_worker_spec() -> tuple[list[str], str | None]:
    payload = sys.stdin.buffer.read(_MAX_SPEC_BYTES + 1)
    if len(payload) > _MAX_SPEC_BYTES:
        raise WatchWorkerProtocolError("specTooLarge")
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WatchWorkerProtocolError("invalidEncoding") from exc
    try:
        parsed = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise WatchWorkerProtocolError("invalidJson") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != WATCH_WORKER_PROTOCOL_VERSION:
        raise WatchWorkerProtocolError("unsupportedVersion")

    command = parsed.get("command")
    shell_command = parsed.get("shell_command")
    if (
        not isinstance(command, list)
        or any(not isinstance(part, str) for part in command)
        or not isinstance(shell_command, (str, type(None)))
        or bool(command) == bool(shell_command)
        or (command and not command[0])
        or (isinstance(shell_command, str) and not shell_command.strip())
    ):
        raise WatchWorkerProtocolError("invalidCommand")
    return command, shell_command


def _install_windows_kill_on_close_job() -> Any:
    if os.name != "nt":
        return None

    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    information = JobObjectExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ctypes.WinError(error)
    return job


def _forward_stream(source: BinaryIO, target: BinaryIO) -> None:
    while True:
        chunk = source.read(64 * 1024)
        if not chunk:
            return
        target.write(chunk)
        target.flush()


def _handle_posix_termination(_signal_number: int, _frame: Any) -> None:
    # The signal is delivered to the entire process group. The command should
    # decide whether to exit; the supervisor must remain for forced escalation.
    return None


def _install_posix_supervisor_signal_handlers() -> None:
    if os.name != "nt":
        signal.signal(signal.SIGTERM, _handle_posix_termination)


def _posix_process_group_has_other_members(identity_anchor_pid: int | None = None) -> bool:
    if os.name == "nt":
        return False
    own_pid = os.getpid()
    try:
        own_pgid = os.getpgrp()
        processes = psutil.process_iter(["pid"])
        for process in processes:
            pid = process.info.get("pid")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or pid == own_pid
                or pid == identity_anchor_pid
            ):
                continue
            try:
                if os.getpgid(pid) == own_pgid:
                    return True
            except (ProcessLookupError, PermissionError, psutil.Error):
                continue
    except (OSError, psutil.Error):
        # Fail closed: retaining the supervisor is safer than orphaning an
        # unverified descendant and clearing its persisted recovery identity.
        return True
    return False


def _wait_for_posix_process_group_exit(identity_anchor_pid: int | None = None) -> None:
    while _posix_process_group_has_other_members(identity_anchor_pid):
        time.sleep(0.1)


def _run_posix_identity_anchor() -> int:
    signal.signal(signal.SIGTERM, _handle_posix_termination)
    while True:
        signal.pause()


def _start_posix_identity_anchor() -> subprocess.Popen[bytes] | None:
    marker = os.environ.get(PROCESS_IDENTITY_ENV)
    if os.name == "nt" or not marker:
        return None
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--identity-anchor"],
        env={PROCESS_IDENTITY_ENV: marker},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _stop_posix_identity_anchor(anchor: subprocess.Popen[bytes] | None) -> None:
    if anchor is None or anchor.poll() is not None:
        return
    anchor.kill()
    anchor.wait()


def _run_watch_worker() -> int:
    _install_posix_supervisor_signal_handlers()
    # The handle must stay open for the supervisor lifetime so Windows keeps
    # every descendant in the kill-on-close job.
    _job_handle = _install_windows_kill_on_close_job()
    command, shell_command = _read_watch_worker_spec()
    child_env = dict(os.environ)
    child_env.pop(PROCESS_IDENTITY_ENV, None)
    identity_anchor = _start_posix_identity_anchor()

    try:
        popen_args: Any = shell_command if shell_command is not None else command
        process = subprocess.Popen(
            popen_args,
            shell=shell_command is not None,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            raise RuntimeError("watch worker output pipes are unavailable")

        stdout_thread = threading.Thread(
            target=_forward_stream,
            args=(process.stdout, sys.stdout.buffer),
            daemon=False,
        )
        stderr_thread = threading.Thread(
            target=_forward_stream,
            args=(process.stderr, sys.stderr.buffer),
            daemon=False,
        )
        stdout_thread.start()
        stderr_thread.start()
        return_code = process.wait()
        stdout_thread.join()
        stderr_thread.join()
        _wait_for_posix_process_group_exit(
            identity_anchor.pid if identity_anchor is not None else None
        )
        return return_code
    finally:
        _stop_posix_identity_anchor(identity_anchor)


def main() -> int:
    if os.name != "nt" and sys.argv[1:] == ["--identity-anchor"]:
        return _run_posix_identity_anchor()
    try:
        return_code = _run_watch_worker()
    except WatchWorkerProtocolError as exc:
        print(encode_watch_worker_error(exc.code), file=sys.stderr, flush=True)
        return 1
    except Exception as exc:
        print(
            encode_watch_worker_error("supervisorFailed", str(exc)),
            file=sys.stderr,
            flush=True,
        )
        return 1

    if os.name == "nt":
        return return_code
    if os.name != "nt" and return_code < 0:
        signal_number = -return_code
        if signal_number == signal.SIGTERM:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal_number)
        return 128 + signal_number
    if 0 <= return_code <= 255:
        return return_code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
