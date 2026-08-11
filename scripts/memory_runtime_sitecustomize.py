"""Artifact-local bootstrap for the one gated Memory ``cascade sync`` launch."""

from __future__ import annotations

import math
import os
import signal
import sys


_MODULE = "everos.entrypoints.cli.main"
_COMMAND = ("cascade", "sync")
_ORIGINAL_ARGV = ("-I", "-m", _MODULE, *_COMMAND)
_GATE = "AVIBE_MEMORY_SYNC_BOOTSTRAP"
_ROLE = "AVIBE_MEMORY_CHILD_ROLE"
_NONCE = "AVIBE_MEMORY_SYNC_NONCE"
_PARENT_PID = "AVIBE_MEMORY_SYNC_PARENT_PID"
_PARENT_CREATE_TIME = "AVIBE_MEMORY_SYNC_PARENT_CREATE_TIME"
_PARENT_UID = "AVIBE_MEMORY_SYNC_PARENT_UID"


_FAILURE_EXIT_CODE = 79


def _fail_closed(_message: str, _cause: BaseException | None = None) -> None:
    os._exit(_FAILURE_EXIT_CODE)


def _exact_interpreter_argv() -> bool:
    """Match CPython's real early ``.pth`` view of ``-I -m`` invocation."""

    original = tuple(getattr(sys, "orig_argv", ()))
    if len(original) < 2 or original[1:] != _ORIGINAL_ARGV:
        return False
    return True


def bootstrap() -> None:
    """No-op unless the parent explicitly gates the exact sync launch."""

    if os.environ.get(_GATE) != "1":
        return
    if not _exact_interpreter_argv():
        _fail_closed("Memory sync bootstrap received an unexpected argv")
    if os.environ.get(_ROLE) != "cascade_sync":
        _fail_closed("Memory sync bootstrap received an unexpected role")
    nonce = os.environ.get(_NONCE, "")
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        _fail_closed("Memory sync bootstrap received an invalid nonce")
    try:
        parent_pid = int(os.environ[_PARENT_PID])
        parent_create_time = float.fromhex(os.environ[_PARENT_CREATE_TIME])
        parent_uid = os.environ[_PARENT_UID]
        if not math.isfinite(parent_create_time):
            raise ValueError("invalid parent envelope")
        if parent_uid:
            if int(parent_uid) < 0:
                raise ValueError("invalid parent envelope")
    except (KeyError, TypeError, ValueError) as exc:
        _fail_closed("Memory sync bootstrap received an invalid parent", exc)
    if parent_pid <= 1 or parent_pid != os.getppid():
        _fail_closed("Memory sync bootstrap parent changed")
    try:
        os.kill(os.getpid(), signal.SIGSTOP)
    except BaseException as exc:
        _fail_closed("Memory sync bootstrap stop failed", exc)
    try:
        from avibe_memory_sync_scrubbers import install_error_scrubbers

        install_error_scrubbers()
    except BaseException as exc:
        _fail_closed("Memory sync bootstrap scrubber installation failed", exc)


bootstrap()
