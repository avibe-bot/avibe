"""Gate covered scenario evidence in PR CI and in the release workflows.

The PR citation check (``tests/scenarios/test_catalog_citations.py``) proves
that each cited case is collected, while this gate reads the JUnit records from
pytest itself. A normal CI run may classify a case as ``release-gated`` because
its release workflow selects it; release mode is stricter and requires every
selected covered ``test`` case to have a passing JUnit record from that release
run.

PR usage: ``python scripts/ci_scenario_evidence.py <junit-dir>``, where each
subdirectory is one job's ``junit-<job>-<index>`` artifact.

Release usage: ``python scripts/ci_scenario_evidence.py --release <junit-dir>
--workflow <workflow> --pytest-root <root> --pytest-command-index <index>``.
The workflow command is collected from ``<root>`` and its one JUnit report is
required to contain a passing result for every covered case that command
selects. The explicit root is important for ``publish.yml``: its retired
updater test runs from the separate ``release-automation`` checkout.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPO_ROOT / "tests" / "scenarios"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
LINT_WORKFLOW = "lint.yml"
# The workflows a release needs green at its SHA (docs/release.md).
RELEASE_WORKFLOWS = ("model-hub-e2e.yml", "publish.yml")
# What scripts/ci_unit_tests.sh runs: every test file outside tests/e2e, without the integration marker.
UNIT_SHARD_ARGS = ("-m", "not integration", "--ignore=tests/e2e", "tests")
# The job this gate runs after on every event: its reports must all be there,
# or a broken report would read as every case "not run".
UNIT_SHARD_JOB = "unit-test-shards"
# Options that change what a pytest command selects, and those that take a value.
_SELECTING = {"-m", "-k", "-p", "-c", "-o", "--noconftest", "--ignore", "--deselect", "--rootdir"}
_VALUED = {"-m", "-k", "-p", "-c", "-o", "--ignore", "--deselect", "--rootdir"}
_SEPARATORS = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


class ReleaseEvidenceError(RuntimeError):
    """A release evidence contract is missing or records a non-passing case."""


@dataclass
class Evidence:
    """One case a ``covered`` row cites, the rows citing it, and what CI recorded for it."""

    citation: str
    rows: list[str]
    skips: list[str] = field(default_factory=list)


def covered_evidence(scenario_root: Path = SCENARIO_ROOT) -> dict[str, Evidence]:
    """Every pytest case a ``covered`` row cites as evidence, by citation.

    Its evidence keys are those the citation check reads as evidence: each case
    under ``test``, and the one ``partial_evidence.test`` names.
    """

    evidence: dict[str, Evidence] = {}
    for catalog in sorted(scenario_root.glob("*/catalog.yaml")):
        for row in yaml.safe_load(catalog.read_text(encoding="utf-8")).get("scenarios") or []:
            if row.get("status") != "covered":
                continue
            cited = row.get("test")
            cited = list(cited) if isinstance(cited, list) else [cited]
            partial = row.get("partial_evidence")
            if isinstance(partial, dict):
                cited.append(partial.get("test"))
            for citation in cited:
                if isinstance(citation, str) and citation.partition("::")[0].endswith(".py") and "::" in citation:
                    evidence.setdefault(citation, Evidence(citation, [])).rows.append(row["id"])
    return evidence


def covered_test_evidence(scenario_root: Path = SCENARIO_ROOT) -> dict[str, Evidence]:
    """The covered rows' primary ``test`` evidence, excluding #2775 partial evidence."""

    evidence: dict[str, Evidence] = {}
    for catalog in sorted(scenario_root.glob("*/catalog.yaml")):
        for row in yaml.safe_load(catalog.read_text(encoding="utf-8")).get("scenarios") or []:
            if row.get("status") != "covered":
                continue
            cited = row.get("test")
            cited = list(cited) if isinstance(cited, list) else [cited]
            for citation in cited:
                if isinstance(citation, str) and citation.partition("::")[0].endswith(".py") and "::" in citation:
                    evidence.setdefault(citation, Evidence(citation, [])).rows.append(row["id"])
    return evidence


def names_case(citation: str, nodeid: str) -> bool:
    """Whether ``nodeid`` is a run of the case ``citation`` names, by the citation check's rule."""

    file, _, name = citation.partition("::")
    node_file, _, rest = nodeid.partition("::")
    if node_file != file:
        return False
    base, bracket, param = name.partition("[")
    node_base, node_bracket, node_param = rest.partition("[")
    parts, node_parts = base.split("::"), node_base.split("::")
    named = node_parts == parts or (len(parts) == 1 and node_parts[-1] == parts[0])
    return named and (not bracket or node_bracket + node_param == bracket + param)


