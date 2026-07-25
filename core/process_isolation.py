"""Helpers for isolating and terminating managed child processes."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import psutil

KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
PROCESS_IDENTITY_ENV = "AVIBE_PROCESS_IDENTITY"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    worker_fingerprint: str | None


@dataclass(frozen=True)
class PersistedProcessIdentity:
    pid: int
    create_time: float
    worker_fingerprint: str


def new_process_identity_marker() -> str:
    """Return a high-entropy marker inherited by a managed process tree."""
    return secrets.token_hex(32)


def fingerprint_process_marker(marker: str) -> str:
    return f"sha256:{hashlib.sha256(marker.encode('ascii')).hexdigest()}"


def process_identity_subprocess_env(marker: str) -> dict[str, str]:
    env = dict(os.environ)
    env[PROCESS_IDENTITY_ENV] = marker
    return env


def capture_spawned_process_identity(
    pid: int,
    marker: str,
) -> PersistedProcessIdentity | None:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        create_time = float(psutil.Process(pid).create_time())
    except (psutil.Error, OSError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(create_time) or create_time <= 0:
        return None
    return PersistedProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=fingerprint_process_marker(marker),
    )


def process_identity_matches(
    expected: PersistedProcessIdentity,
    live: ProcessIdentity,
) -> bool:
    return (
        expected.pid == live.pid
        and expected.create_time == live.create_time
        and live.worker_fingerprint is not None
        and hmac.compare_digest(
            expected.worker_fingerprint,
            live.worker_fingerprint,
        )
    )


def _open_process_identity(pid: int) -> tuple[psutil.Process, ProcessIdentity]:
    process = psutil.Process(pid)
    create_time = float(process.create_time())
    worker_fingerprint = None
    try:
        marker = process.environ().get(PROCESS_IDENTITY_ENV)
    except (psutil.Error, OSError):
        marker = None
    if isinstance(marker, str) and marker:
        worker_fingerprint = fingerprint_process_marker(marker)
    return process, ProcessIdentity(
        pid=pid,
        create_time=create_time,
        worker_fingerprint=worker_fingerprint,
    )


def inspect_process_identity(pid: int) -> ProcessIdentity | None:
    """Return the birth identity and inherited marker for a live process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    try:
        _process, identity = _open_process_identity(pid)
    except (psutil.Error, OSError, ValueError):
        return None
    return identity


def isolated_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that put a child outside this process group."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _safe_signal_known_process_group(
    pgid: int,
    pid: int,
    sig: int,
    logger: logging.Logger,
    label: str,
) -> bool:
    if os.name == "nt" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"):
        return False
    try:
        own_pgid = os.getpgrp()
    except Exception:
        logger.debug("Failed to inspect the service process group while signaling %s pid=%s", label, pid, exc_info=True)
        return False
    if pgid == own_pgid:
        logger.error(
            "Refusing to signal %s process group for pid=%s because it matches the avibe service pgid=%s",
            label,
            pid,
            own_pgid,
        )
        return False
    try:
        logger.info(
            "Signaling %s process group pgid=%s pid=%s signal=%s service_pgid=%s",
            label,
            pgid,
            pid,
            sig,
            own_pgid,
        )
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return True
    except Exception:
        logger.debug("Failed to signal %s process group pgid=%s", label, pgid, exc_info=True)
        return False


def _safe_signal_process_group(pid: int, sig: int, logger: logging.Logger, label: str) -> bool:
    if os.name == "nt" or not hasattr(os, "getpgid") or not hasattr(os, "killpg"):
        return False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    except Exception:
        logger.debug("Failed to inspect process group for %s pid=%s", label, pid, exc_info=True)
        return False
    return _safe_signal_known_process_group(pgid, pid, sig, logger, label)


def signal_process_tree(process: Any, sig: int, logger: logging.Logger, label: str) -> None:
    """Signal a managed process group, falling back to the direct process."""
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and _safe_signal_process_group(pid, sig, logger, label):
        return

    try:
        logger.info("Signaling %s direct process pid=%s signal=%s", label, pid, sig)
        if sig == signal.SIGTERM:
            process.terminate()
        elif sig == KILL_SIGNAL:
            process.kill()
        else:
            process.send_signal(sig)
    except ProcessLookupError:
        return


def _process_group_exists(pgid: int, logger: logging.Logger, label: str) -> bool:
    if os.name == "nt" or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        logger.debug("Failed to inspect %s process group pgid=%s", label, pgid, exc_info=True)
        return True
    return True


