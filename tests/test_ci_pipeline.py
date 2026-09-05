from __future__ import annotations

import itertools
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())["jobs"]


def _run_isolated_unit_files(tmp_path: Path, sources: dict[str, str], *, timeout: str = "15"):
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    for name, source in sources.items():
        (tmp_path / "tests" / name).write_text(source, encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/ci_unit_test_shards.py", tmp_path / "scripts/ci_unit_test_shards.py")
    shutil.copyfile(ROOT / "scripts/ci_pytest_metrics.py", tmp_path / "scripts/ci_pytest_metrics.py")
    return subprocess.run(
        ["bash", str(ROOT / "scripts/ci_unit_tests.sh")], cwd=tmp_path,
        env={**os.environ, "PYTHON": sys.executable, "CI_TEST_FILE_TIMEOUT_SECONDS": timeout,
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=40,
    )


def test_unit_shard_has_an_outer_deadline_without_weakening_failure_propagation() -> None:
    shard = _jobs()["unit-test-shards"]
    assert shard["timeout-minutes"] == 20
    assert shard["strategy"]["fail-fast"] is False
    assert not shard.get("continue-on-error")
    assert all(not step.get("continue-on-error") for step in shard["steps"])


@pytest.mark.parametrize("phase", ["collection", "test", "shutdown"])
def test_unit_file_watchdog_dumps_stacks_and_continues_fail_closed(tmp_path: Path, phase: str) -> None:
    sources = {
        "collection": "import time\ntime.sleep(3600)\n",
        "test": "import time\ndef test_stuck():\n    time.sleep(3600)\n",
        "shutdown": (
            "import threading\n"
            "def test_stuck():\n"
            "    threading.Thread(target=threading.Event().wait).start()\n"
        ),
    }
    result = _run_isolated_unit_files(tmp_path, {
        "test_a_stuck.py": sources[phase],
        "test_b_pass.py": "def test_after_stuck_file():\n    pass\n",
    }, timeout="3")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Starting tests/test_a_stuck.py (timeout 3s)." in result.stdout
    assert "Timeout (0:00:03)!" in result.stderr
    assert "most recent call first" in result.stderr
    assert "Finished tests/test_b_pass.py" in result.stdout
    assert "test_after_stuck_file PASSED" in result.stdout
    assert "FAILED files:\n  tests/test_a_stuck.py" in result.stdout
    assert "All unit test files passed." not in result.stdout
    records = _metrics(result)
    assert records[-1]["file"] == "tests/test_b_pass.py"
    if phase == "shutdown":
        assert records[0]["file"] == "tests/test_a_stuck.py"
        assert records[0]["boundary"] == "pytest_returned_before_interpreter_shutdown"
    else:
        assert len(records) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_unit_file_runner_preserves_assertions_and_integration_selection(tmp_path: Path, failed: bool) -> None:
    result = _run_isolated_unit_files(tmp_path, {
        "test_assertion.py": f"def test_assertion():\n    assert {not failed}\n",
        "test_integration.py": "import pytest\n@pytest.mark.integration\ndef test_excluded():\n    assert False\n",
    })
    assert result.returncode == int(failed), result.stdout + result.stderr
    assert "Ran 2 unit test file(s), one process each" in result.stdout
    assert "No unit tests collected (skipped):\n  tests/test_integration.py" in result.stdout
    records = _metrics(result)
    assert [(row["file"], row["exit_code"]) for row in records] == [
        ("tests/test_assertion.py", int(failed)), ("tests/test_integration.py", 5),
    ]


def _metrics(result) -> list[dict]:
    prefix = "CI_TEST_METRICS "
    return [json.loads(line.removeprefix(prefix)) for line in result.stderr.splitlines() if line.startswith(prefix)]


def test_file_metrics_observe_real_phases_and_waited_child_cpu(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_metrics.py": (
        "import subprocess, sys, time, pytest\n"
        "time.sleep(0.03)\n"
        "@pytest.fixture\n"
        "def prepared():\n"
        "    time.sleep(0.03)\n"
        "    yield\n"
        "    time.sleep(0.03)\n"
        "def test_work(prepared):\n"
        "    started = time.process_time()\n"
        "    while time.process_time() - started < 0.03: pass\n"
        "    subprocess.run([sys.executable, '-c', "
        "'import time; start=time.process_time()\\nwhile time.process_time()-start<0.03: pass'], check=True)\n"
    )})
    assert result.returncode == 0, result.stdout + result.stderr
    record, = _metrics(result)
    assert record["schema_version"] == 1
    assert record["boundary"] == "pytest_returned_before_interpreter_shutdown"
    assert record["file"] == "tests/test_metrics.py"
    assert record["exit_code"] == 0
    assert record["phase_counts"] == {"collection": 1, "setup": 1, "call": 1, "teardown": 1}
    assert all(value >= 0.02 for value in record["phase_seconds"].values())
    assert record["outside_phases_seconds"] >= 0
    assert record["wall_seconds"] >= sum(record["phase_seconds"].values())
    if sys.platform != "win32":
        usage = record["process_usage"]
        for scope in ("self", "waited_children"):
            assert usage[scope]["user_cpu_seconds"] + usage[scope]["system_cpu_seconds"] >= 0.02
        assert usage["self"]["peak_rss_bytes"] > 0
    if sys.platform == "linux":
        assert record["linux_proc_io"]["rchar"] > 0
        assert record["cpu_affinity_count"] >= 1
    assert {row["phase"] for row in record["slowest_phases"]} == {"setup", "call", "teardown"}


