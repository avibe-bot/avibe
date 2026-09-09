"""Temporary Linux test envelope; no VM, host network, package or service setup.

Run with sudo only in an explicitly allocated task directory. Privileged setup
finishes before any candidate/test code runs. Every mount lives in the child
mount namespace, and its PID 1 exit destroys all remaining child processes.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import resource
import socket
import stat
import subprocess
import sys
import tempfile
import threading


def namespace_ids() -> dict[str, str]:
    return {name: os.readlink(f"/proc/self/ns/{name}") for name in ("mnt", "net", "pid")}


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
            stdout=subprocess.PIPE, timeout=30,
        )
        if result.returncode:
            return result.returncode
        if len(result.stdout) > 65536:
            raise RuntimeError("Preflight result exceeds the receipt limit.")
        proof = json.loads(result.stdout)
        if proof.get("isolation_probe") != "pass":
            raise RuntimeError("Preflight did not return a passing result.")
        channel.write(result.stdout)
        channel.flush()
        os.fsync(channel.fileno())
    result = subprocess.run([*drop, *args.command], env=env, cwd=args.source, close_fds=True)
    return result.returncode


def open_receipts(args: argparse.Namespace, stack: ExitStack):
    """Reserve root-owned output and an unnamed probe channel before execution."""
    control = args.root / "receipts"
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env"):
        path = getattr(args, name)
        if control == path or control.is_relative_to(path) or path.is_relative_to(control):
            raise ValueError("Parent receipts must be outside every child bind mount.")
    root_fd = os.open(args.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, root_fd)
    created = False
    try:
        os.mkdir("receipts", mode=0o750, dir_fd=root_fd)
        created = True
    except FileExistsError:
        pass
    control_fd = os.open("receipts", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
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
    return terminal, probe


def inside(args: argparse.Namespace) -> None:
    outer = json.loads(args.parent_namespaces)
    actual = namespace_ids()
    if any(actual[name] == outer[name] for name in outer):
        raise RuntimeError("Refusing mount/network setup outside new mount, network and PID namespaces.")
    # MUST be first: never propagate a bind or mount operation into the guest.
    run("/usr/bin/mount", "--make-rprivate", "/")
    interfaces = json.loads(subprocess.check_output(["/usr/sbin/ip", "-j", "link"]))
    if [item["ifname"] for item in interfaces] != ["lo"]:
        raise RuntimeError("Private namespace has an unexpected interface.")
    if args.network == "loopback":
        run("/usr/sbin/ip", "link", "set", "lo", "up")

    rootfs = args.rootfs
    run("/usr/bin/mount", "-t", "tmpfs", "-o", "mode=0755,nosuid,nodev", "tmpfs", str(rootfs))

    def bind(source: Path, *, writable: bool = False) -> None:
        target = rootfs / str(source).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.touch()
        run("/usr/bin/mount", "--bind", str(source), str(target))
        flags = "remount,bind,nosuid" + ("" if writable else ",ro")
        run("/usr/bin/mount", "-o", flags, str(target))

    for name in ("/usr/bin", "/usr/sbin", "/usr/lib", "/usr/share"):
        bind(Path(name))
    for name in ("bin", "sbin", "lib", "lib64"):
        original = Path("/") / name
        if original.is_symlink():
            (rootfs / name).symlink_to(os.readlink(original))
        elif original.exists():
            bind(original)
    for path in (args.source, args.fixture, args.recipe, args.toolchain, args.python_env):
        bind(path)
    bind(args.state, writable=True)
    for name in ("null", "zero", "random", "urandom"):
        bind(Path("/dev") / name, writable=True)
    (rootfs / "dev/fd").symlink_to("/proc/self/fd")
    (rootfs / "etc").mkdir()
    (rootfs / "etc/passwd").write_text(f"root:x:0:0:root:/nonexistent:/bin/false\nsandbox:x:{args.uid}:{args.gid}:sandbox:/nonexistent:/bin/false\n")
    (rootfs / "etc/group").write_text(f"root:x:0:\nsandbox:x:{args.gid}:\n")
    (rootfs / "etc/hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
    (rootfs / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files\n")
    (rootfs / "etc/resolv.conf").write_text("")
    (rootfs / "proc").mkdir()
    run("/usr/bin/mount", "-t", "proc", "-o", "ro,nosuid,nodev,noexec,hidepid=2", "proc", str(rootfs / "proc"))
    (rootfs / "run").mkdir()
    marker = {
        "outer_namespaces": outer, "namespaces": actual, "network": args.network,
        "uid": args.uid, "gid": args.gid, "sentinel_ports": json.loads(args.sentinel_ports),
        "source": str(args.source), "fixture": str(args.fixture),
        "recipe": str(args.recipe), "state": str(args.state),
        "receipt": args.receipt,
    }
    (rootfs / "run/avibe-engine-test-isolation.json").write_text(json.dumps(marker))
    run("/usr/bin/mount", "-o", "remount,ro,nosuid,nodev", str(rootfs))
    os.chroot(rootfs)
    os.chdir("/")
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
        "HOME": str(args.state / "home"), "PYTHONDONTWRITEBYTECODE": "1",
    }
    # Exiting this namespace's PID 1 reaps/kills any descendant left behind.
    raise SystemExit(run_candidate(args, drop, env))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "fixture", "state", "recipe", "toolchain", "python-env"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--network", required=True, choices=("none", "loopback"))
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--parent-namespaces", help=argparse.SUPPRESS)
    parser.add_argument("--sentinel-ports", help=argparse.SUPPRESS)
    parser.add_argument("--rootfs", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--uid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--gid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--proof-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Use sudo in the explicitly authorized Linux task scratch; no fallback.")
    args.root = args.root.resolve(strict=True)
    if args.root.parent == Path("/") or args.root in (Path("/tmp"), Path("/var/tmp")):
        raise ValueError("Root must be a narrow, allocated task directory.")
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env"):
        path = getattr(args, name).resolve(strict=True)
        if path == args.root or not path.is_relative_to(args.root):
            raise ValueError(f"{name} must be a dedicated child of the allocated scratch.")
        setattr(args, name, path)
    paths = [args.source, args.fixture, args.state, args.recipe, args.toolchain, args.python_env]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("Source, fixture, state, recipe, toolchain and Python directories must be disjoint.")
    if Path(args.receipt).name != args.receipt or args.receipt in ("", ".", ".."):
        raise ValueError("Receipt must be one new file name in the parent's receipts directory.")
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        raise ValueError("An explicit test/build command is required.")
    if args.parent_namespaces:
        inside(args)
        return
    args.uid, args.gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    if args.uid <= 0 or args.gid <= 0:
        raise ValueError("The invoking task owner must be non-root.")
    with ExitStack() as stack:
        terminal, probe = open_receipts(args, stack)
        rootfs = Path(tempfile.mkdtemp(prefix="rootfs-", dir=args.root))
        sentinels = []
        result = None
        failure = None
        cleanup_errors = []
        rootfs_removed = False
        try:
            for family in (socket.AF_INET, socket.AF_INET6):
                sentinels.append(Sentinel(family))
            command = ["/usr/bin/unshare", "--mount", "--net", "--pid", "--fork", "--kill-child=KILL",
                       "/usr/bin/python3", "-B", str(Path(__file__).resolve())]
            for name in ("root", "source", "fixture", "state", "recipe", "toolchain", "python_env"):
                command += ["--" + name.replace("_", "-"), str(getattr(args, name))]
            command += [
                "--network", args.network, "--receipt", args.receipt,
                "--rootfs", str(rootfs), "--uid", str(args.uid), "--gid", str(args.gid),
                "--proof-fd", str(probe.fileno()),
                "--parent-namespaces", json.dumps(namespace_ids()),
                "--sentinel-ports", json.dumps([server.port for server in sentinels]),
                "--", *args.command,
            ]
            # subprocess.run kills and waits for the launcher on timeout.
            # unshare --kill-child also kills its PID 1 and all its descendants.
            result = subprocess.run(
                command, close_fds=True, pass_fds=(probe.fileno(),),
                stdin=subprocess.DEVNULL, timeout=900,
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
                rootfs.rmdir()
                rootfs_removed = True
            except OSError as exc:
                cleanup_errors.append(type(exc).__name__)
        receipt = {
            "command": args.command, "network": args.network,
            "exit_code": result.returncode if result is not None else None,
            "outside_sentinel_connections": [server.connections for server in sentinels],
            "launcher_reaped": result is not None or isinstance(failure, subprocess.TimeoutExpired),
            "temporary_rootfs_removed": rootfs_removed, "cleanup_errors": cleanup_errors,
            "failure": type(failure).__name__ if failure is not None else None,
            "preflight_custody": "unnamed-supervisor-fd-closed-before-candidate",
        }
        probe.seek(0)
        raw = probe.read(65537)
        try:
            proof = json.loads(raw)
            if len(raw) > 65536 or not isinstance(proof, dict) or proof.get("isolation_probe") != "pass":
                raise ValueError("Invalid preflight.")
        except (ValueError, UnicodeError):
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
