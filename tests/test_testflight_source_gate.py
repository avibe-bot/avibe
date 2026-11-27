from __future__ import annotations

from typing import Any

import pytest

from scripts import ci_release_backstop as backstop
from scripts import testflight_source_gate as gate

SHA = "c" * 40
RUN = 45807662322


def _run(run_id: int = RUN, *, status: str = "completed", conclusion: str | None = "success",
         event: str = "push", attempt: int = 1) -> dict[str, Any]:
    return {
        "id": run_id, "status": status, "conclusion": conclusion, "event": event,
        "head_branch": "master", "head_sha": SHA, "path": ".github/workflows/lint.yml",
        "run_attempt": attempt,
    }


def _job(name: str, conclusion: str | None = "success", status: str = "completed", job_id: int = 7) -> dict[str, Any]:
    return {"id": job_id, "name": name, "status": status, "conclusion": conclusion}


GREEN_JOBS = [_job("master-full-backstop", job_id=1), _job("ios-shell / simulator", job_id=2)]


class FakeApi:
    def __init__(self, *, compare: Any = None, runs: dict[str, list[dict[str, Any]]] | None = None,
                 jobs: dict[int, list[dict[str, Any]]] | None = None) -> None:
        self.compare = {"status": "ahead", "merge_base_commit": {"sha": SHA}} if compare is None else compare
        self.runs = {"push": [_run()], "workflow_dispatch": []} if runs is None else runs
        self.jobs = {RUN: GREEN_JOBS} if jobs is None else jobs
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, path: str, *, query: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, query))
        if path == f"compare/{SHA}...master":
            if isinstance(self.compare, Exception):
                raise self.compare
            return self.compare
        if path == "actions/workflows/lint.yml/runs":
            assert query is not None and query["head_sha"] == SHA and query["branch"] == "master"
            return {"workflow_runs": self.runs.get(query["event"], []) if query["page"] == 1 else []}
        if path.startswith("actions/runs/") and path.endswith("/jobs"):
            assert query is not None
            return {"jobs": self.jobs.get(int(path.split("/")[2]), []) if query["page"] == 1 else []}
        raise AssertionError(f"unexpected GitHub API path: {path}")


def test_a_green_master_commit_is_proven_with_the_run_and_attempt_it_trusted():
    api = FakeApi()
    proof = gate.prove(api, SHA.upper())
    assert proof == gate.SourceProof(source_sha=SHA, run_id=RUN, run_attempt=1, event="push", ios_job_id=2)
    assert f"run {RUN} attempt 1 (event push)" in proof.line()
    assert "ios-shell / simulator job 2 succeeded" in proof.line()
    # The iOS job is read as last run.
    assert (f"actions/runs/{RUN}/jobs", {"filter": "latest", "page": 1, "per_page": 100}) in api.calls


@pytest.mark.parametrize("value", ["", "c" * 39, "c" * 41, "c" * 7, "g" * 40, f" {'c' * 39} ", "HEAD", "master"])
def test_anything_but_a_full_sha_fails_before_github_is_asked(value):
    api = FakeApi()
    with pytest.raises(gate.TestFlightSourceError, match="full 40-character commit SHA"):
        gate.prove(api, value)
    assert api.calls == []


@pytest.mark.parametrize("compare", [
    {"status": "diverged", "merge_base_commit": {"sha": "d" * 40}},
    {"status": "behind", "merge_base_commit": {"sha": SHA}},
    {"status": "ahead", "merge_base_commit": {"sha": "d" * 40}},
    backstop.ReleaseBackstopError("GitHub API request failed (HTTP 404)"),
], ids=["diverged", "behind", "other-merge-base", "unknown-commit"])
def test_a_commit_off_master_fails_before_any_run_is_read(compare):
    api = FakeApi(compare=compare)
    with pytest.raises(gate.TestFlightSourceError, match="is not on master"):
        gate.prove(api, SHA)
    assert [path for path, _ in api.calls] == [f"compare/{SHA}...master"]


@pytest.mark.parametrize("runs", [
    {"push": [], "workflow_dispatch": []},
    {"push": [_run(conclusion="cancelled")], "workflow_dispatch": []},
    {"push": [_run(status="in_progress", conclusion=None)], "workflow_dispatch": []},
    {"push": [_run(conclusion="failure")], "workflow_dispatch": []},
], ids=["no-run", "cancelled", "in-progress", "failed"])
def test_a_commit_without_a_green_full_run_fails(runs):
    with pytest.raises(gate.TestFlightSourceError, match="No successful full master backstop"):
        gate.prove(FakeApi(runs=runs), SHA)


@pytest.mark.parametrize("ios", [
    [],
    [_job("ios-shell / simulator", conclusion="cancelled")],
    [_job("ios-shell / simulator", conclusion="skipped")],
    [_job("ios-shell / simulator", status="in_progress", conclusion=None)],
    [_job("ios-shell / simulator"), _job("ios-shell / simulator", job_id=8)],
], ids=["missing", "cancelled", "skipped", "running", "twice"])
def test_a_green_run_without_exactly_one_green_ios_simulator_job_fails(ios):
    api = FakeApi(jobs={RUN: [_job("master-full-backstop", job_id=1), *ios]})
    with pytest.raises(gate.TestFlightSourceError, match="ios-shell / simulator"):
        gate.prove(api, SHA)


def test_a_rerun_attempt_and_a_dispatched_full_run_count_as_the_backstop_reads_them():
    rerun = FakeApi(runs={"push": [_run(attempt=3)], "workflow_dispatch": []})
    assert gate.prove(rerun, SHA).run_attempt == 3
    dispatched = FakeApi(runs={"push": [_run(41, conclusion="cancelled")],
                               "workflow_dispatch": [_run(event="workflow_dispatch")]})
    proof = gate.prove(dispatched, SHA)
    assert (proof.run_id, proof.event) == (RUN, "workflow_dispatch")


def test_main_writes_the_proven_sha_for_the_build_job_and_fails_closed(tmp_path, monkeypatch, capsys):
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(gate.backstop, "GitHubApi", lambda repository, token: FakeApi())
    assert gate.main(["--repository", "o/r", "--token", "t", "--source-sha", SHA]) == 0
    assert output.read_text() == f"source_sha={SHA}\nrun_id={RUN}\nrun_attempt=1\n"
    assert "verified" in capsys.readouterr().out

    output.write_text("")
    assert gate.main(["--repository", "o/r", "--token", "t", "--source-sha", "c" * 7]) == 1
    assert output.read_text() == ""
    assert "::error::" in capsys.readouterr().err
