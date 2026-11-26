from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_release_backstop as gate


TAG = "v4.2.0"
RC_TAG = "gh-v4.2.0rc1"
SOURCE_SHA = "a" * 40
HEAD_SHA = "d" * 40
ACTIONS_APP = 15368
WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
MERGED_AT = "2026-11-11T02:42:32Z"
BEFORE_MERGE = "2026-11-11T02:40:00Z"
AFTER_MERGE = "2026-11-11T02:55:00Z"


class FakeApi:
    """GitHub REST responses for one tag; the pull-request fields default to a green RC."""

    def __init__(
        self,
        *,
        runs: list[dict[str, Any]],
        jobs: dict[int, list[dict[str, Any]]],
        pages: dict[int, list[dict[str, Any]]] | None = None,
        compare: dict[str, Any] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        rules: list[dict[str, Any]] | None = None,
        check_runs: list[dict[str, Any]] | None = None,
        runs_unreadable: bool = False,
    ):
        self.runs = runs
        self.jobs = jobs
        self.pages = pages or {}
        self.compare = {"status": "ahead", "merge_base_commit": {"sha": SOURCE_SHA}} if compare is None else compare
        self.pulls = [_pull()] if pulls is None else pulls
        self.rules = _rules("ruff", "unit-tests") if rules is None else rules
        self.check_runs = [_check("ruff", 1), _check("unit-tests", 2)] if check_runs is None else check_runs
        self.runs_unreadable = runs_unreadable
        self.calls: list[tuple[str, dict[str, str | int] | None]] = []

    def get(self, path: str, *, query: dict[str, str | int] | None = None) -> Any:
        self.calls.append((path, query))
        if path.startswith("git/ref/tags/"):
            return {"object": {"type": "tag", "sha": "b" * 40}}
        if path == f"git/tags/{'b' * 40}":
            return {"object": {"type": "commit", "sha": SOURCE_SHA}}
        if path == f"compare/{SOURCE_SHA}...master":
            return self.compare
        if path == f"commits/{SOURCE_SHA}/pulls":
            return self.pulls
        if path == "rules/branches/master":
            return self.rules
        if path == f"commits/{HEAD_SHA}/check-runs":
            return {"total_count": len(self.check_runs), "check_runs": self.check_runs}
        if path.endswith("/runs") and self.runs_unreadable:
            raise gate.ReleaseBackstopError("GitHub API request failed (HTTP 502)")
        if path.endswith("/runs"):
            assert query is not None
            if query["page"] == 1:
                return {"workflow_runs": self.runs}
            return {"workflow_runs": self.pages.get(int(query["page"]), [])}
        if path.startswith("actions/runs/") and path.endswith("/jobs"):
            run_id = int(path.split("/")[2])
            assert query is not None
            if query["page"] == 1:
                return {"jobs": self.jobs.get(run_id, [])}
            return {"jobs": []}
        raise AssertionError(f"unexpected GitHub API path: {path}")


def _run(
    run_id: int = 1,
    *,
    event: str = "push",
    branch: str = "master",
    sha: str = SOURCE_SHA,
    conclusion: str | None = "success",
    status: str = "completed",
    run_attempt: int | None = 2,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "run_attempt": run_attempt,
        "event": event,
        "path": ".github/workflows/lint.yml",
        "head_branch": branch,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
    }


def _job(*, conclusion: str | None = "success", status: str = "completed", name: str = gate.TERMINAL_JOB) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def _pull(**overrides: Any) -> dict[str, Any]:
    return {
        "number": 7,
        "merged_at": MERGED_AT,
        "merge_commit_sha": SOURCE_SHA,
        "base": {"ref": "master"},
        "head": {"sha": HEAD_SHA},
        **overrides,
    }


def _rules(*contexts: str) -> list[dict[str, Any]]:
    checks = [{"context": context, "integration_id": ACTIONS_APP} for context in contexts]
    return [
        {"type": "required_linear_history"},
        {"type": "required_status_checks", "parameters": {"required_status_checks": checks}},
    ]


def _check(
    name: str,
    check_id: int,
    *,
    conclusion: str | None = "success",
    status: str = "completed",
    app: int = ACTIONS_APP,
    completed_at: str | None = BEFORE_MERGE,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"id": app},
        "completed_at": completed_at if status == "completed" else None,
    }