def process_group_exists(pgid: int, logger: logging.Logger, label: str) -> bool:
    """Return whether an isolated POSIX process group still has members."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    return _process_group_exists(pgid, logger, label)


def process_group_identity_status(
    pgid: int,
    expected_identity: PersistedProcessIdentity,
    logger: logging.Logger,
    label: str,
) -> str:
    """Return match, mismatch, or unknown for a persisted process group."""
    if (
        os.name == "nt"
        or not isinstance(pgid, int)
        or isinstance(pgid, bool)
        or pgid <= 0
        or not isinstance(expected_identity, PersistedProcessIdentity)
    ):
        return "unknown"
    saw_marker = False
    saw_unverified_member = False
    try:
        processes = psutil.process_iter(["pid"])
        for process in processes:
            pid = process.info.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                continue
            try:
                if os.getpgid(pid) != pgid:
                    continue
            except (ProcessLookupError, PermissionError):
                continue
            except Exception:
                logger.debug(
                    "Failed to inspect %s process group member pid=%s",
                    label,
                    pid,
                    exc_info=True,
                )
                continue
            live_identity = inspect_process_identity(pid)
            if (
                live_identity is None
                or live_identity.create_time < expected_identity.create_time
                or live_identity.worker_fingerprint is None
            ):
                saw_unverified_member = True
                continue
            saw_marker = True
            if hmac.compare_digest(
                expected_identity.worker_fingerprint,
                live_identity.worker_fingerprint,
            ):
                return "match"
    except Exception:
        logger.debug(
            "Failed to enumerate %s process group pgid=%s",
            label,
            pgid,
            exc_info=True,
        )
    return "mismatch" if saw_marker and not saw_unverified_member else "unknown"


def process_group_matches_identity(
    pgid: int,
    expected_identity: PersistedProcessIdentity,
    logger: logging.Logger,
    label: str,
) -> bool:
    return (
        process_group_identity_status(
            pgid,
            expected_identity,
            logger,
            label,
        )
        == "match"
    )


def _wait_for_process_group_exit(
    pgid: int,
    logger: logging.Logger,
    label: str,
    *,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _process_group_exists(pgid, logger, label):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))
    return True


def terminate_process_group_by_pgid(
    pgid: int,
    logger: logging.Logger,
    label: str,
    *,
    expected_identity: PersistedProcessIdentity,
    terminate_timeout: float = 3.0,
) -> bool:
    """Terminate a persisted isolated group after its original leader exited."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    if os.name == "nt":
        return False
    try:
        own_pgid = os.getpgrp()
    except Exception:
        logger.debug("Failed to inspect the service process group before recovering %s", label, exc_info=True)
        return False
    if pgid == own_pgid:
        logger.error(
            "Refusing to terminate %s process group because pgid=%s matches the avibe service",
            label,
            pgid,
        )
        return False
    if not _process_group_exists(pgid, logger, label):
        return True
    if not process_group_matches_identity(pgid, expected_identity, logger, label):
        logger.error(
            "Refusing to terminate %s process group pgid=%s without a matching worker marker",
            label,
            pgid,
        )
        return False
    if not _safe_signal_known_process_group(pgid, pgid, signal.SIGTERM, logger, label):
        return False
    if _wait_for_process_group_exit(pgid, logger, label, timeout=terminate_timeout):
        return True

    logger.warning("Escalating termination of %s process group pgid=%s", label, pgid)
    if not process_group_matches_identity(pgid, expected_identity, logger, label):
        logger.error(
            "Refusing to force-terminate %s process group pgid=%s after its identity changed",
            label,
            pgid,
        )
        return False
    if not _safe_signal_known_process_group(pgid, pgid, KILL_SIGNAL, logger, label):
        return False
    if _wait_for_process_group_exit(pgid, logger, label, timeout=terminate_timeout):
        return True
    logger.error("%s process group pgid=%s survived forced termination", label, pgid)
    return False


def _windows_process_tree(process: psutil.Process) -> list[psutil.Process]:
    try:
        children = process.children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    except psutil.Error:
        children = []
    return children + [process]


