"""Windows Job Objects, and the pipe check the job wrapper needs, through ``ctypes`` alone.

Standard library only: the job wrapper beside it runs as ``python -I -S`` and loads this file by its
path, as ``core/watch_worker.py`` does; each runs from a copy with this file beside it
(``core.frozen_script``). Importing it works everywhere; calling it works only on Windows.

A job object holds a process tree as a POSIX session does, and more strictly. A process started by
a member is a member, and it cannot leave, since no job here allows breakaway. With
``KILL_ON_JOB_CLOSE``, every member ends when the last handle to the job closes, so a tree never
outlives the process that holds the handle. Nested jobs need Windows 8 or later.
"""

from __future__ import annotations

import ctypes
from typing import Any, Optional

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_QUERY = 0x0004
_JOB_OBJECT_TERMINATE = 0x0008
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x102
_ERROR_FILE_NOT_FOUND = 2
_ERROR_BROKEN_PIPE = 109
_ERROR_ALREADY_EXISTS = 183

_api: Any = None


def _kernel32() -> Any:
    """kernel32 with the signatures used here, set up on first use."""
    global _api
    if _api is not None:
        return _api
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
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

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "PeekNamedPipe": (
            [
                wintypes.HANDLE,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.c_void_p,
            ],
            wintypes.BOOL,
        ),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(kernel32, name)
        function.argtypes = argtypes
        function.restype = restype
    kernel32.ExtendedLimitInformation = ExtendedLimitInformation
    kernel32.BasicAccountingInformation = BasicAccountingInformation
    kernel32.BOOL = wintypes.BOOL
    kernel32.DWORD = wintypes.DWORD
    _api = kernel32
    return kernel32


def _job_name(name: str) -> str:
    # Local\ is this logon session's namespace, which needs no privilege to create in.
    return f"Local\\{name}"


def create_kill_on_close_job(name: Optional[str] = None) -> int:
    """Create a job whose members all end when its last handle closes, and make this process a member.

    Returns the handle, which the caller keeps open for as long as the tree may run. A name that is
    already taken is refused (``FileExistsError``) rather than joined, so no other job is ever taken
    for this one.
    """
    kernel32 = _kernel32()
    ctypes.set_last_error(0)
    job = kernel32.CreateJobObjectW(None, _job_name(name) if name is not None else None)
    error = ctypes.get_last_error()
    if not job:
        raise ctypes.WinError(error)
    try:
        if error == _ERROR_ALREADY_EXISTS:
            raise FileExistsError(error, "a job object of this name already exists", name)
        information = kernel32.ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(information), ctypes.sizeof(information)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    return job


def open_job(name: str) -> Optional[int]:
    """A handle that can query and terminate the job ``name``; ``None`` once no such job exists."""
    kernel32 = _kernel32()
    job = kernel32.OpenJobObjectW(_JOB_OBJECT_QUERY | _JOB_OBJECT_TERMINATE, False, _job_name(name))
    if job:
        return job
    error = ctypes.get_last_error()
    if error == _ERROR_FILE_NOT_FOUND:
        return None
    raise ctypes.WinError(error)


def job_contains(job: int, pid: int) -> bool:
    """Whether the process ``pid`` is a member of ``job``; ``False`` once no such process exists."""
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    try:
        member = kernel32.BOOL()
        if not kernel32.IsProcessInJob(process, job, ctypes.byref(member)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(member.value)
    finally:
        kernel32.CloseHandle(process)


def process_running(pid: int) -> bool:
    """Whether the process ``pid`` exists and has not ended.

    Its object is signaled once it has ended, which is the last step of its exit: a job counts a process out,
    and its command line stops being readable, a little earlier.
    """
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not process:
        return False
    try:
        return kernel32.WaitForSingleObject(process, 0) == _WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(process)


def active_processes(job: int) -> int:
    """How many processes of ``job`` are still running."""
    kernel32 = _kernel32()
    information = kernel32.BasicAccountingInformation()
    if not kernel32.QueryInformationJobObject(
        job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(information), ctypes.sizeof(information), None
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.ActiveProcesses)


def terminate_job(job: int, exit_code: int = 1) -> None:
    """End every process of ``job``, the caller included when it is a member."""
    kernel32 = _kernel32()
    if not kernel32.TerminateJobObject(job, exit_code):
        raise ctypes.WinError(ctypes.get_last_error())


def close_handle(handle: int) -> None:
    _kernel32().CloseHandle(handle)


def pipe_readable(fd: int) -> bool:
    """Whether reading the pipe ``fd`` would not block: bytes are waiting, or every writer has closed it."""
    import msvcrt

    kernel32 = _kernel32()
    available = kernel32.DWORD()
    if not kernel32.PeekNamedPipe(msvcrt.get_osfhandle(fd), None, 0, None, ctypes.byref(available), None):
        error = ctypes.get_last_error()
        if error == _ERROR_BROKEN_PIPE:
            return True  # the read returns end of file at once
        raise ctypes.WinError(error)
    return available.value > 0
