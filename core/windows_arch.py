"""The architecture Windows itself runs on, which emulated x64 Python does not report.

On Windows on Arm, Avibe runs on x64 Python under Windows' x64 emulation until
cryptography and claude-agent-sdk publish win_arm64 wheels. Python's own
platform tag then says x64, while a standalone native program, such as the
Model Hub engine, should still run as arm64.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_IMAGE_FILE_MACHINE_ARM64 = 0xAA64
# The interpreter a uv install uses on Windows on Arm; install.ps1 carries its
# own copy, since it runs before any Avibe Python exists.
WINDOWS_ARM64_UV_PYTHON = "cpython-3.12-windows-x86_64-none"


@lru_cache(maxsize=1)
def windows_native_arm64() -> bool:
    """Whether this is Windows running natively on Arm64, whatever Python's architecture."""

    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        is_wow64_process2 = kernel32.IsWow64Process2
    except AttributeError:
        # Older than Windows 10 1709, which is also older than x64 emulation on Arm64.
        return False
    is_wow64_process2.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.USHORT),
        ctypes.POINTER(wintypes.USHORT),
    ]
    is_wow64_process2.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process_machine = wintypes.USHORT()
    native_machine = wintypes.USHORT()
    if not is_wow64_process2(
        kernel32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine),
    ):
        # Without an answer, Python's own architecture is the only one known;
        # an x64 choice still runs here, emulated if the machine is Arm64.
        logger.warning("Cannot read the native Windows architecture: %s", ctypes.WinError(ctypes.get_last_error()))
        return False
    return native_machine.value == _IMAGE_FILE_MACHINE_ARM64
