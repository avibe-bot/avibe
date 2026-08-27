from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from vibe import restart_supervisor, runtime
from vibe.upgrade import RollbackTarget, build_upgrade_plan, execute_upgrade_plan


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


def _pin_host_memory_extra(wheel: Path, version: str) -> None:
    """Give each fixture host release its own target-owned Memory pin."""

    with zipfile.ZipFile(wheel) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    metadata_name = next(name for name in entries if name.endswith(".dist-info/METADATA"))
    record_name = next(name for name in entries if name.endswith(".dist-info/RECORD"))
    metadata = re.sub(
        rb"Requires-Dist: avibe-memory[^\r\n]*",
        f'Requires-Dist: avibe-memory=={version}; extra == "memory"'.encode(),
        entries[metadata_name],
    )
    assert metadata != entries[metadata_name]
    entries[metadata_name] = metadata

    rows = list(csv.reader(io.StringIO(entries[record_name].decode())))
    digest = base64.urlsafe_b64encode(hashlib.sha256(metadata).digest()).rstrip(b"=").decode()
    for row in rows:
        if row[0] == metadata_name:
            row[1:] = [f"sha256={digest}", str(len(metadata))]
        elif row[0] == record_name:
            row[1:] = ["", ""]
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[record_name] = record.getvalue().encode()

    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)


def _build_release_wheels(version: str, wheelhouse: Path) -> None:
    _wheel(ROOT, version, wheelhouse)
    host_wheel = next(wheelhouse.glob(f"avibe_os-{version}-*.whl"))
    _pin_host_memory_extra(host_wheel, version)
    _wheel(ROOT / "packaging" / "avibe-memory", version, wheelhouse)


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _installed_versions(python: Path) -> dict[str, str]:
    result = subprocess.run(
        [
            str(python),
            "-c",
            "from importlib.metadata import distributions; print('\\n'.join(f'{d.metadata[\"Name\"]}=={d.version}' for d in distributions()))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "==" in line:
            name, version = line.split("==", 1)
            values[name.lower()] = version
    return values


def _venv(path: Path) -> Path:
    result = subprocess.run([sys.executable, "-m", "venv", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return _python(path)


def _install_initial(python: Path, wheelhouse: Path, *, memory: bool) -> None:
    wheels = [next(wheelhouse.glob("avibe_os-3.0.14-*.whl"))]
    if memory:
        wheels.append(next(wheelhouse.glob("avibe_memory-3.0.14-*.whl")))
    seed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheelhouse),
            "setuptools",
            "wheel",
            *(str(wheel) for wheel in wheels),
        ],
        env={**os.environ, "PIP_FIND_LINKS": str(wheelhouse)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheelhouse)}
    result = subprocess.run(
        [str(python), "-m", "pip", "install", *(str(wheel) for wheel in wheels)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _plan_env(wheelhouse: Path) -> dict[str, str]:
    return {
        **os.environ,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse),
    }


@pytest.mark.integration
def test_packaged_memory_shape_survives_synchronous_upgrade_and_supervisor_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the public planner/executor, then the existing supervisor rollback path."""

    if not (ROOT / "vibe" / "show_runtime_manifest.json").is_file():
        pytest.fail("packaged wheel smoke requires the prepared local Show Runtime manifest")

    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("3.0.14", "3.0.15"):
        _build_release_wheels(version, wheelhouse)

    environment = tmp_path / "memory-venv"
    python = _venv(environment)
    _install_initial(python, wheelhouse, memory=True)
    env = _plan_env(wheelhouse)
    assert _installed_versions(python)["avibe-os"] == "3.0.14"
    assert _installed_versions(python)["avibe-memory"] == "3.0.14"

    forward = build_upgrade_plan(
        python_executable=str(python),
        base_env=env,
        memory_enabled=True,
        memory_package=True,
        package_spec="avibe-os",
    )
    assert forward.command[-1] == "avibe-os[memory]"
    assert all("avibe-memory" not in argument for argument in forward.command)
    result = execute_upgrade_plan(forward, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    upgraded = _installed_versions(python)
    assert upgraded["avibe-os"] == "3.0.15"
    assert upgraded["avibe-memory"] == "3.0.15"

    launcher = runtime.ServiceLauncher(python=str(python), main=str(ROOT / "main.py"))
    rollback_target = RollbackTarget(
        version="3.0.14",
        package="avibe-os",
        launcher=launcher,
        memory_package=True,
        memory_version="3.0.14",
    )
    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", str(wheelhouse))
    monkeypatch.setattr(restart_supervisor, "_stop_runtime_for_restart", lambda **kwargs: (True, {}, 0, None, True, 0))
    monkeypatch.setattr(restart_supervisor, "_failed_generation_still_running", lambda **kwargs: False)
    monkeypatch.setattr(restart_supervisor, "get_safe_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(restart_supervisor.runtime, "wait_for_service_ready", lambda *args, **kwargs: 901)
    monkeypatch.setattr(restart_supervisor.runtime, "pid_alive", lambda pid: True)
    monkeypatch.setattr(
        restart_supervisor,
        "_start_runtime_processes",
        lambda **kwargs: restart_supervisor.StartedRuntime(901, None, None),
    )
    events: list[dict] = []
    rollback = restart_supervisor._roll_back_failed_upgrade(
        rollback_to=rollback_target,
        vibe_path=None,
        start_ui=False,
        backup_watermark=None,
        write=lambda message: None,
        record=lambda payload: events.append(dict(payload)),
    )
    assert rollback["state"] == "succeeded"
    restored = _installed_versions(python)
    assert restored["avibe-os"] == "3.0.14"
    assert restored["avibe-memory"] == "3.0.14"
    assert any(event.get("install", {}).get("ok") is True for event in events)


@pytest.mark.integration
def test_packaged_core_only_upgrade_preserves_core_only_shape(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("3.0.14", "3.0.15"):
        _build_release_wheels(version, wheelhouse)
    python = _venv(tmp_path / "core-only-venv")
    _install_initial(python, wheelhouse, memory=False)
    plan = build_upgrade_plan(
        python_executable=str(python),
        base_env=_plan_env(wheelhouse),
        memory_enabled=False,
        memory_package=False,
        package_spec="avibe-os",
    )
    result = execute_upgrade_plan(plan, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    versions = _installed_versions(python)
    assert versions["avibe-os"] == "3.0.15"
    assert "avibe-memory" not in versions


@pytest.mark.integration
def test_packaged_missing_memory_fails_before_install(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _build_release_wheels("3.0.14", wheelhouse)
    _build_release_wheels("3.0.15", wheelhouse)
    python = _venv(tmp_path / "missing-memory-venv")
    _install_initial(python, wheelhouse, memory=True)
    for wheel in wheelhouse.glob("avibe_memory-*.whl"):
        wheel.unlink()
    plan = build_upgrade_plan(
        python_executable=str(python),
        base_env=_plan_env(wheelhouse),
        memory_enabled=True,
        memory_package=True,
        package_spec="avibe-os",
    )
    result = execute_upgrade_plan(plan, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode != 0
    versions = _installed_versions(python)
    assert versions["avibe-os"] == "3.0.14"
    assert versions["avibe-memory"] == "3.0.14"
