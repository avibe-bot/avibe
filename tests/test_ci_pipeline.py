from __future__ import annotations

import itertools
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())["jobs"]


def _artifact_build_commands() -> list[list[str]]:
    step, = [step for step in _jobs()["build-linux-artifacts"]["steps"]
             if step.get("name") == "Build package artifact"]
    assert not step.get("if") and not step.get("continue-on-error")
    return [shlex.split(line, comments=True) for line in step["run"].splitlines()
            if line.strip() and not line.lstrip().startswith("#")]








@pytest.mark.parametrize("exit_code", [0, 1, 7])
def test_ci_tool_install_command_propagates_failure(tmp_path, exit_code):
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit {exit_code}\n")
    python.chmod(0o755)
    for job in _jobs().values():
        for step in job["steps"]:
            if step.get("name") == "Install pinned uv":
                result = subprocess.run(
                    [shutil.which("bash"), "-e", "-c", step["run"]], cwd=tmp_path,
                    env={**os.environ, "PATH": str(binary)}, capture_output=True, text=True,
                )
                assert result.returncode == exit_code
                assert "--only-binary=:all:" in result.stdout


def _run_isolated_unit_files(
    tmp_path: Path,
    sources: dict[str, str],
    *,
    timeout: str = "15",
    timings: dict[str, float] | None = None,
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    for name, source in sources.items():
        (tmp_path / "tests" / name).write_text(source, encoding="utf-8")
    shutil.copyfile(ROOT / "scripts/ci_unit_test_shards.py", tmp_path / "scripts/ci_unit_test_shards.py")
    shutil.copyfile(ROOT / "scripts/ci_pytest_metrics.py", tmp_path / "scripts/ci_pytest_metrics.py")
    if timings is not None:
        (tmp_path / "scripts" / "ci_unit_test_timings.json").write_text(
            json.dumps({"durations_seconds": timings}), encoding="utf-8",
        )
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
    # The dump names whichever frame the main thread held at the deadline, which
    # in a file whose cost is spread across many tests is an ordinary cheap one.
    # Saying so is the difference between reading the trace as evidence and
    # reading it as a verdict on the test it happens to name.
    assert "^ tests/test_a_stuck.py hit its 3s watchdog." in result.stdout
    assert "No recorded duration for this file, so it ran on the 3s floor" in result.stdout
    assert "not necessarily where it stalled" in result.stdout
    records = _metrics(result)
    assert records[-1]["file"] == "tests/test_b_pass.py"
    if phase == "shutdown":
        assert records[0]["file"] == "tests/test_a_stuck.py"
        assert records[0]["boundary"] == "pytest_returned_before_interpreter_shutdown"
    else:
        assert len(records) == 1


def test_unit_file_watchdog_budget_follows_the_file_own_recorded_cost(tmp_path: Path) -> None:
    """A file the snapshot knows is slow gets headroom measured against itself.

    The fixed budget this replaces could not tell "hung" from "slow": it was
    generous for a 1s file and thin for the suite's slowest, so a uniformly
    slow runner killed the slowest file first and named it as the culprit.
    """
    result = _run_isolated_unit_files(
        tmp_path,
        {
            "test_a_stuck.py": "import time\ndef test_stuck():\n    time.sleep(3600)\n",
            "test_b_pass.py": "def test_after_stuck_file():\n    pass\n",
        },
        timeout="4",
        timings={"tests/test_a_stuck.py": 1.0},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    # 8x its own second, not the 4s floor the same run gives an unknown file.
    assert "Starting tests/test_a_stuck.py (timeout 8s, budgeted from its recorded 1s)." in result.stdout
    assert "Timeout (0:00:08)!" in result.stderr
    assert "^ tests/test_a_stuck.py hit its 8s watchdog." in result.stdout
    assert "That budget is a multiple of this file's own recorded 1s" in result.stdout
    # An unknown file keeps the floor, and the line it prints is unchanged.
    assert "Starting tests/test_b_pass.py (timeout 4s)." in result.stdout
    assert "test_after_stuck_file PASSED" in result.stdout
    assert "FAILED files:\n  tests/test_a_stuck.py" in result.stdout


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












@pytest.mark.parametrize("enabled,contents,expected", [
    ("1", "3000000000 7000000000 12", {"enabled": True, "run_ns": 3000000000, "runqueue_ns": 7000000000}),
    ("0", "3 0 12", {"enabled": False, "run_ns": 3, "runqueue_ns": 0}),
    ("unknown", "3 4 12", None),
    ("1", "3 4", None),
    ("1", "3 bad 12", None),
    ("1", "3 -4 12", None),
])
def test_wait_scheduler_parses_only_known_nonnegative_counters(monkeypatch, enabled, contents, expected):
    from scripts import ci_pytest_metrics as metrics

    paths = {
        "/proc/sys/kernel/sched_schedstats": enabled,
        "/proc/thread-self/schedstat": contents,
    }
    monkeypatch.setattr(metrics.Path, "read_text", lambda path: paths[str(path)])
    assert metrics.linux_scheduler() == expected


@pytest.mark.parametrize("resource_name,contents,expected", [
    ("cpu", "some avg10=1.00 avg60=0.00 avg300=0.00 total=123\nfull total=0", {"some": 123}),
    ("io", "some total=123\nfull total=100", {"some": 123, "full": 100}),
    ("memory", "some total=2\nfull total=1", {"some": 2, "full": 1}),
    ("cpu", "some total=0", {"some": 0}),
    ("io", "some total=123", None),
    ("io", "some total=-1\nfull total=0", None),
    ("cpu", "some total=123\nsome total=124", None),
    ("cpu", "some avg10=1.00", None),
    ("cpu", "some total=invalid", None),
    ("cpu", "", None),
])
def test_wait_pressure_has_explicit_host_scope_and_units(monkeypatch, resource_name, contents, expected):
    from scripts import ci_pytest_metrics as metrics

    def read(path):
        assert str(path) == f"/proc/pressure/{resource_name}"
        return contents

    monkeypatch.setattr(metrics.Path, "read_text", read)
    assert metrics.linux_pressure(resource_name) == expected










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




@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped"])
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
