"""Local process acceptance for Harbor's outer cancel fence; no Docker/Harbor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.terminal_bench.launch import run, stop


def test_late_start_is_fenced_and_untrusted_identity_not_signalled(tmp_path):
    root = tmp_path / "launch"
    root.mkdir()
    assert stop(root, "nonce", tmp_path / "execution.json", .1) == {"quiescent": True, "started": False}
    assert run(root, "nonce", ["must-not-start"]) == 125
    (root / "starting").touch()
    with pytest.raises(RuntimeError, match="identity_not_recorded"):
        stop(root, "nonce", tmp_path / "execution.json", .1)
    (root / "wrapper-process.json").write_text(json.dumps({"nonce": "other", "pid": os.getpid(), "created": 0}))
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        stop(root, "nonce", tmp_path / "execution.json", .1)


@pytest.mark.reap_processes
def test_stop_joins_exact_wrapper_and_demands_inner_cleanup_receipt(tmp_path):
    import psutil
    root = tmp_path / "launch"
    root.mkdir()
    receipt = tmp_path / "execution.json"
    ready = tmp_path / "ready"
    code = (
        "import sys,json,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: (Path(sys.argv[1]).write_text(json.dumps("
        "{'cleanup':{'owned_processes_stopped':True}})),sys.exit(0))); "
        "Path(sys.argv[2]).touch(); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", code, str(receipt), str(ready)],
                                env={"PATH": os.defpath, "AVIBE_BENCHMARK_LAUNCH_ID": "nonce"}, start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        (root / "wrapper-process.json").write_text(json.dumps({
            "nonce": "nonce", "pid": process.pid, "created": psutil.Process(process.pid).create_time(),
        }))
        assert stop(root, "nonce", receipt, 2)["quiescent"] is True
        process.wait(timeout=3)
        receipt.unlink()
        with pytest.raises(RuntimeError, match="receipt_missing"):
            stop(root, "nonce", receipt, .1)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