def _nodeid(classname: str, name: str, repo_root: Path) -> str | None:
    """The node id junit's ``classname`` and ``name`` came from; None for a file outside the repository."""

    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        path = "/".join(parts[:end]) + ".py"
        if (repo_root / path).is_file():
            return "::".join([path, *parts[end:], name])
    return None


@dataclass
class Reports:
    """What each job's junit artifacts recorded, and how many artifacts each left."""

    outcomes: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    artifacts: dict[str, int] = field(default_factory=dict)

    def by_file(self) -> dict[str, list[tuple[str, str, str]]]:
        indexed: dict[str, list[tuple[str, str, str]]] = {}
        for job in self.outcomes.values():
            for nodeid, (outcome, message) in job.items():
                indexed.setdefault(nodeid.partition("::")[0], []).append((nodeid, outcome, message))
        return indexed


def read_reports(junit_dir: Path, repo_root: Path = REPO_ROOT) -> Reports:
    """Each job's outcomes, ``{nodeid: (outcome, message)}``, from its ``junit-<job>-<index>`` artifacts."""

    reports = Reports()
    for artifact in sorted(path for path in junit_dir.iterdir() if path.is_dir() and path.name.startswith("junit-")):
        job = artifact.name.removeprefix("junit-").rsplit("-", 1)[0]
        reports.artifacts[job] = reports.artifacts.get(job, 0) + 1
        outcomes = reports.outcomes.setdefault(job, {})
        for report in sorted(artifact.rglob("*.xml")):
            for case in ElementTree.parse(report).getroot().iter("testcase"):
                nodeid = _nodeid(case.get("classname", ""), case.get("name", ""), repo_root)
                if nodeid is None:
                    continue
                skipped = case.find("skipped")
                if case.find("failure") is not None or case.find("error") is not None:
                    outcome = ("failed", "")
                elif skipped is not None:
                    outcome = ("skipped", skipped.get("message", ""))
                else:
                    outcome = ("passed", "")
                # One pass anywhere in the job is the job's answer.
                if outcomes.get(nodeid, ("",))[0] != "passed":
                    outcomes[nodeid] = outcome
    return reports


def read_release_report(junit_dir: Path, repo_root: Path = REPO_ROOT) -> dict[str, tuple[str, str]]:
    """Read exactly one non-empty release JUnit report, failing closed on bad input."""

    if not junit_dir.is_dir():
        raise ReleaseEvidenceError(f"release JUnit directory does not exist: {junit_dir}")
    reports = sorted(path for path in junit_dir.rglob("*.xml") if path.is_file())
    if not reports:
        raise ReleaseEvidenceError(f"release JUnit directory is empty: {junit_dir}")
    if len(reports) != 1:
        names = ", ".join(str(path.relative_to(junit_dir)) for path in reports)
        raise ReleaseEvidenceError(
            f"release JUnit directory must contain exactly one report, found {len(reports)}: {names}"
        )

    report = reports[0]
    try:
        root = ElementTree.parse(report).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise ReleaseEvidenceError(f"could not parse release JUnit report {report}: {exc}") from exc
    cases = list(root.iter("testcase"))
    if not cases:
        raise ReleaseEvidenceError(f"release JUnit report contains no testcases: {report}")

    outcomes: dict[str, tuple[str, str]] = {}
    for case in cases:
        nodeid = _nodeid(case.get("classname", ""), case.get("name", ""), repo_root)
        if nodeid is None:
            continue
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        if failure is not None or error is not None:
            outcome = ("failed", (failure if failure is not None else error).get("message", ""))
        elif skipped is not None:
            outcome = ("skipped", skipped.get("message", "") or skipped.get("type", ""))
        else:
            outcome = ("passed", "")
        if nodeid in outcomes and outcomes[nodeid] != outcome:
            raise ReleaseEvidenceError(f"release JUnit report records {nodeid} more than once")
        outcomes[nodeid] = outcome

    if not outcomes:
        raise ReleaseEvidenceError(
            f"release JUnit report contains no testcases from checkout {repo_root}: {report}"
        )
    return outcomes


def matrix_size(job: dict) -> int:
    """How many runs a job's matrix makes: the product of its value lists, or its ``include`` entries alone."""

    matrix = (job.get("strategy") or {}).get("matrix")
    if not isinstance(matrix, dict):
        return 1
    size = 1
    lists = [value for key, value in matrix.items() if key not in ("include", "exclude") and isinstance(value, list)]
    for value in lists:
        size *= len(value)
    if not lists and isinstance(matrix.get("include"), list):
        size = len(matrix["include"])
    return max(size, 1)


