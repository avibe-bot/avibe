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
    (("ui/design/frames/vault/example.design.tsx", "docs/guide.md"), set()),
    (("assets/screenshots/example.png",), set()),
    (("ui/design/frame.ts", "desktop/src-tauri/src/main.rs"), {"ui-browser-checks", "desktop-shell", "install-upgrade-shards", "web-fetch-runtime", "web-fetch-macos", "windows-install-smoke-runners"}),
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
    # desktop-shell's composer caret probe guards these.
    (("ui/src/components/workbench/Composer.tsx",), {"ui-browser-checks", "desktop-shell"}),
    (("ui/e2e/voice-shortcut/fixture.tsx",), {"ui-browser-checks", "desktop-shell"}),
    (("ui/src/components/workbench/Composer.layout.test.tsx",), {"ui-browser-checks"}),
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


def test_real_unit_matrix_evidence_count_matches_pr_and_master():
    from scripts.ci_scenario_evidence import matrix_size

    job = workflow()['jobs']['unit-test-shards']
    assert matrix_size(job, mode='PR') == 8
    assert matrix_size(job, mode='MASTER_FULL') == 16


def test_design_fixture_and_added_reserved_names_remain_covered():
    for path in ('ui/design/shell/storage.ts', 'ui/design/frames/chat/fixtures.ts'):
        assert heavy_routes(scope(path))['ui-browser-checks'] == 'run'
    for paths in [('assets/screenshots/aux.png',), ('assets/screenshots/safe.png', 'ui/design/aux.ts')]:
        changed = scope_of([('A', '000000', '100644', p) for p in paths], True)
        assert set(heavy_routes(changed).values()) == {'run'}


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())


def test_workflow_routes_match_the_contract_and_every_producer_has_a_required_gate():
    jobs = workflow()["jobs"]
    assert set(jobs) == {"changes", "master-full-backstop", "fast-gate", "sensitive-proof", "advisory-ios"} | FAST | PR_TESTS | HEAVY | GATES.keys()
    leaves = {"ruff", "show-router-integration", "memory-insight-contract", "migration-release-guard", "npm-wrapper", "build-linux-artifacts"}
    for name in FAST | PR_TESTS | HEAVY:
        job = jobs[name]
        assert not job.get("continue-on-error")
        needs = job.get("needs")
        assert "changes" in ([needs] if isinstance(needs, str) else needs)
        if name == "scenario-evidence":
            assert job["if"] == "always() && needs.changes.result == 'success' && fromJSON(needs.changes.outputs.dispatch_jobs)['scenario-evidence'] == 'run'"
        else:
            assert job["if"] == f"fromJSON(needs.changes.outputs.dispatch_jobs)['{name}'] == 'run'"
        assert name in leaves or any(name in members for members in GATES.values())
    for name, producers in GATES.items():
        assert set(jobs[name]["needs"]) == {"changes", *producers}
        assert jobs[name]["if"] == "always() && needs.changes.outputs.portfolio != 'true'"
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


