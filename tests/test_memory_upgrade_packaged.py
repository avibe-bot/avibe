from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import re
import shutil
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


@pytest.fixture(scope="module")
def packaged_dependency_seed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    seed_root = tmp_path_factory.mktemp("packaged-dependency-seed")
    source_wheelhouse = seed_root / "source"
    source_wheelhouse.mkdir()
    _build_release_wheels("3.0.14", source_wheelhouse)
    dependency_seed = seed_root / "dependencies"
    dependency_seed.mkdir()
    seed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(dependency_seed),
            "setuptools",
            "wheel",
            str(next(source_wheelhouse.glob("avibe_os-3.0.14-*.whl"))),
            str(next(source_wheelhouse.glob("avibe_memory-3.0.14-*.whl"))),
        ],
        env={**os.environ, "PIP_FIND_LINKS": str(source_wheelhouse)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert seed.returncode == 0, seed.stdout + seed.stderr
    return dependency_seed


def _install_initial(python: Path, wheelhouse: Path, *, memory: bool, dependency_seed: Path) -> None:
    for artifact in dependency_seed.iterdir():
        if artifact.name.lower().startswith(("avibe_os-", "avibe_memory-")):
            continue
        shutil.copy2(artifact, wheelhouse / artifact.name)
    wheels = [next(wheelhouse.glob("avibe_os-3.0.14-*.whl"))]
    if memory:
        wheels.append(next(wheelhouse.glob("avibe_memory-3.0.14-*.whl")))
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
def test_memory_indep_021_packaged_core_only_status_blocks_optional_imports(
    tmp_path: Path,
    packaged_dependency_seed: Path,
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _build_release_wheels("3.0.14", wheelhouse)
    python = _venv(tmp_path / "core-only-status-venv")
    _install_initial(
        python,
        wheelhouse,
        memory=False,
        dependency_seed=packaged_dependency_seed,
    )
    script = r'''
import importlib.abc
import json
import os
import sys

from config import paths
from config.v2_config import V2Config

config_path = paths.get_config_path()
config_path.parent.mkdir(parents=True, exist_ok=True)
V2Config.default().save(config_path)
if os.environ["MEMORY_CONFIG_CASE"] == "malformed":
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["memory"]["recovery_intent"] = "invalid"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

class BlockMemoryImplementation(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "avibe_memory" or fullname.startswith("avibe_memory."):
            raise AssertionError(f"optional implementation import: {fullname}")
        return None

sys.meta_path.insert(0, BlockMemoryImplementation())
from vibe import api

package, runtime = api._memory_dependencies_status(offline=True)
assert package["readiness"] == "not_required"
assert package["provider_count"] == 0
assert runtime["status"] == "not_required"
assert not any(
    name == "avibe_memory" or name.startswith("avibe_memory.")
    for name in sys.modules
)
'''
    for case in ("disabled", "malformed"):
        home = tmp_path / f"avibe-home-{case}"
        result = subprocess.run(
            [str(python), "-c", script],
            cwd=tmp_path,
            env={
                **os.environ,
                "AVIBE_HOME": str(home),
                "MEMORY_CONFIG_CASE": case,
                "PYTHONPATH": "",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.integration
def test_packaged_memory_shape_survives_synchronous_upgrade_and_supervisor_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packaged_dependency_seed: Path,
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
    _install_initial(python, wheelhouse, memory=True, dependency_seed=packaged_dependency_seed)
    env = _plan_env(wheelhouse)
    assert _installed_versions(python)["avibe-os"] == "3.0.14"
    assert _installed_versions(python)["avibe-memory"] == "3.0.14"

    forward = build_upgrade_plan(
        python_executable=str(python),
        base_env=env,
        memory_enabled=True,
        memory_package=True,
        target_version="3.0.15",
        package_spec="avibe-os",
    )
    assert "avibe-os[memory]" in forward.command
    assert "avibe-memory==3.0.15" in forward.command
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
def test_packaged_core_only_upgrade_preserves_core_only_shape(
    tmp_path: Path, packaged_dependency_seed: Path
) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for version in ("3.0.14", "3.0.15"):
        _build_release_wheels(version, wheelhouse)
    python = _venv(tmp_path / "core-only-venv")
    _install_initial(python, wheelhouse, memory=False, dependency_seed=packaged_dependency_seed)
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
def test_packaged_missing_memory_fails_before_install(tmp_path: Path, packaged_dependency_seed: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _build_release_wheels("3.0.14", wheelhouse)
    _build_release_wheels("3.0.15", wheelhouse)
    python = _venv(tmp_path / "missing-memory-venv")
    _install_initial(python, wheelhouse, memory=True, dependency_seed=packaged_dependency_seed)
    for wheel in wheelhouse.glob("avibe_memory-*.whl"):
        wheel.unlink()
    plan = build_upgrade_plan(
        python_executable=str(python),
        base_env=_plan_env(wheelhouse),
        memory_enabled=True,
        memory_package=True,
        target_version="3.0.15",
        package_spec="avibe-os",
    )
    result = execute_upgrade_plan(plan, cwd=tmp_path, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode != 0
    versions = _installed_versions(python)
    assert versions["avibe-os"] == "3.0.14"
    assert versions["avibe-memory"] == "3.0.14"