def _rc_api(**overrides: Any) -> FakeApi:
    """A green RC whose SHA has no full master backstop run at all."""

    return FakeApi(**{"runs": [], "jobs": {}, **overrides})


def test_prove_backstop_resolves_annotated_tag_and_exact_successful_run() -> None:
    api = FakeApi(runs=[_run()], jobs={1: [_job()]})

    proof = gate.prove_backstop(api, tag=TAG)

    assert proof == gate.BackstopProof(run_id=1, source_sha=SOURCE_SHA, event="push", run_attempt=2)
    assert [path for path, _query in api.calls[:2]] == ["git/ref/tags/v4.2.0", f"git/tags/{'b' * 40}"]
    run_queries = [query for path, query in api.calls if path.endswith("/runs")]
    assert {query["event"] for query in run_queries if query is not None} == {"push", "workflow_dispatch"}
    assert all(query is not None and query["head_sha"] == SOURCE_SHA for query in run_queries)


@pytest.mark.parametrize(
    "run_kwargs",
    [
        {"conclusion": "failure"},
        {"status": "cancelled", "conclusion": "cancelled"},
        {"event": "pull_request"},
        {"branch": "release"},
        {"sha": "c" * 40},
    ],
    ids=["failed-run", "cancelled-run", "wrong-event", "wrong-ref", "wrong-sha"],
)
def test_prove_backstop_rejects_non_exact_or_unsuccessful_runs(run_kwargs: dict[str, Any]) -> None:
    api = FakeApi(runs=[_run(**run_kwargs)], jobs={1: [_job()]})

    with pytest.raises(gate.ReleaseBackstopError, match="wait for or re-run the full backstop"):
        gate.prove_backstop(api, tag=TAG)


def test_prove_backstop_rejects_wrong_workflow_path() -> None:
    run = _run()
    run["path"] = ".github/workflows/other.yml"
    api = FakeApi(runs=[run], jobs={1: [_job()]})

    with pytest.raises(gate.ReleaseBackstopError, match="exact source SHA"):
        gate.prove_backstop(api, tag=TAG)


def test_prove_backstop_rejects_empty_jobs_page_with_actionable_error() -> None:
    api = FakeApi(runs=[_run()], jobs={})

    with pytest.raises(gate.ReleaseBackstopError, match="wait for or re-run the full backstop"):
        gate.prove_backstop(api, tag=TAG)


def test_prove_backstop_rejects_missing_terminal_job() -> None:
    api = FakeApi(runs=[_run()], jobs={1: [{"name": "unit-tests", "status": "completed", "conclusion": "success"}]})

    with pytest.raises(gate.ReleaseBackstopError, match="coalesced-away SHA"):
        gate.prove_backstop(api, tag=TAG)


def test_prove_backstop_rejects_cancelled_terminal_job() -> None:
    api = FakeApi(runs=[_run()], jobs={1: [_job(status="completed", conclusion="cancelled")]})

    with pytest.raises(gate.ReleaseBackstopError, match="exact source SHA"):
        gate.prove_backstop(api, tag=TAG)


def test_prove_backstop_accepts_explicit_full_manual_master_run() -> None:
    api = FakeApi(runs=[_run(event="workflow_dispatch")], jobs={1: [_job()]})

    proof = gate.prove_backstop(api, tag=TAG)

    assert proof.event == "workflow_dispatch"


