"""Native owner-crash driver. Parent kills this recorded owner at a boundary.

No exception simulates death: the driver waits after reaching a real journal,
process, or health boundary, and its parent terminates the owner process.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import yaml

from core.model_hub_engine_channel import TrustRoot, TrustRoots
from core.process_isolation import PROCESS_IDENTITY_ENV, fingerprint_process_marker
from tests.engine_channel_support import LocalVerifier
from vibe.model_hub_runtime.engine_channel import EngineChannel
from vibe.model_hub_runtime.installer import EngineRuntimeManager
from vibe.model_hub_runtime.lifecycle import EngineGeneration
from vibe.model_hub_runtime.state import EngineStateStore
from vibe.model_hub_runtime.supervisor import EngineSupervisor


def main(spec):
    manager = EngineRuntimeManager(runtime_dir=Path(spec["runtime"]), offline=False)
    manager.bundled_manifest_bytes = lambda: Path(spec["bundle"]).read_bytes()
    state = EngineStateStore(Path(spec["state"]))
    candidate = EngineGeneration.read(spec["candidate"], manager.runtime_dir)
    native_popen = subprocess.Popen

    def boundary(name):
        if name != spec["boundary"]:
            return
        Path(spec["reached"]).write_text(json.dumps({"owner_pid": os.getpid(), "boundary": name}))
        while True:
            time.sleep(1)

    def spawn(command, *args, **kwargs):
        binary = Path(command[0])
        if (manager.runtime_dir / "versions") not in binary.parents:
            return native_popen(command, *args, **kwargs)
        config = yaml.safe_load(Path(command[-1]).read_text())
        isolated = Path(config["auth-dir"]).resolve() != state.auth_dir.resolve()
        # Only the reviewed fake CPA runs here; retain its startup diagnostics
        # in this test-owned driver log, never output from a packaged engine.
        kwargs.update(stdout=sys.stderr, stderr=sys.stderr)
        process = native_popen([sys.executable, spec["fixture_engine"], *command[1:]], *args, **kwargs)
        record = {
            "pid": process.pid, "isolated": isolated,
            "fingerprint": fingerprint_process_marker(kwargs["env"][PROCESS_IDENTITY_ENV]),
        }
        with Path(spec["children"]).open("a") as records:
            records.write(json.dumps(record) + "\n")
            records.flush()
        if isolated:
            boundary("preflight_spawn")
        elif binary == Path(candidate.install_dir) / candidate.bin_path:
            boundary("start_after_spawn")
        return process

    supervisor = EngineSupervisor(installer=manager, state_store=state, process_factory=spawn, startup_timeout=15)
    owner = supervisor.lifecycle
    roots = spec["roots"]
    owner.channel = EngineChannel(
        manager, roots=TrustRoots(roots=tuple(TrustRoot(**item) for item in roots["roots"]), sequence_floor=roots["sequence_floor"]),
        verifier=LocalVerifier(), avibe_version="3.2.6",
    )
    # Isolated health uses the same native fixture launcher.
    import vibe.model_hub_runtime.lifecycle as lifecycle_module
    lifecycle_module.subprocess.Popen = spawn
    owner.recover()
    owner.ensure_running()
    actual_save = owner._save

    def save(value):
        pending, launch = value["pending"], value["launch"]
        is_candidate = pending and pending["candidate"] == candidate.document()
        phase = (
            "launch" if is_candidate and launch and launch["generation"] == candidate.document()
            else "intent" if is_candidate else "commit" if value["active"] == candidate.document() and pending is None
            else "preflight_retire" if value["preflight"] is None and owner.journal.load()["preflight"] is not None
            else None
        )
        if phase:
            boundary(f"{phase}_before_save")
        actual_save(value)
        if phase:
            boundary(f"{phase}_after_save")

    owner._save = save
    actual_stop = supervisor.stop_for_activation

    def stop():
        result = actual_stop()
        pending = owner.journal.load()["pending"]
        if pending and pending["candidate"] == candidate.document():
            boundary("stop_after")
        return result

    supervisor.stop_for_activation = stop
    actual_health = supervisor._check_health_locked

    def health(client):
        result = actual_health(client)
        if result and getattr(owner._start_context, "generation", None) == candidate:
            boundary("health_after")
        return result

    supervisor._check_health_locked = health
    actual_publish = owner.publish_status

    def publish():
        actual_publish()
        if owner.running_release == candidate.asset_release_tag:
            boundary("identity_after_publish")

    owner.publish_status = publish
    owner.activate(candidate)
    # An unknown boundary is a fixture error, never a passing death test.
    raise AssertionError(f"did not reach {spec['boundary']}: {asdict(candidate)}")


if __name__ == "__main__":
    from model_hub_engine_test_environment import install_child_audit

    spec = json.loads(Path(sys.argv[1]).read_text())
    install_child_audit(Path(spec["bundle"]).parent)
    main(spec)
