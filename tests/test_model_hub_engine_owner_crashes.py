"""Real owner process deaths at journal/native process boundaries.

Unlike injected write-failure tests, these kill the test-owned controller and
recover its real surviving HTTP fixture children. This is not power-loss testing.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from core.process_isolation import PROCESS_IDENTITY_ENV, fingerprint_process_marker
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
])
def test_native_owner_death_recovers_only_a_durable_authorized_generation(
    tmp_path, monkeypatch, boundary, record_damage,
):
    owner, supervisor, baseline, _observations, processes = real_owner(tmp_path, monkeypatch)
    candidate, signer, _manifest, _envelope = signed_candidate(owner, tmp_path)
    bundle = tmp_path / "bundle-manifest.json"
    bundle.write_bytes(owner.manager.bundled_manifest_bytes())
    reached, children = tmp_path / "boundary.json", tmp_path / "children.jsonl"
    spec_path = tmp_path / "crash-spec.json"
    spec_path.write_text(json.dumps({
        "runtime": str(owner.manager.runtime_dir), "state": str(supervisor.state_store.root),
        "bundle": str(bundle), "candidate": candidate.document(), "roots": asdict(signer.roots()),
        "fixture_engine": str(tmp_path / "fixture-cpa.py"), "boundary": boundary,
        "reached": str(reached), "children": str(children),
    }))
    source = Path(__file__).resolve().parents[1]
    environment = child_environment(tmp_path / "crash-home", source)
    driver = Path(__file__).with_name("model_hub_owner_crash_driver.py")
    owner_process = None
    records = []
    try:
        with (tmp_path / "owner-output.log").open("w+") as output:
            owner_process = subprocess.Popen(
                [sys.executable, str(driver), str(spec_path)], cwd=source, env=environment,
                stdout=output, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 30
            while not reached.exists() and owner_process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            output.flush()
            assert reached.exists(), (tmp_path / "owner-output.log").read_text()
            assert json.loads(reached.read_text())["owner_pid"] == owner_process.pid
            # Kill only this test's recorded owner. No mocked exception or stop
            # callback stands in for operating-system process death here.
            owner_process.kill()
            owner_process.wait(timeout=10)
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