@pytest.mark.parametrize(
    "runs,jobs,expected",
    [
        ([_run()], {1: [_job()]}, 0),
        ([_run(event="workflow_dispatch")], {1: [_job()]}, 0),
        ([], {}, 1),
        ([_run(status="in_progress", conclusion=None)], {1: [_job()]}, 1),
        ([_run(conclusion="failure")], {1: [_job()]}, 1),
        ([_run(conclusion="cancelled")], {1: [_job()]}, 1),
        ([_run(conclusion="skipped")], {1: [_job()]}, 1),
        ([_run(sha="c" * 40)], {1: [_job()]}, 1),
        ([_run(branch="topic")], {1: [_job()]}, 1),
        ([_run(event="pull_request")], {1: [_job()]}, 1),
        ([{**_run(), "path": ".github/workflows/other.yml"}], {1: [_job()]}, 1),
        ([_run()], {}, 1),
        ([_run()], {1: [_job(name="unit-tests")]}, 1),
        ([_run()], {1: [_job(conclusion="skipped")]}, 1),
        ([_run()], {1: [_job(status="in_progress", conclusion=None)]}, 1),
        ([_run()], {1: [_job(conclusion="failure")]}, 1),
    ],
    ids=["push", "dispatch", "missing", "running", "failed", "cancelled", "skipped",
         "wrong-sha", "wrong-branch", "wrong-event", "wrong-workflow", "empty-jobs",
         "missing-terminal", "coalesced", "terminal-running", "terminal-failed"],
)
@pytest.mark.parametrize("rc_flag", [[], ["--rc-pull-request-gate"]], ids=["default", "rc-flag-cannot-bypass-sha-proof"])
def test_source_sha_cli_requires_exact_full_proof_without_a_tag(monkeypatch, runs, jobs, expected, rc_flag) -> None:
    # The old tag-only CLI cannot certify a source before publication creates a
    # tag; this boundary must enforce the same full proof without a ref lookup.
    api = FakeApi(runs=runs, jobs=jobs)
    monkeypatch.setattr(gate, "GitHubApi", lambda *_args: api)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert gate.main(["--repository", "avibe-bot/avibe", "--source-sha", SOURCE_SHA, *rc_flag]) == expected
    assert api.calls
    assert all(not path.startswith("git/") for path, _query in api.calls)
    assert all(query["head_sha"] == SOURCE_SHA for path, query in api.calls if path.endswith("/runs"))


@pytest.mark.parametrize("sha", ["", "a" * 39, "g" * 40, "master"])
def test_source_sha_rejects_invalid_commit_before_api_access(sha) -> None:
    api = FakeApi(runs=[_run()], jobs={1: [_job()]})
    with pytest.raises(gate.ReleaseBackstopError, match="invalid source SHA"):
        gate.prove_backstop_sha(api, source_sha=sha)
    assert not api.calls


def test_source_sha_cli_api_errors_fail_closed(monkeypatch) -> None:
    api = FakeApi(runs=[], jobs={})

    def unavailable(*_args, **_kwargs):
        raise gate.ReleaseBackstopError("GitHub API request failed (HTTP 403)")

    monkeypatch.setattr(api, "get", unavailable)
    monkeypatch.setattr(gate, "GitHubApi", lambda *_args: api)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert gate.main(["--repository", "avibe-bot/avibe", "--source-sha", SOURCE_SHA]) == 1


@pytest.mark.parametrize("args", [[], ["--tag", TAG, "--source-sha", SOURCE_SHA]])
def test_backstop_cli_requires_exactly_one_source(args) -> None:
    with pytest.raises(SystemExit) as error:
        gate._parser().parse_args(args)
    assert error.value.code == 2


def test_workflow_run_pagination_is_followed() -> None:
    api = FakeApi(
        runs=[_run(run_id=index, conclusion="failure") for index in range(1, 101)],
        pages={2: [_run(run_id=101)]},
        jobs={101: [_job()]},
    )

    proof = gate.prove_backstop(api, tag=TAG)

    assert proof.run_id == 101
    run_pages = [query["page"] for path, query in api.calls if path.endswith("/runs") and query is not None]
    assert run_pages == [1, 2, 1, 2]