def test_file_metrics_do_not_double_count_subtests_or_grow_with_test_count(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_subtests.py": (
        "import pytest\n"
        "@pytest.mark.parametrize('case', range(8))\n"
        "def test_subtests(case, subtests):\n"
        "    for item in range(4):\n"
        "        with subtests.test(item=item):\n"
        "            assert item < 4\n"
    )})
    assert result.returncode == 0, result.stdout + result.stderr
    record, = _metrics(result)
    assert record["phase_counts"] == {"collection": 1, "setup": 8, "call": 8, "teardown": 8}
    assert len(record["slowest_phases"]) == 5


def test_file_metrics_do_not_turn_collection_errors_into_success(tmp_path: Path) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_collection.py": "raise RuntimeError('collection failed')\n"})
    assert result.returncode == 1
    record, = _metrics(result)
    assert record["exit_code"] == 2
    assert record["phase_counts"] == {"collection": 1, "setup": 0, "call": 0, "teardown": 0}


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_file_metrics_preserve_fixture_failures(tmp_path: Path, phase: str) -> None:
    setup = "    raise RuntimeError('setup failed')\n" if phase == "setup" else ""
    teardown = "    raise RuntimeError('teardown failed')\n" if phase == "teardown" else ""
    result = _run_isolated_unit_files(tmp_path, {"test_fixture.py": (
        "import pytest\n@pytest.fixture\ndef broken():\n" + setup + "    yield\n" + teardown
        + "def test_fixture(broken): pass\n"
    )})
    assert result.returncode == 1
    record, = _metrics(result)
    assert record["exit_code"] == 1
    assert record["phase_counts"]["call"] == (0 if phase == "setup" else 1)
    assert record["phase_counts"][phase] == 1


def test_file_metrics_mark_unavailable_platform_counters_explicitly(monkeypatch) -> None:
    from scripts import ci_pytest_metrics

    monkeypatch.setattr(ci_pytest_metrics, "resource", None)
    assert ci_pytest_metrics.process_usage() is None

    def denied(_path):
        raise PermissionError("proc is unavailable")

    monkeypatch.setattr(ci_pytest_metrics.Path, "read_text", denied)
    monkeypatch.setattr(ci_pytest_metrics.os, "sched_getaffinity", denied, raising=False)
    assert ci_pytest_metrics.linux_io() is None
    stream = io.StringIO()
    metrics = ci_pytest_metrics.FileMetrics("tests/test_unavailable.py", ci_pytest_metrics.time.perf_counter())
    metrics.emit(stream, 0)
    payload = json.loads(stream.getvalue().removeprefix("CI_TEST_METRICS "))
    assert payload["cpu_affinity_count"] is None


