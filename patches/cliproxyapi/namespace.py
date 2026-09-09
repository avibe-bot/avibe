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

from budgets import PHASES
from isolation import validate_state_root, validate_temporary_root


# No public re-entry option. Only the parent constructs this fixed invocation,
# with an unnamed control descriptor in pass_fds, after parsing the public CLI.
# Arbitrary root Python is outside this approved CLI's threat boundary.
CHILD_TRAMPOLINE = (
    "import sys; sys.path.insert(0, sys.argv[1]); "
    "import namespace; namespace.child_from_control(int(sys.argv[2]))"
)


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
            stdout=subprocess.PIPE, timeout=PHASES[args.phase].preflight_seconds,
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


def open_receipts(args: argparse.Namespace, stack: ExitStack):
    """Reserve root-owned output and an unnamed probe channel before execution."""
    control = args.root / "receipts"
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "go_archive", "output"):
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
    handoff_name = args.receipt + ".control"
    handoff_fd = os.open(handoff_name, os.O_RDWR | flags, 0o600, dir_fd=control_fd)
    handoff = stack.enter_context(os.fdopen(handoff_fd, "w+b"))
    os.unlink(handoff_name, dir_fd=control_fd)
    return terminal, probe, handoff, control_fd


def allocate_output(args: argparse.Namespace, stack: ExitStack) -> None:
    """One new child-writable output, beneath a parent-owned unmounted directory."""
    root_fd = os.open(args.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, root_fd)
    try:
        os.mkdir("runs", mode=0o750, dir_fd=root_fd)
        created = True
    except FileExistsError:
        created = False
    runs_fd = os.open("runs", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    stack.callback(os.close, runs_fd)
    if created:
        os.fchown(runs_fd, -1, args.gid)
        os.fchmod(runs_fd, 0o750)
    info = os.fstat(runs_fd)
    if info.st_uid != os.getuid() or info.st_gid != args.gid or stat.S_IMODE(info.st_mode) != 0o750:
        raise RuntimeError("Run collection must be parent-owned, not child-writable.")
    os.mkdir(args.receipt, mode=0o750, dir_fd=runs_fd)
    output_fd = os.open(args.receipt, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=runs_fd)
    stack.callback(os.close, output_fd)
    os.fchown(output_fd, args.uid, args.gid)
    os.fchmod(output_fd, 0o750)


def selected_build_receipt(args: argparse.Namespace, control_fd: int) -> dict | None:
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
    return {"output": str(args.build), "receipt": args.build.name,
            "parent_receipt_sha256": hashlib.sha256(raw).hexdigest()}


def child_from_control(fd: int) -> None:
    """Private fixed trampoline: consume parent custody and close it before setup."""
    with os.fdopen(fd, "rb") as control:
        info = os.fstat(control.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 0
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise RuntimeError("Missing unnamed root-owned parent handoff.")
        raw = control.read(65537)
    if len(raw) > 65536:
        raise RuntimeError("Oversized parent handoff.")
    values = json.loads(raw)
    outer = values.pop("outer_namespaces")
    for name in ("root", "rootfs", "source", "fixture", "state", "recipe", "toolchain",
                 "python_env", "go_archive", "output", "build"):
        values[name] = Path(values[name]) if values[name] is not None else None
    inside(argparse.Namespace(**values), outer)


def inside(args: argparse.Namespace, outer: dict) -> None:
    actual = namespace_ids()
    required = {"mnt", "net", "pid"}
    if (set(outer) != required or set(actual) != required
            or any(not isinstance(outer[name], str) or actual[name] == outer[name] for name in required)):
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
    for path in (args.source, args.fixture, args.recipe, args.toolchain, args.python_env, args.go_archive):
        bind(path)
    if args.build is not None:
        bind(args.build)
    bind(args.state, writable=True)
    bind(args.output, writable=True)
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
        "uid": args.uid, "gid": args.gid, "sentinel_ports": args.sentinel_ports,
        "source": str(args.source), "fixture": str(args.fixture),
        "recipe": str(args.recipe), "state": str(args.state),
        "receipt": args.receipt, "output": str(args.output), "phase": args.phase,
        "budget": PHASES[args.phase].receipt(), "selected_build": args.selected_build,
        "toolchain": str(args.toolchain), "go_archive": str(args.go_archive),
        "python_env": str(args.python_env),
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
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Use sudo in the explicitly authorized Linux task scratch; no fallback.")
    args.uid, args.gid = int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    if args.uid <= 0 or args.gid <= 0:
        raise ValueError("The invoking task owner must be non-root.")
    args.root = validate_temporary_root(args.root, owner_uid=args.uid).resolve(strict=True)
    for name in ("source", "fixture", "state", "recipe", "toolchain", "python_env", "go_archive"):
        path = getattr(args, name).resolve(strict=True)
        if path == args.root or not path.is_relative_to(args.root):
            raise ValueError(f"{name} must be a dedicated child of the allocated scratch.")
        setattr(args, name, path)
    if args.recipe != Path(__file__).resolve().parent:
        raise ValueError("Recipe mount must be the directory of this inspected launcher.")
    validate_state_root(args.state, owner_uid=args.uid)
    paths = [args.source, args.fixture, args.state, args.recipe, args.toolchain, args.python_env, args.go_archive]
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(paths) for b in paths[i + 1:]):
        raise ValueError("Source, fixture, state, recipe, toolchain and Python directories must be disjoint.")
    if Path(args.receipt).name != args.receipt or args.receipt in ("", ".", ".."):
        raise ValueError("Receipt must be one new file name in the parent's receipts directory.")
    args.output = args.root / "runs" / args.receipt
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
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        raise ValueError("An explicit test/build command is required.")
    with ExitStack() as stack:
        terminal, probe, handoff, control_fd = open_receipts(args, stack)
        rootfs = None
        sentinels = []
        result = None
        failure = None
        cleanup_errors = []
        rootfs_removed = False
        try:
            args.selected_build = selected_build_receipt(args, control_fd)
            allocate_output(args, stack)
            rootfs = Path(tempfile.mkdtemp(prefix="rootfs-", dir=args.root))
            for family in (socket.AF_INET, socket.AF_INET6):
                sentinels.append(Sentinel(family))
            values = {
                **vars(args), "rootfs": rootfs, "proof_fd": probe.fileno(),
                "outer_namespaces": namespace_ids(), "sentinel_ports": [server.port for server in sentinels],
            }
            handoff.write(json.dumps(values, default=str).encode())
            handoff.flush()
            handoff.seek(0)
            command = ["/usr/bin/unshare", "--mount", "--net", "--pid", "--fork", "--kill-child=KILL",
                       "/usr/bin/python3", "-I", "-B", "-c", CHILD_TRAMPOLINE,
                       str(Path(__file__).resolve().parent), str(handoff.fileno())]
            # subprocess.run kills and waits for the launcher on timeout.
            # unshare --kill-child also kills its PID 1 and all its descendants.
            result = subprocess.run(
                command, close_fds=True, pass_fds=(probe.fileno(), handoff.fileno()),
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
            "entry_custody": "fixed-trampoline-unnamed-parent-control",
            "phase": args.phase, "budget": PHASES[args.phase].receipt(),
            "output": str(args.output), "selected_build": getattr(args, "selected_build", None),
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