@pytest.mark.parametrize(
    ("overrides", "backstop"),
    [
        ({}, "missing"),
        (
            # GitHub lists a head's superseded runs beside its newest; skipped satisfies a required check.
            {
                "check_runs": [
                    _check("unit-tests", 1, conclusion="failure"),
                    _check("ruff", 2, conclusion="cancelled"),
                    _check("ruff", 3),
                    _check("unit-tests", 4, conclusion="skipped"),
                ]
            },
            "missing",
        ),
        ({"runs": [_run(status="in_progress", conclusion=None)]}, "pending"),
        ({"runs": [_run(conclusion="failure")]}, "failed"),
        ({"runs": [_run(conclusion="cancelled")]}, "missing"),
        ({"runs_unreadable": True}, "unknown"),
    ],
    ids=[
        "no-backstop",
        "newest-check-run-counts",
        "backstop-pending",
        "backstop-failed",
        "backstop-coalesced-away",
        "backstop-unreadable",
    ],
)
def test_rc_releases_on_its_green_pull_request_whatever_the_backstop_state(
    overrides: dict[str, Any], backstop: str
) -> None:
    proof = gate.prove_release(_rc_api(**overrides), tag=RC_TAG, rc_pull_request_gate=True)

    assert isinstance(proof, gate.PullRequestProof)
    assert (proof.source_sha, proof.pull_number, proof.head_sha) == (SOURCE_SHA, 7, HEAD_SHA)
    assert proof.backstop == backstop


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"check_runs": [_check("ruff", 1), _check("unit-tests", 2, conclusion="failure")]}, r"unit-tests \(failure\)"),
        (
            {"check_runs": [_check("ruff", 1), _check("unit-tests", 2), _check("unit-tests", 3, conclusion="failure")]},
            r"unit-tests \(failure\)",
        ),
        (
            {"check_runs": [_check("ruff", 1), _check("unit-tests", 2, status="in_progress", conclusion=None)]},
            r"unit-tests \(no run completed before the merge; newest is in_progress\)",
        ),
        ({"check_runs": [_check("ruff", 1)]}, r"unit-tests \(missing\)"),
        ({"check_runs": [_check("ruff", 1), _check("unit-tests", 2, app=99)]}, r"unit-tests \(missing\)"),
        ({"pulls": []}, "Could not resolve RC source SHA"),
        ({"pulls": [_pull(merged_at=None)]}, "Could not resolve RC source SHA"),
        ({"pulls": [_pull(merge_commit_sha="e" * 40)]}, "Could not resolve RC source SHA"),
        ({"pulls": [_pull(base={"ref": "release"})]}, "Could not resolve RC source SHA"),
        ({"compare": {"status": "diverged", "merge_base_commit": {"sha": "e" * 40}}}, "is not on master"),
        ({"rules": [{"type": "required_linear_history"}]}, "no required status checks"),
    ],
    ids=[
        "failed-required-check",
        "newer-failure-over-older-success",
        "required-check-still-running",
        "required-check-absent",
        "required-check-from-another-app",
        "no-pull-request",
        "unmerged-pull-request",
        "not-the-merge-commit",
        "merged-into-another-branch",
        "not-on-master",
        "no-required-checks-to-prove",
    ],
)
def test_rc_is_refused_without_a_green_merged_pull_request(overrides: dict[str, Any], message: str) -> None:
    # A green full backstop does not stand in for the pull-request proof.
    api = _rc_api(runs=[_run()], jobs={1: [_job()]}, **overrides)

    with pytest.raises(gate.ReleaseBackstopError, match=message):
        gate.prove_release(api, tag=RC_TAG, rc_pull_request_gate=True)


# Only the state the ruleset enforced when the pull request merged counts:
# a run started after the merge (a label event, a manual rerun) neither blocks
# nor vouches for the RC.
def test_rc_gate_ignores_a_run_still_pending_after_a_green_merge() -> None:
    checks = [_check("ruff", 1), _check("unit-tests", 2), _check("unit-tests", 3, status="in_progress", conclusion=None)]

    proof = gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)

    assert isinstance(proof, gate.PullRequestProof)


def test_rc_gate_ignores_a_failure_completed_after_a_green_merge() -> None:
    checks = [_check("ruff", 1), _check("unit-tests", 2), _check("unit-tests", 3, conclusion="failure", completed_at=AFTER_MERGE)]

    proof = gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)

    assert isinstance(proof, gate.PullRequestProof)


def test_rc_gate_refuses_a_failure_at_merge_even_if_a_later_run_is_green() -> None:
    checks = [_check("ruff", 1), _check("unit-tests", 2, conclusion="failure"), _check("unit-tests", 3, completed_at=AFTER_MERGE)]

    with pytest.raises(gate.ReleaseBackstopError, match=r"unit-tests \(failure\)"):
        gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)


def test_rc_gate_treats_a_context_with_only_post_merge_runs_as_missing() -> None:
    checks = [_check("ruff", 1), _check("unit-tests", 3, completed_at=AFTER_MERGE)]

    with pytest.raises(gate.ReleaseBackstopError, match=r"unit-tests \(no run completed before the merge; newest is success\)"):
        gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)


def test_rc_gate_judges_by_the_newest_run_not_the_last_to_finish_before_the_merge() -> None:
    # The superseded run is cancelled after its replacement already passed; both settle before the merge.
    checks = [
        _check("ruff", 1),
        _check("unit-tests", 2, conclusion="cancelled", completed_at="2026-11-11T02:41:00Z"),
        _check("unit-tests", 3, completed_at="2026-11-11T02:40:00Z"),
    ]

    proof = gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)

    assert isinstance(proof, gate.PullRequestProof)


