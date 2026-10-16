"""The scenario evidence gate: a covered row's evidence ran in CI, or a release runs it (scripts/ci_scenario_evidence.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts import pytest_collection
from scripts.ci_scenario_evidence import (
    RELEASE_WORKFLOWS,
    UNIT_SHARD_JOB,
    WORKFLOWS,
    Evidence,
    Reports,
    covered_evidence,
    judge,
    pytest_commands,
    read_reports,
)

CASE = "tests/test_x.py::test_case"
WINDOWS = ("tests/test_x.py", "-k", "windows")
RELEASE = ("-m", "release", "tests/e2e")


def _reports(**jobs: dict[str, tuple[str, str]]) -> Reports:
    reports = Reports(outcomes={UNIT_SHARD_JOB: {}}, artifacts={UNIT_SHARD_JOB: 8})
    for job, outcomes in jobs.items():
        job = job.replace("_", "-")
        reports.outcomes[job] = outcomes
        reports.artifacts[job] = reports.artifacts.get(job, 0) or 1
    return reports


@pytest.mark.parametrize(
    ("reports", "selected", "verdict"),
    [
        pytest.param(
            _reports(unit_test_shards={f"{CASE}[linux]": ("skipped", "not on Linux")}, windows_control_ipc={f"{CASE}[win]": ("passed", "")}),
            {},
            None,
            id="passed-on-one-platform",
        ),
        pytest.param(
            _reports(unit_test_shards={CASE: ("skipped", "not on Linux")}),
            {WINDOWS: [CASE]},
            "not run",
            id="its-other-job-was-path-skipped",
        ),
        pytest.param(
            _reports(unit_test_shards={CASE: ("skipped", "needs a Codex binary")}, windows_control_ipc={CASE: ("skipped", "needs a Codex binary")}),
            {WINDOWS: [CASE]},
            "skipped everywhere it ran (needs a Codex binary)",
            id="skipped-everywhere-it-ran",
        ),
        pytest.param(_reports(), {RELEASE: [CASE]}, "release-gated", id="a-release-runs-it"),
        pytest.param(_reports(), {}, "collected by no CI job that ran", id="no-job-runs-it"),
        pytest.param(
            Reports(outcomes={UNIT_SHARD_JOB: {}}, artifacts={UNIT_SHARD_JOB: 7}),
            {},
            f"{UNIT_SHARD_JOB} left 7 of 8 junit reports",
            id="a-unit-shard-left-no-report",
        ),
    ],
)
def test_only_evidence_skipped_or_absent_everywhere_it_could_have_run_fails(reports, selected, verdict) -> None:
    """A job that left no report, such as one its path filter skipped, says nothing about its cases.

    The required gate must not turn red because a job did not run on this
    change: such a job's cases are "not run", and a case fails only when it
    was skipped, or absent, wherever CI did run and no release runs it.
    """

    lint_commands = {UNIT_SHARD_JOB: [("tests",)], "windows-control-ipc": [WINDOWS]}
    lint_runs = {UNIT_SHARD_JOB: 8, "windows-control-ipc": 1}
    if verdict == "not run":
        reports.artifacts.pop("windows-control-ipc", None)
    problems, accounted = judge(
        {CASE: Evidence(CASE, ["ROW-1"])},
        reports,
        lint_commands,
        lint_runs,
        [RELEASE],
        lambda args: selected.get(args, []),
    )

    if verdict is None:
        assert (problems, accounted) == ([], {"release-gated": [], "not run": []})
    elif verdict in ("not run", "release-gated"):
        assert problems == [] and accounted[verdict] == [CASE]
    else:
        (problem,) = problems
        assert verdict in problem


def test_a_report_names_each_case_by_its_node_id(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    for artifact, body in (
        ("junit-unit-test-shards-0", '<testcase classname="tests.test_x.TestA" name="test_m[a b]"><skipped message="off"/></testcase>'),
        ("junit-unit-test-shards-1", '<testcase classname="tests.test_x.TestA" name="test_m[a b]"/>'),
        ("junit-windows-agent-0", '<testcase classname="test_inner" name="test_elsewhere"/>'),
    ):
        (tmp_path / "junit" / artifact).mkdir(parents=True)
        (tmp_path / "junit" / artifact / "1.xml").write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8")

    reports = read_reports(tmp_path / "junit", tmp_path)

    assert reports.artifacts == {"unit-test-shards": 2, "windows-agent": 1}
    assert reports.outcomes["unit-test-shards"] == {"tests/test_x.py::TestA::test_m[a b]": ("passed", "")}
    # A case outside the repository, such as a test's own inner pytest run, is no evidence.
    assert reports.outcomes["windows-agent"] == {}


def test_release_gating_is_read_from_the_release_workflows() -> None:
    """The Model Hub E2E and the release automation are the only CI that runs some covered rows' evidence."""

    commands = [args for name in RELEASE_WORKFLOWS for job in pytest_commands(WORKFLOWS / name).values() for args in job]
    evidence = covered_evidence()
    gated = {
        citation
        for args in commands
        for nodeid in (entry["nodeid"] for entry in pytest_collection.collect(list(args)))
        for citation in evidence
        if nodeid.partition("[")[0] in (citation, citation.partition("[")[0])
    }

    assert evidence["tests/e2e/test_model_hub_mock_upstream.py::test_mh_routing_001_mock_model_errors_are_request_scoped_and_inventory_independent"].rows == ["MH-ROUTING-001"]
    assert "tests/e2e/test_model_hub_mock_upstream.py::test_mh_routing_001_mock_model_errors_are_request_scoped_and_inventory_independent" in gated
    assert "tests/e2e/test_retired_updater_bridge.py::test_released_updater_replaces_companion_without_touching_user_data" in gated


def test_a_covered_rows_partial_evidence_case_is_evidence_too(tmp_path: Path) -> None:
    """#2775: the case ``partial_evidence.test`` names must run as its row's ``test`` cases must."""

    (tmp_path / "capability").mkdir()
    (tmp_path / "capability" / "catalog.yaml").write_text(
        yaml.safe_dump(
            {
                "scenarios": [
                    {
                        "id": "ROW-1",
                        "status": "covered",
                        "test": "tests/test_x.py::test_main",
                        "partial_evidence": {"test": "tests/test_x.py::test_more", "covers": "the second half"},
                    },
                    {"id": "ROW-2", "status": "partial", "test": "tests/test_x.py::test_not_covered"},
                ]
            }
        ),
        encoding="utf-8",
    )

    evidence = covered_evidence(tmp_path)

    assert sorted(evidence) == ["tests/test_x.py::test_main", "tests/test_x.py::test_more"]
    assert evidence["tests/test_x.py::test_more"].rows == ["ROW-1"]
