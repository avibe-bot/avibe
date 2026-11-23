"""Serialize CLI transitions with the one listener that owns device effects."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import secrets
import socket
import time

import psutil

CONTROL_TIMEOUT = 12


@contextmanager
def locked(pairing):
    """Never held by the listener while servicing a control request."""
    pairing.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if pairing.root.stat().st_uid != os.getuid():
        raise ValueError("invalid_state_owner")
    os.chmod(pairing.root, 0o700)
    fd = os.open(pairing.root / "lifecycle.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def claim(pairing):
    fd = os.open(pairing.root / "serve.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def register(fd):
    owner = {"pid": os.getpid(), "created": psutil.Process().create_time(), "nonce": secrets.token_hex(16)}
    os.ftruncate(fd, 0)
    os.write(fd, json.dumps(owner).encode())
    os.fsync(fd)
    return owner


def commit(pairing, command):
    if command == "revoke":
        pairing.revoke()
    elif command in {"stop", "disable", "enable"}:
        pairing.enable(command == "enable")
    else:
        raise ValueError("invalid_lifecycle_command")


def _terminate(pairing, owner):
    # A stale PID is not authority to signal a different process. Process caches
    # creation time and rechecks PID identity for both terminate() and kill().
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise ValueError("invalid_listener_owner")
    try:
        process = psutil.Process(pid)
        if process.create_time() != owner.get("created") or process.uids().real != os.getuid():
            raise ValueError("listener_identity_changed")
        process.terminate()
        try:
            process.wait(timeout=1)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    except psutil.NoSuchProcess:
        pass
    except psutil.Error:
        raise ValueError("listener_did_not_exit") from None


def transition(pairing, command):
    """Return only after the old listener is gone and the mutation is durable."""
    with locked(pairing):
        lock = claim(pairing)
        if lock is not None:
            try:
                commit(pairing, command)
            finally:
                os.close(lock)
            return
        if command == "enable":
            # Lifecycle serialization means this cannot overtake a stop caller.
            # Enabling an already-running listener changes no generation.
            commit(pairing, command)
            return
        owner = json.loads((pairing.root / "serve.lock").read_text())
        acknowledged = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(CONTROL_TIMEOUT)
                client.connect(str(pairing.root / "control.sock"))
                client.sendall(json.dumps({"command": command, "nonce": owner["nonce"]}).encode() + b"\n")
                data = bytearray()
                while b"\n" not in data and len(data) < 1024:
                    chunk = client.recv(1024)
                    if not chunk:
                        break
                    data.extend(chunk)
                acknowledged = json.loads(data).get("ok") is True
        except (OSError, ValueError, KeyError):
            pass
        deadline = time.monotonic() + (2 if acknowledged else 0)
        while True:
            lock = claim(pairing)
            if lock is not None:
                break
            if time.monotonic() >= deadline:
                _terminate(pairing, owner)
                lock = claim(pairing)
                if lock is None:
                    raise ValueError("listener_still_running")
                break
            time.sleep(0.02)
        try:
            if not acknowledged:
                commit(pairing, command)
        finally:
            os.close(lock)