def _terminate_windows_process_tree(
    process: psutil.Process,
    logger: logging.Logger,
    label: str,
    *,
    terminate_timeout: float,
) -> bool:
    control_signal = getattr(signal, "CTRL_BREAK_EVENT", None)
    if control_signal is None:
        logger.error("Refusing to terminate %s because Windows process-group signaling is unavailable", label)
        return False

    victims = _windows_process_tree(process)
    try:
        process.send_signal(control_signal)
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError):
        logger.debug("Failed to signal the Windows process group for %s", label, exc_info=True)
        return False

    _gone, alive = psutil.wait_procs(victims, timeout=terminate_timeout)
    if not alive:
        return True

    logger.warning("Escalating termination of %s after graceful timeout", label)
    for victim in reversed(alive):
        try:
            victim.terminate()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            logger.debug("Failed to terminate %s descendant pid=%s", label, victim.pid, exc_info=True)
    _gone, alive = psutil.wait_procs(alive, timeout=terminate_timeout)
    for victim in alive:
        try:
            victim.kill()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            logger.debug("Failed to kill %s descendant pid=%s", label, victim.pid, exc_info=True)
    _gone, alive = psutil.wait_procs(alive, timeout=terminate_timeout)
    if alive:
        logger.error("%s process tree survived forced termination", label)
        return False
    return True


def terminate_process_tree_by_pid(
    pid: int,
    logger: logging.Logger,
    label: str,
    *,
    expected_identity: PersistedProcessIdentity,
    terminate_timeout: float = 3.0,
) -> bool:
    """Terminate an isolated process tree identified only by its root PID.

    This is intended for recovering persisted children after their original
    ``asyncio.subprocess.Process`` handle has been lost. Unlike
    :func:`signal_process_tree`, it never falls back to a direct signal when a
    POSIX target is in the service's own process group or its group cannot be
    verified.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        logger.error("Refusing to terminate %s because pid=%s is the current process", label, pid)
        return False
    if not isinstance(expected_identity, PersistedProcessIdentity) or expected_identity.pid != pid:
        logger.error("Refusing to terminate %s pid=%s without a matching inspected identity", label, pid)
        return False

    try:
        process, live_identity = _open_process_identity(pid)
    except psutil.NoSuchProcess:
        return True
    except (psutil.Error, OSError, ValueError):
        logger.debug("Failed to inspect %s pid=%s before termination", label, pid, exc_info=True)
        return False
    if live_identity.create_time != expected_identity.create_time:
        logger.warning("Refusing to terminate %s pid=%s because its process identity changed", label, pid)
        return True
    if not process_identity_matches(expected_identity, live_identity):
        logger.warning("Refusing to terminate %s pid=%s because its worker marker changed", label, pid)
        return False

    if os.name == "nt":
        return _terminate_windows_process_tree(
            process,
            logger,
            label,
            terminate_timeout=terminate_timeout,
        )

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return True
    except Exception:
        logger.debug("Failed to inspect %s process group pid=%s", label, pid, exc_info=True)
        return False
    if pgid != pid:
        logger.error(
            "Refusing to terminate %s pid=%s because its pgid=%s does not identify an isolated worker group",
            label,
            pid,
            pgid,
        )
        return False

    if not _safe_signal_known_process_group(pgid, pid, signal.SIGTERM, logger, label):
        return False
    if _wait_for_process_group_exit(pgid, logger, label, timeout=terminate_timeout):
        return True

    logger.warning("Escalating termination of %s process group pgid=%s", label, pgid)
    if not _safe_signal_known_process_group(pgid, pid, KILL_SIGNAL, logger, label):
        return False
    if _wait_for_process_group_exit(pgid, logger, label, timeout=terminate_timeout):
        return True
    logger.error("%s process group pgid=%s survived forced termination", label, pgid)
    return False


async def terminate_process_tree(
    process: Any,
    logger: logging.Logger,
    label: str,
    *,
    terminate_timeout: float = 3.0,
) -> None:
    """Terminate a managed subprocess without signaling the service group."""
    if getattr(process, "returncode", None) is not None:
        return

    signal_process_tree(process, signal.SIGTERM, logger, label)
    try:
        await asyncio.wait_for(process.wait(), timeout=terminate_timeout)
        return
    except asyncio.TimeoutError:
        pass

    signal_process_tree(process, KILL_SIGNAL, logger, label)
    try:
        await process.wait()
    except ProcessLookupError:
        return


async def terminate_and_communicate(
    process: Any,
    logger: logging.Logger,
    label: str,
    *,
    terminate_timeout: float = 3.0,
) -> tuple[bytes, bytes]:
    """Terminate a process tree and drain stdout/stderr."""
    if getattr(process, "returncode", None) is None:
        signal_process_tree(process, signal.SIGTERM, logger, label)
    try:
        return await asyncio.wait_for(process.communicate(), timeout=terminate_timeout)
    except asyncio.TimeoutError:
        signal_process_tree(process, KILL_SIGNAL, logger, label)
        return await process.communicate()
