#!/usr/bin/env python3
"""Own GitHub Release state transitions shared by release workflows."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote


NOTES_READY_MARKER_PREFIX = "<!-- avibe:release-notes=ready source="
_SHA_RE = re.compile(r"[0-9a-f]{40}")


class ReleaseError(RuntimeError):
    """Raised when a GitHub Release transition cannot be proven complete."""


@dataclass(frozen=True)
class ReleaseState:
    tag: str
    draft: bool
    prerelease: bool
    body: str
    url: str


def notes_ready_marker(source_sha: str, run_id: int) -> str:
    if _SHA_RE.fullmatch(source_sha) is None:
        raise ReleaseError(f"Invalid release source SHA: {source_sha!r}")
    if run_id <= 0:
        raise ReleaseError(f"Invalid release notes run ID: {run_id!r}")
    return f"{NOTES_READY_MARKER_PREFIX}{source_sha} run={run_id} -->"


def _run_gh(arguments: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["gh", *arguments]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ReleaseError(f"{' '.join(command)} failed: {detail}")
    return completed


def _release_path(repo: str, tag: str) -> str:
    return f"repos/{repo}/releases/tags/{quote(tag, safe='')}"


def _release_state(payload: Any, *, tag: str) -> ReleaseState:
    try:
        state = ReleaseState(
            tag=str(payload["tag_name"]),
            draft=bool(payload["draft"]),
            prerelease=bool(payload["prerelease"]),
            body=str(payload.get("body") or ""),
            url=str(payload["html_url"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseError(f"Invalid GitHub Release payload for {tag}") from exc
    if state.tag != tag:
        raise ReleaseError(f"GitHub Release payload did not match tag {tag}")
    return state


def _get_release_from_list(repo: str, tag: str) -> ReleaseState | None:
    completed = _run_gh(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/releases?per_page=100",
        ],
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ReleaseError(f"Could not inspect GitHub Release {tag}: {detail}")

    try:
        pages = json.loads(completed.stdout)
        matches = [
            release
            for page in pages
            for release in page
            if release.get("tag_name") == tag
        ]
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseError(f"Invalid GitHub Release list payload for {tag}") from exc
    if len(matches) > 1:
        raise ReleaseError(f"Multiple GitHub Releases found for tag {tag}")
    return _release_state(matches[0], tag=tag) if matches else None


def get_release(repo: str, tag: str) -> ReleaseState | None:
    completed = _run_gh(["api", _release_path(repo, tag)], check=False)
    if completed.returncode != 0:
        if "HTTP 404" in completed.stderr:
            # GitHub's get-by-tag endpoint intentionally omits draft releases.
            return _get_release_from_list(repo, tag)
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ReleaseError(f"Could not inspect GitHub Release {tag}: {detail}")

    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise ReleaseError(f"Invalid GitHub Release payload for {tag}") from exc
    return _release_state(payload, tag=tag)


def _notes_arguments(*, notes: str | None, notes_file: Path | None) -> list[str]:
    if notes_file is not None:
        return ["--notes-file", str(notes_file)]
    if notes is not None:
        return ["--notes", notes]
    raise ReleaseError("Release notes or a release notes file is required")


def ensure_draft(
    *,
    repo: str,
    tag: str,
    title: str,
    notes: str | None,
    notes_file: Path | None,
) -> ReleaseState:
    existing = get_release(repo, tag)
    if existing is not None:
        return existing

    arguments = [
        "release",
        "create",
        tag,
        "--repo",
        repo,
        "--title",
        title,
        *_notes_arguments(notes=notes, notes_file=notes_file),
        "--draft",
        "--latest=false",
        "--verify-tag",
    ]
    created = _run_gh(arguments, check=False)
    if created.returncode != 0:
        # Another release workflow may have won the create race. Only that
        # proven state makes the failed create recoverable.
        raced = get_release(repo, tag)
        if raced is None:
            detail = created.stderr.strip() or created.stdout.strip() or "unknown error"
            raise ReleaseError(f"Could not create draft GitHub Release {tag}: {detail}")
        return raced

    for _ in range(5):
        state = get_release(repo, tag)
        if state is not None:
            return state
        time.sleep(0.2)
    raise ReleaseError(f"Draft GitHub Release {tag} was created but cannot be read back")


def update_notes(
    *,
    repo: str,
    tag: str,
    title: str,
    notes_file: Path,
) -> ReleaseState:
    ensure_draft(
        repo=repo,
        tag=tag,
        title=title,
        notes=None,
        notes_file=notes_file,
    )
    _run_gh(
        [
            "release",
            "edit",
            tag,
            "--repo",
            repo,
            "--title",
            title,
            "--notes-file",
            str(notes_file),
        ]
    )
    state = get_release(repo, tag)
    if state is None:
        raise ReleaseError(f"GitHub Release {tag} disappeared after updating notes")
    return state


def _ready_run_id(body: str, source_sha: str) -> int | None:
    marker = re.search(
        re.escape(NOTES_READY_MARKER_PREFIX)
        + re.escape(source_sha)
        + r" run=([1-9][0-9]*) -->",
        body,
    )
    return int(marker.group(1)) if marker else None


def _get_workflow_run(*, repo: str, run_id: int) -> dict[str, Any]:
    completed = _run_gh(["api", f"repos/{repo}/actions/runs/{run_id}"])
    try:
        payload = json.loads(completed.stdout)
    except ValueError as exc:
        raise ReleaseError("Invalid GitHub Actions run payload") from exc
    if not isinstance(payload, dict):
        raise ReleaseError("GitHub Actions run payload is not an object")
    return payload


def wait_for_notes(
    *,
    repo: str,
    tag: str,
    workflow: str,
    branch: str,
    run_sha: str,
    source_sha: str,
    event: str,
    timeout: float,
    interval: float,
) -> ReleaseState:
    deadline = time.monotonic() + timeout
    while True:
        state = get_release(repo, tag)
        run_id = _ready_run_id(state.body, source_sha) if state is not None else None
        if run_id is not None:
            run = _get_workflow_run(repo=repo, run_id=run_id)
            run_path = str(run.get("path") or "").split("@", 1)[0]
            expected_path = f".github/workflows/{workflow}"
            if run_path != expected_path:
                raise ReleaseError(
                    f"Release notes marker points to unexpected workflow {run_path!r}"
                )
            if run.get("head_sha") != run_sha or run.get("head_branch") != branch:
                raise ReleaseError(
                    f"Release notes run {run_id} does not match the expected workflow revision"
                )
            if run.get("event") != event:
                raise ReleaseError(
                    f"Release notes run {run_id} has unexpected event {run.get('event')!r}"
                )
            if run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise ReleaseError(
                        f"Release notes workflow did not succeed for {source_sha}: "
                        f"{run.get('html_url') or run_id}"
                    )
                return state

        if time.monotonic() >= deadline:
            raise ReleaseError(
                f"Timed out waiting for {workflow} notes marker for {tag}"
            )
        time.sleep(interval)


def finalize_release(
    *,
    repo: str,
    tag: str,
    prerelease: bool,
    latest: str,
) -> ReleaseState:
    if get_release(repo, tag) is None:
        raise ReleaseError(f"Cannot finalize missing GitHub Release {tag}")

    arguments = [
        "release",
        "edit",
        tag,
        "--repo",
        repo,
        "--draft=false",
        "--prerelease" if prerelease else "--prerelease=false",
    ]
    if latest == "true":
        arguments.append("--latest")
    elif latest == "false":
        arguments.append("--latest=false")
    _run_gh(arguments)

    state = None
    for _ in range(10):
        state = get_release(repo, tag)
        if state is not None and not state.draft and state.prerelease == prerelease:
            break
        time.sleep(1)
    if state is None or state.draft:
        raise ReleaseError(f"GitHub Release {tag} is still a draft after finalization")
    if state.prerelease != prerelease:
        raise ReleaseError(f"GitHub Release {tag} has the wrong prerelease state")

    if latest != "preserve":
        for _ in range(10):
            current_latest = _run_gh(
                ["api", f"repos/{repo}/releases/latest", "--jq", ".tag_name"],
                check=False,
            )
            latest_tag = (
                current_latest.stdout.strip() if current_latest.returncode == 0 else ""
            )
            latest_matches = latest_tag == tag
            if (latest == "true" and latest_matches) or (
                latest == "false" and not latest_matches
            ):
                break
            time.sleep(1)
        else:
            if latest == "true":
                raise ReleaseError(f"GitHub Release {tag} was not made latest")
            raise ReleaseError(f"GitHub Release {tag} unexpectedly became latest")
    return state


def _print_state(state: ReleaseState) -> None:
    print(json.dumps(asdict(state), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure-draft")
    ensure.add_argument("--repo", required=True)
    ensure.add_argument("--tag", required=True)
    ensure.add_argument("--title", required=True)
    ensure_notes = ensure.add_mutually_exclusive_group(required=True)
    ensure_notes.add_argument("--notes")
    ensure_notes.add_argument("--notes-file", type=Path)

    update = subparsers.add_parser("update-notes")
    update.add_argument("--repo", required=True)
    update.add_argument("--tag", required=True)
    update.add_argument("--title", required=True)
    update.add_argument("--notes-file", required=True, type=Path)

    wait = subparsers.add_parser("wait-notes")
    wait.add_argument("--repo", required=True)
    wait.add_argument("--tag", required=True)
    wait.add_argument("--workflow", required=True)
    wait.add_argument("--branch", required=True)
    wait.add_argument("--run-sha", required=True)
    wait.add_argument("--source-sha", required=True)
    wait.add_argument("--event", required=True)
    wait.add_argument("--timeout", type=float, default=3600)
    wait.add_argument("--interval", type=float, default=15)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--repo", required=True)
    finalize.add_argument("--tag", required=True)
    finalize.add_argument("--prerelease", choices=("true", "false"), required=True)
    finalize.add_argument("--latest", choices=("true", "false", "preserve"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "ensure-draft":
            state = ensure_draft(
                repo=arguments.repo,
                tag=arguments.tag,
                title=arguments.title,
                notes=arguments.notes,
                notes_file=arguments.notes_file,
            )
        elif arguments.command == "update-notes":
            state = update_notes(
                repo=arguments.repo,
                tag=arguments.tag,
                title=arguments.title,
                notes_file=arguments.notes_file,
            )
        elif arguments.command == "wait-notes":
            state = wait_for_notes(
                repo=arguments.repo,
                tag=arguments.tag,
                workflow=arguments.workflow,
                branch=arguments.branch,
                run_sha=arguments.run_sha,
                source_sha=arguments.source_sha,
                event=arguments.event,
                timeout=arguments.timeout,
                interval=arguments.interval,
            )
        else:
            state = finalize_release(
                repo=arguments.repo,
                tag=arguments.tag,
                prerelease=arguments.prerelease == "true",
                latest=arguments.latest,
            )
    except ReleaseError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    _print_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
