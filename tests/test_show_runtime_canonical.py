from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from scripts import show_runtime_canonical as canonical


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


@pytest.mark.parametrize("complete", [True, False])
def test_find_checks_newest_complete_set_across_workflows_and_stops(monkeypatch, complete):
    # Older code queried every run even after finding a valid set (~70s/RC).
    workflows = [".github/workflows/release_ai.yml", ".github/workflows/publish.yml"]
    platforms = ["linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64"]
    calls = []

    def api(arguments):
        path = arguments[1]
        calls.append(path)
        for index, workflow in enumerate(workflows):
            if f"workflows/{Path(workflow).name}/runs?" in path:
                ids = [4, 1] if index == 0 else [3, 2]
                return {"workflow_runs": [{"id": number, "created_at": f"2026-11-0{number}T00:00:00Z",
                                          "path": workflow, "name": f"workflow-{index}", "conclusion": "success",
                                          "run_attempt": 2} for number in ids]}
        number = int(path.split("/runs/")[1].split("/")[0])
        targets = platforms if number == 3 and complete else platforms[:-1]
        return {"artifacts": [{"name": f"canonical-show-runtime-ref-{platform}", "expired": False,
                               "archive_download_url": "https://fixture.invalid/artifact"} for platform in targets]}

    monkeypatch.setattr(canonical, "gh_json", api)
    result = canonical.find_run("fixture/repo", workflows, "ref", platforms)
    if complete:
        assert result == ("3", workflows[1], "workflow-1", 2)
        assert len(calls) == 4  # two histories + incomplete newest + selected set
    else:
        assert result is None
        assert len(calls) == 6  # exhaustive miss still permits native fallback


@pytest.mark.parametrize("corruption", [None, "missing-platform", "archive", "router", "platform", "attempt", "node", "npm", "workflow"])
def test_linux_reuse_verifies_every_native_identity_before_promoting(tmp_path, monkeypatch, corruption):
    # Exercise real recording, ZIP extraction and identity verification; fake
    # only GitHub transport. A late bad platform must not leave a partial set.
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    import release_workflow_artifacts as transport

    platforms = ["linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64"]
    arguments = argparse.Namespace(
        repository="fixture/avibe", directory=str(tmp_path / "reused"), platforms=platforms,
        runtime_ref="a" * 40, lock_sha="b" * 64, producer_run_id="42", producer_run_attempt=2,
        producer_workflow_file=".github/workflows/release_ai.yml", producer_workflow_name="Release (AI Notes)",
        producer_source_sha="c" * 40, node_version="v22.23.4", npm_version="10.9.9",
    )
    artifacts = []
    archives = {}
    for index, platform in enumerate(platforms):
        directory = tmp_path / platform
        directory.mkdir()
        (directory / f"vibe-show-runtime-node-{platform}.tgz").write_bytes(f"native bytes {platform}".encode())
        (directory / f"show-router-{platform}.tsx").write_text("router")
        canonical.record(argparse.Namespace(**(vars(arguments) | {"artifact": platform, "directory": str(directory)})))
        if platform == platforms[-1] and corruption:
            identity_path = directory / f"show-runtime-identity-{platform}.json"
            identity = json.loads(identity_path.read_text())
            fields = {"platform": "platform", "attempt": "producer_run_attempt", "node": "node_version",
                      "npm": "npm_version", "workflow": "producer_workflow_file"}
            if corruption in fields:
                identity[fields[corruption]] = "wrong"
                identity_path.write_text(json.dumps(identity))
            elif corruption in {"archive", "router"}:
                path = canonical.archive_path(directory, platform) if corruption == "archive" else canonical.router_path(directory, platform)
                path.write_bytes(b"tampered")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path in directory.iterdir():
                archive.writestr(path.name, path.read_bytes())
        archives[index] = buffer.getvalue()
        artifacts.append({"id": index, "name": f"canonical-show-runtime-{'a' * 40}-{platform}",
                          "expired": False, "digest": "sha256:" + hashlib.sha256(archives[index]).hexdigest(),
                          "workflow_run": {"id": 42, "head_sha": "c" * 40}})
    if corruption == "missing-platform":
        artifacts.pop()
    run = {"id": 42, "run_attempt": 2, "head_sha": "c" * 40, "path": arguments.producer_workflow_file,
           "status": "completed", "conclusion": "success"}
    monkeypatch.setattr(canonical, "gh_json", lambda args: run)
    monkeypatch.setattr(transport, "gh_json", lambda repo, path: {"artifacts": artifacts} if "/artifacts?" in path else run)

    def download(command, **kwargs):
        identifier = int(command[-1].split("/")[-2])
        kwargs["stdout"].write(archives[identifier])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(transport.subprocess, "run", download)
    if corruption:
        with pytest.raises((transport.ArtifactError, ValueError)):
            canonical.reuse_set(arguments)
        assert not Path(arguments.directory).exists()
    else:
        assert canonical.reuse_set(arguments)
        assert len(list(Path(arguments.directory).iterdir())) == 24
        for platform in platforms:
            assert (Path(arguments.directory) / f"vibe-show-runtime-node-{platform}.tgz").read_bytes() == f"native bytes {platform}".encode()