def test_full_master_workflow_batches_pending_runs():
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
    assert data['jobs']['advisory-ios']['if'] == "needs.changes.outputs.advisory_ios == 'run'"
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
    assert outputs['advisory_ios'] == ios and outputs['portfolio'] == 'false'
    output.unlink()
    enabled = {**env, 'FAST_PR_ENABLED': 'true', 'RUNNER_TEMP': str(tmp_path)}
    result = subprocess.run(command, cwd=repo, env=enabled, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    routed = dict(line.split('=', 1) for line in output.read_text().splitlines())
    from scripts.ci_fast_pr import LINUX
    extras = {'ios/Avibe/NavigationPolicy.swift': {'ios-shell'},
              'scripts/ci_scope.py': {'manifest-consistency'},
              '.github/PULL_REQUEST_TEMPLATE.md': {'manifest-consistency'}}.get(path, set())
    assert routed['portfolio'] == 'true' and routed['sensitive'] == str(bool(extras)).lower()
    assert {k for k, v in json.loads(routed['dispatch_jobs']).items() if v == 'run'} == LINUX | extras
    assert json.loads(routed['unit_shards']) == list(range(16))
    assert routed['advisory_ios'] == 'skip'
    output.unlink()
    result = subprocess.run([*command, "--optional-ios"], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"ios={ios}\n"
    output.unlink()
    # Missing event evidence never silently skips the optional native probe.
    event_file.write_text("{}")
    result = subprocess.run([*command, "--optional-ios"], cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0 and output.read_text() == "ios=run\n"


@pytest.mark.parametrize("path", ["vibe/ui_network_access.py", "tests/test_ui_listener_lifecycle.py"])
def test_native_ui_listener_changes_run_on_all_three_operating_systems(path):
    jobs = make_plan("PR", scope(path))["jobs"]
    assert all(jobs[job] == "run" for job in ("unit-test-shards", "install-upgrade-shards", "windows-control-ipc"))
    for job in ("install-upgrade-shards", "windows-control-ipc"):
        assert any("tests/test_ui_listener_lifecycle.py" in step.get("run", "") for step in workflow()["jobs"][job]["steps"])


@pytest.mark.parametrize("path", ["vibe/ui_server.py", "vibe/desktop_runtime.py"])
def test_listener_consumers_alone_do_not_trigger_install_regressions(path):
    assert make_plan("PR", scope(path))["jobs"]["install-upgrade-shards"] == "skip"


def test_portfolio_unions_mixed_sensitive_paths_and_ordinary_unknown_paths():
    from scripts.ci_fast_pr import LINUX, selected

    assert selected(scope('unknown/area', 'ui/src/App.tsx')) == (set(), LINUX)
    groups, jobs = selected(scope('core/auth.py', 'storage/alembic/env.py', 'uv.lock'))
    assert groups == {'identity', 'migration', 'shared'}
    assert jobs == LINUX | {'ui-browser-checks', 'windows-control-ipc', 'install-upgrade-shards', 'manifest-consistency'}
    rename = scope_of([('D', '100644', '000000', 'storage/vault_data.py'), ('A', '000000', '100644', 'docs/b.md')], True)
    assert 'vault' in selected(rename)[0]
    with pytest.raises(ValueError):
        selected(full('unknown diff', False))


@pytest.mark.parametrize('path,group,proof', [
    ('vibe/model_hub_runtime/cliproxyapi_manifest.json', 'engine-wire', 'model-hub-hosted-wire'),
    ('core/control_ipc.py', 'identity', 'windows-control-ipc'),
    ('core/private_fs.py', 'identity', 'windows-control-ipc'),
    ('tests/test_control_ipc.py', 'identity', 'windows-control-ipc'),
    ('scripts/desktop_release.py', 'desktop', 'desktop-shell'),
    ('packaging/iroh-apple/release.json', 'ios', 'ios-shell'),
    ('storage/compatibility.py', 'migration', 'install-upgrade-shards'),
])
def test_indirect_sensitive_inputs_require_the_consuming_producer(path, group, proof):
    from scripts.ci_fast_pr import selected

    groups, jobs = selected(scope(path, 'docs/guide.md'))
    assert group in groups and proof in jobs
    assert proof in workflow()['jobs']['sensitive-proof']['needs']


@pytest.mark.parametrize('gate', ['fast-gate', 'sensitive-proof'])
def test_portfolio_aggregate_rejects_missing_cancelled_skipped_or_red(gate):
    from scripts.ci_fast_pr import LINUX, EXTRA, verify

    jobs = LINUX if gate == 'fast-gate' else EXTRA
    dispatch = dict.fromkeys(jobs, 'run')
    needs = {name: {'result': 'success'} for name in jobs}
    if gate == 'sensitive-proof':
        needs.update({name: {'result': 'skipped'} for name in GATES})
    needs['changes'] = {'result': 'success', 'outputs': {'portfolio': 'true', 'dispatch_jobs': json.dumps(dispatch)}}
    assert set(workflow()['jobs'][gate]['needs']) == set(needs)
    verify(gate, needs)
    for status in ('failure', 'cancelled', 'skipped', ''):
        with pytest.raises(ValueError):
            verify(gate, {**needs, min(jobs): {'result': status}})
    with pytest.raises(ValueError):
        verify(gate, {k: v for k, v in needs.items() if k != min(jobs)})


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
