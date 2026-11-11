from __future__ import annotations

from typing import Any

import pytest

from scripts import ci_release_backstop as gate


TAG = "v4.2.0"
SOURCE_SHA = "a" * 40


class FakeApi:
    def __init__(self, *, runs: list[dict[str, Any]], jobs: dict[int, list[dict[str, Any]]], pages: dict[int, list[dict[str, Any]]] | None = None):
        self.runs = runs
        self.jobs = jobs
        self.pages = pages or {}
        self.calls: list[tuple[str, dict[str, str | int] | None]] = []

    def get(self, path: str, *, query: dict[str, str | int] | None = None) -> Any:
        self.calls.append((path, query))
        if path == "git/ref/tags/v4.2.0":
            return {"object": {"type": "tag", "sha": "b" * 40}}
        if path == f"git/tags/{'b' * 40}":
            return {"object": {"type": "commit", "sha": SOURCE_SHA}}
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
) -> dict[str, Any]:
    return {
        "id": run_id,
        "event": event,
        "path": ".github/workflows/lint.yml",
        "head_branch": branch,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
    }


def _job(*, conclusion: str | None = "success", status: str = "completed", name: str = gate.TERMINAL_JOB) -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion}


def test_prove_backstop_resolves_annotated_tag_and_exact_successful_run() -> None:
    api = FakeApi(runs=[_run()], jobs={1: [_job()]})

    proof = gate.prove_backstop(api, tag=TAG)

    assert proof == gate.BackstopProof(run_id=1, source_sha=SOURCE_SHA, event="push")
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
def test_source_sha_cli_requires_exact_full_proof_without_a_tag(monkeypatch, runs, jobs, expected) -> None:
    # The old tag-only CLI cannot certify a source before publication creates a
    # tag; this boundary must enforce the same full proof without a ref lookup.
    api = FakeApi(runs=runs, jobs=jobs)
    monkeypatch.setattr(gate, "GitHubApi", lambda *_args: api)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    assert gate.main(["--repository", "avibe-bot/avibe", "--source-sha", SOURCE_SHA]) == expected
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