@pytest.mark.parametrize("timeout", ["0", "00", "-1", "invalid"])
def test_unit_file_runner_rejects_invalid_deadline(tmp_path: Path, timeout: str) -> None:
    result = _run_isolated_unit_files(tmp_path, {"test_one.py": "def test_one():\n    pass\n"}, timeout=timeout)
    assert result.returncode == 2
    assert "must be a positive integer" in result.stderr


def test_ui_checks_run_once_without_fencing_artifact_consumers() -> None:
    jobs = _jobs()
    checks = jobs["ui-checks"]
    build = jobs["build-linux-artifacts"]
    assert not checks.get("needs")
    assert not build.get("needs")
    assert "ui-checks" in jobs["unit-tests"]["needs"]
    assert "unit-test-shards" in jobs["unit-tests"]["needs"]

    check_steps = [step for step in checks["steps"] if "run" in step]
    build_steps = [step for step in build["steps"] if step.get("working-directory") == "ui"]
    assert all(step.get("working-directory") == "ui" for step in check_steps)
    check_commands = "\n".join(step["run"] for step in check_steps).splitlines()
    build_commands = "\n".join(step["run"] for step in build_steps).splitlines()
    for command in ("npm run validate:theme", "npm run lint", "npm run typecheck:tests", "npm test"):
        assert check_commands.count(command) == 1
        assert command not in build_commands
    assert build_commands.count("npm run build") == 1
    assert "npm ci" in check_commands
    assert "npm ci" in build_commands
    for job in (checks, build):
        assert not job.get("if")
        assert not job.get("continue-on-error")
        assert all(not step.get("if") and not step.get("continue-on-error") for step in job["steps"])
    for name in ("install-upgrade-shards", "windows-install-smoke"):
        assert jobs[name]["needs"] == "build-linux-artifacts"


def test_install_suites_run_independently_without_losing_checks() -> None:
    jobs = _jobs()
    shards = jobs["install-upgrade-shards"]
    assert shards["strategy"]["fail-fast"] is False
    suites = shards["strategy"]["matrix"]["suite"]
    assert len(suites) == len(set(suites)) == 2
    assert not shards.get("if")
    assert not shards.get("continue-on-error")
    assert all(not step.get("continue-on-error") for step in shards["steps"])
    steps = {step["name"]: step for step in shards["steps"] if "name" in step}
    test_steps = [steps["Run packaged Memory package-shape smoke"], steps["Run install and upgrade regressions"]]
    for suite in suites:
        selected = [step for step in test_steps if step["if"] == f"matrix.suite == '{suite}'"]
        assert len(selected) == 1, f"Every suite must select exactly one regression command: {suite}"
    # Both suites build real source wheels, including the released-generation
    # bridge inside the Docker upgrade test.
    prepare = steps["Prepare Show Runtime manifest for fixture wheels"]
    assert not prepare.get("if")
    assert "python scripts/prepare_local_show_runtime_manifest.py" in prepare["run"]
    assert "tests/test_memory_upgrade_packaged.py -m integration" in test_steps[0]["run"]
    assert "SKIPPED" in test_steps[0]["run"] and "exit 1" in test_steps[0]["run"]
    for test_file in (
        "tests/test_upgrade_flow.py", "tests/test_install_script.py",
        "tests/e2e/test_install_command.py", "tests/e2e/test_upgrade_command.py",
    ):
        assert test_file in test_steps[1]["run"]
    assert "docker info" in test_steps[1]["run"]
    assert jobs["install-upgrade-regression"]["needs"] == "install-upgrade-shards"


