"""The scenario evidence gate: a covered row's evidence ran in CI, or a release runs it (scripts/ci_scenario_evidence.py)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from scripts import pytest_collection
from scripts.ci_docs_only import full
from scripts.ci_scope import make_plan
from scripts.ci_scenario_evidence import (
    LINT_WORKFLOW,
    RELEASE_SELECTIONS,
    RELEASE_SHARD_VERSIONS,
    RELEASE_WORKFLOWS,
    UNIT_SHARD_ARGS,
    UNIT_SHARD_JOB,
    WORKFLOWS,
    Evidence,
    ReleaseEvidenceError,
    Reports,
    check_release_evidence,
    check_release_join,
    covered_evidence,
    covered_test_evidence,
    judge,
    pytest_commands,
    read_reports,
    release_pytest_commands,
    release_shards,
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


def _release_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "checkout"
    test = root / "tests" / "test_x.py"
    test.parent.mkdir(parents=True)
    test.write_text("def test_case():\n    pass\n", encoding="utf-8")
    scenarios = root / "tests" / "scenarios" / "capability"
    scenarios.mkdir(parents=True)
    scenarios.joinpath("catalog.yaml").write_text(
        "scenarios:\n  - id: ROW-1\n    status: covered\n    test: tests/test_x.py::test_case\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "workflow.yml"
    workflow.write_text(
        "jobs:\n  release:\n    steps:\n      - run: python -m pytest tests/test_x.py --junitxml=report.xml\n",
        encoding="utf-8",
    )
    junit = tmp_path / "junit"
    junit.mkdir()
    return root, scenarios.parent, workflow, junit


def _write_release_report(junit: Path, body: str) -> None:
    junit.joinpath("release.xml").write_text(
        f"<testsuites><testsuite>{body}</testsuite></testsuites>", encoding="utf-8"
    )


@pytest.mark.parametrize(
    "result",
    [
        "",
        '<skipped message="conditional skip"/>',
        '<failure message="assertion failed"/>',
        '<error message="fixture error"/>',
        '<skipped type="pytest.xfail" message="known gap"/>',
    ],
    ids=["pass", "skip", "failure", "error", "xfail"],
)
def test_release_evidence_requires_a_passing_junit_case(tmp_path: Path, result: str) -> None:
    root, scenarios, workflow, junit = _release_fixture(tmp_path)
    _write_release_report(junit, f'<testcase classname="tests.test_x" name="test_case">{result}</testcase>')

    if result:
        with pytest.raises(ReleaseEvidenceError, match="did not pass in the release run"):
            check_release_evidence(junit, workflow, root, scenario_root=scenarios)
    else:
        assert check_release_evidence(junit, workflow, root, scenario_root=scenarios) == {
            CASE: [CASE]
        }


@pytest.mark.parametrize("report_state", ["missing", "empty", "malformed"])
def test_release_evidence_fails_closed_for_bad_reports(tmp_path: Path, report_state: str) -> None:
    root, scenarios, workflow, junit = _release_fixture(tmp_path)
    if report_state == "missing":
        junit = tmp_path / "missing-junit"
    elif report_state == "empty":
        junit.joinpath("release.xml").write_text("<testsuites/>", encoding="utf-8")
    elif report_state == "malformed":
        junit.joinpath("release.xml").write_text("<testsuites>", encoding="utf-8")

    with pytest.raises(ReleaseEvidenceError):
        check_release_evidence(junit, workflow, root, scenario_root=scenarios)


A_REQUIRED, A_PLAIN = "tests/test_a.py::test_required", "tests/test_a.py::test_plain"
B_REQUIRED, B_OPTIONAL = "tests/test_b.py::test_required", "tests/test_b.py::test_optional"
SKIPPED = '<skipped message="not on this runner"/>'
XFAILED = '<skipped type="pytest.xfail" message="known gap"/>'
FAILED = '<failure message="assertion failed"/>'


def _junit(report: dict[str, str], again: str = "") -> str:
    """A JUnit report with one record for each case, and a second for ``again``."""

    cases = "".join(
        f'<testcase classname="{nodeid.partition("::")[0][:-3].replace("/", ".")}" '
        f'name="{nodeid.partition("::")[2]}">{result}</testcase>'
        for nodeid, result in [*report.items(), *((again, report[again]) for _ in [again] if again)]
    )
    return f"<testsuites><testsuite>{cases}</testsuite></testsuites>"


@dataclass
class _ShardedRun:
    """A release run shared out over two shard jobs: everything its join reads, before it is written.

    The checkout holds two required (catalog-covered) cases, one per shard, and
    two others. As written here the run is green: each shard ran exactly its own
    cases at the join's commit, run and attempt, against the same versions, and
    the one skip is a case no covered row cites.
    """

    root: Path
    source: dict[str, str] = field(
        default_factory=lambda: {
            "tests/test_a.py": "def test_required():\n    pass\n\n\ndef test_plain():\n    pass\n",
            "tests/test_b.py": "def test_required():\n    pass\n\n\ndef test_optional():\n    pass\n",
        }
    )
    covered: list[str] = field(default_factory=lambda: [A_REQUIRED, B_REQUIRED])
    selection: list[str] = field(default_factory=lambda: ["tests"])
    commands: dict[str, str] = field(
        default_factory=lambda: {"shard-a": "tests/test_a.py", "shard-b": "tests --ignore=tests/test_a.py"}
    )
    needs: dict[str, dict] = field(
        default_factory=lambda: {"shard-a": {"result": "success"}, "shard-b": {"result": "success"}}
    )
    reports: dict[str, dict[str, str] | None] = field(
        default_factory=lambda: {
            "shard-a": {A_REQUIRED: "", A_PLAIN: ""},
            "shard-b": {B_REQUIRED: "", B_OPTIONAL: SKIPPED},
        }
    )
    provenance: dict[str, dict | str | None] = field(
        default_factory=lambda: {
            job: {
                "sha": "a" * 40,
                "run_id": "77",
                "run_attempt": "2",
                "versions": {"codex": "codex-cli 1.2.3", "opencode": "4.5.6", "engine": "v1 sha256:abc"},
            }
            for job in ("shard-a", "shard-b")
        }
    )
    extra_evidence: dict[str, str] = field(default_factory=dict)

    def join(self) -> dict[str, list[str]]:
        for name, body in self.source.items():
            self.root.joinpath("checkout", name).parent.mkdir(parents=True, exist_ok=True)
            self.root.joinpath("checkout", name).write_text(body, encoding="utf-8")
        scenarios = self.root / "checkout" / "tests" / "scenarios"
        scenarios.joinpath("capability").mkdir(parents=True)
        scenarios.joinpath("capability", "catalog.yaml").write_text(
            yaml.safe_dump(
                {"scenarios": [{"id": f"ROW-{index}", "status": "covered", "test": case} for index, case in enumerate(self.covered)]}
            ),
            encoding="utf-8",
        )
        workflow = self.root / "workflow.yml"
        workflow.write_text(
            yaml.safe_dump(
                {
                    "jobs": {
                        **{
                            job: {"steps": [{"run": f"python -m pytest {command} --junitxml=junit.xml"}]}
                            for job, command in self.commands.items()
                        },
                        "join": {"steps": [{"run": "python scripts/ci_scenario_evidence.py --release-join evidence"}]},
                    }
                }
            ),
            encoding="utf-8",
        )
        evidence = self.root / "evidence"
        evidence.mkdir()
        for job, report in self.reports.items():
            if report is not None:
                self.extra_evidence.setdefault(f"{job}/junit.xml", _junit(report))
        for job, record in self.provenance.items():
            if record is not None:
                self.extra_evidence[f"{job}/provenance.json"] = record if isinstance(record, str) else json.dumps(record)
        for name, body in self.extra_evidence.items():
            evidence.joinpath(name).parent.mkdir(parents=True, exist_ok=True)
            evidence.joinpath(name).write_text(body, encoding="utf-8")
        return check_release_join(
            evidence,
            workflow,
            self.root / "checkout",
            self.selection,
            needs=self.needs,
            source_sha="a" * 40,
            run_id="77",
            run_attempt="2",
            version_names=("codex", "opencode", "engine"),
            expected_versions={"engine": "v1 sha256:abc"},
            scenario_root=scenarios,
        )


def _no_evidence(run: _ShardedRun, job: str) -> None:
    run.reports[job] = run.provenance[job] = None


def _swapped_reports(run: _ShardedRun) -> None:
    run.reports["shard-a"], run.reports["shard-b"] = run.reports["shard-b"], run.reports["shard-a"]


def _only_the_engine_named(run: _ShardedRun) -> None:
    for record in run.provenance.values():
        record["versions"] = {"engine": "v1 sha256:abc"}


def _same_wrong_engine(run: _ShardedRun) -> None:
    for record in run.provenance.values():
        record["versions"]["engine"] = "v0 sha256:old"


def _new_file_in_no_shard(run: _ShardedRun) -> None:
    run.commands["shard-b"] = "tests/test_b.py"
    run.source["tests/test_c.py"] = "def test_new():\n    pass\n"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # The shard jobs themselves.
        pytest.param(lambda run: run.needs["shard-b"].update(result="failure"), "shard-b concluded failure", id="shard-failed"),
        pytest.param(lambda run: run.needs["shard-b"].update(result="skipped"), "shard-b concluded skipped", id="shard-skipped"),
        pytest.param(lambda run: run.needs["shard-b"].update(result="cancelled"), "shard-b concluded cancelled", id="shard-cancelled"),
        pytest.param(lambda run: run.needs.pop("shard-b"), "must wait for exactly the shard jobs", id="join-does-not-wait-for-a-shard"),
        # What each shard left behind.
        pytest.param(lambda run: _no_evidence(run, "shard-b"), "shard-b: release JUnit directory does not exist", id="shard-left-nothing"),
        pytest.param(lambda run: run.reports.update({"shard-b": None}), "shard-b: release JUnit directory is empty", id="shard-left-no-report"),
        pytest.param(
            lambda run: run.extra_evidence.update({"shard-b/again.xml": "<testsuites/>"}),
            "shard-b: release JUnit directory must contain exactly one report",
            id="shard-left-two-reports",
        ),
        pytest.param(
            lambda run: run.extra_evidence.update({"shard-c/junit.xml": "<testsuites/>"}),
            "unknown release evidence producers: shard-c",
            id="evidence-from-an-unknown-job",
        ),
        pytest.param(lambda run: run.provenance.update({"shard-b": None}), "shard-b: could not read release shard provenance", id="no-provenance"),
        pytest.param(lambda run: run.provenance.update({"shard-b": "{"}), "shard-b: could not read release shard provenance", id="malformed-provenance"),
        pytest.param(lambda run: run.provenance["shard-b"].pop("versions"), "shard-b: release shard provenance is incomplete", id="no-versions"),
        pytest.param(
            lambda run: run.provenance["shard-b"]["versions"].update(codex=""),
            "shard-b: release shard provenance names no version of codex",
            id="blank-version",
        ),
        pytest.param(
            _only_the_engine_named,
            "shard-a: release shard provenance must name a version of exactly codex, engine, opencode, not engine",
            id="both-shards-name-no-cli",
        ),
        pytest.param(
            lambda run: run.provenance["shard-b"]["versions"].pop("opencode"),
            "shard-b: release shard provenance must name a version of exactly codex, engine, opencode, not codex, engine",
            id="version-recorded-by-one-shard-only",
        ),
        # Which run each shard belongs to, and what it ran against.
        pytest.param(lambda run: run.provenance["shard-b"].update(sha="b" * 40), "shard-b recorded sha bbbb", id="another-commit"),
        pytest.param(lambda run: run.provenance["shard-b"].update(run_id="76"), "shard-b recorded run_id 76, but this join is for 77", id="another-run"),
        pytest.param(
            lambda run: run.provenance["shard-a"].update(run_attempt="1"),
            "shard-a recorded run_attempt 1, but this join is for 2",
            id="shard-kept-from-an-earlier-attempt",
        ),
        pytest.param(
            lambda run: run.provenance["shard-b"]["versions"].update(codex="codex-cli 1.2.4"),
            "shard-b and shard-a ran against different versions: codex 'codex-cli 1.2.4' != 'codex-cli 1.2.3'",
            id="cli-version-drifted-between-shards",
        ),
        pytest.param(_same_wrong_engine, "shard-a ran against engine 'v0 sha256:old', but this checkout pins 'v1 sha256:abc'", id="both-shards-off-the-pinned-engine"),
        # How the shards share the selection out.
        pytest.param(_new_file_in_no_shard, "1 case(s) of the release selection are selected by no shard: tests/test_c.py::test_new", id="selection-omitted"),
        pytest.param(
            lambda run: run.commands.update({"shard-b": "tests"}),
            f"2 case(s) are selected by more than one shard: {A_PLAIN}, {A_REQUIRED}",
            id="selection-duplicated",
        ),
        pytest.param(
            lambda run: run.selection.append(f"--deselect={B_OPTIONAL}"),
            f"1 case(s) are selected by a shard but not by the release selection: {B_OPTIONAL}",
            id="shard-runs-outside-the-selection",
        ),
        pytest.param(lambda run: run.commands.update({"shard-b": "tests/test_b.py -k no_such_case"}), "could not collect release pytest command", id="shard-selects-nothing"),
        # What each shard ran.
        pytest.param(lambda run: run.reports["shard-b"].pop(B_REQUIRED), f"shard-b did not run 1 case(s) it selects: {B_REQUIRED}", id="required-case-not-run"),
        pytest.param(lambda run: run.reports["shard-b"].pop(B_OPTIONAL), f"shard-b did not run 1 case(s) it selects: {B_OPTIONAL}", id="other-case-not-run"),
        pytest.param(_swapped_reports, "shard-a reported 2 case(s) it does not select", id="reports-swapped"),
        pytest.param(
            lambda run: run.extra_evidence.update({"shard-b/junit.xml": _junit(run.reports["shard-b"], again=B_OPTIONAL)}),
            f"shard-b: release JUnit report records {B_OPTIONAL} more than once",
            id="case-reported-twice",
        ),
        pytest.param(lambda run: run.reports["shard-b"].update({B_REQUIRED: SKIPPED}), f"{B_REQUIRED} did not pass in the release run", id="required-case-skipped"),
        pytest.param(lambda run: run.reports["shard-a"].update({A_REQUIRED: XFAILED}), f"{A_REQUIRED} did not pass in the release run", id="required-case-xfailed"),
        pytest.param(lambda run: run.reports["shard-a"].update({A_REQUIRED: FAILED}), f"{A_REQUIRED} did not pass in the release run", id="required-case-failed"),
        pytest.param(lambda run: run.reports["shard-b"].update({B_OPTIONAL: FAILED}), f"shard-b recorded 1 failed case(s): {B_OPTIONAL}", id="other-case-failed"),
        pytest.param(lambda run: run.covered.clear(), "selects no covered scenario test evidence", id="no-required-case-in-the-selection"),
    ],
)
def test_release_join_fails_unless_the_shards_ran_the_whole_selection_once(tmp_path: Path, change, problem: str) -> None:
    """One departure from a green two-shard run at a time, each of which the join must name.

    The join is the release verdict once the selection is shared out: a shard
    that was skipped, left no report, belongs to another commit or attempt, ran
    another CLI or engine, or left a case out, would otherwise still read green.
    """

    run = _ShardedRun(tmp_path)
    change(run)

    with pytest.raises(ReleaseEvidenceError) as raised:
        run.join()

    assert problem in str(raised.value)


def test_release_join_passes_a_complete_run_and_lets_an_uncited_case_skip(tmp_path: Path) -> None:
    """Only covered cases must pass: another case may skip or xfail, as it could in the one-job run."""

    run = _ShardedRun(tmp_path)
    run.reports["shard-a"][A_PLAIN] = XFAILED

    assert run.join() == {"shard-a": [A_REQUIRED, A_PLAIN], "shard-b": [B_REQUIRED, B_OPTIONAL]}
    # The same reports with a required case skipped are refused, so the pass above is not vacuous.
    again = _ShardedRun(tmp_path / "again", reports=copy.deepcopy(run.reports))
    again.reports["shard-a"][A_REQUIRED] = SKIPPED
    with pytest.raises(ReleaseEvidenceError, match="did not pass in the release run"):
        again.join()


@pytest.mark.parametrize(
    ("reports", "selected", "verdict", "windows_route"),
    [
        pytest.param(
            _reports(unit_test_shards={f"{CASE}[linux]": ("skipped", "not on Linux")}, windows_control_ipc={f"{CASE}[win]": ("passed", "")}),
            {},
            None,
            "run",
            id="passed-on-one-platform",
        ),
        pytest.param(
            _reports(unit_test_shards={CASE: ("skipped", "not on Linux")}),
            {WINDOWS: [CASE]},
            "out of scope",
            "skip",
            id="its-other-job-was-path-skipped",
        ),
        pytest.param(
            _reports(unit_test_shards={CASE: ("skipped", "needs a Codex binary")}, windows_control_ipc={CASE: ("skipped", "needs a Codex binary")}),
            {WINDOWS: [CASE]},
            "skipped everywhere it ran (needs a Codex binary)",
            "run",
            id="skipped-everywhere-it-ran",
        ),
        pytest.param(_reports(), {RELEASE: [CASE]}, "release-gated", "skip", id="a-release-runs-it"),
        pytest.param(_reports(), {}, "collected by no CI job that ran", "skip", id="no-job-runs-it"),
        pytest.param(
            Reports(outcomes={UNIT_SHARD_JOB: {}}, artifacts={UNIT_SHARD_JOB: 7}),
            {},
            f"{UNIT_SHARD_JOB} left 7 of 8 junit reports",
            "skip",
            id="a-unit-shard-left-no-report",
        ),
    ],
)
def test_only_evidence_skipped_or_absent_everywhere_it_could_have_run_fails(reports, selected, verdict, windows_route) -> None:
    """A job that left no report, such as one its path filter skipped, says nothing about its cases.

    The required gate must not turn red because a job did not run on this
    change: such a job's cases are "out of scope", and a case fails only when it
    was skipped, or absent, wherever CI did run and no release runs it.
    """

    lint_commands = {UNIT_SHARD_JOB: [("tests",)], "windows-control-ipc": [WINDOWS]}
    lint_runs = {UNIT_SHARD_JOB: 8, "windows-control-ipc": 1}
    if verdict == "out of scope":
        reports.artifacts.pop("windows-control-ipc", None)
    plan = make_plan("PR", full("fixture", True))
    plan["jobs"]["windows-control-ipc"] = windows_route
    problems, accounted = judge(
        {CASE: Evidence(CASE, ["ROW-1"])},
        reports,
        lint_commands,
        lint_runs,
        [RELEASE],
        lambda args: selected.get(args, []),
        plan=plan,
    )

    if verdict is None:
        assert (problems, accounted) == ([], {"release-gated": [], "out of scope": []})
    elif verdict in ("out of scope", "release-gated"):
        assert problems == [] and accounted[verdict] == [CASE]
    else:
        (problem,) = problems
        assert verdict in problem


def test_evidence_scope_never_confuses_an_omitted_report_with_a_routed_job():
    """A missing Windows report used to be accepted as if its path filter skipped it."""
    for mode in ("PR", "MASTER_FULL"):
        plan = make_plan(mode, full("fixture", True))
        problems, accounted = judge(
            {CASE: Evidence(CASE, ["ROW-1"])}, _reports(),
            {UNIT_SHARD_JOB: [("tests",)], "windows-control-ipc": [WINDOWS]},
            {UNIT_SHARD_JOB: 8, "windows-control-ipc": 1}, [], lambda _args: [CASE], plan=plan,
        )
        assert problems == ["windows-control-ipc left 0 of 1 junit reports"]
        assert accounted == {}


def test_thin_queue_does_not_claim_any_scenario_ran():
    plan = make_plan("MG_THIN")
    problems, accounted = judge(
        {CASE: Evidence(CASE, ["ROW-1"])}, Reports(), {}, {}, [], lambda _args: [], plan=plan,
    )
    assert problems == []
    assert accounted == {"out of scope (MG_THIN)": [CASE]}
    problems, _ = judge(
        {CASE: Evidence(CASE, ["ROW-1"])}, _reports(unit_test_shards={CASE: ("passed", "")}),
        {}, {}, [], lambda _args: [], plan=plan,
    )
    assert problems == ["MG_THIN cannot claim scenario execution evidence"]


@pytest.mark.parametrize("count", [0, 1, 7, 9])
def test_every_full_mode_requires_exact_unit_shard_artifact_count(count):
    for mode in ("PR", "MASTER_FULL"):
        plan = make_plan(mode, full("fixture", True))
        problems, _ = judge(
            {CASE: Evidence(CASE, ["ROW-1"])},
            Reports(outcomes={UNIT_SHARD_JOB: {CASE: ("passed", "")}}, artifacts={UNIT_SHARD_JOB: count}),
            {UNIT_SHARD_JOB: [("tests",)]}, {UNIT_SHARD_JOB: 8}, [], lambda _args: [CASE], plan=plan,
        )
        assert problems == [f"unit-test-shards left {count} of 8 junit reports"]


def test_a_routed_producer_cannot_supply_pass_evidence():
    plan = make_plan("PR", full("fixture", True))
    plan["jobs"]["windows-control-ipc"] = "skip"
    problems, _ = judge(
        {CASE: Evidence(CASE, ["ROW-1"])}, _reports(windows_control_ipc={CASE: ("passed", "")}),
        {UNIT_SHARD_JOB: [("tests",)], "windows-control-ipc": [WINDOWS]},
        {UNIT_SHARD_JOB: 8, "windows-control-ipc": 1}, [], lambda _args: [CASE], plan=plan,
    )
    assert problems == ["windows-control-ipc left evidence despite being out of scope"]


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


def test_release_evidence_from_a_nested_automation_checkout(tmp_path: Path) -> None:
    """Explicit root keeps node IDs and JUnit names relative to nested release automation.

    Pytest otherwise inherits the outer checkout's configuration and prefixes node IDs
    with the nested directory. Keep this generic evidence regression even though the
    retired Memory updater no longer needs a nested publish.yml gate.
    """

    command = ["tests/test_x.py", "--rootdir=."]
    outer = tmp_path / "release"
    outer.mkdir()
    outer.joinpath("pyproject.toml").write_text("[tool.pytest.ini_options]\nmarkers = []\n", encoding="utf-8")
    root = outer / "release-automation"
    test = root / "tests" / "test_x.py"
    test.parent.mkdir(parents=True)
    test.write_text("def test_case():\n    pass\n", encoding="utf-8")
    scenarios = root / "tests" / "scenarios" / "capability"
    scenarios.mkdir(parents=True)
    scenarios.joinpath("catalog.yaml").write_text(
        "scenarios:\n  - id: ROW-1\n    status: covered\n    test: tests/test_x.py::test_case\n",
        encoding="utf-8",
    )
    workflow = tmp_path / "publish.yml"
    workflow.write_text(
        "jobs:\n  build:\n    steps:\n      - run: (cd release-automation && python -m pytest "
        + " ".join(command) + " -v --junitxml=report.xml)\n",
        encoding="utf-8",
    )
    junit = tmp_path / "junit"
    junit.mkdir()
    run = subprocess.run(
        [sys.executable, "-m", "pytest", *command, "-q", "-p", "no:cacheprovider", f"--junitxml={junit / 'release.xml'}"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr

    assert check_release_evidence(junit, workflow, root, scenario_root=scenarios.parent) == {CASE: [CASE]}


def test_release_evidence_uses_the_explicit_release_checkout_root(tmp_path: Path) -> None:
    root, scenarios, workflow, junit = _release_fixture(tmp_path)
    wrong_root = tmp_path / "primary-checkout"
    wrong_test = wrong_root / "tests" / "test_x.py"
    wrong_test.parent.mkdir(parents=True)
    wrong_test.write_text("def test_other():\n    pass\n", encoding="utf-8")
    _write_release_report(junit, '<testcase classname="tests.test_x" name="test_case"/>')

    assert check_release_evidence(junit, workflow, root, scenario_root=scenarios) == {CASE: [CASE]}
    with pytest.raises(ReleaseEvidenceError, match="selects no covered"):
        check_release_evidence(junit, workflow, wrong_root, scenario_root=scenarios)


def test_release_workflows_select_only_current_contracts() -> None:
    wire, mock = "tests/e2e/test_model_hub_engine_wire.py", "tests/e2e/test_model_hub_mock_upstream.py"
    # Each shard's literal command, and the index its own evidence gate reads it by.
    assert pytest_commands(WORKFLOWS / "model-hub-e2e.yml") == {
        "model-hub-e2e-wire-mock": [("-m", "e2e_model_hub", wire, mock, "-p", "no:cacheprovider")],
        "model-hub-e2e-remaining": [
            ("-m", "e2e_model_hub", "tests/e2e", f"--ignore={wire}", f"--ignore={mock}", "-p", "no:cacheprovider")
        ],
    }
    jobs = yaml.safe_load((WORKFLOWS / "model-hub-e2e.yml").read_text(encoding="utf-8"))["jobs"]
    for index, job in enumerate(("model-hub-e2e-wire-mock", "model-hub-e2e-remaining")):
        (gate,) = [step["run"] for step in jobs[job]["steps"] if "--release" in step.get("run", "")]
        assert "--workflow .github/workflows/model-hub-e2e.yml" in gate and f"--pytest-command-index {index} " in gate
        assert '--junitxml="$RUNNER_TEMP/model-hub-e2e-evidence/junit.xml"' in gate
    # What the shards are compared with is fixed in the checker, as the one-job command was written.
    assert RELEASE_SELECTIONS == {"model-hub-e2e.yml": ("-m", "e2e_model_hub", "tests/e2e")}
    # So is what each shard must name: both agent CLIs, the SDK that carries Claude Code, and the engine.
    assert RELEASE_SHARD_VERSIONS == {"model-hub-e2e.yml": ("codex", "opencode", "claude-agent-sdk", "model-hub-engine")}
    # The join is the last word: it waits for both shards and runs whatever they concluded.
    join = jobs["model-hub-e2e"]
    assert sorted(join["needs"]) == ["model-hub-e2e-remaining", "model-hub-e2e-wire-mock"] and join["if"] == "always()"
    assert release_pytest_commands(WORKFLOWS / "publish.yml") == [
        ("tests/test_upgrade_flow.py", "tests/test_install_script.py", "tests/e2e/test_install_command.py")
    ]
    assert all("MUC-004" not in evidence.rows for evidence in covered_test_evidence().values())


def test_the_lint_unit_shards_count_as_running_the_whole_unit_suite() -> None:
    """Whichever launcher the shards use, the gate knows they select every unit file.

    Missing this, every covered case that only the unit shards run would read
    as never run, and the evidence gate would fail every pull request.
    """

    assert pytest_commands(WORKFLOWS / LINT_WORKFLOW)[UNIT_SHARD_JOB] == [UNIT_SHARD_ARGS]


def test_release_gating_is_read_from_the_release_workflows() -> None:
    """The live Model Hub shards run the whole E2E selection once; with release automation they gate the expected rows.

    The whole selection is written out here and fixed in the checker, apart from
    the workflow: a shard edit that drops or doubles a file fails this pull
    request, not the next release, and cannot shrink what it is compared with.
    """

    shards = release_shards(WORKFLOWS / "model-hub-e2e.yml", WORKFLOWS.parents[1], ["-m", "e2e_model_hub", "tests/e2e"])

    assert sorted(shards) == ["model-hub-e2e-remaining", "model-hub-e2e-wire-mock"]
    assert {nodeid.partition("::")[0] for nodeid in shards["model-hub-e2e-wire-mock"]} == {
        "tests/e2e/test_model_hub_engine_wire.py", "tests/e2e/test_model_hub_mock_upstream.py"
    }
    assert RELEASE_WORKFLOWS == ("model-hub-e2e.yml", "publish.yml")
    publish = [args for job in pytest_commands(WORKFLOWS / "publish.yml").values() for args in job]
    evidence = covered_evidence()
    gated = {
        citation
        for nodeid in (
            *(nodeid for nodeids in shards.values() for nodeid in nodeids),
            *(entry["nodeid"] for args in publish for entry in pytest_collection.collect(list(args))),
        )
        for citation in evidence
        if nodeid.partition("[")[0] in (citation, citation.partition("[")[0])
    }

    assert evidence["tests/e2e/test_model_hub_mock_upstream.py::test_mh_routing_001_mock_model_errors_are_request_scoped_and_inventory_independent"].rows == ["MH-ROUTING-001"]
    assert "tests/e2e/test_model_hub_mock_upstream.py::test_mh_routing_001_mock_model_errors_are_request_scoped_and_inventory_independent" in gated


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
