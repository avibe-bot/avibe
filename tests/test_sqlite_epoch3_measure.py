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


def test_s2_rechecks_actual_fixture_size_instead_of_trusting_manifest(tmp_path):
    from scripts import sqlite_epoch3_s2_measure as s2

    db = tmp_path / "large" / "state" / "vibe.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"a replaced small fixture")
    with pytest.raises(RuntimeError, match="Actual predecessor database"):
        s2._verify_workload_contract(tmp_path, {"target_bytes": 4_500_000_000, "operations": 1000}, Path("/unused/python"), "copy")


def test_s2_rechecks_installed_predecessor_identity(tmp_path, monkeypatch):
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
        s2._verify_workload_contract(tmp_path, {"target_bytes": 4_500_000_000, "operations": 1000, "predecessor": identity}, old, "copy")


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
