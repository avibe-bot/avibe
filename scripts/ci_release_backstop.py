#!/usr/bin/env python3
"""Gate a release tag on proof about its exact source SHA.

Every tag needs a successful full master lint backstop at that SHA. With
``--rc-pull-request-gate``, a ``gh-vX.Y.ZrcN`` tag instead needs its SHA to be
the merge commit of a pull request merged into master whose head passed every
check master's ruleset requires; the full backstop is then only reported.
"""

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
# The canonical GitHub-only pre-release tag, as scripts/desktop_release.py reads it.
RC_TAG_RE = re.compile(r"gh-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)rc(0|[1-9][0-9]*)")
PASSING_CHECK_CONCLUSIONS = frozenset({"success", "skipped"})


class ReleaseBackstopError(RuntimeError):
    """Raised when an exact full master backstop cannot be proven."""


@dataclass(frozen=True)
class BackstopProof:
    run_id: int
    source_sha: str
    event: str


@dataclass(frozen=True)
class PullRequestProof:
    source_sha: str
    pull_number: int
    head_sha: str
    # Reported, never required: "success", "pending", "failed", "missing" or "unknown".
    backstop: str
    backstop_run_id: int | None = None


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
        page_items = payload
        if isinstance(payload, dict):
            page_items = next(
                (payload[key] for key in ("jobs", "workflow_runs", "check_runs") if key in payload),
                None,
            )
        if not isinstance(page_items, list):
            raise ReleaseBackstopError("GitHub returned an invalid list response")
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


def _backstop(api: GitHubApi, source_sha: str) -> tuple[BackstopProof | None, str, int | None]:
    """Return the exact-SHA proof, or the state and run that explain its absence.

    ``missing`` means no full pass finished at this SHA: none started, or the
    pending one was coalesced away or cancelled.
    """

    state: tuple[str, int | None] = ("missing", None)
    for run in _candidate_runs(api, source_sha):
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        if run.get("status") != "completed":
            state = ("pending", run_id)
        elif run.get("conclusion") == "success":
            terminal = [job for job in _jobs(api, run_id) if job.get("name") == TERMINAL_JOB]
            if any(job.get("status") == "completed" and job.get("conclusion") == "success" for job in terminal):
                proof = BackstopProof(run_id=run_id, source_sha=source_sha, event=str(run.get("event")))
                return proof, "success", run_id
        elif run.get("conclusion") not in {"cancelled", "skipped"} and state[0] == "missing":
            state = ("failed", run_id)
    return None, *state


def prove_backstop(api: GitHubApi, *, tag: str) -> BackstopProof:
    source_sha = resolve_tag_sha(api, tag)
    proof, _state, _run_id = _backstop(api, source_sha)
    if proof is not None:
        return proof
    raise ReleaseBackstopError(
        f"No successful full master backstop found for exact source SHA {source_sha}; "
        "wait for or re-run the full backstop at that SHA before releasing. "
        "A coalesced-away SHA cannot release without exact full proof."
    )


def _require_on_master(api: GitHubApi, source_sha: str) -> None:
    comparison = api.get(f"compare/{source_sha}...{MASTER_BRANCH}", query={"per_page": 1})
    comparison = comparison if isinstance(comparison, dict) else {}
    merge_base = comparison.get("merge_base_commit")
    if (
        comparison.get("status") not in {"ahead", "identical"}
        or not isinstance(merge_base, dict)
        or merge_base.get("sha") != source_sha
    ):
        raise ReleaseBackstopError(f"RC source SHA {source_sha} is not on {MASTER_BRANCH}")


def _merged_pull(api: GitHubApi, source_sha: str) -> tuple[int, str]:
    """Return the number and head SHA of the one PR whose merge into master is this SHA."""

    merged = [
        pull
        for pull in _paged(api, f"commits/{source_sha}/pulls", query={})
        if pull.get("merged_at")
        and pull.get("merge_commit_sha") == source_sha
        and isinstance(pull.get("base"), dict)
        and pull["base"].get("ref") == MASTER_BRANCH
    ]
    if len(merged) != 1:
        raise ReleaseBackstopError(
            f"Could not resolve RC source SHA {source_sha} to the one pull request merged into "
            f"{MASTER_BRANCH} as that commit; an RC is cut only from a pull request's merge commit."
        )
    number = merged[0].get("number")
    head = merged[0].get("head")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(head, dict):
        raise ReleaseBackstopError("GitHub returned an invalid pull request")
    return number, _sha(head.get("sha"), context=f"pull request #{number} head")


