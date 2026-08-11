"""Artifact-local bootstrap for the one gated Memory ``cascade sync`` launch."""

from __future__ import annotations

import os
import signal
import sys


_ARGV = ("everos.entrypoints.cli.main", "cascade", "sync")
_GATE = "AVIBE_MEMORY_SYNC_BOOTSTRAP"
_ROLE = "AVIBE_MEMORY_CHILD_ROLE"
_NONCE = "AVIBE_MEMORY_SYNC_NONCE"
_PARENT_PID = "AVIBE_MEMORY_SYNC_PARENT_PID"


def bootstrap() -> None:
    """No-op unless the parent explicitly gates the exact sync launch."""

    if os.environ.get(_GATE) != "1":
        return
    if tuple(sys.argv[1:]) != _ARGV:
        raise RuntimeError("Memory sync bootstrap received an unexpected argv")
    if os.environ.get(_ROLE) != "cascade_sync":
        raise RuntimeError("Memory sync bootstrap received an unexpected role")
    nonce = os.environ.get(_NONCE, "")
    if len(nonce) != 64 or any(character not in "0123456789abcdef" for character in nonce):
        raise RuntimeError("Memory sync bootstrap received an invalid nonce")
    try:
        parent_pid = int(os.environ[_PARENT_PID])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Memory sync bootstrap received an invalid parent") from exc
    if parent_pid != os.getppid():
        raise RuntimeError("Memory sync bootstrap parent changed")
    from avibe_memory_sync_scrubbers import install_error_scrubbers

    os.kill(os.getpid(), signal.SIGSTOP)
    install_error_scrubbers()


bootstrap()
