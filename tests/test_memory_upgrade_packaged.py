from __future__ import annotations

import os
import shutil
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


def _build_release_wheels(version: str, wheelhouse: Path) -> None:
    _wheel(ROOT, version, wheelhouse)
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
def packaged_release_wheels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    wheelhouse = tmp_path_factory.mktemp("packaged-release-wheels")
    for version in ("3.0.14", "3.0.15"):
        _build_release_wheels(version, wheelhouse)
    return wheelhouse


@pytest.fixture(scope="module")
def packaged_dependency_seed(
    tmp_path_factory: pytest.TempPathFactory, packaged_release_wheels: Path
) -> Path:
    seed_root = tmp_path_factory.mktemp("packaged-dependency-seed")
    source_wheelhouse = packaged_release_wheels
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


@pytest.fixture
def wheelhouse(tmp_path: Path, packaged_release_wheels: Path) -> Path:
    # Builds are immutable inputs; removals and installs stay local to each case.
    return Path(shutil.copytree(packaged_release_wheels, tmp_path / "wheelhouse"))


def test_wheelhouse_mutations_do_not_change_shared_builds_or_other_cases(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    for name in ("avibe_os-3.0.14.whl", "avibe_memory-3.0.14.whl", "future-package.whl"):
        (shared / name).write_bytes(name.encode())
    before = {path.name: path.read_bytes() for path in shared.iterdir()}
    first = wheelhouse.__wrapped__(tmp_path / "first", shared)
    second = wheelhouse.__wrapped__(tmp_path / "second", shared)
    for path in first.iterdir():
        path.write_bytes(b"changed")
        path.unlink()

    assert {path.name: path.read_bytes() for path in shared.iterdir()} == before
    assert {path.name: path.read_bytes() for path in second.iterdir()} == before


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
    wheelhouse: Path,
) -> None:
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
def test_packaged_memory_shape_survives_synchronous_upgrade(
    tmp_path: Path,
    packaged_dependency_seed: Path,
    wheelhouse: Path,
) -> None:
    """Run the public planner and executor across a paired package upgrade."""

    if not (ROOT / "vibe" / "show_runtime_manifest.json").is_file():
        pytest.fail("packaged wheel smoke requires the prepared local Show Runtime manifest")

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


@pytest.mark.integration
def test_packaged_core_only_upgrade_preserves_core_only_shape(
    tmp_path: Path, packaged_dependency_seed: Path, wheelhouse: Path
) -> None:
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
def test_packaged_missing_memory_fails_before_install(
    tmp_path: Path, packaged_dependency_seed: Path, wheelhouse: Path
) -> None:
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
