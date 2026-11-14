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


@pytest.mark.parametrize("action", ["seed", "upgrade", "history", "refuse", "populate", "workload"])
def test_every_worker_checks_existing_fixture_size(tmp_path, monkeypatch, action):
    monkeypatch.setattr(measure.platform, "system", lambda: "Darwin")
    # Stat-only boundary: no large database is created on the desktop for this test.
    monkeypatch.setattr(measure, "fixture_bytes", lambda _: 17 * measure.MIB)
    with pytest.raises(RuntimeError, match="Incus-only"):
        measure.main(["--root", str(tmp_path), "--worker", action, "--count", "1", "--target-bytes", "1024"])


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
