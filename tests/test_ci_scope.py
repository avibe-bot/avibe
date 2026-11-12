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
    "web-fetch-macos", "windows-install-smoke-runners", "windows-agent", "windows-model-hub", "windows-control-ipc", "model-hub-lifecycle-native",
}
GATES = {
    "unit-tests": {
        "unit-test-shards", "scenario-evidence", "ui-checks", "ui-tests", "ui-browser-checks",
        "model-hub-hosted-wire", "model-hub-lifecycle-native", "manifest-consistency", "python-smoke", "ios-shell", "desktop-shell",
        "ruff", "memory-insight-contract", "show-router-integration", "build-linux-artifacts",
        "migration-release-guard", "npm-wrapper",
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
    (("core/controller.py",), {"web-fetch-runtime", "windows-agent", "windows-model-hub", "windows-control-ipc", "model-hub-lifecycle-native"}),
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
    assert all(plan["jobs"][job] == ("skip" if plan["docs_only"] else "run") for job in FAST | PR_TESTS)
    assert {job for job in HEAVY if plan["jobs"][job] == "run"} == expected - {"ios-shell"}
    # The same classification drives the visible but non-required PR iOS workflow.
    assert heavy_routes(scope(*paths))["ios-shell"] == ("run" if "ios-shell" in expected else "skip")


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
    expected = FAST if mode == "MG_THIN" else set() if mode == "PR" else FAST | PR_TESTS | HEAVY
    assert {job for job, route in plan["jobs"].items() if route == "run"} == expected
    assert validate_plan(plan) == plan


def fixture(mode, gate, paths=("ui/src/App.tsx",)):
    plan = make_plan(mode, scope(*paths))
    jobs = FAST | PR_TESTS | HEAVY | GATES.keys() if gate == "master-full-backstop" else GATES[gate]
    needs = {"changes": {"result": "success", "outputs": {"mode": mode, "plan": json.dumps(plan)}}}
    needs.update({job: {"result": "success" if plan["jobs"].get(job, "run") == "run" else "skipped"} for job in jobs})
    return plan, needs


@pytest.mark.parametrize("paths", [("ui/src/App.tsx",), ("docs/guide.md",)])
@pytest.mark.parametrize("mode", EVENT)
@pytest.mark.parametrize("gate", GATES)
def test_every_gate_checks_each_scope_success_skip_failure_cancel_and_missing(mode, gate, paths):
    _, needs = fixture(mode, gate, paths)
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
        if name == "scenario-evidence":
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
        if filename == "ios-shell.yml":
            assert set(called[True]) == {"workflow_call", "pull_request"}
            assert called["concurrency"] == {
                "group": "ios-shell-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
                "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
            }
            assert called["jobs"]["route"]["if"] == "github.event_name == 'pull_request'"
            simulator = called["jobs"]["simulator"]
            assert simulator["needs"] == "route"
            assert simulator["if"] == (
                "!cancelled() && (github.event_name != 'pull_request' || "
                "(needs.route.result == 'success' && needs.route.outputs.ios == 'run'))"
            )
            assert not simulator.get("continue-on-error")
        else:
            assert set(called[True]) == {"workflow_call"}
            assert "concurrency" not in called
    for name in ("ios-shell", "desktop-shell", "web-fetch-runtime", "web-fetch-macos"):
        call = data["jobs"][name]
        assert call["uses"].startswith("./.github/workflows/")
    assert data["jobs"]["windows-install-smoke-runners"]["strategy"]["matrix"]["runner"] == ["windows-latest", "windows-11-arm"]


@pytest.mark.parametrize("path", ["docs/guide.md", "AGENTS.md", ".github/PULL_REQUEST_TEMPLATE.md"])
def test_docs_fast_path_is_explicit_and_cannot_leak_into_full_modes(path):
    plan = make_plan("PR", scope(path))
    assert plan["docs_only"] is True and set(plan["jobs"].values()) == {"skip"}
    assert set(heavy_routes(scope(path)).values()) == {"skip"}
    for job in plan["jobs"]:
        bad = copy.deepcopy(plan)
        bad["jobs"][job] = "run"
        with pytest.raises(ValueError):
            validate_plan(bad)
    for value in ("true", "false", None, 0, 1):
        with pytest.raises(ValueError):
            validate_plan({**plan, "docs_only": value})
    for mode in ("MG_THIN", "MASTER_FULL"):
        with pytest.raises(ValueError):
            validate_plan({**plan, "mode": mode})
    uncertain = scope(path)._replace(routable=False)
    plan = make_plan("PR", uncertain)
    assert plan["docs_only"] is False
    assert all(plan["jobs"][job] == "run" for job in FAST | PR_TESTS | (HEAVY - {"ios-shell"}))


def test_docs_fast_path_keeps_only_the_existing_dependency_free_guidance_check():
    steps = workflow()["jobs"]["changes"]["steps"]
    guidance, = [s for s in steps if "pytest --noconftest" in s.get("run", "")]
    assert guidance["if"] == "steps.scope.outputs.docs_only == 'true'"
    assert guidance["env"] == {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    assert guidance["run"].splitlines() == [
        "python -m pip install --disable-pip-version-check pytest==9.0.3",
        "python -m pytest --noconftest -p no:cacheprovider -c /dev/null -q tests/test_repository_guidance.py",
    ]


@pytest.mark.parametrize("path,docs,ios", [
    ("docs/guide.md", True, "skip"),
    (".github/PULL_REQUEST_TEMPLATE.md", True, "skip"),
    ("ui/src/App.tsx", False, "skip"),
    ("ios/Avibe/NavigationPolicy.swift", False, "run"),
    ("scripts/ci_scope.py", False, "run"),
    ("unknown/input", False, "run"),
])
def test_real_scope_cli_routes_docs_and_advisory_ios_from_the_same_git_diff(tmp_path, path, docs, ios):
    from tests.test_ci_docs_only import make_repo, synthetic_pr, writes

    repo = make_repo(tmp_path / "repo")
    event, sha = synthetic_pr(repo, writes(path))
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event))
    output = tmp_path / "output"
    env = {**os.environ, "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/1/merge",
           "GITHUB_EVENT_PATH": str(event_file), "GITHUB_SHA": sha, "GITHUB_OUTPUT": str(output)}
    command = [sys.executable, str(ROOT / "scripts/ci_scope.py")]
    result = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    plan = json.loads(outputs["plan"])
    assert plan["docs_only"] is docs and outputs["docs_only"] == str(docs).lower()
    assert plan["jobs"]["ios-shell"] == "skip"
    output.unlink()
    result = subprocess.run([*command, "--optional-ios"], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"ios={ios}\n"
    output.unlink()
    # Missing event evidence never silently skips the optional native probe.
    event_file.write_text("{}")
    result = subprocess.run([*command, "--optional-ios"], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and output.read_text() == "ios=run\n"


def test_check_results_never_accepts_absent_results():
    plan = make_plan("MG_THIN")
    assert check_results(plan, {}, ("unit-test-shards", "ui-checks"))


def test_native_lifecycle_harness_is_a_required_six_platform_pr_producer_not_qualification():
    job = workflow()["jobs"]["model-hub-lifecycle-native"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {item["engine_platform"] for item in matrix} == {
        "linux-amd64", "linux-arm64", "darwin-arm64", "darwin-x64", "windows-amd64", "windows-arm64",
    }
    assert all(item["python_arch"] == "x64" for item in matrix if item["runner"].startswith("windows"))
    assert job["needs"] == "changes" and not job.get("continue-on-error")
    scripts = "\n".join(step.get("run", "") for step in job["steps"])
    assert "git fetch --no-tags --depth=1 origin ea402b8bc3ab5f7f29cc85b28f22aed17544cc2d" in scripts
    for suffix in ("lifecycle", "admission", "consumers", "processes", "owner_crashes", "released_callers"):
        assert f"tests/test_model_hub_engine_{suffix}.py" in scripts
    assert "win-amd64" in scripts and "AVIBE_EXPECT_ENGINE_PLATFORM" in scripts
    assert "qualification" not in scripts and "workflow_dispatch" not in scripts
    for path in ("vibe/model_hub_runtime/lifecycle.py", "core/managed_runtime.py", "tests/model_hub_owner_crash_driver.py"):
        assert make_plan("PR", scope(path))["jobs"]["model-hub-lifecycle-native"] == "run"