def workflow_jobs(workflow: Path) -> dict[str, dict]:
    return yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}


def pytest_commands(workflow: Path) -> dict[str, list[tuple[str, ...]]]:
    """Each job's pytest commands, as the arguments that decide what they select.

    A command whose arguments depend on the shell (``$``, a backtick) is left
    out: its selection cannot be known from the file.
    """

    jobs = workflow_jobs(workflow)
    commands: dict[str, list[tuple[str, ...]]] = {}
    for job_id, job in jobs.items():
        for step in job.get("steps") or []:
            script = re.sub(r"[\\`]\r?\n", " ", step.get("run") or "")
            if "scripts/ci_unit_tests.sh" in script:
                commands.setdefault(job_id, []).append(UNIT_SHARD_ARGS)
                continue
            for line in script.splitlines():
                for command in _SEPARATORS.split(line):
                    try:
                        tokens = [token.strip("()") for token in shlex.split(command)]
                    except ValueError:
                        continue
                    if "pytest" not in tokens:
                        continue
                    args = _selecting_args(tokens[tokens.index("pytest") + 1 :])
                    if args is not None:
                        commands.setdefault(job_id, []).append(args)
    return commands


def _selecting_args(tokens: list[str]) -> tuple[str, ...] | None:
    selecting: list[str] = []
    take_value = False
    for token in tokens:
        if take_value:
            selecting.append(token)
            take_value = False
        elif token.split("=", 1)[0] in _SELECTING:
            selecting.append(token)
            take_value = token in _VALUED
        elif token.startswith("tests"):
            selecting.append(token)
    if any("$" in token or "`" in token for token in selecting) or not any(token.startswith("tests") for token in selecting):
        return None
    return tuple(selecting)


def release_pytest_commands(workflow: Path) -> list[tuple[str, ...]]:
    """The parsed pytest commands in workflow order for a release contract."""

    return [args for commands in pytest_commands(workflow).values() for args in commands]


def check_release_evidence(
    junit_dir: Path,
    workflow: Path,
    pytest_root: Path,
    pytest_command_index: int = 0,
    scenario_root: Path = SCENARIO_ROOT,
) -> dict[str, list[str]]:
    """Require every covered case selected by one release command to pass in its JUnit report."""

    if not workflow.is_file():
        raise ReleaseEvidenceError(f"release workflow does not exist: {workflow}")
    if not pytest_root.is_dir():
        raise ReleaseEvidenceError(f"release pytest checkout does not exist: {pytest_root}")
    commands = release_pytest_commands(workflow)
    if not commands:
        raise ReleaseEvidenceError(f"could not parse a pytest command from release workflow: {workflow}")
    if not 0 <= pytest_command_index < len(commands):
        raise ReleaseEvidenceError(
            f"release workflow has {len(commands)} parsed pytest command(s), "
            f"but --pytest-command-index is {pytest_command_index}: {workflow}"
        )

    args = commands[pytest_command_index]
    from scripts import pytest_collection

    try:
        collected = pytest_collection.collect(list(args), pytest_root)
    except Exception as exc:
        raise ReleaseEvidenceError(
            f"could not collect release pytest command from {pytest_root}: {' '.join(args)}: {exc}"
        ) from exc
    selected: dict[str, list[str]] = {}
    for citation in covered_test_evidence(scenario_root):
        nodeids = [entry["nodeid"] for entry in collected if names_case(citation, entry["nodeid"])]
        if nodeids:
            selected[citation] = nodeids
    if not selected:
        raise ReleaseEvidenceError(
            "parsed release pytest command selects no covered scenario test evidence: "
            + " ".join(args)
        )

    outcomes = read_release_report(junit_dir, pytest_root)
    problems: list[str] = []
    for citation, nodeids in selected.items():
        missing = [nodeid for nodeid in nodeids if nodeid not in outcomes]
        if missing:
            problems.append(f"{citation} is missing from the release JUnit report: {', '.join(missing)}")
            continue
        failed = [
            f"{nodeid}={outcomes[nodeid][0]}{': ' + outcomes[nodeid][1] if outcomes[nodeid][1] else ''}"
            for nodeid in nodeids
            if outcomes[nodeid][0] != "passed"
        ]
        if failed:
            problems.append(f"{citation} did not pass in the release run ({'; '.join(failed)})")
    if problems:
        raise ReleaseEvidenceError("; ".join(problems))
    return selected


