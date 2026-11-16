#!/usr/bin/env python3
"""S2 acceptance on one explicitly authorized, bounded non-cloning Incus disk.

Uses the existing measurement manifest/prepare phase. Run copy, no-copy and
transaction-full once, in order. Only synthetic databases owned by that manifest
are retired between trials; the immutable predecessor backup is kept. No service
or instance operations, network access, production paths or implicit retries.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import errno
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def _fixture_state(home: Path) -> dict:
    db = home / "state" / "vibe.sqlite"
    with closing(sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        notice = conn.execute("SELECT value_json FROM state_meta WHERE key='storage.epoch3_backup_skipped'").fetchone()
        return {
            "revision": conn.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "epoch": json.loads(conn.execute("SELECT value_json FROM state_meta WHERE key='compatibility_epoch'").fetchone()[0]),
            "notice": json.loads(notice[0]) if notice else None,
            "schema": conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
        }


def _repair(home: Path) -> dict:
    from storage import importer, migrations

    db = home / "state" / "vibe.sqlite"
    copy_seconds = 0.0
    copies = []
    original = migrations.create_sqlite_migration_backup
    def timed(*args, **kwargs):
        nonlocal copy_seconds
        started = time.monotonic()
        try:
            path = original(*args, **kwargs)
            copies.append({"path": str(path), "manifest": json.loads((path / "manifest.json").read_text())})
            return path
        finally:
            copy_seconds += time.monotonic() - started
    migrations.create_sqlite_migration_backup = timed
    started = time.monotonic()
    try:
        importer.repair_sqlite_state(db_path=db, state_dir=db.parent, include_counts=False)
    finally:
        migrations.create_sqlite_migration_backup = original
    return {"preparation_seconds": time.monotonic() - started, "backup_seconds": copy_seconds,
            "copies": copies, "state": _fixture_state(home),
            "timing_scope": "database migration/import preparation only, NOT full service readiness"}


def _consume_remaining_space(path: Path, *, timeout: float = 30) -> dict:
    """Real ENOSPC from a test-owned O_DSYNC file, not an injected SQLite error."""
    started = time.monotonic()
    available = shutil.disk_usage(path.parent).free
    if available > 128 * MIB:
        raise RuntimeError("transaction-full phase must prefill to at most128MiB before its bounded hook")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_DSYNC, 0o600)
    written = 0
    block = os.urandom(4096)
    try:
        while written <= available + 16 * MIB:
            if time.monotonic() - started > timeout:
                raise RuntimeError("bounded transaction filler timed out")
            try:
                written += os.write(fd, block)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise
                return {"filler_bytes": written, "available_before_bytes": available,
                        "available_after_bytes": shutil.disk_usage(path.parent).free, "errno": exc.errno}
        raise RuntimeError("filler exceeded observed free capacity without ENOSPC")
    except BaseException:
        path.unlink(missing_ok=True)  # This call successfully created it exclusively.
        raise
    finally:
        os.close(fd)


def _transaction_full(home: Path, quota_bytes: int) -> dict:
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    from scripts.sqlite_epoch3_measure import require_guest
    from storage import migrations
    from vibe.runtime import clear_service_start_failure
    from scripts.sqlite_epoch3_notice_probe import disk_full_outcome

    require_guest(home, quota_bytes)  # Never saturate any filesystem on the desktop.
    # Match the service's normal startup path. An absolutely full volume may
    # still lose this record; that limitation is documented and out of scope.
    clear_service_start_failure()
    db = home / "state" / "vibe.sqlite"
    before = _fixture_state(home)
    assert before["revision"] == "20261112_0088" and before["epoch"] == 2 and before["notice"] is None
    filler = home / "transaction-filler"
    if filler.exists():
        raise RuntimeError("a prior transaction filler exists; do not replay the phase")
    exhausted = None
    def fill(conn, cursor, statement, parameters, context, executemany):
        nonlocal exhausted
        if statement.startswith("CREATE INDEX ix_agent_events_catalog_ref"):
            assert exhausted is None and conn.connection.driver_connection.in_transaction
            exhausted = _consume_remaining_space(filler)
    event.listen(Engine, "before_cursor_execute", fill)
    caught = None
    outcome = None
    free_before_reporting = None
    # Keep the WAL/SHM readable while the last writer rolls back. This also
    # models an existing reader; it never mutates the fixture or its schema.
    keeper = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        keeper.execute("BEGIN")
        keeper.execute("SELECT version_num FROM alembic_version").fetchall()
        try:
            migrations.run_migrations(db)
        except migrations.DatabaseDiskFullError as exc:
            caught = exc
            assert filler.exists() and exhausted is not None
            # The transaction has already failed with genuine ENOSPC. Release
            # a small tail only before recording the outcome, so this acceptance
            # proves the realistic "little space left" path rather than relying
            # on a new emergency reserve mechanism.
            slack_bytes = min(8 * MIB, filler.stat().st_size // 4)
            if slack_bytes <= 0:
                raise AssertionError("transaction filler was too small to leave reporting slack")
            with filler.open("r+b") as stream:
                stream.truncate(filler.stat().st_size - slack_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            free_before_reporting = shutil.disk_usage(home).free
            outcome = disk_full_outcome(home, caught, quota_bytes=quota_bytes)
            assert outcome["ok"], "transaction failure lost its status/Desktop outcome"
        if caught is None:
            raise AssertionError("actual full filesystem did not fail the conversion")
    finally:
        event.remove(Engine, "before_cursor_execute", fill)
        keeper.close()
        # Only a file exclusively created by this worker can exist here.
        if exhausted is not None:
            filler.unlink(missing_ok=True)
    assert exhausted is not None and caught.rollback_confirmed
    after = _fixture_state(home)
    assert after == before, "real disk exhaustion left a changed schema/epoch/notice"
    assert outcome is not None and outcome["ok"]
    return {"transaction_failed": True, "rollback_verified": True,
            "rollback_confirmed_at_failure": caught.rollback_confirmed,
            "free_before_reporting_bytes": free_before_reporting,
            "disk_exhaustion": exhausted, "state": after, "user_outcome": outcome}


def worker(action: str, home: Path, *, quota_bytes: int) -> dict:
    from scripts.sqlite_epoch3_measure import fixture_bytes, require_guest

    if os.environ.get("HOME") != str(home / "home") or os.environ.get("AVIBE_HOME") != str(home / "avibe"):
        raise RuntimeError("S2 worker requires isolated HOME/AVIBE_HOME")
    if fixture_bytes(home) > 16 * MIB or action == "transaction-full":
        require_guest(home, quota_bytes)
    if action == "repair":
        return _repair(home)
    if action == "transaction-full":
        return _transaction_full(home, quota_bytes)
    if action == "notice":
        from scripts.sqlite_epoch3_notice_probe import probe

        return probe(home, language="zh", repeat=3, quota_bytes=quota_bytes)
    raise ValueError(action)


def _prefill(root: Path, free_bytes: int, quota_bytes: int) -> Path:
    from scripts.sqlite_epoch3_measure import require_guest

    before = require_guest(root, quota_bytes)
    path = root / "s2-prefill"
    remaining = max(0, before["free_bytes"] - free_bytes)
    block = os.urandom(MIB)
    stream = path.open("xb")
    try:
        with stream:
            while remaining:
                count = min(remaining, len(block))
                stream.write(block[:count])
                remaining -= count
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)  # Only after exclusive creation succeeded.
        raise
    return path


def _retire_database(home: Path) -> None:
    # Only exact, manifest-owned trial names are passed here; backups and all
    # durable evidence remain. A worker must have exited before this call.
    for name in ("vibe.sqlite", "vibe.sqlite-wal", "vibe.sqlite-shm", "vibe.sqlite-journal"):
        (home / "state" / name).unlink(missing_ok=True)


def _copy_predecessor(root: Path, name: str, receipt: dict) -> Path:
    source = Path(receipt["copies"][0]["path"]).resolve() / "vibe.sqlite"
    if not source.is_relative_to((root / "large" / "state" / "backups").resolve()):
        raise RuntimeError("snapshot is not inside the manifest-owned backup directory")
    home = root / name
    home.mkdir(exist_ok=False)
    (home / "avibe" / "state").mkdir(parents=True)
    # The explicit worker path remains available to migration and fixture code,
    # while AVIBE_HOME resolves the same inode for the real notice consumer.
    (home / "state").symlink_to(home / "avibe" / "state", target_is_directory=True)
    with source.open("rb") as reader, (home / "avibe" / "state" / "vibe.sqlite").open("xb") as writer:
        shutil.copyfileobj(reader, writer, MIB)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copyfile(root / "large" / "fixture.json", home / "fixture.json")
    return home


def _assert_history(root: Path, facts: dict) -> None:
    before = json.loads((root / "history-before.json").read_text())
    assert facts["history_sha256"] == before["history_sha256"] and facts["rows"] == before["rows"]


def _verify_workload_contract(root: Path, manifest: dict, previous: Path, phase: str) -> None:
    from scripts.sqlite_predecessor_contract import run_probe

    target = manifest.get("target_bytes")
    operations = manifest.get("operations")
    if (type(target) is not int or not 4_500_000_000 <= target <= 4_600_000_000
            or type(operations) is not int or operations < 1000):
        raise RuntimeError("S2 acceptance requires the prepared4.5GB/>=1000-operation workload")
    if phase == "copy":
        database = root / "large" / "state" / "vibe.sqlite"
    else:
        receipt = json.loads((root / "s2-copy.json").read_text())
        if len(receipt["copies"]) != 1:
            raise RuntimeError("S2 requires exactly one verified predecessor snapshot")
        copy = receipt["copies"][0]
        if (copy["manifest"]["copy_method"] != "sqlite_online_backup"
                or copy["manifest"]["from_revisions"] != ["20261112_0088"]):
            raise RuntimeError("S2 snapshot is not the measured non-cloning predecessor copy")
        database = Path(copy["path"]).resolve() / "vibe.sqlite"
        if not database.is_relative_to((root / "large" / "state" / "backups").resolve()):
            raise RuntimeError("S2 snapshot is outside the manifest-owned directory")
    if not target <= database.stat().st_size <= target + 32 * MIB:
        raise RuntimeError("Actual predecessor database does not match the prepared4.5GB workload")
    identity = run_probe(previous, "identity", root / "identity")
    if (identity != manifest.get("predecessor") or identity.get("version") != "3.2.6"
            or not Path(identity["storage"]).is_relative_to(previous.parent.parent)):
        raise RuntimeError("Published predecessor identity changed since preparation")


def main(argv=None) -> int:
    from scripts.sqlite_epoch3_measure import durable_write, probe, require_guest
    from scripts.sqlite_predecessor_contract import run_probe

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--previous-python", required=True, type=Path)
    parser.add_argument("--quota-bytes", required=True, type=int)
    parser.add_argument("--phase", required=True, choices=("copy", "no-copy", "transaction-full"))
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    require_guest(root, args.quota_bytes)
    manifest = json.loads((root / "manifest.json").read_text())
    previous = args.previous_python.expanduser().absolute()
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if (manifest["head"] != head or manifest["previous_python"] != str(previous)
            or manifest["capacity"]["quota_bytes"] != args.quota_bytes
            or subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip()):
        raise RuntimeError("S2 must use the clean exact head and predecessor from prepare")
    predecessor_phase = {"copy": "prepare", "no-copy": "s2-copy", "transaction-full": "s2-no-copy"}[args.phase]
    if not (root / f"{predecessor_phase}.complete").exists():
        raise RuntimeError(f"{predecessor_phase} must finish first")
    _verify_workload_contract(root, manifest, previous, args.phase)
    durable_write(root / f"s2-{args.phase}.started", str(time.time()), exclusive=True)
    python = Path(sys.executable)
    if args.phase == "copy":
        from storage.backups import _clone_file

        # Prove this actual destination filesystem cannot clone, independently
        # of whether a live SQLite reader might force an online-copy fallback.
        source, destination = root / "cow-source", root / "cow-target"
        with source.open("xb") as stream:
            stream.write(os.urandom(4096))
        try:
            try:
                _clone_file(source, destination)
            except OSError as exc:
                if exc.errno not in {errno.ENOTSUP, errno.ENOTTY, errno.ENOSYS, errno.EXDEV}:
                    raise
                durable_write(root / "non-cloning.json", json.dumps({"errno": exc.errno, "filesystem": "actual clone refused"}))
            else:
                raise RuntimeError("S2 non-cloning acceptance requires a disk that cannot clone")
        finally:
            source.unlink()
            destination.unlink(missing_ok=True)
        result = probe(python, "repair", root / "large", root / "s2-copy.json", current=True)
        assert len(result["copies"]) == 1 and result["copies"][0]["manifest"]["copy_method"] == "sqlite_online_backup"
        assert result["state"]["epoch"] == 3 and result["state"]["notice"] is None
        _assert_history(root, probe(python, "history", root / "large", root / "s2-copy-history.json", current=True))
        _assert_history(root, probe(previous, "history", root / "large", root / "s2-copy-old-history.json"))
    else:
        copy = json.loads((root / "s2-copy.json").read_text())
        retired = root / ("large" if args.phase == "no-copy" else "low")
        _retire_database(retired)
        home = _copy_predecessor(root, "low" if args.phase == "no-copy" else "full", copy)
        filler = _prefill(root, 512 * MIB if args.phase == "no-copy" else 64 * MIB, args.quota_bytes)
        try:
            if args.phase == "no-copy":
                result = probe(python, "repair", home, root / "s2-no-copy.json", current=True)
                assert result["copies"] == [] and result["state"]["epoch"] == 3 and result["state"]["notice"]
                _assert_history(root, probe(python, "history", home, root / "s2-no-copy-history.json", current=True))
                _assert_history(root, probe(previous, "history", home, root / "s2-no-copy-old-history.json"))
                notice = probe(python, "notice", home, root / "s2-notice.json", current=True)
                assert notice["ok"], "notice persistence, exact copy or provenance acknowledgement failed"
                # Real3.2.6 writes THIS large converted database; candidate
                # reopens and verifies those writes. No full in-memory dump.
                for label, binary, action, current in (
                    ("old-seed", previous, "seed", False), ("old-write", previous, "exercise", False),
                    ("candidate-return", python, "verify", True),
                ):
                    facts = run_probe(binary, action, home, current=current)
                    durable_write(root / f"s2-{label}.json", json.dumps(facts, indent=2))
            else:
                result = probe(python, "transaction-full", home, root / "s2-transaction-full.json", current=True)
                assert result["transaction_failed"] and result["rollback_verified"]
                _assert_history(root, probe(previous, "history", home, root / "s2-full-old-history.json"))
        finally:
            filler.unlink(missing_ok=True)
        if args.phase == "transaction-full":
            # Normal retry after freeing space must not need schema surgery.
            result = probe(python, "repair", home, root / "s2-full-retry.json", current=True)
            assert result["state"]["epoch"] == 3
    durable_write(root / f"s2-{args.phase}.complete", str(time.time()), exclusive=True)
    print(json.dumps({"phase": args.phase, "head": head, "complete": True}))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
