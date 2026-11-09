"""Observable CI contract: every mode gates exactly its declared producers.

The old docs/browser gates could not express a thin queue, routed native jobs,
or a full master certificate. These cases catch a silently skipped producer,
unknown diff, cancelled run, or stale mode before it can admit a queue tree.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from scripts.ci_docs_only import full, scope_of
from scripts.ci_scope import check_gate, check_results, heavy_routes, make_plan, mode_for_event, validate_plan

ROOT = Path(__file__).resolve().parents[1]
FAST = {
    "ruff", "memory-insight-contract", "show-router-integration", "build-linux-artifacts",
    "migration-release-guard", "npm-wrapper", "ui-checks", "manifest-consistency",
    "python-smoke",
}
PR_TESTS = {"unit-test-shards", "ui-tests", "scenario-evidence", "model-hub-hosted-wire"}
HEAVY = {
    "ui-browser-checks", "ios-shell", "desktop-shell", "install-upgrade-shards", "web-fetch-runtime",
    "web-fetch-macos", "windows-install-smoke-runners", "windows-agent", "windows-model-hub", "windows-control-ipc",
}
GATES = {
    "unit-tests": {
        "unit-test-shards", "scenario-evidence", "ui-checks", "ui-tests", "ui-browser-checks",
        "model-hub-hosted-wire", "manifest-consistency", "python-smoke", "ios-shell", "desktop-shell",
    },
    "install-upgrade-regression": {"install-upgrade-shards"},
    "windows-install-smoke": {
        "windows-install-smoke-runners", "web-fetch-runtime", "web-fetch-macos", "windows-agent",
        "windows-model-hub", "windows-control-ipc",
    },
}
EVENT = {"PR": "pull_request", "MG_THIN": "merge_group", "MASTER_FULL": "push"}


def scope(*paths):
    return scope_of([("M", "100644", "100644", path) for path in paths], True)


@pytest.mark.parametrize("paths,expected", [
    (("ui/src/App.tsx",), {"ui-browser-checks"}),
    (("core/controller.py",), {"web-fetch-runtime", "windows-agent", "windows-model-hub", "windows-control-ipc"}),
    (("ios/Avibe/NavigationPolicy.swift",), {"ui-browser-checks", "ios-shell", "web-fetch-runtime"}),
    (("desktop/src-tauri/src/main.rs",), {"desktop-shell", "install-upgrade-shards", "web-fetch-runtime", "web-fetch-macos", "windows-install-smoke-runners"}),
    (("pyproject.toml",), HEAVY),
    (("install.ps1",), HEAVY),
    (("docs/guide.md",), set()),
    ((".github/workflows/lint.yml",), HEAVY),
    (("unknown/new.py",), HEAVY),
    (("ui/new-config.ts",), HEAVY),
    (("ui/src/mobile/bridge.ts",), {"ui-browser-checks", "ios-shell", "desktop-shell"}),
    (("ui/src/i18n/en.json",), {"ui-browser-checks", "desktop-shell"}),
])
def test_representative_pr_diffs_are_conservative(paths, expected):
    plan = make_plan("PR", scope(*paths))
    assert set(plan["jobs"]) == FAST | PR_TESTS | HEAVY
    assert all(plan["jobs"][job] == "run" for job in FAST | PR_TESTS)
    assert {job for job in HEAVY if plan["jobs"][job] == "run"} == expected


@pytest.mark.parametrize("change", [
    [("M", "100644", "100755", "docs/guide.md")],
    [("A", "000000", "120000", "ui/src/link.ts")],
    [],
])
def test_uncertain_diff_runs_every_heavy_pr_job(change):
    assert set(heavy_routes(scope_of(change, True)).values()) == {"run"}
    assert set(heavy_routes(full("Git failed", True)).values()) == {"run"}
    assert set(heavy_routes(scope_of([("M", "100644", "100644", "docs/guide.md")], False)).values()) == {"run"}
    # A Windows-reserved added UI filename is not proven safe for packaging.
    assert set(heavy_routes(scope_of([("A", "000000", "100644", "ui/src/aux.tsx")], True)).values()) == {"run"}


@pytest.mark.parametrize("mode", EVENT)
def test_mode_declares_exact_coverage_even_for_docs(mode):
    plan = make_plan(mode, scope("docs/guide.md"))
    expected = FAST if mode == "MG_THIN" else FAST | PR_TESTS if mode == "PR" else FAST | PR_TESTS | HEAVY
    assert {job for job, route in plan["jobs"].items() if route == "run"} == expected
    assert validate_plan(plan) == plan


def fixture(mode, gate, paths=("ui/src/App.tsx",)):
    plan = make_plan(mode, scope(*paths))
    jobs = FAST | PR_TESTS | HEAVY | GATES.keys() if gate == "master-full-backstop" else GATES[gate]
    needs = {"changes": {"result": "success", "outputs": {"mode": mode, "plan": json.dumps(plan)}}}
    needs.update({job: {"result": "success" if plan["jobs"].get(job, "run") == "run" else "skipped"} for job in jobs})
    return plan, needs


@pytest.mark.parametrize("mode", EVENT)
@pytest.mark.parametrize("gate", GATES)
def test_every_gate_checks_each_scope_success_skip_failure_cancel_and_missing(mode, gate):
    _, needs = fixture(mode, gate)
    kwargs = {"event_name": EVENT[mode], "ref": "refs/heads/master"}
    assert check_gate(gate, needs, **kwargs) == []
    for job in GATES[gate]:
        expected = needs[job]["result"]
        for result in ("success", "skipped", "failure", "cancelled", "timed_out", "unknown", "", None):
            changed = copy.deepcopy(needs)
            changed[job]["result"] = result
            assert bool(check_gate(gate, changed, **kwargs)) == (result != expected), (job, mode, result)
        missing = copy.deepcopy(needs)
        del missing[job]
        with pytest.raises(ValueError, match="dependencies"):
            check_gate(gate, missing, **kwargs)
    for result in ("skipped", "failure", "cancelled", "", None):
        bad = copy.deepcopy(needs)
        bad["changes"]["result"] = result
        with pytest.raises(ValueError, match="classification"):
            check_gate(gate, bad, **kwargs)


def test_master_certificate_requires_all_jobs_including_native_and_gates():
    _, needs = fixture("MASTER_FULL", "master-full-backstop")
    kwargs = {"event_name": "push", "ref": "refs/heads/master"}
    assert check_gate("master-full-backstop", needs, **kwargs) == []
    for job in FAST | PR_TESTS | HEAVY | GATES.keys():
        for result in ("skipped", "failure", "cancelled", "unknown", "", None):
            bad = copy.deepcopy(needs)
            bad[job]["result"] = result
            assert check_gate("master-full-backstop", bad, **kwargs), (job, result)
    for event in ("pull_request", "merge_group"):
        with pytest.raises(ValueError, match="mode"):
            check_gate("master-full-backstop", needs, event_name=event, ref="refs/heads/master")


@pytest.mark.parametrize("mode", EVENT)
def test_invalid_missing_or_unknown_disposition_cannot_authorize_skips(mode):
    plan = make_plan(mode, scope("ui/src/App.tsx"))
    for value in ("", "unknown", "RUN", "skipped", None):
        bad = copy.deepcopy(plan)
        bad["jobs"]["ui-browser-checks"] = value
        with pytest.raises(ValueError):
            validate_plan(bad)
    for mutated in ({}, {**plan, "extra": "skip"}, {**plan, "mode": "PR_FULL"}, {**plan, "jobs": {}}):
        with pytest.raises(ValueError):
            validate_plan(mutated)
    for job in ("ui-checks", "manifest-consistency", "python-smoke"):
        bad = copy.deepcopy(plan)
        bad["jobs"][job] = "skip"
        with pytest.raises(ValueError):
            validate_plan(bad)


@pytest.mark.parametrize("event,ref,expected", [
    ("pull_request", "refs/pull/1/merge", "PR"),
    ("merge_group", "refs/heads/gh-readonly-queue/master/pr-1", "MG_THIN"),
    ("push", "refs/heads/master", "MASTER_FULL"),
    ("workflow_dispatch", "refs/heads/master", "MASTER_FULL"),
    ("workflow_dispatch", "refs/heads/feature", None),
    ("push", "refs/tags/v3.0.0", None),
    ("pull_request_target", "refs/heads/master", None),
])
def test_only_master_events_can_claim_full_mode(event, ref, expected):
    if expected is None:
        with pytest.raises(ValueError):
            mode_for_event(event, ref)
    else:
        assert mode_for_event(event, ref) == expected


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())


def test_workflow_routes_match_the_contract_and_every_producer_has_a_required_gate():
    jobs = workflow()["jobs"]
    assert set(jobs) == {"changes", "master-full-backstop"} | FAST | PR_TESTS | HEAVY | GATES.keys()
    leaves = {"ruff", "show-router-integration", "memory-insight-contract", "migration-release-guard", "npm-wrapper", "build-linux-artifacts"}
    for name in FAST | PR_TESTS | HEAVY:
        job = jobs[name]
        assert not job.get("continue-on-error")
        needs = job.get("needs")
        assert "changes" in ([needs] if isinstance(needs, str) else needs)
        if name in FAST:
            assert "if" not in job
        elif name == "scenario-evidence":
            assert job["if"] == "always() && needs.changes.result == 'success' && fromJSON(needs.changes.outputs.plan).jobs['scenario-evidence'] == 'run'"
        else:
            assert job["if"] == f"fromJSON(needs.changes.outputs.plan).jobs['{name}'] == 'run'"
        assert name in leaves or any(name in members for members in GATES.values())
    for name, producers in GATES.items():
        assert set(jobs[name]["needs"]) == {"changes", *producers}
        assert jobs[name]["if"] == "always()"
        step, = [s for s in jobs[name]["steps"] if "run" in s]
        assert step["run"] == f"python3 scripts/ci_scope.py --gate {name}"
        assert step["env"] == {"NEEDS_JSON": "${{ toJSON(needs) }}"}
    assert set(jobs["master-full-backstop"]["needs"]) == {"changes"} | FAST | PR_TESTS | HEAVY | GATES.keys()


@pytest.mark.parametrize("mode", EVENT)
def test_real_workflow_gate_command_enforces_the_event_mode(tmp_path, mode):
    _, needs = fixture(mode, "unit-tests")
    step = next(s for s in workflow()["jobs"]["unit-tests"]["steps"] if "run" in s)
    command = step["run"].replace("python3", sys.executable, 1)
    env = {**os.environ, "GITHUB_EVENT_NAME": EVENT[mode], "GITHUB_REF": "refs/heads/master", "NEEDS_JSON": json.dumps(needs)}
    result = subprocess.run(command, shell=True, cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    needs["ui-checks"]["result"] = "skipped"
    result = subprocess.run(command, shell=True, cwd=ROOT, env={**env, "NEEDS_JSON": json.dumps(needs)}, capture_output=True, text=True)
    assert result.returncode == 1 and "expected success" in result.stdout


def test_backstop_coalesces_whole_reusable_workflow_without_cancelling_running_master():
    data = workflow()
    assert data[True]["push"] == {"branches": ["master"]}
    assert "workflow_dispatch" in data[True]
    assert data["concurrency"] == {
        "group": "${{ github.workflow }}-${{ github.event_name == 'workflow_dispatch' && 'push' || github.event_name }}-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }
    for filename in ("ios-shell.yml", "desktop-shell.yml", "web-fetch-runtime.yml"):
        called = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        assert set(called[True]) == {"workflow_call"}
        assert "concurrency" not in called
    for name in ("ios-shell", "desktop-shell", "web-fetch-runtime", "web-fetch-macos"):
        call = data["jobs"][name]
        assert call["uses"].startswith("./.github/workflows/")
    assert data["jobs"]["windows-install-smoke-runners"]["strategy"]["matrix"]["runner"] == ["windows-latest", "windows-11-arm"]


def test_check_results_never_accepts_absent_results():
    plan = make_plan("MG_THIN")
    assert check_results(plan, {}, ("unit-test-shards", "ui-checks"))