def test_rc_gate_accepts_a_context_skipped_before_the_merge_as_the_ruleset_does() -> None:
    checks = [_check("ruff", 1), _check("unit-tests", 2, conclusion="skipped")]

    proof = gate.prove_release(_rc_api(check_runs=checks), tag=RC_TAG, rc_pull_request_gate=True)

    assert isinstance(proof, gate.PullRequestProof)


@pytest.mark.parametrize(
    ("tag", "rc_pull_request_gate"),
    [(TAG, True), (RC_TAG, False), ("v4.2.0rc1", True), ("gh-v4.2.0", True)],
    ids=["stable", "rc-without-opt-in", "pypi-prerelease", "gh-tag-that-is-not-an-rc"],
)
def test_only_an_opted_in_rc_releases_without_the_full_backstop(tag: str, rc_pull_request_gate: bool) -> None:
    with pytest.raises(gate.ReleaseBackstopError, match="No successful full master backstop"):
        gate.prove_release(_rc_api(), tag=tag, rc_pull_request_gate=rc_pull_request_gate)

    proof = gate.prove_release(
        _rc_api(runs=[_run()], jobs={1: [_job()]}), tag=tag, rc_pull_request_gate=rc_pull_request_gate
    )

    assert proof == gate.BackstopProof(run_id=1, source_sha=SOURCE_SHA, event="push", run_attempt=2)


@pytest.mark.parametrize(
    ("tag", "api", "outputs", "warned"),
    [
        (RC_TAG, _rc_api(runs=[_run(conclusion="failure")]), f"source_sha={SOURCE_SHA}\n", True),
        (RC_TAG, _rc_api(), f"source_sha={SOURCE_SHA}\n", False),
        (RC_TAG, _rc_api(runs=[_run(status="in_progress", conclusion=None)]), f"source_sha={SOURCE_SHA}\n", False),
        (RC_TAG, _rc_api(runs=[_run()], jobs={1: [_job()]}), f"source_sha={SOURCE_SHA}\nrun_id=1\nrun_attempt=2\n", False),
        (TAG, _rc_api(runs=[_run()], jobs={1: [_job()]}), f"source_sha={SOURCE_SHA}\nrun_id=1\nrun_attempt=2\n", False),
    ],
    ids=["rc-with-red-backstop", "rc-without-backstop", "rc-pending-backstop", "rc-with-green-backstop", "stable"],
)
def test_gate_hands_the_exact_source_to_the_release_and_reports_a_red_rc_backstop(
    tag: str,
    api: FakeApi,
    outputs: str,
    warned: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every later job checks out `source_sha`; an RC must still provide it, and
    # its red backstop is a warning on the release run, never a refusal.
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: api)

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", tag, "--rc-pull-request-gate"]) == 0

    assert github_output.read_text(encoding="utf-8") == outputs
    assert not any("artifacts" in path for path, _ in api.calls)
    if tag == RC_TAG and "run_id=" not in outputs:
        assert not any(path.endswith("/jobs") for path, _ in api.calls)
    assert ("::warning::The full master backstop failed at RC source" in capsys.readouterr().out) is warned


@pytest.mark.parametrize("attempt", [None, 0, -1, True, "2"])
def test_missing_attempt_disables_reuse_without_changing_gate_acceptance(attempt):
    api = _rc_api(runs=[_run(run_attempt=attempt)], jobs={1: [_job()]})
    proof = gate.prove_release(api, tag=RC_TAG, rc_pull_request_gate=True)
    assert proof.backstop == "success"
    assert proof.full_backstop is not None
    assert proof.full_backstop.run_attempt == 0


@pytest.mark.parametrize("workflow", ["publish.yml", "release_ai.yml", "desktop-release.yml"])
def test_no_release_stage_opts_in_to_the_rc_gate(workflow: str) -> None:
    # Every release stage, RC or stable, requires the full master backstop at
    # the tag's exact SHA: ordinary PRs no longer run the native and browser
    # legs, so a green pull request is not full proof. The RC gate stays in the
    # script for reading state by hand only.
    job = yaml.safe_load((WORKFLOWS / workflow).read_text(encoding="utf-8"))["jobs"]["release-backstop"]
    (step,) = [step for step in job["steps"] if "ci_release_backstop.py" in step.get("run", "")]

    assert "--rc-pull-request-gate" not in step["run"].split()
    assert "--tag" in step["run"].split()


