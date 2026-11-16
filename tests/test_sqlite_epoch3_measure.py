"""Tiny API/guard checks only; multi-GB acceptance belongs to isolated Incus."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sqlite_epoch3_measure as measure, sqlite_epoch3_workload as workload
from storage import compatibility


@pytest.mark.parametrize("group", ["cache", "vault", "skill"])
@pytest.mark.parametrize("proof", [None, 3])
def test_matched_workload_uses_real_stores_and_counts_all_pages(tmp_path, monkeypatch, group, proof):
    # Distinct risk from writer tests: the acceptance harness must exercise real
    # stores successfully on both inline and compact paths, not measure no-ops.
    monkeypatch.setattr(compatibility, "VERIFIED_PREDECESSOR_EPOCH", proof)
    workload.seed(tmp_path)
    before = workload.history_facts(tmp_path)
    result = workload.workload(tmp_path, group, 3)
    after = workload.history_facts(tmp_path)
    assert result["operations"] == 3
    assert result["after"]["logical_bytes"] >= result["before"]["logical_bytes"]
    table = {"cache": "messages", "vault": "vault_audit", "skill": "agent_events"}[group]
    assert after["rows"][table] >= before["rows"][table] + 3
    assert after["history_sha256"] != before["history_sha256"]
    representation = result["representation"]
    assert representation["inline_rows"] == (3 if proof is None else 0)
    assert representation["compact_rows"] == (3 if proof == 3 else 0)
    assert representation["state_rows"] == ((3 if group == "cache" else 1) if proof == 3 else 0)
    if group == "cache":
        assert representation["audit_prefix_rows"] == (3 if proof is None else 0)


def test_large_fixture_entrypoints_refuse_the_desktop_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    root = tmp_path / "never-created"
    with pytest.raises(RuntimeError, match="Incus-only"):
        measure.main(["--root", str(root), "--phase", "prepare", "--previous-python", "/unused",
                      "--quota-bytes", str(16 * 1024**3)])
    with pytest.raises(RuntimeError, match="Incus-only"):
        measure.main(["--root", str(root), "--worker", "populate"])
    assert not root.exists()


@pytest.mark.parametrize("phase", ["copy", "no-copy", "transaction-full", "transaction-full-only"])
def test_s2_phase_entrypoints_refuse_desktop_before_preparation(tmp_path, monkeypatch, phase):
    from scripts import sqlite_epoch3_s2_measure as s2

    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    root = tmp_path / "never-created"
    with pytest.raises(RuntimeError, match="Incus-only"):
        s2.main(["--root", str(root), "--phase", phase, "--previous-python", "/unused",
                 "--quota-bytes", str(14 * 1024**3)])
    assert not root.exists()


@pytest.mark.parametrize("action", ["seed", "upgrade", "history", "refuse", "populate", "workload", "repair", "transaction-full", "notice"])
def test_every_worker_checks_existing_fixture_size(tmp_path, monkeypatch, action):
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    # Stat-only boundary: no large database is created on the desktop for this test.
    monkeypatch.setattr(measure, "fixture_bytes", lambda _: 17 * measure.MIB)
    with pytest.raises(RuntimeError, match="Incus-only"):
        measure.main(["--root", str(tmp_path), "--worker", action, "--count", "1", "--target-bytes", "1024"])


def test_s2_transaction_exhaustion_refuses_desktop_even_for_tiny_fixture(tmp_path, monkeypatch):
    from scripts import sqlite_epoch3_s2_measure as s2

    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    with pytest.raises(RuntimeError, match="Incus-only"):
        measure.main(["--root", str(tmp_path), "--worker", "transaction-full", "--target-bytes", "1024"])
    with pytest.raises(RuntimeError, match="Incus-only"):
        s2._transaction_full(tmp_path, 14 * 1024**3)
    with pytest.raises(RuntimeError, match="Incus-only"):
        s2._prefill(tmp_path, 1024, 14 * 1024**3)
    assert not (tmp_path / "transaction-filler").exists() and not (tmp_path / "s2-prefill").exists()


def test_restored_delete_snapshot_keeper_waits_for_real_wal_transaction(tmp_path, monkeypatch):
    # Contract: our keeper cannot precede the migration's DELETE->WAL switch.
    # Exercise the real worker's event hook, but forbid all disk filling.
    from contextlib import closing
    import sqlite3

    from sqlalchemy import create_engine
    from scripts import sqlite_epoch3_s2_measure as s2
    from storage import migrations
    from vibe import runtime

    home = tmp_path / "fixture"
    db = home / "state" / "vibe.sqlite"
    db.parent.mkdir(parents=True)
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE alembic_version(version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES ('20261112_0088')")
        conn.execute("CREATE TABLE state_meta(key TEXT, value_json TEXT)")
        conn.execute("INSERT INTO state_meta VALUES ('compatibility_epoch', '2')")
        conn.commit()
        with closing(sqlite3.connect(db)) as destination:
            conn.backup(destination)
            assert destination.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = s2._fixture_state(home)
    observed = []
    keepers = []
    original_keeper = s2._open_rollback_keeper

    def tracked_keeper(path):
        keeper = original_keeper(path)
        keepers.append(keeper)
        return keeper

    class ReachedFiller(Exception):
        pass

    def never_fill(path):
        assert path == home / "transaction-filler"
        assert len(keepers) == 1 and keepers[0].in_transaction
        assert keepers[0].execute("PRAGMA journal_mode").fetchone() == ("wal",)
        observed.append("WAL reader acquired after migration BEGIN")
        raise ReachedFiller("disk filling forbidden on desktop")

    def migrate(path):
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 0.1})
        try:
            with engine.connect() as writer:
                assert writer.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
                writer.commit()
                writer.exec_driver_sql("BEGIN IMMEDIATE")
                writer.exec_driver_sql("CREATE INDEX ix_agent_events_catalog_ref ON alembic_version(version_num)")
        finally:
            engine.dispose()

    monkeypatch.setattr(measure, "require_guest", lambda *a: None)
    monkeypatch.setattr(runtime, "clear_service_start_failure", lambda: None)
    monkeypatch.setattr(migrations, "run_migrations", migrate)
    monkeypatch.setattr(s2, "_open_rollback_keeper", tracked_keeper)
    monkeypatch.setattr(s2, "_consume_remaining_space", never_fill)
    with pytest.raises(ReachedFiller, match="forbidden"):
        s2._transaction_full(home, 14 * 1024**3)
    assert observed == ["WAL reader acquired after migration BEGIN"]
    assert s2._fixture_state(home) == before
    assert not (home / "transaction-filler").exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        keepers[0].execute("SELECT 1")


def test_s2_worker_contract_checks_home_before_opening_database(tmp_path, monkeypatch):
    from scripts import sqlite_epoch3_s2_measure as s2

    monkeypatch.setenv("HOME", str(tmp_path / "wrong"))
    with pytest.raises(RuntimeError, match="isolated HOME"):
        s2.worker("repair", tmp_path, quota_bytes=0)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("target,operations", [(1024, 1000), (4_700_000_000, 1000), (4_500_000_000, 999), (4_500_000_000, True)])
def test_s2_rejects_a_manifest_that_is_not_the_acceptance_workload(tmp_path, target, operations):
    from scripts import sqlite_epoch3_s2_measure as s2

    with pytest.raises(RuntimeError, match="prepared4.5GB"):
        s2._verify_workload_contract(tmp_path, {"target_bytes": target, "operations": operations}, Path("/unused/python"), "copy")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("phase", ["copy", "transaction-full-only"])
def test_s2_rechecks_actual_fixture_size_instead_of_trusting_manifest(tmp_path, phase):
    from scripts import sqlite_epoch3_s2_measure as s2

    db = tmp_path / "large" / "state" / "vibe.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"a replaced small fixture")
    with pytest.raises(RuntimeError, match="Actual predecessor database"):
        s2._verify_workload_contract(tmp_path, {"target_bytes": 4_500_000_000, "operations": 1000}, Path("/unused/python"), phase)


@pytest.mark.parametrize("phase", ["copy", "transaction-full-only"])
def test_s2_rechecks_installed_predecessor_identity(tmp_path, monkeypatch, phase):
    from types import SimpleNamespace
    from scripts import sqlite_epoch3_s2_measure as s2, sqlite_predecessor_contract as contract

    old = tmp_path / "previous" / "bin" / "python"
    identity = {"version": "3.2.6", "storage": str(tmp_path / "previous" / "lib" / "storage" / "__init__.py")}
    db = tmp_path / "large" / "state" / "vibe.sqlite"
    real_stat = Path.stat
    # Stat-only test; never create a multi-GB fixture on the desktop.
    monkeypatch.setattr(Path, "stat", lambda self, *a, **kw:
                        SimpleNamespace(st_size=4_500_000_000) if self == db else real_stat(self, *a, **kw))
    monkeypatch.setattr(contract, "run_probe", lambda *a, **kw: {**identity, "version": "3.2.5"})
    with pytest.raises(RuntimeError, match="predecessor identity changed"):
        s2._verify_workload_contract(tmp_path, {"target_bytes": 4_500_000_000, "operations": 1000, "predecessor": identity}, old, phase)


@pytest.mark.parametrize("worker_fails", [False, True])
def test_transaction_only_phase_has_its_own_receipts_and_cleans_prefill(tmp_path, monkeypatch, worker_fails):
    # Orchestration-only contract: a fresh prepared fixture needs no fabricated
    # copy/no-copy markers, and worker failure cannot publish completion.
    from scripts import sqlite_epoch3_s2_measure as s2, sqlite_predecessor_contract as contract
    from storage import backups

    root = tmp_path / "trial"
    root.mkdir()
    previous = tmp_path / "previous" / "bin" / "python"
    quota = 14 * 1024**3
    manifest = {"head": "exact-head", "previous_python": str(previous),
                "capacity": {"quota_bytes": quota}, "target_bytes": 4_500_000_000, "operations": 1000}
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "prepare.complete").write_text("complete")
    monkeypatch.setattr(measure, "require_guest", lambda *a: None)
    monkeypatch.setattr(s2.subprocess, "check_output", lambda argv, **kw: "" if "status" in argv else "exact-head\n")
    gates = []
    monkeypatch.setattr(s2, "_verify_workload_contract", lambda *args: gates.append(args))
    monkeypatch.setattr(s2, "_fixture_state", lambda _: {"revision": "20261112_0088", "epoch": 2, "notice": None})
    snapshot = root / "large" / "state" / "backups" / "snapshot"
    snapshot.mkdir(parents=True)
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(snapshot / "vibe.sqlite")) as conn:
        conn.execute("CREATE TABLE fixture(id INTEGER)")
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    (snapshot / "manifest.json").write_text(json.dumps({"from_revisions": ["20261112_0088"]}))
    (root / "large" / "fixture.json").write_text("{}")
    monkeypatch.setattr(backups, "create_sqlite_migration_backup", lambda *a, **kw: snapshot)
    filler = root / "s2-prefill"
    def prefill(*args):
        filler.touch(exist_ok=False)  # Never fill a real desktop filesystem.
        return filler
    monkeypatch.setattr(s2, "_prefill", prefill)
    facts = {"history_sha256": "same", "rows": {}}
    (root / "history-before.json").write_text(json.dumps(facts))
    actions = []
    def probe(python, action, home, output, **kwargs):
        actions.append(action)
        assert (home / "state" / "vibe.sqlite").samefile(home / "avibe" / "state" / "vibe.sqlite")
        if action == "transaction-full":
            assert filler.exists()
            if worker_fails:
                raise RuntimeError("worker failed before proof")
            return {"transaction_failed": True, "rollback_verified": True}
        if action == "history":
            assert python == previous
            return facts
        assert action == "repair" and not filler.exists()
        return {"state": {"epoch": 3}}
    monkeypatch.setattr(measure, "probe", probe)
    monkeypatch.setattr(contract, "run_probe", lambda *a, **kw: pytest.fail("no synthetic predecessor proof"))
    argv = ["--root", str(root), "--previous-python", str(previous), "--quota-bytes", str(quota),
            "--phase", "transaction-full-only"]
    if worker_fails:
        with pytest.raises(RuntimeError, match="before proof"):
            s2.main(argv)
    else:
        assert s2.main(argv) == 0
    assert len(gates) == 1 and gates[0] == (root, manifest, previous, "transaction-full-only")
    assert actions == (["transaction-full"] if worker_fails else ["transaction-full", "history", "repair"])
    assert not filler.exists()
    assert (root / "s2-transaction-full-only.complete").exists() is (not worker_fails)
    assert not (root / "s2-copy.complete").exists() and not (root / "s2-no-copy.complete").exists()


def test_receipts_sync_data_and_directory_before_returning(tmp_path, monkeypatch):
    import os
    import stat
    events = []
    original_fsync = os.fsync
    def synced(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_fsync(fd)
    monkeypatch.setattr(os, "fsync", synced)
    marker = tmp_path / "phase.started"
    measure.durable_write(marker, "started", exclusive=True)
    assert events == ["file", "directory"]
    with pytest.raises(FileExistsError):
        measure.durable_write(marker, "replay", exclusive=True)
    result = tmp_path / "result.json"
    measure.durable_write(result, "old")
    events.clear()
    def interrupted(*args):
        raise OSError("interrupted before publication")
    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        measure.durable_write(result, "new")
    assert events == ["file"] and result.read_text() == "old"


def test_tiny_population_preserves_real_message_shape(tmp_path):
    workload.seed(tmp_path)
    result = workload.populate(tmp_path, 2 * 1024 * 1024)
    assert result["footprint"]["logical_bytes"] >= 2 * 1024 * 1024
    facts = workload.history_facts(tmp_path)
    assert facts["rows"]["messages"] == result["bulk_messages"] + 1

def test_subprocess_worker_uses_isolated_fixture_home(tmp_path):
    import sys
    # Small seed tests the isolated subprocess bootstrapping, not a large run.
    output = tmp_path / "seed.json"
    result = measure.probe(Path(sys.executable), "seed", tmp_path / "fixture", output, current=True)
    assert result["session"]
    assert json.loads(output.read_text())["session"] == result["session"]
    assert (tmp_path / "fixture" / "state" / "vibe.sqlite").exists()


def test_s2_notice_worker_uses_the_converted_database_layout(tmp_path, monkeypatch):
    from scripts import sqlite_epoch3_s2_measure as s2
    import scripts.sqlite_epoch3_notice_probe as notice_probe

    source = tmp_path / "large" / "state" / "backups" / "copy" / "vibe.sqlite"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"converted fixture")
    (tmp_path / "large" / "fixture.json").write_text("{}")
    home = s2._copy_predecessor(
        tmp_path,
        "low",
        {"copies": [{"path": str(source.parent)}]},
    )
    monkeypatch.setenv("HOME", str(home / "home"))
    monkeypatch.setenv("AVIBE_HOME", str(home / "avibe"))

    def composed_probe(root, **kwargs):
        database = root / "avibe" / "state" / "vibe.sqlite"
        assert database.is_file()
        assert (root / "state" / "vibe.sqlite").samefile(database)
        return {"ok": True, "database": str(database)}

    monkeypatch.setattr(notice_probe, "probe", composed_probe)
    assert s2.worker("notice", home, quota_bytes=0)["ok"]
