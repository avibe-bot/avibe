#!/usr/bin/env python3
"""Verified Actions artifact transport for the release hot path (stdlib only)."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any
import zipfile


class ArtifactError(RuntimeError):
    """A producer or its bytes could not be proven."""


def gh_json(repository: str, path: str) -> Any:
    result = subprocess.run(
        ["gh", "api", f"repos/{repository}/{path}"],
        capture_output=True, text=True, check=True, timeout=60,
    )
    return json.loads(result.stdout)


def listed(repository: str, path: str, key: str) -> list[dict[str, Any]]:
    items = []
    for page in range(1, 101):
        payload = gh_json(repository, f"{path}?per_page=100&page={page}")[key]
        if not isinstance(payload, list):
            raise ArtifactError(f"Invalid {key} response")
        items.extend(payload)
        if len(payload) < 100:
            return items
    raise ArtifactError(f"Too many {key} pages")


def check_run(repository: str, run_id: str, attempt: int, sha: str, workflow: str, *, completed: bool) -> dict:
    run = gh_json(repository, f"actions/runs/{run_id}")
    if (str(run.get("id")) != str(run_id) or run.get("run_attempt") != attempt
            or run.get("head_sha") != sha or run.get("path") != workflow):
        raise ArtifactError("Workflow run identity or attempt changed")
    if completed and (run.get("status"), run.get("conclusion")) != ("completed", "success"):
        raise ArtifactError("Producer run is not successful")
    if not completed and run.get("conclusion") not in (None, "success"):
        raise ArtifactError("Release run failed or was cancelled")
    return run


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ArtifactError("Missing producer timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactError("Invalid producer timestamp") from exc
    if result.tzinfo is None:
        raise ArtifactError("Producer timestamp has no timezone")
    return result


def successful_job(repository: str, run_id: str, attempt: int, name: str, *, run_started_at: str) -> dict | None:
    jobs = listed(repository, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    matches = [job for job in jobs if job.get("name") == name]
    if not matches:
        return None
    if len(matches) != 1:
        raise ArtifactError(f"Ambiguous producer job: {name}")
    job = matches[0]
    if job.get("run_attempt") != attempt or str(job.get("run_id")) != str(run_id):
        raise ArtifactError("Producer job belongs to another run or attempt")
    if job.get("status") != "completed":
        return None
    if job.get("conclusion") != "success":
        raise ArtifactError(f"Producer job did not succeed: {name}")
    # GitHub relabels inherited successful jobs with the new run_attempt on a
    # partial rerun. Their original timestamps expose that they did not rerun.
    if timestamp(job.get("started_at")) < timestamp(run_started_at):
        raise ArtifactError("Producer job was inherited from an earlier attempt")
    return job


def artifact_for_job(repository: str, run_id: str, sha: str, name: str, job: dict) -> dict:
    matches = [artifact for artifact in listed(repository, f"actions/runs/{run_id}/artifacts", "artifacts")
               if artifact.get("name") == name and not artifact.get("expired")]
    if len(matches) != 1:
        raise ArtifactError(f"Missing or ambiguous artifact: {name}")
    artifact = matches[0]
    producer = artifact.get("workflow_run") or {}
    if str(producer.get("id")) != str(run_id) or producer.get("head_sha") != sha:
        raise ArtifactError("Artifact belongs to another source or run")
    # The artifacts API has no attempt field. Bind its immutable ID/digest to
    # the successful producer job's exact-attempt upload window instead.
    start, end = (timestamp(job.get(key)) for key in ("started_at", "completed_at"))
    created, updated = (timestamp(artifact.get(key)) for key in ("created_at", "updated_at"))
    if not start <= created <= updated <= end:
        raise ArtifactError("Artifact was not uploaded by this attempt's producer job")
    return artifact


def download_verified(repository: str, artifact: dict, destination: Path) -> None:
    digest = artifact.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ArtifactError("Artifact has no SHA256 digest")
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
        raise ArtifactError("Invalid artifact ID")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-artifact-") as temporary:
        archive = Path(temporary) / "artifact.zip"
        with archive.open("wb") as output:
            subprocess.run(["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"],
                           stdout=output, stderr=subprocess.PIPE, check=True, timeout=180)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest.removeprefix("sha256:"):
            raise ArtifactError("Artifact ZIP digest mismatch")
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            names = set()
            for member in members:
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (not path.parts or path.is_absolute() or ".." in path.parts
                        or "\\" in member.filename or ":" in member.filename
                        or stat.S_ISLNK(mode) or path.as_posix() in names):
                    raise ArtifactError("Unsafe or duplicate artifact member")
                names.add(path.as_posix())
            for member in members:
                target = destination / member.filename
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as incoming, target.open("xb") as output:
                        shutil.copyfileobj(incoming, output)


def reuse_ui(repository: str, run_id: str, attempt: int, sha: str, destination: Path) -> bool:
    if not run_id or attempt < 1:
        return False  # RCs with unfinished backstops pay no discovery/network cost.
    run = check_run(repository, run_id, attempt, sha, ".github/workflows/lint.yml", completed=True)
    if run.get("event") not in {"push", "workflow_dispatch"} or run.get("head_branch") != "master":
        raise ArtifactError("UI producer is not a master backstop")
    if not successful_job(repository, run_id, attempt, "master-full-backstop", run_started_at=run.get("run_started_at")):
        raise ArtifactError("Full backstop certificate is missing")
    job = successful_job(repository, run_id, attempt, "build-linux-artifacts", run_started_at=run.get("run_started_at"))
    if not job:
        raise ArtifactError("UI producer did not finish")
    artifact = artifact_for_job(repository, run_id, sha, "ui-dist-linux", job)
    with tempfile.TemporaryDirectory(prefix="release-ui-") as temporary:
        candidate = Path(temporary) / "dist"
        download_verified(repository, artifact, candidate)
        if not (candidate / "index.html").is_file():
            raise ArtifactError("UI artifact has no index.html")
        check_run(repository, run_id, attempt, sha, ".github/workflows/lint.yml", completed=True)
        if destination.exists():
            raise ArtifactError("UI destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(candidate, destination)
    return True


def wait_build(repository: str, run_id: str, attempt: int, sha: str, destination: Path, *, timeout: float = 1800) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            run = check_run(repository, run_id, attempt, sha, ".github/workflows/release_ai.yml", completed=False)
            job = successful_job(repository, run_id, attempt, "build-assets", run_started_at=run.get("run_started_at"))
        except (subprocess.SubprocessError, json.JSONDecodeError):
            # Transport failures mean unknown, never success. Retry polling
            # within the same budget; identity/failure verdicts still fail now.
            print("Actions status temporarily unavailable; waiting for proof.", flush=True)
            job = None
        if job:
            artifact = artifact_for_job(repository, run_id, sha, f"release-dist-{attempt}", job)
            download_verified(repository, artifact, destination)
            check_run(repository, run_id, attempt, sha, ".github/workflows/release_ai.yml", completed=False)
            return
        if time.monotonic() >= deadline:
            raise ArtifactError("Timed out waiting for this attempt's build-assets job")
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("reuse-ui", "wait-build"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", type=int, default=0)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "reuse-ui":
            reused = reuse_ui(args.repository, args.run_id, args.attempt, args.sha, args.destination)
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"reused={str(reused).lower()}\n")
        else:
            wait_build(args.repository, args.run_id, args.attempt, args.sha, args.destination)
    except (ArtifactError, OSError, ValueError, KeyError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        if args.command == "reuse-ui":
            # Do not log credential-bearing subprocess stderr or signed URLs.
            print(f"UI reuse unavailable ({type(exc).__name__}); building UI from source.")
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write("reused=false\n")
            return 0
        print(f"::error::Release build/artifact proof failed ({type(exc).__name__}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
