from __future__ import annotations

from email.parser import Parser
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
MEMORY_PROJECT = ROOT / "packaging" / "avibe-memory"
COMPATIBILITY = SpecifierSet(">=3.0.14.dev0,<3.1")
RELEASE_GATE = "Block Memory Wave 3a release until Waves 3b and 3c"


def _project(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _requirement(requirements: list[str], name: str) -> Requirement:
    canonical_name = canonicalize_name(name)
    matches = [
        Requirement(value)
        for value in requirements
        if canonicalize_name(Requirement(value).name) == canonical_name
    ]
    assert len(matches) == 1
    return matches[0]


def _wheel_path(variable: str) -> Path:
    value = os.environ.get(variable)
    if value is None:
        pytest.skip(f"{variable} is set by the package-matrix build job")
    path = Path(value).resolve()
    assert path.is_file()
    return path


def _wheel_metadata(wheel: Path) -> tuple[set[str], Any]:
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        metadata_paths = [name for name in names if name.endswith(".dist-info/METADATA")]
        assert len(metadata_paths) == 1
        metadata = Parser().parsestr(
            archive.read(metadata_paths[0]).decode("utf-8")
        )
    return names, metadata


def _run(python: Path, *args: str, cwd: Path) -> None:
    environment = {**os.environ, "PYTHONPATH": ""}
    result = subprocess.run(
        [str(python), *args],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _venv(tmp_path: Path, name: str) -> Path:
    environment = tmp_path / name
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        capture_output=True,
        text=True,
    )
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def test_memory_extra_and_distribution_share_one_compatibility_window() -> None:
    host = _project(ROOT / "pyproject.toml")
    memory = _project(MEMORY_PROJECT / "pyproject.toml")

    memory_requirement = _requirement(host["optional-dependencies"]["memory"], "avibe-memory")
    host_requirement = _requirement(memory["dependencies"], "avibe-os")

    assert memory_requirement.specifier == COMPATIBILITY
    assert host_requirement.specifier == COMPATIBILITY


def test_publish_order_guard_requires_memory_before_the_first_host_extra() -> None:
    contract = " ".join(
        (MEMORY_PROJECT / "README.md").read_text(encoding="utf-8").split()
    )

    assert "publish the matching `avibe-memory` release first" in contract
    assert "before publishing `avibe-os`" in contract
    assert "This package split is not release-ready by itself" in contract
    assert "upgrade and rollback package-shape planner (Wave 3b)" in contract
    assert "release automation and manifest ownership changes (Wave 3c)" in contract
    assert "Wave 3a intentionally leaves both the upgrade planner" in contract
    assert "Upgrade and rollback package-shape planning is" in contract


def test_release_workflows_fail_closed_before_the_split_package_builds() -> None:
    expectations = (
        ("publish.yml", "Build package", "Verify package runtime manifests"),
        ("release_ai.yml", "Build Python package", "Verify bundled runtime manifests"),
    )

    for workflow_name, build_step, wheel_assertion_step in expectations:
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / workflow_name).read_text(
                encoding="utf-8"
            )
        )
        matching_jobs = []
        for job in workflow["jobs"].values():
            steps = job.get("steps", [])
            names = [step.get("name") for step in steps]
            if RELEASE_GATE in names:
                matching_jobs.append((steps, names))

        assert len(matching_jobs) == 1
        steps, names = matching_jobs[0]
        gate_index = names.index(RELEASE_GATE)
        gate_command = steps[gate_index]["run"]
        assert gate_index < names.index(build_step) < names.index(wheel_assertion_step)
        assert "Wave 3b" in gate_command
        assert "Wave 3c" in gate_command
        assert "exit 1" in gate_command


def test_built_wheels_have_independent_contents_and_compatible_metadata() -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    core_names, core_metadata = _wheel_metadata(core_wheel)
    memory_names, memory_metadata = _wheel_metadata(memory_wheel)

    assert core_metadata["Name"] == "avibe-os"
    assert memory_metadata["Name"] == "avibe-memory"
    assert not any(name.startswith("avibe_memory/") for name in core_names)
    assert "vibe/memory_runtime_manifest.json" not in core_names
    assert "core/memory_loader.py" in core_names

    source_files = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "avibe_memory").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    packaged_files = {
        name for name in memory_names if name.startswith("avibe_memory/")
    }
    assert packaged_files == source_files
    assert "vibe/memory_runtime_manifest.json" in memory_names
    with zipfile.ZipFile(memory_wheel) as archive:
        assert archive.read("vibe/memory_runtime_manifest.json") == (
            ROOT / "vibe/memory_runtime_manifest.json"
        ).read_bytes()

    core_requires = core_metadata.get_all("Requires-Dist") or []
    memory_requires = memory_metadata.get_all("Requires-Dist") or []
    memory_requirement = _requirement(core_requires, "avibe-memory")
    host_requirement = _requirement(memory_requires, "avibe-os")
    core_version = Version(core_metadata["Version"])
    memory_version = Version(memory_metadata["Version"])

    assert memory_requirement.specifier == COMPATIBILITY
    assert memory_requirement.marker is not None
    assert memory_requirement.marker.evaluate({"extra": "memory"})
    assert memory_version in memory_requirement.specifier
    assert host_requirement.specifier == COMPATIBILITY
    assert core_version in host_requirement.specifier
    assert memory_version == core_version