def judge(
    evidence: dict[str, Evidence],
    reports: Reports,
    lint_commands: dict[str, list[tuple[str, ...]]],
    lint_runs: dict[str, int],
    release_commands: list[tuple[str, ...]],
    select: Callable[[tuple[str, ...]], Iterable[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """The problems with ``evidence`` given what CI reported, and which cases were release-gated or not run.

    A ``lint.yml`` job counts as reported only when every run of its matrix
    left an artifact (``lint_runs``): a run that left none -- skipped by its
    path or event filter, or gone before its upload -- says nothing about the
    cases it would have run. The unit shards are the exception: this gate runs
    whenever they do, so a missing shard report is itself a problem. ``select`` returns the node ids a command's
    arguments select; it is only asked about commands that could account for a
    case no report showed passing.
    """

    expected = lint_runs.get(UNIT_SHARD_JOB, 1)
    if reports.artifacts.get(UNIT_SHARD_JOB, 0) < expected:
        return [f"{UNIT_SHARD_JOB} left {reports.artifacts.get(UNIT_SHARD_JOB, 0)} of {expected} junit reports"], {}
    by_file = reports.by_file()
    pending: list[Evidence] = []
    for case in evidence.values():
        outcomes = [
            (outcome, message)
            for nodeid, outcome, message in by_file.get(case.citation.partition("::")[0], [])
            if names_case(case.citation, nodeid)
        ]
        if any(outcome == "passed" for outcome, _ in outcomes):
            continue
        case.skips = sorted({message or outcome for outcome, message in outcomes})
        pending.append(case)
    accounted: dict[str, list[str]] = {"release-gated": [], "not run": []}
    if not pending:
        return [], accounted
    selections: dict[tuple[str, ...], list[str]] = {}

    def selects(args: tuple[str, ...], citation: str) -> bool:
        if args not in selections:
            selections[args] = list(select(args))
        return any(names_case(citation, nodeid) for nodeid in selections[args])

    unreported = [
        args
        for job, commands in lint_commands.items()
        if reports.artifacts.get(job, 0) < lint_runs.get(job, 1)
        for args in commands
    ]
    problems: list[str] = []
    for case in pending:
        rows = ", ".join(case.rows)
        if any(selects(args, case.citation) for args in release_commands):
            accounted["release-gated"].append(case.citation)
        elif any(selects(args, case.citation) for args in unreported):
            accounted["not run"].append(case.citation)
        elif case.skips:
            problems.append(f"{rows}: {case.citation} was skipped everywhere it ran ({'; '.join(case.skips)})")
        else:
            problems.append(f"{rows}: {case.citation} was collected by no CI job that ran, and no release runs it")
    return problems, accounted


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit_dir", help="JUnit directory to inspect")
    parser.add_argument("--release", action="store_true", help="enforce the release-run evidence contract")
    parser.add_argument("--workflow", type=Path, help="release workflow containing the pytest command")
    parser.add_argument("--pytest-root", type=Path, default=Path.cwd(), help="checkout root used by that pytest command")
    parser.add_argument("--pytest-command-index", type=int, default=0, help="parsed pytest command to verify")
    return parser.parse_args(argv)


def _path_from_cwd(path: Path) -> Path:
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.release:
        if args.workflow is None:
            print("::error::release mode requires --workflow")
            return 1
        try:
            selected = check_release_evidence(
                _path_from_cwd(Path(args.junit_dir)),
                _path_from_cwd(args.workflow),
                _path_from_cwd(args.pytest_root),
                args.pytest_command_index,
            )
        except ReleaseEvidenceError as exc:
            print(f"::error::{exc}")
            return 1
        print(
            f"Release evidence passed for {len(selected)} covered test case(s) "
            f"from {args.workflow} using checkout {_path_from_cwd(args.pytest_root)}."
        )
        return 0

    from scripts import pytest_collection

    junit_dir = Path(args.junit_dir)
    reports = read_reports(junit_dir)
    lint_commands = pytest_commands(WORKFLOWS / LINT_WORKFLOW)
    lint_runs = {job_id: matrix_size(job) for job_id, job in workflow_jobs(WORKFLOWS / LINT_WORKFLOW).items()}
    release_commands = [args for name in RELEASE_WORKFLOWS for commands in pytest_commands(WORKFLOWS / name).values() for args in commands]
    evidence = covered_evidence()
    problems, accounted = judge(
        evidence,
        reports,
        lint_commands,
        lint_runs,
        release_commands,
        lambda args: [entry["nodeid"] for entry in pytest_collection.collect(list(args))],
    )
    print(f"Read reports from {len(reports.outcomes)} job(s): {', '.join(sorted(reports.outcomes)) or 'none'}.")
    print(f"{len(evidence)} case(s) evidence covered rows; {len(problems)} never ran.")
    for kind, citations in accounted.items():
        for citation in citations:
            print(f"  {kind}: {citation}")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main(sys.argv[1:]))
