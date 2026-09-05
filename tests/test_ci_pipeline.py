from __future__ import annotations

import itertools
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


@pytest.mark.parametrize("failed", [False, True])
def test_unit_file_runner_preserves_assertions_and_integration_selection(tmp_path: Path, failed: bool) -> None:
    result = _run_isolated_unit_files(tmp_path, {
        "test_assertion.py": f"def test_assertion():\n    assert {not failed}\n",
        "test_integration.py": "import pytest\n@pytest.mark.integration\ndef test_excluded():\n    assert False\n",
    })
    assert result.returncode == int(failed), result.stdout + result.stderr
    assert "Ran 2 unit test file(s), one process each" in result.stdout
    assert "No unit tests collected (skipped):\n  tests/test_integration.py" in result.stdout


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
