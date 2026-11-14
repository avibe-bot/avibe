#!/usr/bin/env python3
"""Repeatable epoch-3 measurements in a disposable, quota-bounded Incus guest.

Requires an already installed, isolated published 3.2.6 interpreter. No network,
credentials, service operations, quota changes or cleanup of other state. The
lead owns provisioning, pool-capacity evidence and deleting the whole target.
Run each phase once; completed JSON receipts survive interruption. An interrupted
phase needs a fresh test root/guest, not a blind replay of its writers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def durable_write(path: Path, content: str, *, exclusive: bool = False) -> None:
    """Persist phase intent before writes, and publish complete receipts last."""
    temporary = path if exclusive else path.with_name("." + path.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def fixture_bytes(home: Path) -> int:
    db = home / "state" / "vibe.sqlite"
    return sum(path.stat().st_size for path in (db, Path(str(db) + "-wal"), Path(str(db) + "-journal")) if path.exists())


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def protected_files(home: Path) -> dict:
    state = home / "state"
    candidates = list(state.glob("vibe.sqlite*")) + list(state.glob(".*.backup-lifecycle.json"))
    candidates += list((state / "backups").rglob("*"))
    return {str(p.relative_to(state)): digest_file(p) for p in candidates if p.is_file() and not p.name.endswith("-shm")}


def require_guest(root: Path, quota_bytes: int) -> dict:
    """Fail closed unless the visible filesystem is actually bounded as claimed."""
    if platform.system() != "Linux":
        raise RuntimeError("Large fixtures and disk filling are Incus-only, never on the desktop")
    marker = Path("/run/systemd/container")
    if not marker.exists() or marker.read_text().strip() not in {"lxc", "incus"}:
        raise RuntimeError("This measurement requires a disposable Incus container")
    if not 12 * 1024**3 <= quota_bytes <= 16 * 1024**3:
        raise RuntimeError("Expected an explicitly provisioned 12–16 GiB instance root quota")
    ancestor = root
    while not ancestor.exists():
        ancestor = ancestor.parent
    usage = shutil.disk_usage(ancestor)
    if usage.total > quota_bytes or usage.total < quota_bytes * 0.8:
        raise RuntimeError("Guest filesystem does not expose the expected quota; do not fill a shared pool. Ask the lead to verify provisioning")
    return {"total_bytes": usage.total, "free_bytes": usage.free, "quota_bytes": quota_bytes}


def probe(python: Path, action: str, home: Path, output: Path, *, current: bool = False,
          group: str = "cache", count: int = 1000, target_bytes: int = 4_500_000_000) -> dict:
    from scripts.sqlite_predecessor_contract import _probe_env

    home.mkdir(parents=True, exist_ok=True)
    command = [str(python), "-I"]
    if current:
        command += ["-c", f"import sys,runpy;sys.path.insert(0,{str(ROOT)!r});sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"]
    manifest_path = home.parent / "manifest.json"
    quota = json.loads(manifest_path.read_text())["capacity"]["quota_bytes"] if manifest_path.exists() else 0
    command += [str(Path(__file__).resolve()), "--worker", action, "--root", str(home),
                "--group", group, "--count", str(count), "--target-bytes", str(target_bytes), "--quota-bytes", str(quota)]
    before_free = shutil.disk_usage(home).free
    minimum_free = before_free
    started = time.monotonic()
    with output.with_suffix(".stdout.log").open("w") as stdout, output.with_suffix(".stderr.log").open("w") as stderr:
        child = subprocess.Popen(command, cwd=home, env=_probe_env(home), stdout=stdout, stderr=stderr)
        try:
            while child.poll() is None:
                minimum_free = min(minimum_free, shutil.disk_usage(home).free)
                time.sleep(0.2)
            minimum_free = min(minimum_free, shutil.disk_usage(home).free)
        except BaseException:
            child.terminate()
            child.wait(timeout=10)
            raise
    if child.returncode:
        raise RuntimeError(f"{action} failed ({child.returncode}); inspect {output.with_suffix('.stderr.log')}")
    result = json.loads(output.with_suffix(".stdout.log").read_text().strip().splitlines()[-1])
    result["observation"] = {"wall_seconds": time.monotonic() - started, "free_before_bytes": before_free,
                             "minimum_sampled_free_bytes": minimum_free,
                             "sampled_peak_additional_bytes": max(0, before_free - minimum_free), "sample_interval_seconds": 0.2,
                             "scope": "guest filesystem, includes incidental guest writes; sampled peak is a lower bound"}
    durable_write(output, json.dumps(result, indent=2) + "\n")
    return result


def fill_to_usage(root: Path, fraction: float, quota_bytes: int) -> dict:
    before = require_guest(root, quota_bytes)
    destination = root / "test-owned-filler"
    # Exclusive creation prevents accidentally overwriting anyone else's bytes.
    target_free = int(before["total_bytes"] * (1 - fraction))
    remaining = max(0, before["free_bytes"] - target_free)
    block = os.urandom(MIB)  # Do not let filesystem compression defeat occupancy.
    stream = destination.open("xb")
    try:
        with stream:
            while remaining:
                take = min(remaining, len(block))
                stream.write(block[:take])
                remaining -= take
            stream.flush()
            os.fsync(stream.fileno())
        after = require_guest(root, quota_bytes)
        if not 0.90 <= 1 - after["free_bytes"] / after["total_bytes"] <= 0.92:
            raise RuntimeError("Observed occupancy is not near 91%; inspect quota/compression/concurrent writes")
        return {"before": before, "after": after, "filler_bytes": destination.stat().st_size}
    except BaseException:
        destination.unlink()  # Only after this call exclusively created it.
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="fresh test-owned directory, never a runtime home")
    parser.add_argument("--previous-python", type=Path, help="isolated interpreter containing actual published avibe-os 3.2.6")
    parser.add_argument("--quota-bytes", type=int, default=0)
    parser.add_argument("--phase", choices=("prepare", "refusal", "upgrade", "growth"))
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--target-bytes", type=int, default=4_500_000_000)
    parser.add_argument("--worker", choices=("seed", "workload", "populate", "history", "refuse", "upgrade"), help=argparse.SUPPRESS)
    parser.add_argument("--group", choices=("cache", "vault", "skill"), default="cache", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    if args.worker:
        if (fixture_bytes(root) > 16 * MIB or (args.worker == "populate" and args.target_bytes > 16 * MIB)
                or (args.worker == "workload" and args.count > 32)):
            require_guest(root, args.quota_bytes)
        if os.environ.get("HOME") != str(root / "home") or os.environ.get("AVIBE_HOME") != str(root / "avibe"):
            raise RuntimeError("Worker requires the harness's isolated HOME/AVIBE_HOME environment")
        helpers = runpy.run_path(str(Path(__file__).with_name("sqlite_epoch3_workload.py")))
        print(json.dumps(helpers["run"](args.worker, root, group=args.group, count=args.count, target_bytes=args.target_bytes)))
        return 0
    if not args.phase or not args.previous_python:
        parser.error("--phase and --previous-python are required")
    if args.count < 1000 or not 4_500_000_000 <= args.target_bytes <= 4_600_000_000:
        parser.error("acceptance requires at least 1000 matched operations and a 4.5 GB synthetic database")
    capacity = require_guest(root, args.quota_bytes)
    sys.path.insert(0, str(ROOT))
    from scripts.sqlite_predecessor_contract import run_probe

    # Preserve the venv executable symlink: resolving it selects the base Python
    # and loses the predecessor's isolated site-packages.
    previous = args.previous_python.expanduser().absolute()
    if args.phase == "prepare":
        root.mkdir(parents=True, exist_ok=False)
        identity = run_probe(previous, "identity", root / "identity")
        if identity["version"] != "3.2.6" or not Path(identity["storage"]).is_relative_to(previous.parent.parent):
            raise RuntimeError(f"Not an isolated published 3.2.6 binary: {identity}")
        head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
        if subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip():
            raise RuntimeError("Candidate checkout must be clean and pinned to the reported head")
        durable_write(root / "manifest.json", json.dumps({"head": head, "predecessor": identity, "capacity": capacity,
            "target_bytes": args.target_bytes, "operations": args.count, "previous_python": str(previous)}, indent=2))
    manifest = json.loads((root / "manifest.json").read_text())
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if (manifest["head"] != head or manifest["previous_python"] != str(previous)
            or manifest["target_bytes"] != args.target_bytes or manifest["operations"] != args.count
            or manifest["capacity"]["quota_bytes"] != args.quota_bytes):
        raise RuntimeError("Candidate/predecessor/workload changed; start a new isolated exercise")
    if subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip():
        raise RuntimeError("Candidate changed since preparation; use a clean exact head")
    marker = root / f"{args.phase}.started"
    if marker.exists():
        raise RuntimeError("Phase already started; inspect its receipts. Do not replay a partial workload")
    durable_write(marker, str(time.time()), exclusive=True)
    large = root / "large"
    if args.phase == "prepare":
        probe(previous, "seed", large, root / "large-seed.json")
        for group in ("cache", "vault", "skill"):
            probe(previous, "workload", large, root / f"large-{group}.json", group=group, count=args.count)
        probe(previous, "populate", large, root / "large-populate.json", target_bytes=args.target_bytes)
        probe(previous, "history", large, root / "history-before.json")
    elif args.phase == "refusal":
        if not (root / "prepare.complete").exists():
            raise RuntimeError("prepare must complete first")
        before = protected_files(large)
        fill = fill_to_usage(root, 0.91, args.quota_bytes)
        durable_write(root / "near-full.json", json.dumps(fill, indent=2))
        try:
            result = probe(Path(sys.executable), "refuse", large, root / "refusal.json", current=True)
            assert result["refused"] and protected_files(large) == before
            assert not (large / "state" / "backups").exists()
        finally:
            # Only this script's exclusive-created filler is removed.
            (root / "test-owned-filler").unlink()
    elif args.phase == "upgrade":
        if not (root / "refusal.complete").exists():
            raise RuntimeError("near-full refusal must complete first")
        probe(Path(sys.executable), "upgrade", large, root / "upgrade.json", current=True)
        after = probe(Path(sys.executable), "history", large, root / "history-after.json", current=True)
        before = json.loads((root / "history-before.json").read_text())
        assert after["history_sha256"] == before["history_sha256"] and after["rows"] == before["rows"]
        # Actual old binary still reads all retained history after the switch.
        old = probe(previous, "history", large, root / "history-rollback.json")
        assert old["history_sha256"] == before["history_sha256"]
        # Use the separate extended contract for old writes/GC and candidate return.
        from scripts.sqlite_predecessor_contract import verify_predecessor
        contract_home = root / "rollback-contract"
        contract_home.mkdir()
        assert verify_predecessor(previous, contract_home, expected_version="3.2.6") == 3
    else:
        results = {}
        for group in ("cache", "vault", "skill"):
            pair = {}
            for label, python, current in (("before", previous, False), ("after", Path(sys.executable), True)):
                home = root / f"{group}-{label}"
                # Both fixtures start from the same predecessor schema and API.
                probe(previous, "seed", home, root / f"{group}-{label}-seed.json")
                if current:
                    probe(python, "upgrade", home, root / f"{group}-upgrade.json", current=True)
                pair[label] = probe(python, "workload", home, root / f"{group}-{label}.json",
                                    current=current, group=group, count=args.count)
            for label in ("before", "after"):
                representation = pair[label]["representation"]
                assert representation["compact_rows"] == (args.count if label == "after" else 0)
                assert representation["inline_rows"] == (args.count if label == "before" else 0)
                assert representation["state_rows"] == ((4 if group == "cache" else 1) if label == "after" else 0)
                if group == "cache":
                    assert representation["audit_prefix_rows"] == (args.count if label == "before" else 0)
            pair["saved_bytes"] = pair["before"]["growth_bytes"] - pair["after"]["growth_bytes"]
            results[group] = pair
        durable_write(root / "growth.json", json.dumps(results, indent=2))
    durable_write(root / f"{args.phase}.complete", str(time.time()), exclusive=True)
    print(json.dumps({"phase": args.phase, "root": str(root), "head": head, "complete": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
