from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibe.upgrade import build_upgrade_plan, execute_upgrade_plan


ROOT = Path(__file__).resolve().parents[1]


def _wheel(project: Path, version: str, wheelhouse: Path) -> None:
    env = {**os.environ, "SETUPTOOLS_SCM_PRETEND_VERSION": version}
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(project), "--no-deps", "--wheel-dir", str(wheelhouse)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _installed_versions(python: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(python), "-c", "from importlib.metadata import distributions; print('\\n'.join(f'{d.metadata[\"Name\"]}=={d.version}' for d in distributions()))"],
        capture_output=True,
        text=True,
        check=True,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            values[name.lower()] = version
    return values


@pytest.mark.integration
def test_packaged_memory_shape_survives_forward_upgrade_and_pinned_rollback(tmp_path: Path) -> None:
    """Exercise the real pip resolver and inspect distributions after each mutation."""

    if not (ROOT / "vibe" / "show_runtime_manifest.json").is_file():
        pytest.skip("packaged wheel smoke requires the prepared local Show Runtime manifest")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("3.0.14", "3.0.15"):
        _wheel(ROOT, version, wheelhouse)
        _wheel(ROOT / "packaging" / "avibe-memory", version, wheelhouse)

    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True)
    python = _python(environment)
    env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheelhouse)}
    initial = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(next(wheelhouse.glob("avibe_os-3.0.14-*.whl"))), str(next(wheelhouse.glob("avibe_memory-3.0.14-*.whl")))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial.returncode == 0, initial.stdout + initial.stderr
    assert _installed_versions(python)["avibe-os"] == "3.0.14"
    assert _installed_versions(python)["avibe-memory"] == "3.0.14"

    forward = build_upgrade_plan(
        python_executable=str(python),
        base_env=env,
        memory_enabled=True,
        package_spec="avibe-os",
    )
    result = execute_upgrade_plan(forward, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed_versions(python)["avibe-os"] == "3.0.15"
    assert _installed_versions(python)["avibe-memory"] == "3.0.15"

    rollback = build_upgrade_plan(
        python_executable=str(python),
        base_env=env,
        version="3.0.14",
        package_name="avibe-os",
        memory_package=True,
        memory_version="3.0.14",
    )
    result = execute_upgrade_plan(rollback, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed_versions(python)["avibe-os"] == "3.0.14"
    assert _installed_versions(python)["avibe-memory"] == "3.0.14"

    core_only_environment = tmp_path / "core-only-venv"
    subprocess.run([sys.executable, "-m", "venv", str(core_only_environment)], check=True, capture_output=True)
    core_only_python = _python(core_only_environment)
    initial_core_only = subprocess.run(
        [
            str(core_only_python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            str(next(wheelhouse.glob("avibe_os-3.0.14-*.whl"))),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial_core_only.returncode == 0, initial_core_only.stdout + initial_core_only.stderr

    core_only_forward = build_upgrade_plan(
        python_executable=str(core_only_python),
        base_env=env,
        memory_enabled=False,
        memory_package=False,
        package_spec="avibe-os",
    )
    assert execute_upgrade_plan(
        core_only_forward,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    ).returncode == 0
    core_only_versions = _installed_versions(core_only_python)
    assert core_only_versions["avibe-os"] == "3.0.15"
    assert "avibe-memory" not in core_only_versions
