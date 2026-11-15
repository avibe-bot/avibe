from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_regression_script_has_valid_shell_syntax() -> None:
    subprocess.run(
        ["bash", "-n", str(REPO_ROOT / "scripts" / "run_regression.sh")],
        check=True,
        cwd=REPO_ROOT,
    )


def test_regression_script_is_incus_only_wrapper() -> None:
    script = (REPO_ROOT / "scripts" / "run_regression.sh").read_text(encoding="utf-8")

    assert "scripts/incus_regression.py" in script
    assert "docker compose" not in script
    assert "--docker" not in script
    assert "REGRESSION_STATE_ROOT" not in script


def test_regression_script_maps_legacy_flags_to_incus() -> None:
    script = (REPO_ROOT / "scripts" / "run_regression.sh").read_text(encoding="utf-8")

    assert "--reset-config)" in script
    assert "incus_args+=(--reset-mode config)" in script
    assert "--reset-all|--reset-state)" in script
    assert "incus_args+=(--reset-mode all)" in script
    assert "--no-build)" in script
    assert "incus_args+=(--no-build-ui)" in script


def test_legacy_three_regression_wrapper_was_removed() -> None:
    assert not (REPO_ROOT / "scripts" / "run_three_regression.sh").exists()
    assert not (REPO_ROOT / "scripts" / "prepare_three_regression.py").exists()


def test_regression_maintenance_commands_accept_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.regression"
    env_file.write_text("REGRESSION_PORT=15999\n", encoding="utf-8")

    subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "run_regression.sh"),
            "--status",
            "--env-file",
            str(env_file),
            "--dry-run",
        ],
        check=True,
        cwd=REPO_ROOT,
    )


@pytest.fixture
def wrapper_python(tmp_path, monkeypatch):
    """Observe wrapper arguments without running the runner's live hold lookup."""
    python = tmp_path / "fake-python"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    python.chmod(0o700)
    monkeypatch.setenv("PYTHON_BIN", str(python))
    monkeypatch.delenv("REGRESSION_TARGET", raising=False)


def test_regression_wrapper_forwards_worktree_port_range(wrapper_python) -> None:
    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "run_regression.sh"),
            "--worktree",
            "--slug",
            "demo-branch",
            "--worktree-port-start",
            "15240",
            "--worktree-port-end",
            "15240",
            "--dry-run",
            "--no-build-ui",
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines()[1:] == [
        "up", "--target", "worktree", "--slug", "demo-branch",
        "--worktree-port-start", "15240", "--worktree-port-end", "15240", "--dry-run", "--no-build-ui",
    ]


def test_regression_wrapper_forwards_remote_flag() -> None:
    # --remote is the documented escape hatch for explicit remote ops; the wrapper
    # must forward it to incus_regression.py (which still registers it), not reject
    # it. Asserted statically (running the wrapper would reach the Incus daemon).
    script = (REPO_ROOT / "scripts" / "run_regression.sh").read_text(encoding="utf-8")

    assert "--remote|--incus-remote)" in script
    assert 'incus_args+=(--remote "$2")' in script


def test_regression_wrapper_accepts_paired_master_reset_override(wrapper_python) -> None:
    result = subprocess.run(
        [
            str(REPO_ROOT / "scripts" / "run_regression.sh"),
            "--reset-config",
            "--allow-reset-paired-master",
            "--dry-run",
        ],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines()[1:] == [
        "up", "--target", "master", "--reset-mode", "config", "--allow-reset-paired-master", "--dry-run",
    ]
