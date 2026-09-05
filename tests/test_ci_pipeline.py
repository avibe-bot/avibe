from __future__ import annotations

import itertools
import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/lint.yml").read_text())["jobs"]


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
    for name in ("install-upgrade-regression", "windows-install-smoke"):
        assert jobs[name]["needs"] == "build-linux-artifacts"


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
