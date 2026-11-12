"""Real owner process deaths at journal/native process boundaries.

Unlike injected write-failure tests, these kill the test-owned controller and
recover its real surviving HTTP fixture children. This is not power-loss testing.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from core.process_isolation import (
    PROCESS_IDENTITY_ENV, fingerprint_process_marker, new_process_identity_marker, reap_marked_processes,
)
from tests.model_hub_engine_test_environment import child_environment
from tests.test_model_hub_engine_lifecycle import signed_candidate
from tests.test_model_hub_engine_processes import finish, real_owner

pytestmark = pytest.mark.integration  # dedicated native owner-death harness


def alive(pid):
    try:
        return psutil.Process(pid).status() not in {psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD}
    except psutil.NoSuchProcess:
        return False


@pytest.mark.parametrize("boundary,record_damage", [
    ("preflight_spawn", "valid"), ("preflight_spawn", "missing"), ("preflight_spawn", "malformed"),
    ("preflight_retire_before_save", "valid"),
    ("intent_before_save", "valid"), ("intent_after_save", "valid"), ("stop_after", "valid"),
    ("launch_before_save", "valid"), ("launch_after_save", "valid"),
    ("start_after_spawn", "valid"), ("start_after_spawn", "missing"), ("start_after_spawn", "malformed"),
    ("health_after", "valid"), ("commit_before_save", "valid"), ("commit_after_save", "valid"),
    ("identity_after_publish", "valid"),
    ("rollback_after_save", "missing"), ("rollback_after_save", "malformed"),
])
def test_native_owner_death_recovers_only_a_durable_authorized_generation(
    tmp_path, monkeypatch, boundary, record_damage, *, redirector=False,
):
    from tests.test_model_hub_runtime import _write_mock_engine

    owner, supervisor, baseline, _observations, processes = real_owner(tmp_path, monkeypatch)
    candidate, signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    fixture_engine = tmp_path / "fixture-cpa.py"
    _write_mock_engine(fixture_engine)
    if boundary == "rollback_after_save":
        fixture_engine.write_text(
            "import os\n"
            "if os.environ.get('AVIBE_FIXTURE_FAIL_CANDIDATE'):\n"
            "    raise SystemExit(17)\n"
            + fixture_engine.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    bundle = tmp_path / "bundle-manifest.json"
    bundle.write_bytes(owner.manager.bundled_manifest_bytes())
    reached, children = tmp_path / "boundary.json", tmp_path / "children.jsonl"
    spec_path = tmp_path / "crash-spec.json"
    spec_path.write_text(json.dumps({
        "runtime": str(owner.manager.runtime_dir), "state": str(supervisor.state_store.root),
        "bundle": str(bundle), "candidate": candidate.document(), "roots": asdict(signer.roots()),
        "fixture_engine": str(fixture_engine), "boundary": boundary,
        "fail_candidate": boundary == "rollback_after_save",
        "reached": str(reached), "children": str(children),
    }))
    source = Path(__file__).resolve().parents[1]
    environment = child_environment(tmp_path / "crash-home", source)
    marker = new_process_identity_marker()
    environment[PROCESS_IDENTITY_ENV] = marker
    owner_fingerprint = fingerprint_process_marker(marker)
    driver = Path(__file__).with_name("model_hub_owner_crash_driver.py")
    command = [sys.executable, str(driver), str(spec_path)]
    if redirector:
        # Reproduce the Windows virtualenv launcher shape on any native host:
        # Popen owns a waiting redirector; the boundary writer is its child.
        command = [sys.executable, "-c", "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))", *command]
    owner_process = None
    records = []
    try:
        with (tmp_path / "owner-output.log").open("w+") as output:
            owner_process = subprocess.Popen(
                command, cwd=source, env=environment,
                stdout=output, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 30
            while not reached.exists() and owner_process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            output.flush()
            assert reached.exists(), (tmp_path / "owner-output.log").read_text()
            actual_owner = psutil.Process(json.loads(reached.read_text())["owner_pid"])
            launcher = psutil.Process(owner_process.pid)
            assert actual_owner == launcher or actual_owner in launcher.children(recursive=True)
            actual_marker = actual_owner.environ().get(PROCESS_IDENTITY_ENV)
            assert actual_marker is not None and fingerprint_process_marker(actual_marker) == owner_fingerprint
            if redirector:
                assert actual_owner.pid != owner_process.pid
            # Kill the positively witnessed executing owner, NOT merely its
            # Windows venv redirector. Fixture CPA children survive this death.
            actual_owner.kill()
            owner_process.wait(timeout=10)
            assert not alive(actual_owner.pid)
        records = [json.loads(line) for line in children.read_text().splitlines()]
        assert records
        journal = owner.journal.load()
        if record_damage != "valid":
            record = (Path(journal["preflight"]["root"]) / "engine-process.json"
                      if journal["preflight"] else supervisor._engine_record_path)
            if record_damage == "missing":
                record.unlink()
            else:
                record.write_bytes(b"malformed process record")
        original_spawn = supervisor._process_factory

        def spawn_after_confirmed_reap(*args, **kwargs):
            assert not any(alive(item["pid"]) for item in records)
            return original_spawn(*args, **kwargs)

        monkeypatch.setattr(supervisor, "_process_factory", spawn_after_confirmed_reap)
        owner.recover()
        value = owner.journal.load()
        expected = candidate.document() if boundary in {"commit_after_save", "identity_after_publish"} else baseline["generation"]
        assert value["active"] == expected and value["pending"] is None and value["preflight"] is None
        assert not any(alive(item["pid"]) for item in records)
        assert not list(owner.manager.runtime_dir.glob("engine-preflight-*"))
        owner.ensure_running()
        assert owner.running_release == expected["asset_release_tag"]
        assert owner.journal.load()["active"] == expected
    finally:
        # Failure cleanup also covers redirectors whose actual owner has not
        # published its boundary yet. The unique owner marker cannot match the
        # live Avibe service or the independently marked CPA children.
        owner_cleanup = reap_marked_processes(
            logging.getLogger(__name__), "native fixture owner", worker_fingerprint=owner_fingerprint,
        )
        if owner_process is not None and owner_process.poll() is None:
            owner_process.kill()
            owner_process.wait(timeout=10)
        finish(supervisor, processes)
        if children.exists():
            records = [json.loads(line) for line in children.read_text().splitlines()]
        for record in records:
            # Safety cleanup also uses positive test marker identity, not PID
            # existence or a command-line pattern that could match Avibe.
            try:
                process = psutil.Process(record["pid"])
                if not alive(process.pid):
                    continue
                marker = process.environ().get(PROCESS_IDENTITY_ENV)
                assert marker is not None and fingerprint_process_marker(marker) == record["fingerprint"]
                process.kill()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    assert not alive(process.pid)
            except psutil.NoSuchProcess:
                pass
        assert owner_cleanup != "unconfirmed"


def test_native_owner_death_kills_the_actual_owner_behind_a_redirector(tmp_path, monkeypatch):
    test_native_owner_death_recovers_only_a_durable_authorized_generation(
        tmp_path, monkeypatch, "start_after_spawn", "valid", redirector=True,
    )