def test_distribution_contracts_consume_same_run_artifacts_without_fencing_other_installers() -> None:
    jobs = _jobs()
    build = jobs["build-linux-artifacts"]
    installers = jobs["install-upgrade-shards"]
    uploads = {
        step["with"]["name"]: step
        for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")
    }
    assert uploads["vibe-wheel-linux"]["with"]["path"] == "dist/*.whl"
    companion = uploads["vibe-package-contracts-linux"]
    assert set(companion["with"]["path"].splitlines()) == {
        "dist/*.tar.gz", "memory-dist/*.whl", "memory-dist/*.tar.gz",
    }
    assert companion["with"]["if-no-files-found"] == "error"
    assert not companion.get("if") and not companion.get("continue-on-error")
    downloads = [
        step for step in installers["steps"]
        if step.get("with", {}).get("name") == "vibe-package-contracts-linux"
    ]
    download, = downloads
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["if"] == "matrix.suite == 'installer'"
    assert download["with"] == {"name": "vibe-package-contracts-linux", "path": "."}
    assert not download.get("continue-on-error")
    owners = [
        (name, step) for name, job in jobs.items() for step in job["steps"]
        if "tests/test_memory_distribution.py" in step.get("run", "")
    ]
    (owner, contracts), = owners
    assert owner == "install-upgrade-shards"
    assert contracts["if"] == "matrix.suite == 'installer'"
    assert installers["strategy"]["matrix"]["suite"].count("installer") == 1
    assert installers["steps"].index(download) < installers["steps"].index(contracts)
    build_environment = next(step["env"] for step in build["steps"] if step.get("name") == "Build package artifact")
    assert contracts["env"] == {"AVIBE_PACKAGE_CONTRACT_VERSION": build_environment["AVIBE_PACKAGE_CONTRACT_VERSION"]}
    assert 'AVIBE_CORE_WHEEL="$(ls dist/avibe_os-*.whl)"' in contracts["run"]
    assert 'AVIBE_MEMORY_WHEEL="$(ls memory-dist/avibe_memory-*.whl)"' in contracts["run"]
    assert "pytest tests/test_memory_distribution.py -v -ra" in contracts["run"]
    assert not contracts.get("continue-on-error")
    assert jobs["install-upgrade-regression"]["needs"] == "install-upgrade-shards"
    assert jobs["windows-install-smoke"]["needs"] == "build-linux-artifacts"


@pytest.mark.parametrize(("pytest_exit", "summary"), [(0, "21 passed"), (1, "1 failed"), (0, "14 passed, 7 skipped")])
def test_distribution_contract_command_fails_on_errors_and_skips(tmp_path, pytest_exit, summary):
    for directory, filename in (("dist", "avibe_os-test.whl"), ("memory-dist", "avibe_memory-test.whl")):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / filename).touch()
    binary = tmp_path / "bin"
    binary.mkdir()
    pytest_stub = binary / "pytest"
    pytest_stub.write_text(f"#!/bin/sh\nprintf '%s\\n' '{summary}'\nexit {pytest_exit}\n")
    pytest_stub.chmod(0o755)
    step = next(
        step for step in _jobs()["install-upgrade-shards"]["steps"]
        if step.get("name") == "Verify built distribution contracts"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env={**os.environ, "PATH": f"{binary}{os.pathsep}{os.environ['PATH']}", "RUNNER_TEMP": str(tmp_path)},
        capture_output=True, text=True,
    )
    assert (result.returncode == 0) == (pytest_exit == 0 and "skipped" not in summary), result.stdout + result.stderr


@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", ""])
def test_install_gate_fails_unless_all_suites_succeeded(tmp_path: Path, result: str) -> None:
    gate = _jobs()["install-upgrade-regression"]
    assert gate["if"] == "always()"
    step, = gate["steps"]
    assert step["env"] == {"INSTALL_RESULT": "${{ needs['install-upgrade-shards'].result }}"}
    command = subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=tmp_path,
        env={**os.environ, "INSTALL_RESULT": result}, capture_output=True, text=True,
    )
    assert (command.returncode == 0) == (result == "success"), command.stdout


def test_existing_required_gate_fails_unless_every_dependency_succeeded(tmp_path: Path) -> None:
    gate = _jobs()["unit-tests"]
    assert gate["if"] == "always()"
    step, = gate["steps"]
    assert step["env"] == {
        "UNIT_RESULT": "${{ needs['unit-test-shards'].result }}",
        "UI_RESULT": "${{ needs['ui-checks'].result }}",
    }
    for results in itertools.product(("success", "failure", "cancelled", "skipped", ""), repeat=2):
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=tmp_path,
            env={**os.environ, **dict(zip(step["env"], results))},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) == all(value == "success" for value in results), result.stdout
