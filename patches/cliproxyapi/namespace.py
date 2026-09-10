"""Temporary Linux test envelope; no VM, host network, package or service setup.

Run with sudo only in an explicitly allocated task directory. Privileged setup
finishes before any candidate/test code runs. Every mount lives in the child
mount namespace, and its PID 1 exit destroys all remaining child processes.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import secrets
import socket
import stat
import struct
import subprocess
import sys
import threading

from budgets import PHASES
from isolation import (
    NAMESPACE_NAMES, STORAGE_ENV, StorageContext, environment_directories,
    keyring_identity, require_kernel_evidence, require_namespace_ids, require_namespaces,
    validate_state_root, validate_temporary_root,
)


# No public re-entry option. Only the parent constructs this fixed invocation,
# with an unnamed control descriptor in pass_fds, after parsing the public CLI.
# Arbitrary root Python is outside this approved CLI's threat boundary.
CHILD_TRAMPOLINE = """
import hashlib, json, os, resource, stat, sys, types
def bootstrap():
    with os.fdopen(int(sys.argv[1]), "rb") as control:
        info = os.fstat(control.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("Missing unnamed root-owned parent handoff.")
        raw = control.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Oversized parent handoff.")
    values = json.loads(raw)
    sources = values.pop("bootstrap")
    if not isinstance(sources, dict) or set(sources) != {"budgets", "isolation", "namespace"}:
        raise RuntimeError("Incomplete fixed bootstrap.")
    verified = {}
    fds = [record.get("fd") for record in sources.values() if isinstance(record, dict)]
    if len(fds) != 3 or any(type(fd) is not int or fd < 3 for fd in fds) or len(set(fds)) != 3:
        raise RuntimeError("Invalid bootstrap descriptor.")
    try:
        for name in ("budgets", "isolation", "namespace"):
            record = sources[name]
            fd = record["fd"]
            info = os.fstat(fd)
            identity = {field: getattr(info, "st_" + field) for field in
                        ("dev", "ino", "mode", "uid", "gid", "rdev")}
            if not stat.S_ISREG(info.st_mode) or identity != record["identity"] or info.st_size > 65536:
                raise RuntimeError("Bootstrap source identity changed.")
            data = os.pread(fd, 65537, 0)
            if len(data) > 65536 or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise RuntimeError("Bootstrap source contents changed.")
            verified[name] = data
    finally:
        for fd in fds:
            os.close(fd)
    # All source bytes are verified before the first non-stdlib import/effect.
    for name in ("budgets", "isolation", "namespace"):
        module = types.ModuleType(name)
        module.__file__ = values["recipe"] + "/" + name + ".py"
        sys.modules[name] = module
        exec(compile(verified[name], module.__file__, "exec"), module.__dict__)
    sys.modules["namespace"].child_from_control(values)
try:
    bootstrap()
except Exception:
    # Fresh, single-purpose child: close every inherited/partial acquisition on
    # malformed handoff/import/setup failure, without inspecting Linux proc.
    os.closerange(3, int(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))
    raise
"""

INPUT_NAMES = ("source", "fixture", "state", "recipe", "toolchain", "python_env", "go_archive")
MS_RDONLY, MS_NOSUID, MS_NODEV, MS_NOEXEC = 1, 2, 4, 8
MS_REMOUNT, MS_BIND, MS_REC, MS_PRIVATE = 32, 4096, 16384, 1 << 18


def require_native_abi() -> None:
    """Only the inspected native ABI; this does not measure guest support."""
    if (sys.platform != "linux" or platform.machine() != "aarch64"
            or sys.byteorder != "little" or ctypes.sizeof(ctypes.c_void_p) != 8):
        raise RuntimeError("Unsupported Linux syscall ABI; require native AArch64 little-endian64.")


def keyring_program() -> bytes:
    """The finite inspected classic-BPF program, not a general syscall policy."""
    instructions = (
        (0x20, 0, 0, 4), (0x15, 1, 0, 0xC00000B7), (0x06, 0, 0, 0x80000000),
        (0x20, 0, 0, 0), (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x80000000),
        (0x15, 3, 0, 217), (0x15, 2, 0, 218), (0x15, 1, 0, 219),
        (0x06, 0, 0, 0x7FFF0000), (0x06, 0, 0, 0x00050001),
    )
    return b"".join(struct.pack("<HBBI", *item) for item in instructions)


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def linux_prctl(option: int, a2: int = 0, a3: int = 0, a4: int = 0, a5: int = 0) -> int:
    require_native_abi()
    call = ctypes.CDLL(None, use_errno=True).prctl
    # prctl is variadic: every trailing argument is explicitly native unsigned
    # long, including the sock_fprog pointer value. Never truncate it to int.
    call.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    call.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = call(option, a2, a3, a4, a5)
    if result == -1:
        raise OSError(ctypes.get_errno(), "Linux prctl failed.")
    return result


def install_keyring_boundary() -> dict:
    require_native_abi()
    raw = keyring_program()
    expected = keyring_identity()
    if len(raw) != 88 or hashlib.sha256(raw).hexdigest() != expected["program_sha256"]:
        raise RuntimeError("Unexpected keyring filter program.")
    array = (SockFilter * (len(raw) // 8)).from_buffer_copy(raw)
    program = SockFprog(len(array), array)
    address = ctypes.cast(ctypes.pointer(program), ctypes.c_void_p).value
    # Keep array/program alive through installation and every readback.
    for option, argument, pointer, result in ((38, 1, 0, 0), (22, 2, address, 0),
                                             (39, 0, 0, 1), (21, 0, 0, 2)):
        actual = linux_prctl(option, argument, pointer, 0, 0)
        if type(actual) is not int or actual != result:
            raise RuntimeError("Keyring filter/no-new-privileges installation or readback failed.")
    return expected


def linux_mount(source: str | None, target: str, filesystem: str | None,
                flags: int, data: str | None = None) -> None:
    """Direct native mount(2), never mount(8) helpers or config.

    HOST consumers intercept this adapter. Actual proc-FD bind/overmount
    semantics and the installed Linux kernel remain execution prerequisites.
    """
    require_native_abi()
    if (not isinstance(target, str) or not target.startswith("/") or "\0" in target
            or type(flags) is not int or not 0 <= flags < 2**64):
        raise ValueError("Invalid native mount target or flags.")

    def encoded(value):
        if value is None:
            return None
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("Invalid mount argument.")
        return os.fsencode(value)
    source_bytes, target_bytes, type_bytes, data_bytes = map(encoded, (source, target, filesystem, data))
    call = ctypes.CDLL(None, use_errno=True).mount
    call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
    call.restype = ctypes.c_int
    data_pointer = ctypes.cast(ctypes.c_char_p(data_bytes), ctypes.c_void_p) if data_bytes is not None else None
    ctypes.set_errno(0)
    if call(source_bytes, target_bytes, type_bytes, flags, data_pointer) != 0:
        raise OSError(ctypes.get_errno(), "Linux mount failed.")


def descriptor_identity(info) -> dict:
    return {field: getattr(info, "st_" + field) for field in ("dev", "ino", "mode", "uid", "gid", "rdev")}


def retained_handle(fd: int, kind: str) -> dict:
    info = os.fstat(fd)
    predicate = {"directory": stat.S_ISDIR, "file": stat.S_ISREG, "device": stat.S_ISCHR}[kind]
    if not predicate(info.st_mode):
        raise ValueError("Pinned resource has the wrong type.")
    return {"fd": fd, "kind": kind, "identity": descriptor_identity(info)}


def check_handle(record: dict) -> int:
    if (not isinstance(record, dict) or type(record.get("fd")) is not int or record["fd"] < 3
            or record.get("kind") not in ("directory", "file", "device")):
        raise RuntimeError("Invalid pinned resource.")
    if retained_handle(record["fd"], record["kind"]) != record:
        raise RuntimeError("Pinned resource identity changed.")
    return record["fd"]


def pin_beneath(stack: ExitStack, root_fd: int, relative: Path, *, kind: str,
                expected: dict | None = None) -> dict:
    if relative.is_absolute() or not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise ValueError("Pinned input must be a nonempty relative path.")
    current = root_fd
    for index, part in enumerate(relative.parts):
        selected_kind = kind if index == len(relative.parts) - 1 else "directory"
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        if selected_kind == "directory":
            flags |= os.O_DIRECTORY
        elif selected_kind == "device":
            require_native_abi()
            # Linux asm-generic/fcntl.h. O_PATH opens no device for I/O.
            flags = 0o10000000 | os.O_NOFOLLOW | os.O_CLOEXEC
        current = os.open(part, flags, dir_fd=current)
        stack.callback(os.close, current)
        record = retained_handle(current, selected_kind)
    if expected is not None and record["identity"] != expected:
        raise ValueError("Pinned resource changed after admission.")
    return record


def pin_system_resources(stack: ExitStack) -> tuple[list[dict], dict]:
    """Finite trusted-system prerequisite, not caller-supplied input policy."""
    require_native_abi()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    stack.callback(os.close, fd)
    mounts, aliases = [], {}

    def pin(path: str, kind: str, *, writable: bool = False):
        record = pin_beneath(stack, fd, Path(path).relative_to("/"), kind=kind)
        info = record["identity"]
        if info["uid"] != 0 or (kind != "device" and info["mode"] & 0o002):
            raise RuntimeError("Installed system inputs must retain their inspected trusted ownership.")
        mounts.append({"path": path, "resource": record, "writable": writable})

    for name in ("/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share"):
        pin(name, "directory")
    for name in ("bin", "sbin", "lib", "lib64"):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid != 0:
                raise RuntimeError("Untrusted installed system alias.")
            value = os.readlink(name, dir_fd=fd)
            if value not in ("usr/" + name, "/usr/" + name):
                raise RuntimeError("Unexpected installed system alias; no inferred target.")
            aliases[name] = value
        else:
            pin("/" + name, "directory")
    # No host RNG device authority: syscall randomness is unchanged. A consumer
    # requiring an RNG pathname lacks a startup prerequisite; do not add a fallback.
    for name, minor in (("null", 3), ("zero", 5)):
        pin("/dev/" + name, "device", writable=True)
        device = mounts[-1]["resource"]["identity"]["rdev"]
        if (os.major(device), os.minor(device)) != (1, minor):
            raise RuntimeError("Unexpected installed device identity.")
    return mounts, aliases


def pin_bootstrap(stack: ExitStack, recipe: dict) -> dict:
    result = {}
    for name in ("budgets", "isolation", "namespace"):
        record = pin_beneath(stack, check_handle(recipe), Path(name + ".py"), kind="file")
        fd = record["fd"]
        raw = os.pread(fd, 65537, 0)
        if len(raw) > 65536:
            raise RuntimeError("Oversized inspected bootstrap source.")
        result[name] = {"fd": fd, "identity": record["identity"], "sha256": hashlib.sha256(raw).hexdigest()}
    return result


def _proc_field(directory: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Original sudo process metadata is unavailable.")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise RuntimeError("Original sudo process metadata exceeds its bound.")
    return value


def sudo_storage_context(uid: int, expected: str) -> StorageContext:
    """Bind pre-sudo caller identity to the live sudo process, never root's HOME.

    Linux proc environ exposes the process's exec-time environment. If sudo
    scrubs it, execs away, or otherwise makes it unavailable, the caller's
    independently captured digest will not match and setup fails closed.
    No caller-authored list of allegedly safe roots is accepted.
    """
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("An exact original-caller storage digest is required.")
    parent = os.getppid()
    fd = os.open(f"/proc/{parent}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        sudo = os.stat("/usr/bin/sudo", follow_symlinks=False)
        executable = os.stat("exe", dir_fd=fd)
        if (not stat.S_ISREG(sudo.st_mode) or sudo.st_uid != 0 or sudo.st_mode & 0o022
                or (executable.st_dev, executable.st_ino) != (sudo.st_dev, sudo.st_ino)):
            raise RuntimeError("Original context requires the live /usr/bin/sudo parent.")
        before = _proc_field(fd, "stat", 8192).rsplit(b")", 1)[1].split()[19]
        raw = _proc_field(fd, "environ", 1024 * 1024)
        selected = {}
        names = {name.encode(): name for name in STORAGE_ENV}
        for entry in raw.split(b"\0"):
            name, separator, value = entry.partition(b"=")
            if separator and name in names:
                key = names[name]
                if key in selected:
                    raise RuntimeError("Ambiguous original storage environment.")
                try:
                    selected[key] = value.decode("utf-8")
                except UnicodeError:
                    raise ValueError("Invalid original storage path encoding.") from None
        context = StorageContext.capture(uid=uid, environment=selected)
        after = _proc_field(fd, "stat", 8192).rsplit(b")", 1)[1].split()[19]
        if (os.getppid() != parent or before != after
                or _proc_field(fd, "environ", 1024 * 1024) != raw
                or context.fingerprint() != expected):
            raise RuntimeError("Original caller storage context was lost or changed before sudo.")
    finally:
        os.close(fd)
    # Root's own passwd-home protection is additive, never a replacement for
    # the invoking user's effective HOME and configured storage authorities.
    return context.with_root_identity()


def namespace_ids() -> dict[str, str]:
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in NAMESPACE_NAMES}


class Sentinel:
    """Task-owned outside-namespace listener; even one accepted connection fails."""

    def __init__(self, family: int):
        self.socket = socket.socket(family)
        if family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self.socket.bind(("::1" if family == socket.AF_INET6 else "127.0.0.1", 0))
        self.socket.listen()
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.connections = 0
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run)
        self.thread.start()

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            self.connections += 1
            connection.close()

    def close(self) -> None:
        self.stopping.set()
        self.thread.join(timeout=2)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("Sentinel thread did not stop.")


def run(*command: str) -> None:
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, close_fds=True)


def run_candidate(args: argparse.Namespace, drop: list[str], env: dict[str, str]) -> int:
    """Capture the read-only probe before candidate code, then close its channel."""
    # This unlinked, parent-opened file has no name in the child's filesystem.
    # Neither subprocess inherits its descriptor; it closes before the candidate.
    with os.fdopen(args.proof_fd, "wb") as channel:
        probe = [str(args.python_env / "bin/python"), "-B", str(args.recipe / "isolation_probe.py")]
        result = subprocess.run(
            [*drop, *probe], env=env, cwd=args.state, close_fds=True,
            stdout=subprocess.PIPE, timeout=PHASES[args.phase].preflight_seconds,
        )
        if result.returncode:
            return result.returncode
        if len(result.stdout) > 65536:
            raise RuntimeError("Preflight result exceeds the receipt limit.")
        proof = json.loads(result.stdout)
        if proof.get("isolation_probe") != "pass":
            raise RuntimeError("Preflight did not return a passing result.")
        require_kernel_evidence(proof)
        if proof["outer_namespaces"] != args.outer_namespaces:
            raise RuntimeError("Preflight belongs to another parent namespace epoch.")
        channel.write(result.stdout)
        channel.flush()
        os.fsync(channel.fileno())
    try:
        result = subprocess.run(
            [*drop, *args.command], env=env, cwd=args.source, close_fds=True,
            timeout=PHASES[args.phase].candidate_seconds,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run killed/waited for the direct child. PID 1 then exits
        # so the kernel tears down any remaining descendants in this PID view.
        print("Candidate exceeded the derived phase allowance.", file=sys.stderr, flush=True)
        return 124
    return result.returncode


def check_root_descriptor(args: argparse.Namespace, root_fd: int) -> None:
    """Recheck actual custody and pathname identity, never repair a supplied root."""
    info = os.fstat(root_fd)
    named = args.root.lstat()
    if (args.root.resolve(strict=True) != args.root
            or not stat.S_ISDIR(info.st_mode) or not stat.S_ISDIR(named.st_mode)
            or info.st_uid != args.uid or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
            != (named.st_dev, named.st_ino, named.st_mode, named.st_uid)):
        raise ValueError("Privileged scratch must remain the caller-owned mode-0700 admitted directory.")


def open_receipts(args: argparse.Namespace, stack: ExitStack, root_fd: int):
    """Reserve root-owned output and an unnamed probe channel before execution."""
    control = args.root / "receipts"
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "go_archive", "output"):
        path = getattr(args, name)
        if control == path or control.is_relative_to(path) or path.is_relative_to(control):
            raise ValueError("Parent receipts must be outside every child bind mount.")
    check_root_descriptor(args, root_fd)
    created = False
    try:
        os.mkdir("receipts", mode=0o750, dir_fd=root_fd)
        created = True
    except FileExistsError:
        pass
    control_fd = os.open("receipts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                         os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd)
    stack.callback(os.close, control_fd)
    if created:
        os.fchown(control_fd, -1, args.gid)
        os.fchmod(control_fd, 0o750)
    info = os.fstat(control_fd)
    if info.st_uid != os.getuid() or info.st_gid != args.gid or stat.S_IMODE(info.st_mode) != 0o750:
        raise RuntimeError("Refusing a preexisting receipt directory with different ownership or permissions.")
    flags = os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    terminal_fd = os.open(args.receipt, os.O_WRONLY | flags, 0o640, dir_fd=control_fd)
    terminal = stack.enter_context(os.fdopen(terminal_fd, "w"))
    os.fchown(terminal.fileno(), -1, args.gid)
    os.fchmod(terminal.fileno(), 0o640)
    probe_name = args.receipt + ".preflight"
    probe_fd = os.open(probe_name, os.O_RDWR | flags, 0o600, dir_fd=control_fd)
    probe = stack.enter_context(os.fdopen(probe_fd, "r+b"))
    # Unlink while still in setup. There are no path-based reads, writes,
    # chowns or unlinks of receipt/probe artifacts after candidate execution.
    os.unlink(probe_name, dir_fd=control_fd)
    handoff_name = args.receipt + ".control"
    handoff_fd = os.open(handoff_name, os.O_RDWR | flags, 0o600, dir_fd=control_fd)
    handoff = stack.enter_context(os.fdopen(handoff_fd, "w+b"))
    os.unlink(handoff_name, dir_fd=control_fd)
    return terminal, probe, handoff, control_fd


def allocate_output(args: argparse.Namespace, stack: ExitStack, root_fd: int) -> tuple[dict, dict]:
    """One new child-writable output, beneath a parent-owned unmounted directory."""
    check_root_descriptor(args, root_fd)
    try:
        os.mkdir("runs", mode=0o750, dir_fd=root_fd)
        created = True
    except FileExistsError:
        created = False
    runs_fd = os.open("runs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                      os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root_fd)
    stack.callback(os.close, runs_fd)
    if created:
        os.fchown(runs_fd, -1, args.gid)
        os.fchmod(runs_fd, 0o750)
    info = os.fstat(runs_fd)
    if info.st_uid != os.getuid() or info.st_gid != args.gid or stat.S_IMODE(info.st_mode) != 0o750:
        raise RuntimeError("Run collection must be parent-owned, not child-writable.")
    os.mkdir(args.receipt, mode=0o750, dir_fd=runs_fd)
    output_fd = os.open(args.receipt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                        os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=runs_fd)
    stack.callback(os.close, output_fd)
    os.fchown(output_fd, args.uid, args.gid)
    os.fchmod(output_fd, 0o750)
    return retained_handle(runs_fd, "directory"), retained_handle(output_fd, "directory")


def selected_build_receipt(args: argparse.Namespace, control_fd: int, build: dict | None = None) -> dict | None:
    """Read only a prior PARENT-owned terminal receipt before candidate launch."""
    if args.build is None:
        return None
    fd = os.open(args.build.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=control_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o640):
            raise RuntimeError("Selected build lacks an exclusive parent-owned receipt.")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Selected build parent receipt is oversized.")
    previous = json.loads(raw)
    if previous.get("status") != "passed" or previous.get("phase") != "build" or previous.get("output") != str(args.build):
        raise RuntimeError("Selected build does not have a successful matching parent receipt.")
    if build is None or previous.get("output_identity") != build["identity"]:
        raise RuntimeError("Selected build directory does not match its parent-owned receipt.")
    check_handle(build)
    return {"output": str(args.build), "receipt": args.build.name,
            "parent_receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "output_identity": build["identity"]}


def child_from_control(values: dict) -> None:
    """Only the fixed trampoline supplies its already-authenticated bounded body."""
    outer = values.pop("outer_namespaces")
    for name in ("root", "rootfs", "source", "fixture", "state", "recipe", "toolchain",
                 "python_env", "go_archive", "output", "build"):
        values[name] = Path(values[name]) if values[name] is not None else None
    inside(argparse.Namespace(**values), outer)


def protected_directory(stack: ExitStack, root_fd: int, relative: Path) -> int:
    """Create only fresh-rootfs topology; never traverse an already-bound input."""
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Rootfs target must be relative.")
    current = root_fd
    for component in relative.parts:
        try:
            os.mkdir(component, mode=0o755, dir_fd=current)
        except FileExistsError:
            pass
        current = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                          os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=current)
        stack.callback(os.close, current)
        info = os.fstat(current)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("Rootfs target parent lost privileged custody.")
    return current


def prepare_bind_target(stack: ExitStack, root_fd: int, path: Path, kind: str) -> tuple[int, str]:
    relative = path.relative_to("/")
    if not relative.parts or ".." in relative.parts:
        raise ValueError("Invalid rootfs bind target.")
    parent = protected_directory(stack, root_fd, relative.parent)
    if kind == "directory":
        os.mkdir(relative.name, mode=0o755, dir_fd=parent)
    else:
        fd = os.open(relative.name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW |
                     os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=parent)
        os.close(fd)
    return parent, relative.name


def fd_path(fd: int, component: str | None = None) -> str:
    if type(fd) is not int or fd < 3:
        raise ValueError("Invalid retained descriptor.")
    path = f"/proc/self/fd/{fd}"
    if component is not None:
        if not component or Path(component).name != component or component in (".", "..") or "\0" in component:
            raise ValueError("Expected one protected target component.")
        path += "/" + component
    return path


def reopen_rootfs(stack: ExitStack, control: dict, rootfs: dict, name: str) -> int:
    """Open the OVERMOUNT view, not the pre-mount descriptor's underlying inode."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                 os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=check_handle(control))
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o755
            or (info.st_dev, info.st_ino) == (rootfs["identity"]["dev"], rootfs["identity"]["ino"])):
        raise RuntimeError("Rootfs did not become a distinct protected mounted view.")
    return fd


