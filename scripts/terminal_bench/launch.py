"""In-container wrapper process ownership for Harbor exec cancellation.

A Docker exec client's death does not terminate its container process. This
launcher records the exact owned wrapper identity before releasing its start
lock. The stop path fences late starts, signals only that identity, and requires
the wrapper's completed inner cleanup receipt. Otherwise grading is refused and
the outer owner must destroy the sandbox; a guessed PID is never signalled.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


@contextmanager
def locked(root: Path):
    import fcntl  # This first Harbor integration supports POSIX task containers.
    root.mkdir(parents=True, exist_ok=True)
    with (root / "launch.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run(root: Path, nonce: str, args: list[str]) -> int:
    with locked(root):
        if (root / "cancelled").exists():
            return 125
        if (root / "wrapper-process.json").exists():
            raise RuntimeError("wrapper_already_started")
        # Persist the launch attempt before spawn. If identity persistence fails
        # (e.g. disk full), stop must NOT mistake an absent pidfile for no child.
        (root / "starting").touch(exist_ok=False)
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", "-m", "scripts.terminal_bench.runner", *args],
                env={**os.environ, "AVIBE_BENCHMARK_LAUNCH_ID": nonce},
                start_new_session=True,
            )
        except BaseException:
            (root / "starting").unlink()
            raise
        try:
            identity = {"pid": process.pid, "created": psutil.Process(process.pid).create_time(), "nonce": nonce}
            temporary = root / "wrapper-process.tmp"
            temporary.write_text(json.dumps(identity), encoding="utf-8")
            temporary.replace(root / "wrapper-process.json")
        except BaseException:
            # Preserve `starting`: the outer sandbox remains responsible if
            # we cannot prove inner shutdown after losing the identity write.
            if process.poll() is None:
                process.terminate()
            raise
    return process.wait()


def stop(root: Path, nonce: str, receipt: Path, timeout: float) -> dict:
    with locked(root):
        (root / "cancelled").touch(exist_ok=True)
        path = root / "wrapper-process.json"
        if not path.exists():
            if (root / "starting").exists():
                raise RuntimeError("wrapper_identity_not_recorded_destroy_sandbox")
            return {"quiescent": True, "started": False}
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("nonce") != nonce:
            raise RuntimeError("wrapper_identity_mismatch")

    def owned_live():
        try:
            process = psutil.Process(record["pid"])
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            if (process.create_time() != record["created"]
                    and process.environ().get("AVIBE_BENCHMARK_LAUNCH_ID") != nonce):
                raise RuntimeError("wrapper_identity_mismatch")
            return process
        except psutil.NoSuchProcess:
            return None

    process = owned_live()
    if process is not None:
        process.terminate()
    deadline = time.monotonic() + timeout
    while owned_live() is not None and time.monotonic() < deadline:
        time.sleep(.1)
    process = owned_live()
    if process is not None:
        process.kill()
        raise RuntimeError("wrapper_cleanup_unconfirmed_destroy_sandbox")
    try:
        result = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("wrapper_receipt_missing_destroy_sandbox") from None
    if result.get("cleanup", {}).get("owned_processes_stopped") is not True:
        raise RuntimeError("wrapper_cleanup_unconfirmed_destroy_sandbox")
    return {"quiescent": True, "started": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["run", "stop"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--timeout", type=float, default=90)
    args, wrapper_args = parser.parse_known_args(argv)
    try:
        if args.operation == "run":
            return run(args.root, args.nonce, wrapper_args[1:] if wrapper_args[:1] == ["--"] else wrapper_args)
        if args.receipt is None or args.timeout <= 0:
            parser.error("stop requires --receipt and positive --timeout")
        print(json.dumps(stop(args.root, args.nonce, args.receipt, args.timeout)))
        return 0
    except Exception as exc:
        # Only our closed vocabulary is printed, never arbitrary process output.
        print(json.dumps({"quiescent": False, "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