# The plain override: an operator-attested accepted risk on RC tags only, not machine proof.
UNVERIFIED = gate.UnverifiedJob(job="web-fetch-macos / web-fetch-runtime (macos-15, 3.12)", reason="no macOS runner [see](x)",
                                go="**Product**: ok to ship without it")
LINE = (
    "not verified in this RC: `web-fetch-macos / web-fetch-runtime (macos-15, 3.12)` \u2014 "
    "`no macOS runner [see](x)` (go: `**Product**: ok to ship without it`)"
)


class NotesApi(FakeApi):
    def __init__(self, *, body: str, **kwargs: Any):
        super().__init__(**kwargs)
        self.body = body

    def get(self, path: str, *, query: dict[str, str | int] | None = None) -> Any:
        if path == f"releases/tags/{RC_TAG}":
            self.calls.append((path, query))
            return {"body": self.body}
        return super().get(path, query=query)


def _red() -> dict[str, Any]:
    return {"runs": [_run(conclusion="failure")], "jobs": {1: [_job(conclusion="failure")]}}


def test_rc_override_replaces_a_red_verdict_and_reports_it() -> None:
    proof = gate.prove_release(FakeApi(**_red()), tag=RC_TAG, unverified=UNVERIFIED)

    assert proof == gate.UnverifiedProof(source_sha=SOURCE_SHA, unverified=UNVERIFIED, backstop="failed", backstop_run_id=1)


def test_rc_override_keeps_its_record_when_the_backstop_is_green() -> None:
    proof = gate.prove_release(FakeApi(runs=[_run()], jobs={1: [_job()]}), tag=RC_TAG, unverified=UNVERIFIED)

    assert proof == gate.UnverifiedProof(source_sha=SOURCE_SHA, unverified=UNVERIFIED, backstop="success", backstop_run_id=1)


def test_override_is_refused_for_a_stable_tag() -> None:
    with pytest.raises(gate.ReleaseBackstopError, match=re.escape("only to gh-vX.Y.ZrcN tags")):
        gate.prove_release(FakeApi(**_red()), tag=TAG, unverified=UNVERIFIED)


@pytest.mark.parametrize(
    ("job", "reason", "go"),
    [
        ("ruff", "", "go"), ("ruff", "why", ""), ("", "why", "go"), ("ruff", "why\nnext", "go"), ("ruff", "why", "a `b` c"),
        ("ruff", "<!-- avibe:update-notification=none -->", "go"),
        ("ruff", "why\u2028next", "go"), ("ruff", "why", "a\u0085b"),
    ],
    ids=["no-reason", "no-go", "no-job", "two-lines", "backtick", "release-control-comment", "line-separator", "next-line"],
)
def test_override_needs_all_three_plain_fields(job: str, reason: str, go: str) -> None:
    with pytest.raises(gate.ReleaseBackstopError):
        gate._unverified_from(job, reason, go)


def test_override_line_renders_every_field_literally() -> None:
    assert UNVERIFIED.line() == LINE
    assert gate.NOTES_LINE_RE.findall(LINE) == [(UNVERIFIED.job, UNVERIFIED.reason, UNVERIFIED.go)]


def test_rc_override_prints_the_line_for_the_summary_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    github_output, summary = tmp_path / "github-output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("RELEASE_UNVERIFIED_JOB", UNVERIFIED.job)
    monkeypatch.setenv("RELEASE_UNVERIFIED_REASON", UNVERIFIED.reason)
    monkeypatch.setenv("RELEASE_UNVERIFIED_GO", UNVERIFIED.go)
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: FakeApi(**_red()))

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", RC_TAG]) == 0

    assert github_output.read_text(encoding="utf-8") == f"source_sha={SOURCE_SHA}\nunverified_line={LINE}\n"
    text = summary.read_text(encoding="utf-8")
    assert "Full master backstop at" in text and "failed (run 1)" in text and text.endswith(f"{LINE}\n")
    assert f"::warning::{LINE}" in capsys.readouterr().out


def _marker(line: str = LINE, source: str = SOURCE_SHA) -> str:
    return f"<!-- avibe:rc-unverified source={source} sha256={hashlib.sha256(line.encode()).hexdigest()} -->"


