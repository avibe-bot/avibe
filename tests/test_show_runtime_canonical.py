from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/show_runtime_canonical.py"


def run_command(directory: Path, command: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    values = {
        "--directory": str(directory),
        "--artifact": "linux-x64",
        "--runtime-ref": "a" * 40,
        "--lock-sha": "b" * 64,
        "--producer-workflow-name": "Release (AI Notes)",
        "--producer-workflow-file": ".github/workflows/release_ai.yml",
        "--producer-run-id": "12345",
        "--producer-run-attempt": "2",
        "--producer-source-sha": "c" * 40,
        "--node-version": "v22.23.4",
        "--npm-version": "10.9.2",
    }
    values.update(overrides)
    arguments = [sys.executable, str(SCRIPT), command]
    for key, value in values.items():
        if value is not None:
            arguments.extend([key, value])
    return subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True)


def test_record_and_verify_prove_the_complete_show_runtime_identity(tmp_path: Path):
    (tmp_path / "vibe-show-runtime-node-linux-x64.tgz").write_bytes(b"archive")
    (tmp_path / "show-router-linux-x64.tsx").write_text("router", encoding="utf-8")

    recorded = run_command(tmp_path, "record")
    assert recorded.returncode == 0, recorded.stdout + recorded.stderr
    identity = json.loads((tmp_path / "show-runtime-identity-linux-x64.json").read_text())
    assert identity["producer_run_attempt"] == 2
    assert identity["platform"] == "linux-x64"
    assert identity["archive_name"] == "vibe-show-runtime-node-linux-x64.tgz"

    verified = run_command(tmp_path, "verify", **{"--producer-source-sha": None})
    assert verified.returncode == 0, verified.stdout + verified.stderr

    (tmp_path / "show-router-linux-x64.tsx").write_text("changed", encoding="utf-8")
    rejected = run_command(tmp_path, "verify", **{"--producer-source-sha": None})
    assert rejected.returncode != 0
    assert "identity or digest mismatch" in rejected.stderr


def test_verify_rejects_a_changed_lockfile_identity(tmp_path: Path):
    (tmp_path / "vibe-show-runtime-node-linux-x64.tgz").write_bytes(b"archive")
    (tmp_path / "show-router-linux-x64.tsx").write_text("router", encoding="utf-8")
    assert run_command(tmp_path, "record").returncode == 0

    rejected = run_command(tmp_path, "verify", **{"--producer-source-sha": None, "--lock-sha": "d" * 64})
    assert rejected.returncode != 0
    assert "identity or digest mismatch" in rejected.stderr
