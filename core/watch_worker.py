"""Stable supervisor for one managed watch command."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
from typing import Any, BinaryIO

PROCESS_IDENTITY_ENV = "AVIBE_PROCESS_IDENTITY"
WATCH_WORKER_PROTOCOL_VERSION = 1
_MAX_SPEC_BYTES = 16 * 1024 * 1024


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
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_watch_worker_spec() -> tuple[list[str], str | None]:
    payload = sys.stdin.buffer.read(_MAX_SPEC_BYTES + 1)
    if len(payload) > _MAX_SPEC_BYTES:
        raise ValueError("watch worker specification is too large")
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict) or parsed.get("version") != WATCH_WORKER_PROTOCOL_VERSION:
        raise ValueError("unsupported watch worker specification")

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
        raise ValueError("invalid watch worker command")
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


def _run_watch_worker() -> int:
    # The handle must stay open for the supervisor lifetime so Windows keeps
    # every descendant in the kill-on-close job.
    _job_handle = _install_windows_kill_on_close_job()
    command, shell_command = _read_watch_worker_spec()
    child_env = dict(os.environ)
    child_env.pop(PROCESS_IDENTITY_ENV, None)

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
    return return_code


def main() -> int:
    try:
        return_code = _run_watch_worker()
    except Exception as exc:
        print(f"watch worker supervisor failed: {exc}", file=sys.stderr, flush=True)
        return 1

    if os.name != "nt" and return_code < 0:
        signal_number = -return_code
        os.kill(os.getpid(), signal_number)
        return 128 + signal_number
    if 0 <= return_code <= 255:
        return return_code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