def remove_rootfs(control: dict, rootfs: dict, name: str) -> None:
    """After namespace teardown, remove only our original empty mountpoint."""
    fd = check_handle(control)
    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if descriptor_identity(named) != rootfs["identity"]:
        raise RuntimeError("Rootfs cleanup identity changed; preserve the replacement.")
    os.rmdir(name, dir_fd=fd)


def inside(args: argparse.Namespace, outer: dict) -> None:
    records = [*getattr(args, "handles", {}).values(),
               *(item["resource"] for item in getattr(args, "system_mounts", []))]
    descriptors = [record.get("fd") for record in records if isinstance(record, dict)]
    owned = {fd for fd in descriptors if type(fd) is int and fd >= 3}
    try:
        with ExitStack() as stack:
            for fd in owned:
                stack.callback(os.close, fd)
            actual = namespace_ids()
            require_namespaces(outer, actual)
            context = StorageContext.from_parent(args.storage_context)
            if context.uid != args.uid:
                raise RuntimeError("Private handoff lost its invoking storage owner.")
            required = {"root", "control", "runs", "output", "rootfs", *INPUT_NAMES}
            if args.build is not None:
                required.add("build")
            if set(args.handles) != required:
                raise RuntimeError("Incomplete parent resource custody.")
            if (len(owned) != len(records) or args.proof_fd in owned
                    or type(args.proof_fd) is not int or args.proof_fd < 3):
                raise RuntimeError("Aliased or invalid private descriptor ownership.")
            args.outer_namespaces = outer
            for record in records:
                check_handle(record)
            root_info = args.handles["root"]["identity"]
            if root_info["uid"] != args.uid or stat.S_IMODE(root_info["mode"]) != 0o700:
                raise RuntimeError("Parent task root lost caller custody.")
            for name in ("control", "runs", "rootfs"):
                info = args.handles[name]["identity"]
                if info["uid"] != os.getuid() or stat.S_IMODE(info["mode"]) != (0o700 if name == "rootfs" else 0o750):
                    raise RuntimeError("Parent control directory lost privileged custody.")
            for name in ("state", "output"):
                info = args.handles[name]["identity"]
                if info["uid"] != args.uid or (name == "output" and stat.S_IMODE(info["mode"]) != 0o750):
                    raise RuntimeError("Writable resource lost invoking-user custody.")
            # The original canonical paths remain labels for the private view.
            # Do not resolve them again in a mutable outside pathname namespace.
            for target in (args.root, args.state, args.output):
                for boundary in context.protected:
                    if target == boundary or target.is_relative_to(boundary) or boundary.is_relative_to(target):
                        raise RuntimeError("Private labels overlap original protected storage.")
            linux_mount(None, "/", None, MS_REC | MS_PRIVATE)
            interfaces = json.loads(subprocess.check_output(
                ["/usr/sbin/ip", "-j", "link"], close_fds=True, timeout=10,
            ))
            if [item["ifname"] for item in interfaces] != ["lo"]:
                raise RuntimeError("Private namespace has an unexpected interface.")
            if args.network == "loopback":
                run("/usr/sbin/ip", "link", "set", "lo", "up")
            control = args.handles["control"]
            rootfs_target = fd_path(check_handle(control), args.rootfs.name)
            linux_mount("tmpfs", rootfs_target, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755")
            rootfs_fd = reopen_rootfs(stack, control, args.handles["rootfs"], args.rootfs.name)
            mounts = list(args.system_mounts)
            for name in (*INPUT_NAMES, "output", *(("build",) if args.build is not None else ())):
                mounts.append({"path": str(getattr(args, name)), "resource": args.handles[name],
                               "writable": name in ("state", "output")})
            # Every target and metadata file is opened BEFORE binding any
            # caller-controlled contents. Later privileged work uses retained FDs.
            targets = [prepare_bind_target(stack, rootfs_fd, Path(item["path"]), item["resource"]["kind"])
                       for item in mounts]
            for name, value in args.system_aliases.items():
                if name not in ("bin", "sbin", "lib", "lib64") or value not in ("usr/" + name, "/usr/" + name):
                    raise RuntimeError("Invalid trusted system alias.")
                os.symlink(value, name, dir_fd=rootfs_fd)
            dev = protected_directory(stack, rootfs_fd, Path("dev"))
            os.symlink("/proc/self/fd", "fd", dir_fd=dev)
            etc = protected_directory(stack, rootfs_fd, Path("etc"))
            metadata = {
                "passwd": f"root:x:0:0:root:/nonexistent:/bin/false\nsandbox:x:{args.uid}:{args.gid}:sandbox:/nonexistent:/bin/false\n",
                "group": f"root:x:0:\nsandbox:x:{args.gid}:\n",
                "hosts": "127.0.0.1 localhost\n::1 localhost\n",
                "nsswitch.conf": "passwd: files\ngroup: files\nhosts: files\n", "resolv.conf": "",
            }
            for name, contents in metadata.items():
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o644, dir_fd=etc)
                with os.fdopen(fd, "w") as stream:
                    stream.write(contents)
            proc_parent, proc_name = prepare_bind_target(stack, rootfs_fd, Path("/proc"), "directory")
            run_fd = protected_directory(stack, rootfs_fd, Path("run"))
            marker_fd = os.open("avibe-engine-test-isolation.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=run_fd)
            marker_stream = stack.enter_context(os.fdopen(marker_fd, "w"))
            for item, (parent, name) in zip(mounts, targets):
                source = check_handle(item["resource"])
                target = fd_path(parent, name)
                linux_mount(fd_path(source), target, None, MS_BIND)
                flags = MS_BIND | MS_REMOUNT | MS_NOSUID
                if item["resource"]["kind"] != "device":
                    flags |= MS_NODEV
                if not item["writable"]:
                    flags |= MS_RDONLY
                linux_mount(None, target, None, flags)
            linux_mount("proc", fd_path(proc_parent, proc_name), "proc",
                        MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, "hidepid=2")
            boundary = install_keyring_boundary()
            marker = {
                "outer_namespaces": outer, "namespaces": actual, "network": args.network,
                "uid": args.uid, "gid": args.gid, "sentinel_ports": args.sentinel_ports,
                **{name: str(getattr(args, name)) for name in (*INPUT_NAMES, "root", "output")},
                "receipt": args.receipt, "phase": args.phase, "budget": PHASES[args.phase].receipt(),
                "selected_build": args.selected_build, "storage_context": context.record(),
                "storage_sha256": context.fingerprint(), "keyring_boundary": boundary,
            }
            encoded = json.dumps(marker)
            if len(encoded.encode()) > 65536:
                raise RuntimeError("Oversized private marker.")
            marker_stream.write(encoded)
            marker_stream.flush()
            marker_stream.close()
            linux_mount(None, rootfs_target, None, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV)
            os.fchdir(rootfs_fd)
            os.chroot(".")
            os.chdir("/")
        # Every specifically inherited/setup FD has closed before preflight.
    except BaseException:
        proof_fd = getattr(args, "proof_fd", None)
        if type(proof_fd) is int and proof_fd >= 3 and proof_fd not in owned:
            os.close(proof_fd)
        raise
    # No guest namespace/root directory handles survive into candidate code.
    resource_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    # Keep only the supervisor's result channel. No candidate receives it.
    os.closerange(3, args.proof_fd)
    os.closerange(args.proof_fd + 1, int(resource_limit))
    os.set_inheritable(args.proof_fd, False)
    drop = [
        "/usr/bin/setpriv", "--reuid", str(args.uid), "--regid", str(args.gid),
        "--clear-groups", "--bounding-set=-all", "--inh-caps=-all",
        "--ambient-caps=-all", "--no-new-privs",
    ]
    env = {
        "PATH": f"{args.toolchain}/bin:/usr/bin:/bin:/usr/sbin",
        "HOME": str(args.output / "home"), "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Exiting this namespace's PID 1 reaps/kills any descendant left behind.
    raise SystemExit(run_candidate(args, drop, env))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "fixture", "state", "recipe", "toolchain", "python-env", "go-archive"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--build", type=Path, help="Explicit prior build output; required only for wire.")
    parser.add_argument("--network", required=True, choices=("none", "loopback"))
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--caller-storage-sha256", required=True,
                        help="Storage identity captured by the original non-root caller before sudo.")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Use sudo in the explicitly authorized Linux task scratch; no fallback.")
    args.uid, args.gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    if args.uid <= 0 or args.gid <= 0:
        raise ValueError("The invoking task owner must be non-root.")
    context = sudo_storage_context(args.uid, args.caller_storage_sha256)
    args.storage_context = context.record()
    admitted_root = validate_temporary_root(args.root, owner_uid=args.uid, context=context).resolve(strict=True)
    if args.root != admitted_root:
        raise ValueError("Privileged scratch must use its absolute canonical name.")
    args.root = admitted_root
    admitted_info = args.root.lstat()
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "go_archive"):
        path = context.validate(getattr(args, name)).resolve(strict=True)
        if path == args.root or not path.is_relative_to(args.root):
            raise ValueError(f"{name} must be a dedicated child of the allocated scratch.")
        setattr(args, name, path)
    admitted_inputs = {name: descriptor_identity(getattr(args, name).lstat()) for name in INPUT_NAMES}
    if args.recipe != Path(__file__).resolve().parent:
        raise ValueError("Recipe mount must be the directory of this inspected launcher.")
    validate_state_root(args.state, owner_uid=args.uid, context=context)
    paths = [args.source, args.fixture, args.state, args.recipe, args.toolchain, args.python_env, args.go_archive]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("Source, fixture, state, recipe, toolchain and Python directories must be disjoint.")
    if Path(args.receipt).name != args.receipt or args.receipt in ("", ".", ".."):
        raise ValueError("Receipt must be one new file name in the parent's receipts directory.")
    args.output = args.root / "runs" / args.receipt
    # Check the full public setup and generated environment plan before the
    # first receipt/output allocation, sentinel, subprocess or mount.
    for target in (args.root / "receipts", args.root / "runs", args.output):
        context.validate(target)
    for owner, name in environment_directories(args.output, args.state):
        target = context.validate(owner / name)
        if not target.is_relative_to(owner):
            raise ValueError("Environment directory escapes its task root.")
    for path in paths:
        for reserved in (args.root / "receipts", args.root / "runs"):
            if path == reserved or path.is_relative_to(reserved) or reserved.is_relative_to(path):
                raise ValueError("Input/cache directories must not overlap parent receipts or run outputs.")
    if (args.phase == "wire") != (args.build is not None):
        raise ValueError("Only wire must select one explicit prior build.")
    if args.phase == "build" and args.network != "none":
        raise ValueError("Diagnostic builds require network=none.")
    if args.phase in ("test", "wire") and args.network != "loopback":
        raise ValueError("Network suites require the private loopback mode.")
    if args.build is not None:
        args.build = args.build.resolve(strict=True)
        if args.build.parent != args.root / "runs" or args.build == args.output:
            raise ValueError("Select a different prior build output in this task's runs directory.")
        admitted_build = descriptor_identity(args.build.lstat())
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        raise ValueError("An explicit test/build command is required.")
    outer_namespaces = namespace_ids()
    require_namespace_ids(outer_namespaces)
    with ExitStack() as stack:
        root_fd = os.open(args.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        stack.callback(os.close, root_fd)
        opened_info = os.fstat(root_fd)
        if (opened_info.st_dev, opened_info.st_ino) != (admitted_info.st_dev, admitted_info.st_ino):
            raise ValueError("Privileged scratch changed after original path admission.")
        check_root_descriptor(args, root_fd)
        handles = {"root": retained_handle(root_fd, "directory")}
        for name in INPUT_NAMES:
            handles[name] = pin_beneath(
                stack, root_fd, getattr(args, name).relative_to(args.root),
                kind="file" if name == "go_archive" else "directory", expected=admitted_inputs[name],
            )
        if handles["state"]["identity"]["uid"] != args.uid:
            raise ValueError("Writable state must belong to the invoking user.")
        bootstrap = pin_bootstrap(stack, handles["recipe"])
        system_mounts, system_aliases = pin_system_resources(stack)
        terminal, probe, handoff, control_fd = open_receipts(args, stack, root_fd)
        handles["control"] = retained_handle(control_fd, "directory")
        rootfs = None
        rootfs_identity = None
        sentinels = []
        result = None
        failure = None
        cleanup_errors = []
        rootfs_removed = False
        values = None
        try:
            handles["runs"], handles["output"] = allocate_output(args, stack, root_fd)
            if args.build is not None:
                handles["build"] = pin_beneath(
                    stack, check_handle(handles["runs"]), Path(args.build.name),
                    kind="directory", expected=admitted_build,
                )
            args.selected_build = selected_build_receipt(args, control_fd, handles.get("build"))
            check_root_descriptor(args, root_fd)
            rootfs_name = "rootfs-" + secrets.token_hex(16)
            os.mkdir(rootfs_name, mode=0o700, dir_fd=check_handle(handles["control"]))
            rootfs = args.root / "receipts" / rootfs_name
            rootfs_identity = {"identity": descriptor_identity(os.stat(
                rootfs_name, dir_fd=control_fd, follow_symlinks=False,
            ))}
            handles["rootfs"] = pin_beneath(
                stack, control_fd, Path(rootfs_name), kind="directory", expected=rootfs_identity["identity"],
            )
            for family in (socket.AF_INET, socket.AF_INET6):
                sentinels.append(Sentinel(family))
            values = {
                **vars(args), "rootfs": rootfs, "proof_fd": probe.fileno(),
                "outer_namespaces": outer_namespaces, "sentinel_ports": [server.port for server in sentinels],
                "handles": handles, "system_mounts": system_mounts, "system_aliases": system_aliases,
                "bootstrap": bootstrap,
            }
            encoded = json.dumps(values, default=str).encode()
            if len(encoded) > 65536:
                raise RuntimeError("Oversized private parent handoff.")
            handoff.write(encoded)
            handoff.flush()
            handoff.seek(0)
            command = ["/usr/bin/unshare", "--mount", "--net", "--pid", "--ipc", "--fork", "--kill-child=KILL",
                       "/usr/bin/python3", "-I", "-B", "-c", CHILD_TRAMPOLINE,
                       str(handoff.fileno())]
            passed = {probe.fileno(), handoff.fileno(), *(record["fd"] for record in handles.values()),
                      *(item["resource"]["fd"] for item in system_mounts),
                      *(record["fd"] for record in bootstrap.values())}
            check_root_descriptor(args, root_fd)
            # subprocess.run kills and waits for the launcher on timeout.
            # unshare --kill-child also kills its PID 1 and all its descendants.
            result = subprocess.run(
                command, close_fds=True, pass_fds=tuple(sorted(passed)),
                stdin=subprocess.DEVNULL, timeout=PHASES[args.phase].namespace_seconds,
            )
        except Exception as exc:
            failure = exc
        finally:
            for server in sentinels:
                try:
                    server.close()
                except Exception as exc:
                    cleanup_errors.append(type(exc).__name__)
            try:
                # Only the exact parent-created, empty mount point is removed.
                if rootfs is not None:
                    if rootfs_identity is None:
                        raise RuntimeError("Rootfs allocation identity unavailable; preserve it.")
                    remove_rootfs(handles["control"], rootfs_identity, rootfs.name)
                rootfs_removed = True
            except (OSError, RuntimeError) as exc:
                cleanup_errors.append(type(exc).__name__)
        receipt = {
            "command": args.command, "network": args.network,
            "exit_code": result.returncode if result is not None else None,
            "outside_sentinel_connections": [server.connections for server in sentinels],
            "launcher_reaped": result is not None or isinstance(failure, subprocess.TimeoutExpired),
            "temporary_rootfs_removed": rootfs_removed, "cleanup_errors": cleanup_errors,
            "failure": type(failure).__name__ if failure is not None else None,
            "preflight_custody": "unnamed-supervisor-fd-closed-before-candidate",
            "entry_custody": "fixed-trampoline-unnamed-parent-control",
            "phase": args.phase, "budget": PHASES[args.phase].receipt(),
            "output": str(args.output), "selected_build": getattr(args, "selected_build", None),
            "storage_sha256": context.fingerprint(),
            "output_identity": handles["output"]["identity"] if "output" in handles else None,
        }
        probe.seek(0)
        raw = probe.read(65537)
        try:
            proof = json.loads(raw)
            if len(raw) > 65536 or not isinstance(proof, dict) or proof.get("isolation_probe") != "pass":
                raise ValueError("Invalid preflight.")
            require_kernel_evidence(proof)
            if values is None or proof["outer_namespaces"] != values["outer_namespaces"]:
                raise RuntimeError("Preflight parent epoch changed.")
        except (ValueError, UnicodeError, RuntimeError):
            receipt["preflight"] = "missing-or-invalid"
        else:
            receipt.update(preflight="passed", probe=proof, probe_sha256=hashlib.sha256(raw).hexdigest())
        passed = (
            result is not None and result.returncode == 0 and failure is None
            and not cleanup_errors and len(sentinels) == 2
            and not any(server.connections for server in sentinels)
            and receipt["preflight"] == "passed"
        )
        receipt["status"] = "passed" if passed else "failed"
        # Both files were opened before execution, outside all child mounts.
        # No child-controlled path is inspected even for supplemental evidence.
        terminal.write(json.dumps(receipt, indent=2) + "\n")
        terminal.flush()
        os.fsync(terminal.fileno())
        if failure is not None:
            raise failure
        if not passed:
            raise RuntimeError("Namespace execution or preflight/cleanup/sentinel acceptance failed; inspect parent receipt.")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
