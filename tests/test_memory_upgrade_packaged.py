from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibe import restart_supervisor, runtime
from vibe.upgrade import MEMORY_PACKAGE_REQUIREMENT, RollbackTarget, UpgradeTransaction


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
def test_packaged_memory_shape_survives_supervisor_upgrade_and_pinned_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the supervisor transaction and inspect real installed distributions."""

    if not (ROOT / "vibe" / "show_runtime_manifest.json").is_file():
        pytest.fail("packaged wheel smoke requires the prepared local Show Runtime manifest")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("3.0.14", "3.0.15"):
        _wheel(ROOT, version, wheelhouse)
        _wheel(ROOT / "packaging" / "avibe-memory", version, wheelhouse)

    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True)
    python = _python(environment)
    env = {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
        "PIP_NO_DEPS": "1",
    }
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

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setenv("PIP_NO_DEPS", "1")
    monkeypatch.setattr(restart_supervisor, "sys", type("ProcessSys", (), {"executable": str(python)}))
    monkeypatch.setattr(restart_supervisor.runtime, "verified_service_running", lambda: True)
    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda **kwargs: (True, {}, 0, None, True, 0))
    monkeypatch.setattr(restart_supervisor, "_wait_for_service_lock_release", lambda: True)
    monkeypatch.setattr(restart_supervisor, "_failed_generation_still_running", lambda **kwargs: False)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor.runtime, "pid_alive", lambda pid: pid in {222, 333})
    monkeypatch.setattr(restart_supervisor.runtime, "wait_for_service_ready", lambda *args, **kwargs: 222)

    launcher = runtime.ServiceLauncher(python=str(python), main=str(ROOT / "main.py"))
    rollback_target = RollbackTarget(
        version="3.0.14",
        package="avibe-os",
        launcher=launcher,
        memory_package=True,
        memory_version="3.0.14",
    )
    starts: list[object] = []
    forward_snapshots: list[dict[str, str]] = []

    def start_runtime(*, launcher=None, **kwargs):
        starts.append(launcher)
        if launcher is None:
            forward_snapshots.append(_installed_versions(python))
            raise RuntimeError("activation intentionally failed")
        return restart_supervisor.StartedRuntime(222, None, None)

    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", start_runtime)
    transaction = UpgradeTransaction(
        schema_version=1,
        installer="pip",
        forward_spec="avibe-os",
        include_memory=True,
        memory_requirement=MEMORY_PACKAGE_REQUIREMENT,
        rollback_to=rollback_target,
    )
    assert restart_supervisor._run_restart_job(
        job_id="packaged-memory",
        delay_seconds=0,
        vibe_path=None,
        trigger="upgrade",
        scope="service",
        upgrade_transaction=transaction,
    ) == 1
    assert forward_snapshots[0]["avibe-os"] == "3.0.15"
    assert forward_snapshots[0]["avibe-memory"] == "3.0.15"
    assert _installed_versions(python)["avibe-os"] == "3.0.14"
    assert _installed_versions(python)["avibe-memory"] == "3.0.14"
    assert starts == [None, launcher]

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

    monkeypatch.setattr(restart_supervisor, "sys", type("ProcessSys", (), {"executable": str(core_only_python)}))
    monkeypatch.setattr(restart_supervisor, "_start_runtime_processes", lambda **kwargs: restart_supervisor.StartedRuntime(333, None, None))
    core_only_transaction = UpgradeTransaction(
        schema_version=1,
        installer="pip",
        forward_spec="avibe-os",
        include_memory=False,
        memory_requirement=None,
        rollback_to=RollbackTarget(
            version="3.0.14",
            package="avibe-os",
            launcher=runtime.ServiceLauncher(python=str(core_only_python), main=str(ROOT / "main.py")),
        ),
    )
    assert restart_supervisor._run_restart_job(
        job_id="packaged-core-only",
        delay_seconds=0,
        vibe_path=None,
        trigger="upgrade",
        scope="service",
        upgrade_transaction=core_only_transaction,
    ) == 0
    core_only_versions = _installed_versions(core_only_python)
    assert core_only_versions["avibe-os"] == "3.0.15"
    assert "avibe-memory" not in core_only_versions

    for wheel in wheelhouse.glob("avibe_memory-*.whl"):
        wheel.unlink()
    missing_memory_environment = tmp_path / "missing-memory-venv"
    subprocess.run([sys.executable, "-m", "venv", str(missing_memory_environment)], check=True, capture_output=True)
    missing_memory_python = _python(missing_memory_environment)
    initial_missing = subprocess.run(
        [str(missing_memory_python), "-m", "pip", "install", "--no-deps", str(next(wheelhouse.glob("avibe_os-3.0.14-*.whl")))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial_missing.returncode == 0, initial_missing.stdout + initial_missing.stderr
    monkeypatch.setattr(restart_supervisor, "sys", type("ProcessSys", (), {"executable": str(missing_memory_python)}))
    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda **kwargs: pytest.fail("missing Memory must fail before stop"))
    missing_memory_transaction = UpgradeTransaction(
        schema_version=1,
        installer="pip",
        forward_spec="avibe-os",
        include_memory=True,
        memory_requirement=MEMORY_PACKAGE_REQUIREMENT,
        rollback_to=rollback_target,
    )
    assert restart_supervisor._run_restart_job(
        job_id="packaged-missing-memory",
        delay_seconds=0,
        vibe_path=None,
        trigger="upgrade",
        scope="service",
        upgrade_transaction=missing_memory_transaction,
    ) == 2