@pytest.mark.parametrize(
    ("body", "accepted"),
    [
        (f"# Notes\n\n{LINE}\n{_marker()}\n\n<!-- ready -->", True),
        ("# Notes without the line", False),
        (f"# Generated prose quoting\n{LINE}\n", False),
        (f"{LINE}\n{_marker(source='e' * 40)}", False),
        (f"{LINE.replace('runner', 'Runner')}\n{_marker()}", False),
        (f"{LINE}\n{_marker()}\n{LINE}\n{_marker()}", False),
    ],
    ids=["stage1-line", "no-line", "prose-without-marker", "other-source", "edited-line", "two-markers"],
)
def test_stage2_honours_only_the_stage1_notes_line(
    body: str, accepted: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: NotesApi(body=body, **_red()))

    code = gate.main(["--repository", "fixture/release", "--token", "t", "--tag", RC_TAG, "--unverified-from-notes"])

    assert code == (0 if accepted else 1)


def test_stage2_refuses_its_own_override_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELEASE_UNVERIFIED_JOB", UNVERIFIED.job)
    monkeypatch.setenv("RELEASE_UNVERIFIED_REASON", UNVERIFIED.reason)
    monkeypatch.setenv("RELEASE_UNVERIFIED_GO", UNVERIFIED.go)
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: NotesApi(body=f"{LINE}\n{_marker()}", **_red()))

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", RC_TAG, "--unverified-from-notes"]) == 1


def test_stages_wire_the_override_through_stage1_notes_only() -> None:
    stage1 = yaml.safe_load((WORKFLOWS / "release_ai.yml").read_text(encoding="utf-8"))
    stage2 = yaml.safe_load((WORKFLOWS / "desktop-release.yml").read_text(encoding="utf-8"))
    names = ("unverified_job", "unverified_reason", "unverified_go")
    assert set(names) <= set(stage1[True]["workflow_dispatch"]["inputs"])
    assert not set(names) & set(stage2[True]["workflow_dispatch"]["inputs"])
    (gate1,) = [s for s in stage1["jobs"]["release-backstop"]["steps"] if "ci_release_backstop.py" in s.get("run", "")]
    assert {f"RELEASE_{name.upper()}" for name in names} <= set(gate1["env"])
    (gate2,) = [s for s in stage2["jobs"]["release-backstop"]["steps"] if "ci_release_backstop.py" in s.get("run", "")]
    assert "--unverified-from-notes" in gate2["run"].split() and not any("UNVERIFIED" in k for k in gate2["env"])
    release_steps = stage1["jobs"]["release"]["steps"]
    (dispatch,) = [s for s in release_steps if s.get("name") == "Dispatch desktop Stage 2"]
    assert "unverified" not in dispatch["run"]
    assert any("UNVERIFIED_LINE" in s.get("env", {}) and "${UNVERIFIED_LINE:-}" in s.get("run", "") for s in release_steps)


@pytest.mark.parametrize("tag", [TAG, RC_TAG])
def test_stage2_never_reads_notes_when_the_backstop_is_green(tag: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A stable release can still be a draft here; the get-by-tag lookup would 404.
    api = NotesApi(body="unused", runs=[_run()], jobs={1: [_job()]})
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: api)

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", tag, "--unverified-from-notes"]) == 0
    assert not any(path.startswith("releases/") for path, _ in api.calls)


def test_stage2_never_reads_notes_for_a_red_stable_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    api = NotesApi(body=LINE, **_red())
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: api)

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", TAG, "--unverified-from-notes"]) == 1
    assert not any(path.startswith("releases/") for path, _ in api.calls)


def test_stage2_resolves_the_tag_once_for_marker_and_proof(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    api = NotesApi(body=f"{LINE}\n{_marker()}", **_red())
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setattr(gate, "GitHubApi", lambda repository, token: api)

    assert gate.main(["--repository", "fixture/release", "--token", "t", "--tag", RC_TAG, "--unverified-from-notes"]) == 0
    # One resolution for the plain proof attempt, one shared by the marker and the override proof.
    assert sum(path.startswith("git/ref/tags/") for path, _ in api.calls) == 2


def test_stage1_line_round_trips_through_stage2() -> None:
    unverified = gate._unverified_from("web-fetch-macos", "粘贴的 reason — ok", "她说：可以")
    line = unverified.line()
    api = NotesApi(body=f"# Notes\n{line}\n{_marker(line)}\n", **_red())

    assert gate.unverified_from_notes(api, RC_TAG, SOURCE_SHA) == unverified
