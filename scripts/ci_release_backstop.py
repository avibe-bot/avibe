#!/usr/bin/env python3
"""Gate a release tag on proof about its exact source SHA.

Every tag needs a successful full master lint backstop at that SHA. With
``--rc-pull-request-gate``, a ``gh-vX.Y.ZrcN`` tag instead needs its SHA to be
the merge commit of a pull request merged into master whose head passed every
check master's ruleset requires, plus exact-SHA native evidence for protected
changes since the last published product release. Other backstop jobs are reported.
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
from datetime import datetime
import subprocess
from pathlib import Path
from typing import Any, Callable

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import ci_protected_rc as protected
from scripts.sqlite_predecessor_contract import select_previous_release

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
    # Optional optimization provenance, not a new release-gate requirement.
    run_attempt: int = 0


@dataclass(frozen=True)
class PullRequestProof:
    source_sha: str
    pull_number: int
    head_sha: str
    # Overall backstop state; required named jobs are verified before returning.
    backstop: str
    backstop_run_id: int | None = None
    full_backstop: BackstopProof | None = None


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
                attempt = run.get("run_attempt")
                if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                    attempt = 0  # No exact-attempt UI reuse; gate policy is unchanged.
                proof = BackstopProof(run_id=run_id, source_sha=source_sha, event=str(run.get("event")),
                                      run_attempt=attempt)
                return proof, "success", run_id
        elif run.get("conclusion") not in {"cancelled", "skipped"} and state[0] == "missing":
            state = ("failed", run_id)
    return None, *state


def prove_backstop(api: GitHubApi, *, tag: str) -> BackstopProof:
    return prove_backstop_sha(api, source_sha=resolve_tag_sha(api, tag))


def prove_backstop_sha(api: GitHubApi, *, source_sha: str) -> BackstopProof:
    """Prove an exact commit before a release workflow creates its tag."""
    source_sha = _sha(source_sha, context="source")
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


def _timestamp(value: Any, *, context: str) -> datetime:
    if not isinstance(value, str):
        raise ReleaseBackstopError(f"GitHub returned an invalid {context}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseBackstopError(f"GitHub returned an invalid {context}") from exc
    if parsed.tzinfo is None:
        raise ReleaseBackstopError(f"GitHub returned an invalid {context}")
    return parsed


def _merged_pull(api: GitHubApi, source_sha: str) -> tuple[int, str, datetime]:
    """Return the number, head SHA and merge time of the one PR whose merge into master is this SHA."""

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
    head_sha = _sha(head.get("sha"), context=f"pull request #{number} head")
    return number, head_sha, _timestamp(merged[0].get("merged_at"), context=f"pull request #{number} merge time")


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


def _require_green_checks(api: GitHubApi, *, pull_number: int, head_sha: str, merged_at: datetime) -> None:
    """Judge each required context as the ruleset did when the pull request merged.

    Only check runs that completed at or before the merge count: a run started
    afterwards (a label event, a manual rerun) can neither block nor vouch for
    the merged state. A context with no qualifying run is missing.
    """
    runs = [
        run
        for run in _paged(api, f"commits/{head_sha}/check-runs", query={"filter": "all"})
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
        settled = [
            run
            for run in matching
            if run.get("status") == "completed"
            and run.get("completed_at") is not None
            and _timestamp(run.get("completed_at"), context=f"{context} check-run completion time") <= merged_at
        ]
        if not settled:
            if matching:
                newest = max(matching, key=lambda run: run["id"])
                state = newest.get("conclusion") or newest.get("status")
                problems.append(f"{context} (no run completed before the merge; newest is {state})")
            else:
                problems.append(f"{context} (missing)")
            continue
        # As the ruleset did at merge, judge a context by its newest settled run.
        # Newest means most recently created, not last to finish: a superseded
        # run cancelled after its replacement completed must not decide.
        newest = max(settled, key=lambda run: run["id"])
        if newest.get("conclusion") not in PASSING_CHECK_CONCLUSIONS:
            problems.append(f"{context} ({newest.get('conclusion')})")
    if problems:
        raise ReleaseBackstopError(
            f"Required checks are not green on pull request #{pull_number} head {head_sha} as merged: "
            + ", ".join(problems)
        )


def _checked_range(api: GitHubApi, tag: str, source_sha: str) -> set[str]:
    try:
        baseline = select_previous_release(_paged(api, "releases", query={}), release_tag=tag)
        base = resolve_tag_sha(api, baseline["tag_name"])
        return protected.required_jobs(protected.touched(base, source_sha))
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        raise ReleaseBackstopError("Cannot prove complete range from the previous published product release") from exc


def _named_jobs(api: GitHubApi, sha: str) -> dict[str, dict[str, Any]]:
    # Never combine a red current run with successful jobs from a superseded one.
    runs = _candidate_runs(api, sha)
    if not runs:
        raise ReleaseBackstopError(f"Missing protected native evidence for exact SHA {sha}")
    run = max(runs, key=lambda r: r['id'])
    run_id, attempt = run.get('id'), run.get('run_attempt')
    if type(run_id) is not int or type(attempt) is not int or attempt < 1:
        raise ReleaseBackstopError('Invalid master run identity')
    path = f'actions/runs/{run_id}/attempts/{attempt}'
    proof = api.get(path)
    if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in {
        'id': run_id, 'run_attempt': attempt, 'head_sha': sha, 'path': WORKFLOW_PATH,
        'head_branch': MASTER_BRANCH, 'event': run['event'],
    }.items()):
        raise ReleaseBackstopError('Master attempt identity does not match the selected source')
    began = _timestamp(proof.get('run_started_at'), context='master attempt start')
    jobs: dict[str, dict[str, Any]] = {}
    for job in _paged(api, path + '/jobs', query={}):
        if (job.get('run_id') != run_id or job.get('run_attempt') != attempt or job.get('head_sha') != sha
                or not job.get('started_at') or _timestamp(job['started_at'], context='job start') < began):
            continue  # A partial rerun can relabel an inherited result. It is not fresh evidence.
        name = job.get('name')
        if not isinstance(name, str) or name in jobs:
            raise ReleaseBackstopError("Ambiguous master job evidence")
        jobs[name] = job
    return jobs


def _verify_protected(api: GitHubApi, tag: str, sha: str, waiver: str) -> None:
    wanted = _checked_range(api, tag, sha)
    if not wanted and not waiver:
        return
    jobs = _named_jobs(api, sha)
    def passed(job: dict[str, Any]) -> bool:
        return job.get('status') == 'completed' and job.get('conclusion') == 'success'
    missing = sorted(n for n in wanted if not passed(jobs.get(n, {})))
    if not waiver:
        if missing:
            raise ReleaseBackstopError('Missing or non-green protected job(s): ' + ', '.join(missing))
        return
    # The release lead records Product's explicit approval. It is an operational
    # rule, not a distinct GitHub identity; the mechanical conditions still apply.
    fields = waiver.split(' | ')
    if (len(fields) != 3 or not all(f.strip() for f in fields) or len(waiver) > 500
            or any(not ch.isprintable() for ch in waiver)):
        raise ReleaseBackstopError('Expected one job | reason | verbatim Product approval on one line')
    name, _reason, _approval = fields
    if not missing and name in wanted:
        # The same SHA now has strictly stronger proof. Stage 2 independently
        # checks this historical waiver against the published Stage 1 notes.
        return
    if missing != [name]:
        raise ReleaseBackstopError('Exception must name exactly the one non-green protected job')
    job = jobs.get(name, {})
    if (job.get('status') != 'completed' or job.get('conclusion') != 'cancelled' or
            job.get('runner_id') != 0 or job.get('runner_name') != '' or job.get('steps') != []):
        raise ReleaseBackstopError('Exception requires a cancelled job with no runner and no steps')
    commit = api.get(f'commits/{sha}')
    parents = commit.get('parents') if isinstance(commit, dict) else None
    if not isinstance(parents, list) or len(parents) != 1 or not isinstance(parents[0], dict):
        raise ReleaseBackstopError('Exception requires one exact parent')
    parent = _sha(parents[0].get('sha'), context='RC parent')
    try:
        affected = protected.inputs_changed(name, protected.touched(parent, sha))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ReleaseBackstopError('Cannot prove parent inputs unchanged') from exc
    if affected or not passed(_named_jobs(api, parent).get(name, {})):
        raise ReleaseBackstopError('Parent job must pass and candidate changes must not reach its inputs')


def prove_rc_pull_request(api: GitHubApi, *, tag: str, candidate: str | None = None, waiver: str = '') -> PullRequestProof:
    """Use the same proof before tag creation and at both publication stages."""
    source_sha = _sha(candidate, context='RC candidate') if candidate else resolve_tag_sha(api, tag)
    _require_on_master(api, source_sha)
    pull_number, head_sha, merged_at = _merged_pull(api, source_sha)
    _require_green_checks(api, pull_number=pull_number, head_sha=head_sha, merged_at=merged_at)
    _verify_protected(api, tag, source_sha, waiver)
    try:
        full_backstop, backstop, run_id = _backstop(api, source_sha)
    except ReleaseBackstopError:
        full_backstop, backstop, run_id = None, "unknown", None
    return PullRequestProof(
        source_sha=source_sha,
        pull_number=pull_number,
        head_sha=head_sha,
        backstop=backstop,
        backstop_run_id=run_id,
        full_backstop=full_backstop,
    )


def prove_release(
    api: GitHubApi, *, tag: str, rc_pull_request_gate: bool = False, waiver: str = ''
) -> BackstopProof | PullRequestProof:
    if rc_pull_request_gate and RC_TAG_RE.fullmatch(tag):
        return prove_rc_pull_request(api, tag=tag, waiver=waiver)
    if waiver:
        raise ReleaseBackstopError('Runner exception is only valid for a GitHub-only RC')
    return prove_backstop(api, tag=tag)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--tag", help="Resolve an existing release tag before checking its source")
    source.add_argument("--source-sha", help="Check the source commit before creating a tag")
    parser.add_argument('--candidate-rc-tag', help='Before pushing a canonical RC tag, verify its --source-sha')
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
        api = GitHubApi(args.repository, args.token)
        waiver = os.environ.get('RC_RUNNER_EXCEPTION', '')
        if args.candidate_rc_tag:
            if not args.source_sha or not RC_TAG_RE.fullmatch(args.candidate_rc_tag):
                raise ReleaseBackstopError('Pre-tag RC proof requires --source-sha and a canonical candidate RC tag')
            proof = prove_rc_pull_request(api, tag=args.candidate_rc_tag, candidate=args.source_sha, waiver=waiver)
        elif args.source_sha is not None:
            if waiver:
                raise ReleaseBackstopError('No exception for a full backstop')
            proof = prove_backstop_sha(api, source_sha=args.source_sha)
        else:
            proof = prove_release(api, tag=args.tag, rc_pull_request_gate=args.rc_pull_request_gate, waiver=waiver)
    except ReleaseBackstopError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    outputs = [f"source_sha={proof.source_sha}"]
    if isinstance(proof, BackstopProof):
        print(f"Exact full master backstop verified for {proof.source_sha} (run {proof.run_id}, event {proof.event}).")
    else:
        print(
            f"RC pull-request gate verified for {proof.source_sha}: it is on {MASTER_BRANCH} and pull request "
            f"#{proof.pull_number} passed every required check on its head {proof.head_sha}."
        )
        run = f" (run {proof.backstop_run_id})" if proof.backstop_run_id else ""
        print(f"Protected RC evidence verified. Overall master backstop at {proof.source_sha}: {proof.backstop}{run}.")
        if proof.backstop == "failed":
            print(
                f"::warning::The full master backstop failed at RC source {proof.source_sha}{run}. "
                "Required protected evidence has been verified; report the remaining failure as documented."
            )
    # The RC path already inspected the backstop for reporting. Reuse only that
    # existing successful proof; pending/missing/red RCs incur no new discovery
    # requests and never wait for the backstop just to build their UI.
    full_backstop = proof if isinstance(proof, BackstopProof) else proof.full_backstop
    if full_backstop is not None:
        outputs.extend([f"run_id={full_backstop.run_id}", f"run_attempt={full_backstop.run_attempt}"])
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as output:
            output.write("".join(f"{line}\n" for line in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