@pytest.mark.parametrize("installation", ["core-only", "core+memory"])
def test_wheel_installation_matrix(
    installation: str,
    tmp_path: Path,
) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    python = _venv(tmp_path, installation)
    wheels = [core_wheel]
    if installation == "core+memory":
        wheels.append(memory_wheel)
    _run(
        python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        *(str(wheel) for wheel in wheels),
        cwd=tmp_path,
    )

    expected = installation == "core+memory"
    _run(
        python,
        "-c",
        (
            "import importlib.util; "
            "import core.memory_loader as loader; "
            f"assert (importlib.util.find_spec('avibe_memory') is not None) is {expected!r}; "
            "assert loader.MEMORY_RUNTIME_ENTRYPOINT == 'avibe_memory.runtime'; "
            "assert loader.MEMORY_RUNTIME_PROTOCOL_VERSION == 1"
        ),
        cwd=tmp_path,
    )
    if expected:
        _run(
            python,
            "-c",
            (
                "from importlib.metadata import distribution; "
                "import avibe_memory; "
                "assert distribution('avibe-memory').metadata['Name'] == 'avibe-memory'; "
                "assert avibe_memory.__name__ == 'avibe_memory'"
            ),
            cwd=tmp_path,
        )


def test_packaged_memory_states_missing_disabled_enabled_and_incompatible(
    tmp_path: Path,
) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")

    missing_python = _venv(tmp_path, "missing")
    _run(
        missing_python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        str(core_wheel),
        cwd=tmp_path,
    )
    _run(
        missing_python,
        "-c",
        (
            "from types import SimpleNamespace; "
            "from core.memory_loader import load_memory_runtime; "
            "from vibe.memory_contract import MemoryPluginUnavailableError; "
            "\ntry: load_memory_runtime(SimpleNamespace(enabled=True))\n"
            "except MemoryPluginUnavailableError: pass\n"
            "else: raise AssertionError('missing package did not fail closed')"
        ),
        cwd=tmp_path,
    )

    enabled_python = _venv(tmp_path, "enabled")
    _run(
        enabled_python,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        str(core_wheel),
        str(memory_wheel),
        cwd=tmp_path,
    )
    _run(
        enabled_python,
        "-c",
        (
            "import sys; from importlib.metadata import distribution; "
            "from types import SimpleNamespace; import core.memory_loader as loader; "
            "runtime_file = next(path for path in distribution('avibe-memory').files "
            "if str(path) == 'avibe_memory/runtime.py').locate(); "
            "assert 'MEMORY_RUNTIME_PROTOCOL_VERSION = 1' in runtime_file.read_text(); "
            "assert loader.load_memory_runtime(SimpleNamespace(enabled=False)) is None; "
            "assert 'avibe_memory.runtime' not in sys.modules; "
            "implementation = SimpleNamespace(MEMORY_RUNTIME_PROTOCOL_VERSION=1, "
            "create_memory_runtime=lambda config, **kwargs: "
            "SimpleNamespace(module=object(), close=lambda: None, available=True)); "
            "loader.importlib.import_module = lambda name: implementation; "
            "runtime = loader.load_memory_runtime(SimpleNamespace(enabled=True)); "
            "assert runtime.available is True"
        ),
        cwd=tmp_path,
    )

    incompatible_root = tmp_path / "incompatible"
    package = incompatible_root / "avibe_memory"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "runtime.py").write_text(
        "MEMORY_RUNTIME_PROTOCOL_VERSION = 999\n"
        "def create_memory_runtime(config, **kwargs): raise AssertionError('must not construct')\n",
        encoding="utf-8",
    )
    _run(
        missing_python,
        "-c",
        (
            "from types import SimpleNamespace; "
            "from core.memory_loader import load_memory_runtime; "
            "from vibe.memory_contract import MemoryPluginIncompatibleError; "
            "\ntry: load_memory_runtime(SimpleNamespace(enabled=True))\n"
            "except MemoryPluginIncompatibleError: pass\n"
            "else: raise AssertionError('incompatible package did not fail closed')"
        ),
        cwd=incompatible_root,
    )


def test_packaged_memory_upgrade_and_core_only_rollback_restore_shape(
    tmp_path: Path,
) -> None:
    core_wheel = _wheel_path("AVIBE_CORE_WHEEL")
    memory_wheel = _wheel_path("AVIBE_MEMORY_WHEEL")
    python = _venv(tmp_path, "shape-round-trip")
    install_core = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-deps",
        str(core_wheel),
    ]
    subprocess.run(install_core, check=True, capture_output=True, text=True)

    _run(
        python,
        "-c",
        "import importlib.util; assert importlib.util.find_spec('avibe_memory') is None",
        cwd=tmp_path,
    )
    subprocess.run(
        [*install_core[:-1], str(core_wheel), str(memory_wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(
        python,
        "-c",
        "import importlib.util; assert importlib.util.find_spec('avibe_memory') is not None",
        cwd=tmp_path,
    )

    subprocess.run(install_core, check=True, capture_output=True, text=True)
    subprocess.run(
        [str(python), "-m", "pip", "uninstall", "--yes", "avibe-memory"],
        check=True,
        capture_output=True,
        text=True,
    )
    _run(
        python,
        "-c",
        "import importlib.util; assert importlib.util.find_spec('avibe_memory') is None",
        cwd=tmp_path,
    )
