#!/usr/bin/env python3
"""Require an exact successful full master lint backstop before releasing a tag."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

API_ROOT = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
WORKFLOW = "lint.yml"
WORKFLOW_PATH = ".github/workflows/lint.yml"
MASTER_BRANCH = "master"
TERMINAL_JOB = "master-full-backstop"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class ReleaseBackstopError(RuntimeError):
    """Raised when an exact full master backstop cannot be proven."""


@dataclass(frozen=True)
class BackstopProof:
    run_id: int
    source_sha: str
    event: str


class GitHubApi:
    """Small authenticated GitHub REST client with no secret-bearing errors."""

    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_root: str = API_ROOT,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not repository or "/" not in repository:
            raise ReleaseBackstopError("GITHUB_REPOSITORY must be owner/repository")
        if not token:
            raise ReleaseBackstopError("GH_TOKEN is required to read GitHub Actions status")
        self.repository = repository
        self.token = token
        self.api_root = api_root.rstrip("/")
        self.opener = opener

    def get(self, path: str, *, query: dict[str, str | int] | None = None) -> Any:
        url = f"{self.api_root}/repos/{self.repository}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "avibe-release-backstop/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ReleaseBackstopError(f"GitHub API request failed (HTTP {exc.code})") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ReleaseBackstopError(f"GitHub API request failed ({type(exc).__name__})") from exc


def _sha(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value) is None:
        raise ReleaseBackstopError(f"GitHub returned an invalid {context} SHA")
    return value.lower()


def resolve_tag_sha(api: GitHubApi, tag: str) -> str:
    """Resolve lightweight and arbitrarily nested annotated tags to a commit SHA."""

    if not tag:
        raise ReleaseBackstopError("release tag is empty")
    try:
        ref = api.get(f"git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    except ReleaseBackstopError as exc:
        raise ReleaseBackstopError(f"could not resolve release tag '{tag}'") from exc
    current = ref.get("object") if isinstance(ref, dict) else None
    for _ in range(8):
        if not isinstance(current, dict):
            break
        object_type = current.get("type")
        object_sha = _sha(current.get("sha"), context=f"tag '{tag}'")
        if object_type == "commit":
            return object_sha
        if object_type != "tag":
            break
        annotated = api.get(f"git/tags/{object_sha}")
        current = annotated.get("object") if isinstance(annotated, dict) else None
    raise ReleaseBackstopError(f"release tag '{tag}' does not resolve to a commit")


def _paged(api: GitHubApi, path: str, *, query: dict[str, str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = api.get(path, query={**query, "page": page, "per_page": 100})
        if isinstance(payload, dict):
            page_items = payload.get("jobs") or payload.get("workflow_runs")
        else:
            page_items = payload
        if not isinstance(page_items, list):
            raise ReleaseBackstopError("GitHub returned an invalid Actions response")
        for item in page_items:
            if isinstance(item, dict):
                items.append(item)
        if len(page_items) < 100:
            return items
        page += 1


def _candidate_runs(api: GitHubApi, source_sha: str) -> list[dict[str, Any]]:
    path = f"actions/workflows/{urllib.parse.quote(WORKFLOW, safe='')}/runs"
    runs: list[dict[str, Any]] = []
    for event in ("push", "workflow_dispatch"):
        runs.extend(
            _paged(
                api,
                path,
                query={
                    "event": event,
                    "branch": MASTER_BRANCH,
                    "head_sha": source_sha,
                },
            )
        )
    return [
        run
        for run in runs
        if run.get("event") in {"push", "workflow_dispatch"}
        and run.get("head_branch") == MASTER_BRANCH
        and run.get("path") == WORKFLOW_PATH
        and isinstance(run.get("head_sha"), str)
        and run["head_sha"].lower() == source_sha
    ]


def _jobs(api: GitHubApi, run_id: int) -> list[dict[str, Any]]:
    return _paged(api, f"actions/runs/{run_id}/jobs", query={})


def prove_backstop(api: GitHubApi, *, tag: str) -> BackstopProof:
    source_sha = resolve_tag_sha(api, tag)
    candidates = _candidate_runs(api, source_sha)
    for run in candidates:
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        terminal = [job for job in _jobs(api, run_id) if job.get("name") == TERMINAL_JOB]
        if any(job.get("status") == "completed" and job.get("conclusion") == "success" for job in terminal):
            return BackstopProof(run_id=run_id, source_sha=source_sha, event=str(run.get("event")))

    raise ReleaseBackstopError(
        f"No successful full master backstop found for exact source SHA {source_sha}; "
        "wait for or re-run the full backstop at that SHA before releasing. "
        "A coalesced-away SHA cannot release without exact full proof."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        proof = prove_backstop(GitHubApi(args.repository, args.token), tag=args.tag)
    except ReleaseBackstopError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(f"Exact full master backstop verified for {proof.source_sha} (run {proof.run_id}, event {proof.event}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
