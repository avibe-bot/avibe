"""Artifact trust boundary: API fixtures, real ZIP bytes and filesystem writes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
from pathlib import Path
import subprocess
import zipfile

import pytest

from scripts import release_workflow_artifacts as artifacts

SHA = "a" * 40
REPO = "fixture/avibe"


def zip_bytes(files=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in (files or {"index.html": b"verified UI", "assets/app.js": b"script"}).items():
            archive.writestr(name, content)
    return stream.getvalue()


def job(name="build-linux-artifacts"):
    return {"name": name, "run_id": 42, "run_attempt": 2, "status": "completed", "conclusion": "success",
            "started_at": "2026-11-11T00:01:00Z", "completed_at": "2026-11-11T00:02:00Z"}


@pytest.fixture
def producer(monkeypatch):
    data = zip_bytes()
    run = {"id": 42, "run_attempt": 2, "head_sha": SHA, "path": ".github/workflows/lint.yml",
           "head_branch": "master", "event": "push", "status": "completed", "conclusion": "success"}
    artifact = {"id": 7, "name": "ui-dist-linux", "expired": False,
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "created_at": "2026-11-11T00:01:50Z", "updated_at": "2026-11-11T00:01:51Z",
                "workflow_run": {"id": 42, "head_sha": SHA}}
    state = {"run": run, "jobs": [job(), job("master-full-backstop")], "artifacts": [artifact],
             "zip": data, "calls": []}

    def api(repository, path):
        assert repository == REPO
        state["calls"].append(path)
        if path == "actions/runs/42":
            return deepcopy(state["run"])
        if path == "actions/runs/42/attempts/2/jobs?per_page=100&page=1":
            return {"jobs": deepcopy(state["jobs"])}
        if path == "actions/runs/42/artifacts?per_page=100&page=1":
            return {"artifacts": deepcopy(state["artifacts"])}
        raise AssertionError(path)

    def transport(command, **kwargs):
        assert command == ["gh", "api", f"repos/{REPO}/actions/artifacts/7/zip"]
        kwargs["stdout"].write(state["zip"])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(artifacts, "gh_json", api)
    monkeypatch.setattr(artifacts.subprocess, "run", transport)
    return state


def test_ui_reuse_proves_run_attempt_job_window_and_actual_zip(producer, tmp_path):
    target = tmp_path / "ui/dist"
    assert artifacts.reuse_ui(REPO, "42", 2, SHA, target)
    assert (target / "index.html").read_bytes() == b"verified UI"
    assert (target / "assets/app.js").read_bytes() == b"script"
    assert producer["calls"].count("actions/runs/42") == 2


def test_ui_without_a_backstop_proof_makes_no_network_requests(producer, tmp_path):
    assert not artifacts.reuse_ui(REPO, "", 0, SHA, tmp_path / "dist")
    assert producer["calls"] == []


@pytest.mark.parametrize("change", [
    "sha", "attempt", "workflow", "event", "branch", "run-failed", "certificate",
    "job-attempt", "job-failed", "expired", "missing", "artifact-sha", "old-attempt", "digest", "zip",
])
def test_ui_rejects_unproven_or_corrupt_artifact_before_promoting(producer, tmp_path, change):
    if change in {"sha", "attempt", "workflow", "event", "branch", "run-failed"}:
        key, value = {"sha": ("head_sha", "b" * 40), "attempt": ("run_attempt", 3),
                      "workflow": ("path", "other.yml"), "event": ("event", "pull_request"),
                      "branch": ("head_branch", "feature"), "run-failed": ("conclusion", "failure")}[change]
        producer["run"][key] = value
    elif change == "certificate":
        producer["jobs"].pop()
    elif change == "job-attempt":
        producer["jobs"][0]["run_attempt"] = 1
    elif change == "job-failed":
        producer["jobs"][0]["conclusion"] = "failure"
    elif change == "missing":
        producer["artifacts"] = []
    elif change == "expired":
        producer["artifacts"][0]["expired"] = True
    elif change == "artifact-sha":
        producer["artifacts"][0]["workflow_run"]["head_sha"] = "b" * 40
    elif change == "old-attempt":
        producer["artifacts"][0]["created_at"] = "2026-11-10T00:01:50Z"
    elif change == "digest":
        producer["artifacts"][0]["digest"] = None
    elif change == "zip":
        producer["zip"] += b"corruption"
    target = tmp_path / "dist"
    with pytest.raises(artifacts.ArtifactError):
        artifacts.reuse_ui(REPO, "42", 2, SHA, target)
    assert not target.exists()


@pytest.mark.parametrize("member", ["../outside", "/absolute", "a/../../outside", "a\\outside", "C:/outside"])
def test_even_digest_matching_zip_cannot_escape_destination(producer, tmp_path, member):
    producer["zip"] = zip_bytes({member: b"bad"})
    producer["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(producer["zip"]).hexdigest()
    with pytest.raises(artifacts.ArtifactError, match="Unsafe"):
        artifacts.download_verified(REPO, producer["artifacts"][0], tmp_path / "dist")
    assert not (tmp_path / "outside").exists()


def release_producer(producer):
    producer["run"].update(path=".github/workflows/release_ai.yml", status="in_progress", conclusion=None)
    producer["jobs"] = [job("build-assets")]
    producer["artifacts"][0]["name"] = "release-dist-2"


def test_publish_waits_for_successful_current_attempt_before_downloading(producer, tmp_path, monkeypatch):
    release_producer(producer)
    producer["jobs"][0].update(status="in_progress", conclusion=None)
    sleeps = []

    def finish(seconds):
        sleeps.append(seconds)
        producer["jobs"][0].update(status="completed", conclusion="success")

    monkeypatch.setattr(artifacts.time, "sleep", finish)
    artifacts.wait_build(REPO, "42", 2, SHA, tmp_path / "dist")
    assert sleeps == [5]
    assert (tmp_path / "dist/index.html").read_bytes() == b"verified UI"


@pytest.mark.parametrize("change", ["failed", "cancelled", "skipped", "new-attempt", "job-old-attempt", "old-artifact", "run-cancelled"])
def test_publish_fails_closed_for_failure_cancel_and_rerun(producer, tmp_path, change):
    release_producer(producer)
    if change in {"failed", "cancelled", "skipped"}:
        producer["jobs"][0]["conclusion"] = change
    elif change == "new-attempt":
        producer["run"]["run_attempt"] = 3
    elif change == "job-old-attempt":
        producer["jobs"][0]["run_attempt"] = 1
    elif change == "old-artifact":
        producer["artifacts"][0]["name"] = "release-dist-1"
    else:
        producer["run"]["conclusion"] = "cancelled"
    with pytest.raises(artifacts.ArtifactError):
        artifacts.wait_build(REPO, "42", 2, SHA, tmp_path / "dist")
    assert not (tmp_path / "dist").exists()


def test_missing_job_cannot_wait_forever(producer, tmp_path):
    release_producer(producer)
    producer["jobs"] = []
    with pytest.raises(artifacts.ArtifactError, match="Timed out"):
        artifacts.wait_build(REPO, "42", 2, SHA, tmp_path / "dist", timeout=0)


def test_ui_cli_fallback_is_nonfatal_but_publish_proof_failure_is_fatal(producer, tmp_path, monkeypatch):
    producer["run"]["run_attempt"] = 3
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    arguments = ["script", "reuse-ui", "--repository", REPO, "--run-id", "42", "--attempt", "2",
                 "--sha", SHA, "--destination", str(tmp_path / "dist")]
    monkeypatch.setattr("sys.argv", arguments)
    assert artifacts.main() == 0
    assert output.read_text() == "reused=false\n"
    arguments[1] = "wait-build"
    assert artifacts.main() == 1
