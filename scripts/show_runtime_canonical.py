#!/usr/bin/env python3
"""Find and verify trusted Show Runtime canonical artifacts.

Canonical artifacts are produced only by successful release workflows. Their
identity records the producer workflow/run, Avibe source provenance, Show
Runtime ref and lockfile, platform, toolchain, router digest, and archive
digest. Release jobs may reuse one only when every field matches the current
release identity; otherwise they build normally.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def gh_json(arguments: list[str]) -> Any:
    result = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def find_run(repository: str, workflows: list[str], runtime_ref: str, platforms: list[str]) -> tuple[str, str, str, int] | None:
    expected = {f"canonical-show-runtime-{runtime_ref}-{platform}" for platform in platforms}
    candidates: list[tuple[str, str, str, str, int]] = []
    for workflow in workflows:
        payload = gh_json([
            "api",
            f"repos/{repository}/actions/workflows/{Path(workflow).name}/runs?status=success&per_page=100",
        ])
        for run in payload.get("workflow_runs", []):
            run_id = str(run.get("id", ""))
            if not run_id or run.get("conclusion") != "success":
                continue
            artifacts = gh_json([
                "api",
                f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100",
            ]).get("artifacts", [])
            names = {
                str(artifact.get("name"))
                for artifact in artifacts
                if not artifact.get("expired") and not artifact.get("archive_download_url") is None
            }
            if expected <= names:
                candidates.append((
                    str(run.get("created_at", "")),
                    run_id,
                    workflow,
                    str(run.get("name", "")),
                    int(run.get("run_attempt", 1)),
                ))
    if not candidates:
        return None
    _, run_id, workflow, workflow_name, run_attempt = max(candidates)
    return run_id, workflow, workflow_name, run_attempt


def identity_path(directory: Path, artifact: str) -> Path:
    return directory / f"show-runtime-identity-{artifact}.json"


def archive_path(directory: Path, artifact: str) -> Path:
    expected = directory / f"vibe-show-runtime-node-{artifact}.tgz"
    if not expected.is_file():
        raise ValueError(f"missing expected Show Runtime archive: {expected.name}")
    archives = sorted(directory.glob("vibe-show-runtime-node-*.tgz"))
    if archives != [expected]:
        raise ValueError(f"expected only {expected.name}, found {[path.name for path in archives]}")
    return expected


def router_path(directory: Path, artifact: str) -> Path:
    return directory / f"show-router-{artifact}.tsx"


def ref_path(directory: Path, artifact: str) -> Path:
    return directory / f"show-runtime-ref-{artifact}.txt"


def expected_identity(args: argparse.Namespace, archive: Path, router: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "producer_workflow_name": args.producer_workflow_name,
        "producer_workflow_file": args.producer_workflow_file,
        "producer_job": "show-runtime-bundles",
        "producer_run_id": str(args.producer_run_id),
        "producer_run_attempt": int(args.producer_run_attempt),
        "producer_source_sha": args.producer_source_sha,
        "runtime_ref": args.runtime_ref,
        "lock_sha256": args.lock_sha,
        "platform": args.artifact,
        "node_version": args.node_version,
        "npm_version": args.npm_version,
        "archive_name": archive.name,
        "archive_sha256": digest(archive),
        "router_name": router.name,
        "router_sha256": digest(router),
    }


def record(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    directory.mkdir(parents=True, exist_ok=True)
    archive = archive_path(directory, args.artifact)
    router = router_path(directory, args.artifact)
    if not router.is_file():
        raise ValueError(f"missing router export: {router.name}")
    ref_path(directory, args.artifact).write_text(args.runtime_ref + "\n", encoding="utf-8")
    identity_path(directory, args.artifact).write_text(
        json.dumps(expected_identity(args, archive, router), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def verify(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    archive = archive_path(directory, args.artifact)
    router = router_path(directory, args.artifact)
    ref = ref_path(directory, args.artifact)
    identity = identity_path(directory, args.artifact)
    for path in (router, ref, identity):
        if not path.is_file():
            raise ValueError(f"missing canonical artifact member: {path.name}")
    if ref.read_text(encoding="utf-8").strip() != args.runtime_ref:
        raise ValueError("Show Runtime ref provenance mismatch")
    actual = json.loads(identity.read_text(encoding="utf-8"))
    expected = expected_identity(args, archive, router)
    if args.producer_source_sha is None:
        expected["producer_source_sha"] = actual.get("producer_source_sha")
    if actual != expected:
        raise ValueError("Show Runtime canonical identity or digest mismatch")
    if not SHA_RE.fullmatch(str(actual["producer_source_sha"])):
        raise ValueError("Show Runtime producer source provenance mismatch")
    if not SHA256_RE.fullmatch(str(actual["lock_sha256"])):
        raise ValueError("Show Runtime lockfile provenance mismatch")
    for field in ("archive_sha256", "router_sha256"):
        if not SHA256_RE.fullmatch(str(actual[field])):
            raise ValueError(f"Show Runtime {field} provenance mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    find_parser = subparsers.add_parser("find")
    find_parser.add_argument("--repository", required=True)
    find_parser.add_argument("--workflow", action="append", dest="workflows", required=True)
    find_parser.add_argument("--runtime-ref", required=True)
    find_parser.add_argument("--platform", action="append", dest="platforms", required=True)
    find_parser.add_argument("--output", required=True)

    for command in ("record", "verify"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--directory", required=True)
        command_parser.add_argument("--artifact", required=True)
        command_parser.add_argument("--runtime-ref", required=True)
        command_parser.add_argument("--lock-sha", required=True)
        command_parser.add_argument("--producer-workflow-name", required=True)
        command_parser.add_argument("--producer-workflow-file", required=True)
        command_parser.add_argument("--producer-run-id", required=True)
        command_parser.add_argument("--producer-run-attempt", required=True, type=int)
        command_parser.add_argument("--producer-source-sha", required=command == "record")
        command_parser.add_argument("--node-version", required=True)
        command_parser.add_argument("--npm-version", required=True)

    args = parser.parse_args()
    if args.command == "find":
        result = find_run(args.repository, args.workflows, args.runtime_ref, args.platforms)
        output = Path(args.output)
        with output.open("a", encoding="utf-8") as stream:
            stream.write(f"run_id={result[0] if result else ''}\n")
            stream.write(f"workflow_file={result[1] if result else ''}\n")
            stream.write(f"workflow_name={result[2] if result else ''}\n")
            stream.write(f"run_attempt={result[3] if result else ''}\n")
        return 0
    if args.command == "record":
        record(args)
    else:
        verify(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"error: {error}")