def _required_checks(api: GitHubApi) -> list[tuple[str, int | None]]:
    """Return each context master's active rulesets require, with the app that must report it."""

    required: list[tuple[str, int | None]] = []
    for rule in _paged(api, f"rules/branches/{MASTER_BRANCH}", query={}):
        if rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        checks = parameters.get("required_status_checks") if isinstance(parameters, dict) else None
        for check in checks if isinstance(checks, list) else [None]:
            context = check.get("context") if isinstance(check, dict) else None
            if not isinstance(context, str) or not context:
                raise ReleaseBackstopError("GitHub returned an invalid required status check")
            integration = check.get("integration_id")
            required.append((context, integration if isinstance(integration, int) else None))
    if not required:
        raise ReleaseBackstopError(
            f"{MASTER_BRANCH} has no required status checks in an active ruleset; "
            "an RC cannot be proven without them."
        )
    return required


def _require_green_checks(api: GitHubApi, *, pull_number: int, head_sha: str) -> None:
    runs = [
        run
        for run in _paged(api, f"commits/{head_sha}/check-runs", query={"filter": "latest"})
        if isinstance(run.get("id"), int)
    ]
    problems: list[str] = []
    for context, integration in _required_checks(api):
        matching = [
            run
            for run in runs
            if run.get("name") == context
            and (integration is None or (isinstance(run.get("app"), dict) and run["app"].get("id") == integration))
        ]
        if not matching:
            problems.append(f"{context} (missing)")
            continue
        # As the ruleset does, judge a context by its newest run on the head.
        newest = max(matching, key=lambda run: run["id"])
        # An unfinished run has no conclusion.
        if newest.get("conclusion") not in PASSING_CHECK_CONCLUSIONS:
            problems.append(f"{context} ({newest.get('conclusion') or newest.get('status')})")
    if problems:
        raise ReleaseBackstopError(
            f"Required checks are not green on pull request #{pull_number} head {head_sha}: "
            + ", ".join(problems)
        )


def prove_rc_pull_request(api: GitHubApi, *, tag: str) -> PullRequestProof:
    """Prove an RC's source is a master merge commit whose PR head passed the required checks."""

    source_sha = resolve_tag_sha(api, tag)
    _require_on_master(api, source_sha)
    pull_number, head_sha = _merged_pull(api, source_sha)
    _require_green_checks(api, pull_number=pull_number, head_sha=head_sha)
    try:
        _proof, backstop, run_id = _backstop(api, source_sha)
    except ReleaseBackstopError:
        backstop, run_id = "unknown", None
    return PullRequestProof(
        source_sha=source_sha,
        pull_number=pull_number,
        head_sha=head_sha,
        backstop=backstop,
        backstop_run_id=run_id,
    )


def prove_release(
    api: GitHubApi, *, tag: str, rc_pull_request_gate: bool = False
) -> BackstopProof | PullRequestProof:
    if rc_pull_request_gate and RC_TAG_RE.fullmatch(tag):
        return prove_rc_pull_request(api, tag=tag)
    return prove_backstop(api, tag=tag)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--tag", required=True)
    parser.add_argument("--token", default=os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument(
        "--rc-pull-request-gate",
        action="store_true",
        help="gate a gh-vX.Y.ZrcN tag on its pull request's required checks instead of the full backstop",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        proof = prove_release(
            GitHubApi(args.repository, args.token),
            tag=args.tag,
            rc_pull_request_gate=args.rc_pull_request_gate,
        )
    except ReleaseBackstopError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    outputs = [f"source_sha={proof.source_sha}"]
    if isinstance(proof, BackstopProof):
        print(f"Exact full master backstop verified for {proof.source_sha} (run {proof.run_id}, event {proof.event}).")
        outputs.append(f"run_id={proof.run_id}")
    else:
        print(
            f"RC pull-request gate verified for {proof.source_sha}: it is on {MASTER_BRANCH} and pull request "
            f"#{proof.pull_number} passed every required check on its head {proof.head_sha}."
        )
        run = f" (run {proof.backstop_run_id})" if proof.backstop_run_id else ""
        print(f"Full master backstop at {proof.source_sha}: {proof.backstop}{run}. It does not gate an RC.")
        if proof.backstop == "failed":
            print(
                f"::warning::The full master backstop failed at RC source {proof.source_sha}{run}. "
                "The RC is not blocked; report it as docs/release.md describes."
            )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write("".join(f"{line}\n" for line in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
